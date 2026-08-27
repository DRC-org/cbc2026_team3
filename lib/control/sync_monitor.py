from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING

from lib.axis_sync import SyncGroup
from lib.config_schema import DEFAULT_HEALTH
from lib.control.feedback import FeedbackFreshness
from lib.control.periodic import PeriodicTask

if TYPE_CHECKING:
    from lib.drivers.base import MotorDriver

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_INTERVAL_S", "SyncMonitor"]

# 監視周期 50Hz。機構が壊れる前に止まればよく、位置制御ループの 200Hz は要らない
DEFAULT_INTERVAL_S = 0.02

# 発報に必要な連続超過サンプル数。1 サンプルの外れ値 (CAN の取りこぼしや
# フィードバックの量子化ノイズ) で試合中に緊急停止させないためのノイズ対策。
# 50Hz x 2 サンプル = 40ms なので、機構が破損する前には十分間に合う
# (層ごとに debounce が違う理由は lib/axis_sync.py のモジュール docstring を参照)
DEFAULT_VIOLATION_SAMPLES = 2

ViolationHandler = Callable[[str, float], None]
FeedbackClock = Callable[[], float]
SleepFunc = Callable[[float], Awaitable[None]]


class SyncMonitor(PeriodicTask):
    """左右直結軸の位置ずれを常駐監視し、超過したら発報する。

    EDULITE 05 は位置ループがドライバ内蔵で PC 側に常駐ループが無く、
    ``M3508PositionLoop`` のような偏差検知の置き場所が無い。またシーケンス実行中
    以外 (動作確認中・待機中・手動操作中) にも機構がずれうるため、シーケンスから
    独立した常駐監視としてここに置く。

    ``y_axis`` は ``M3508PositionLoop`` 側の 200Hz 判定と二重になるが、これは意図的な
    多重防護である。ループ側は「電流を即 0 にする」局所的な保護、こちらは
    「試合を止めて人間に知らせる」全体的な保護で役割が違う。

    誤発報の代償が「試合が止まる」ことなので、この層だけ ``violation_samples``
    による debounce を持つ (3 層の比較は lib/axis_sync.py のモジュール docstring)。

    ライフサイクル (start / stop / 例外時の継続) は ``PeriodicTask`` と共通。
    """

    def __init__(
        self,
        groups: Sequence[SyncGroup],
        drivers: Mapping[str, MotorDriver],
        *,
        last_feedback_at: Callable[[str], float | None],
        feedback_timeout_ms: float = DEFAULT_HEALTH.feedback_timeout_ms,
        interval_s: float = DEFAULT_INTERVAL_S,
        violation_samples: int = DEFAULT_VIOLATION_SAMPLES,
        on_violation: ViolationHandler | None = None,
        feedback_clock: FeedbackClock = time.time,
        time_source: Callable[[], float] = time.monotonic,
        sleep: SleepFunc = asyncio.sleep,
    ) -> None:
        """
        Args:
            groups: 監視対象の軸 (config の sync_tolerance を持つ軸)
            drivers: モータ名 → ドライバ。位置は feedback_position() から読む
            last_feedback_at: モータ名 → 最終受信時刻 (CANManager.last_feedback_at)
            feedback_timeout_ms: これより古いフィードバックは判定から除外する
            interval_s: 監視周期 [s]
            violation_samples: 発報に必要な連続超過サンプル数
            on_violation: 超過時に呼ぶハンドラ (main.py で緊急停止に接続する)
            feedback_clock: last_feedback_at と比較する壁時計
            time_source: 周期とログ間引きに使う単調クロック
            sleep: 周期待ちに使う関数 (テストで差し替え可能)
        """
        super().__init__(interval_s=interval_s, time_source=time_source, sleep=sleep, logger=logger)
        self._groups = tuple(groups)
        self._drivers = drivers
        self._freshness = FeedbackFreshness(
            last_feedback_at, timeout_ms=feedback_timeout_ms, clock=feedback_clock
        )
        self._violation_samples = max(1, violation_samples)
        self._on_violation = on_violation

        self._counts: dict[str, int] = {}
        self._violated: set[str] = set()

    # ------------------------------------------------------------------ #
    #  状態
    # ------------------------------------------------------------------ #

    @property
    def group_names(self) -> tuple[str, ...]:
        return tuple(group.name for group in self._groups)

    @property
    def violated(self) -> frozenset[str]:
        """発報済み (ラッチ中) の軸名。"""
        return frozenset(self._violated)

    def reset(self) -> None:
        """ラッチと連続カウントを解除する。

        通す経路は操縦者の緊急停止解除 (``RobotServer._reset_sync_latches``) だけ。
        これを通らないと軸は ``_violated`` に入ったまま二度と発報せず、以後どれだけ
        ずれても誰も止められない。解除しても判定は無効化されないため、ずれが
        残っていれば次のサンプルで再び発報する。
        """
        self._counts.clear()
        self._violated.clear()

    def _label(self) -> str:
        return f"同期監視 ({', '.join(self.group_names) or '対象なし'})"

    # ------------------------------------------------------------------ #
    #  監視
    # ------------------------------------------------------------------ #

    def step(self) -> None:
        """1 周期分の判定を行う。run() から呼ばれるほか、テストから直接駆動できる。"""
        now = self._freshness.now()
        for group in self._groups:
            self._check_group(group, now)

    async def _tick(self) -> None:
        self.step()

    def _check_group(self, group: SyncGroup, now: float) -> None:
        positions = self._fresh_positions(group, now)
        deviation = group.violation(positions)
        if deviation is None:
            # 許容内、または比較対象が揃わず「ずれている」と言えない状態。
            # 連続カウントを捨てることで、鮮度が戻ってから 2 サンプル数え直す
            self._counts[group.name] = 0
            return

        count = self._counts.get(group.name, 0) + 1
        self._counts[group.name] = count
        if count < self._violation_samples:
            return

        if group.name in self._violated:
            # 一度発報した軸で毎周期通知すると緊急停止が連打される。
            # 復帰は人間が reset() 経路を通ることで明示する
            return

        self._violated.add(group.name)
        logger.error(
            "同期ずれを検出 (axis=%s, deviation=%.3f, tolerance=%.3f)",
            group.name,
            deviation,
            group.tolerance,
        )
        self._notify(group.name, deviation)

    def _fresh_positions(self, group: SyncGroup, now: float) -> dict[str, float]:
        """フィードバックが新鮮なメンバの位置だけを集める。

        未受信・途絶したモータを 0 とみなすと、起動直後にいきなり偏差超過と判定して
        緊急停止してしまう。判定できないものは判定しない方が安全側になる。
        """
        positions: dict[str, float] = {}
        for member in group.members:
            driver = self._drivers.get(member.name)
            if driver is None:
                continue
            if self._freshness.is_stale(member.name, now):
                continue
            positions[member.name] = driver.feedback_position()
        return positions

    def _notify(self, group_name: str, deviation: float) -> None:
        if self._on_violation is None:
            return
        try:
            self._on_violation(group_name, deviation)
        except Exception:
            # ハンドラが落ちて監視まで死ぬ方が危険。残りの軸の判定は続ける
            logger.exception("同期ずれハンドラで例外 (axis=%s)", group_name)
