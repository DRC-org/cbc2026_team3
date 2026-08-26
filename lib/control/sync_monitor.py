from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lib.drivers.base import MotorDriver

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_INTERVAL_S", "SyncGroup", "SyncMember", "SyncMonitor"]

# 監視周期 50Hz。機構が壊れる前に止まればよく、位置制御ループの 200Hz は要らない
DEFAULT_INTERVAL_S = 0.02

# 発報に必要な連続超過サンプル数。1 サンプルの外れ値 (CAN の取りこぼしや
# フィードバックの量子化ノイズ) で試合中に緊急停止させないためのノイズ対策。
# 50Hz x 2 サンプル = 40ms なので、機構が破損する前には十分間に合う
DEFAULT_VIOLATION_SAMPLES = 2

# 同一原因のログを毎周期出すと 50Hz でログが溢れるため、種類ごとに間引く
_LOG_THROTTLE_S = 1.0

ViolationHandler = Callable[[str, float], None]
FeedbackClock = Callable[[], float]
SleepFunc = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class SyncMember:
    """同期グループを構成する 1 台のモータの単位換算。

    逆回転で同一動作をするペアは ``scale`` の符号で表す。``lib/sequence`` の
    ``MotorSpec`` と同じ換算だが、制御層からシーケンス層へ依存させないために
    値だけを受け取る (組み立ては main.py が行う)。
    """

    motor_name: str
    scale: float
    offset: float

    def to_value(self, command: float) -> float:
        """指令値・フィードバックを人間の単位へ戻す。"""
        return (command - self.offset) / self.scale


@dataclass(frozen=True)
class SyncGroup:
    """機構的に直結し、位置が揃っていなければならないモータの組。"""

    name: str
    members: tuple[SyncMember, ...]
    tolerance: float

    def deviation(self, positions: Mapping[str, float]) -> float | None:
        """人間の単位へ逆換算した位置の max - min。比較対象が 2 個未満なら None。

        逆回転ペアでは指令単位のまま引き算しても意味を持たない (符号が逆)。
        人間の単位へ戻してから比較することで「同じ動作をしているか」を直接見る。
        """
        values = [
            member.to_value(positions[member.motor_name])
            for member in self.members
            if member.motor_name in positions
        ]
        if len(values) < 2:
            return None
        return max(values) - min(values)


class SyncMonitor:
    """左右直結軸の位置ずれを常駐監視し、超過したら発報する。

    EDULITE 05 は位置ループがドライバ内蔵で PC 側に常駐ループが無く、
    ``M3508PositionLoop`` のような偏差検知の置き場所が無い。またシーケンス実行中
    以外 (動作確認中・待機中・手動操作中) にも機構がずれうるため、シーケンスから
    独立した常駐監視としてここに置く。

    ``y_axis`` は ``M3508PositionLoop`` 側の 200Hz 判定と二重になるが、これは意図的な
    多重防護である。ループ側は「電流を即 0 にする」局所的な保護、こちらは
    「試合を止めて人間に知らせる」全体的な保護で役割が違う。
    """

    def __init__(
        self,
        groups: Sequence[SyncGroup],
        drivers: Mapping[str, MotorDriver],
        *,
        last_feedback_at: Callable[[str], float | None],
        feedback_timeout_ms: float = 500.0,
        interval_s: float = DEFAULT_INTERVAL_S,
        violation_samples: int = DEFAULT_VIOLATION_SAMPLES,
        on_violation: ViolationHandler | None = None,
        feedback_clock: FeedbackClock = time.time,
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
            sleep: 周期待ちに使う関数 (テストで差し替え可能)
        """
        self._groups = tuple(groups)
        self._drivers = drivers
        self._last_feedback_at = last_feedback_at
        self._feedback_timeout_ms = feedback_timeout_ms
        self._interval_s = interval_s
        self._violation_samples = max(1, violation_samples)
        self._on_violation = on_violation
        self._feedback_clock = feedback_clock
        self._sleep = sleep

        self._counts: dict[str, int] = {}
        self._violated: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._log_at: dict[str, float] = {}

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

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def reset(self) -> None:
        """ラッチと連続カウントを解除する。

        通す経路は操縦者の緊急停止解除 (``RobotServer._reset_sync_latches``) だけ。
        これを通らないと軸は ``_violated`` に入ったまま二度と発報せず、以後どれだけ
        ずれても誰も止められない。解除しても判定は無効化されないため、ずれが
        残っていれば次のサンプルで再び発報する。
        """
        self._counts.clear()
        self._violated.clear()

    # ------------------------------------------------------------------ #
    #  監視
    # ------------------------------------------------------------------ #

    def step(self) -> None:
        """1 周期分の判定を行う。run() から呼ばれるほか、テストから直接駆動できる。"""
        now = self._feedback_clock()
        for group in self._groups:
            self._check_group(group, now)

    def _check_group(self, group: SyncGroup, now: float) -> None:
        positions = self._fresh_positions(group, now)
        deviation = group.deviation(positions)
        if deviation is None:
            # 比較対象が揃わない間は「ずれていない」とも「ずれている」とも言えない。
            # 連続カウントも捨てて、判定は鮮度が戻ってからやり直す
            self._counts[group.name] = 0
            return

        if deviation <= group.tolerance:
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
            driver = self._drivers.get(member.motor_name)
            if driver is None:
                continue
            last_rx = self._last_feedback_at(member.motor_name)
            if last_rx is None or (now - last_rx) * 1000.0 > self._feedback_timeout_ms:
                continue
            positions[member.motor_name] = driver.feedback_position()
        return positions

    def _notify(self, group_name: str, deviation: float) -> None:
        if self._on_violation is None:
            return
        try:
            self._on_violation(group_name, deviation)
        except Exception:
            # ハンドラが落ちて監視まで死ぬ方が危険。残りの軸の判定は続ける
            logger.exception("同期ずれハンドラで例外 (axis=%s)", group_name)

    # ------------------------------------------------------------------ #
    #  ライフサイクル
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """停止要求まで監視を回し続ける。"""
        while not self._stop_event.is_set():
            try:
                self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 監視が止まると防護が丸ごと失われる。ログに残して周期は維持する
                self._log_throttled("step", "同期監視の周期処理で例外")
            await self._sleep(self._interval_s)

    def start(self) -> None:
        """run() をバックグラウンドタスクとして起動する。二重呼び出しは無視する。"""
        if self.is_running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self.run())

    def request_stop(self) -> None:
        """次の周期でループを抜けるよう要求する (同期)。"""
        self._stop_event.set()

    async def stop(self) -> None:
        """監視を止めてタスクの終了を待つ。"""
        self.request_stop()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _log_throttled(self, key: str, message: str) -> None:
        now = self._feedback_clock()
        last = self._log_at.get(key)
        if last is not None and now - last < _LOG_THROTTLE_S:
            return
        self._log_at[key] = now
        logger.exception(message)
