"""制御周期ごとの記録を溜め、ステップ応答の窓を切り出す。

**新しい駆動経路は作らない。** 操縦者が手動ジョグで動かした 1 回も、シーケンスの
``move_to`` 1 回も、位置制御ループから見れば同じ「目標値のステップ」なので、
それをそのまま記録すれば調整用の試験になる。専用の「試験を実行する」コマンドを
足すと機体が動く条件が 1 つ増え、フェーズゲート・緊急停止・動作確認との排他を
そのぶん多く守り続けることになる。増やさずに済むならそのほうが安全である。

記録は制御ループの中で回るので、**1 周期あたりの仕事を定数時間に保つ**こと。
解析 (``metrics`` / ``advice``) はここでは行わず、窓が閉じた後に配信側で行う。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from lib.tuning.metrics import Sample

__all__ = ["Capture", "MotorStepRecorder", "PidSnapshot"]


@dataclass(frozen=True)
class PidSnapshot:
    """記録時に効いていたゲイン。

    波形とゲインを別々に運ぶと、送信直後に届いた波形が新旧どちらのゲインの
    ものか判別できない。調整は「変えた結果を見る」作業なので、この対応が
    崩れると記録そのものが使えなくなる。
    """

    kp: float
    ki: float
    kd: float
    #: PID の不感帯。整定帯の下限に使う (これより狭い帯では制御自体が働かない)
    dead_band: float


@dataclass(frozen=True)
class Capture:
    """1 回のステップ応答。``samples`` の ``t`` はステップ時刻を 0 とした相対秒。"""

    motor: str
    #: 壁時計 (UI が「いつの記録か」を出すため)。制御の判断には使わない
    captured_at: float
    samples: tuple[Sample, ...]
    gains: PidSnapshot


class MotorStepRecorder:
    """モータ 1 台分の記録器。

    目標値が ``min_step`` 以上動いた瞬間を起点に ``window_s`` 秒ぶんを切り出す。
    起点より前の ``pre_trigger_s`` 秒も一緒に残すのは、ステップ直前に機体が
    静止していたのか既に動いていたのかで応答の読み方が変わるため
    (動いている最中のステップは立ち上がり時間が実力より速く出る)。
    """

    def __init__(
        self,
        motor: str,
        *,
        gains_snapshot: Callable[[], PidSnapshot],
        window_s: float,
        pre_trigger_s: float,
        min_step: float,
        max_samples: int,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if window_s <= 0:
            raise ValueError(f"window_s は正の秒数: {window_s}")
        if pre_trigger_s < 0:
            raise ValueError(f"pre_trigger_s は 0 以上: {pre_trigger_s}")
        if min_step <= 0:
            raise ValueError(f"min_step は正の値: {min_step}")
        if max_samples <= 0:
            raise ValueError(f"max_samples は正の整数: {max_samples}")

        self._motor = motor
        self._gains_snapshot = gains_snapshot
        self._window_s = window_s
        self._pre_trigger_s = pre_trigger_s
        self._min_step = min_step
        self._max_samples = max_samples
        self._wall_clock = wall_clock

        # 起点より前の記録。時刻で切るので制御周期に依存しない
        self._pre: deque[Sample] = deque()
        self._active: list[Sample] | None = None
        self._trigger_t: float = 0.0
        self._gains: PidSnapshot | None = None
        self._last_target: float | None = None

    @property
    def capturing(self) -> bool:
        return self._active is not None

    def abort(self) -> None:
        """記録中の窓を捨てる。

        フィードバック途絶・緊急停止・ペア相方の異常・ゲイン差し替えのように、
        **応答の意味が変わる事象**では必ず呼ぶ。捨てずに残すと、途中で電流 0 に
        落とされた波形が「行き過ぎもせず整定もしない応答」として記録され、
        操縦者はゲインが悪いのだと読む。
        """
        self._active = None
        self._gains = None
        # 目標も忘れる。復帰後の最初の目標を新しいステップとして扱わないと、
        # 異常のあいだに変わった目標へ機体が動く様子が 1 度も記録されない
        self._last_target = None

    def record(
        self,
        t: float,
        *,
        target: float | None,
        position: float,
        output: float,
        saturated: bool,
    ) -> Capture | None:
        """1 周期を記録する。窓が閉じたらその ``Capture`` を返す。

        Args:
            t: 単調クロックの現在時刻 [s]
            target: 位置目標 [deg]。目標を持たない周期は None
            position: 実測の累積角 [deg]
            output: PID 出力 (電流指令 [counts])
            saturated: 出力が出力レンジの端に張り付いたか
        """
        if target is None:
            # 目標を持たない (停止中・開ループ) 周期は応答として意味を持たない
            self.abort()
            self._remember(
                Sample(t=t, target=position, position=position, output=output, saturated=saturated)
            )
            return None

        triggered = self._last_target is None or abs(target - self._last_target) >= self._min_step
        self._last_target = target

        sample = Sample(t=t, target=target, position=position, output=output, saturated=saturated)

        if triggered:
            # 記録中に次のステップが来たら、古い窓は捨てて新しい起点から取り直す。
            # 連打されたジョグの途中経過を 1 本の応答として綴じると、階段状に
            # 動いた波形が「行き過ぎの大きい 1 回のステップ」として解析される
            self._trigger_t = t
            self._gains = self._gains_snapshot()
            self._active = [s for s in self._pre if t - s.t <= self._pre_trigger_s]

        if self._active is not None:
            self._active.append(sample)

        self._remember(sample)

        if self._active is None:
            return None
        if t - self._trigger_t >= self._window_s or len(self._active) >= self._max_samples:
            return self._finalize()
        return None

    def _remember(self, sample: Sample) -> None:
        self._pre.append(sample)
        while self._pre and sample.t - self._pre[0].t > self._pre_trigger_s:
            self._pre.popleft()

    def _finalize(self) -> Capture:
        assert self._active is not None and self._gains is not None
        origin = self._trigger_t
        samples = tuple(
            Sample(
                t=s.t - origin,
                target=s.target,
                position=s.position,
                output=s.output,
                saturated=s.saturated,
            )
            for s in self._active
        )
        capture = Capture(
            motor=self._motor,
            captured_at=self._wall_clock(),
            samples=samples,
            gains=self._gains,
        )
        self._active = None
        self._gains = None
        return capture
