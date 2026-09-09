from __future__ import annotations

import abc
import math
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import can


class ControlMode(Enum):
    POSITION = "position"
    VELOCITY = "velocity"
    CURRENT = "current"
    DUTY = "duty"
    ON_OFF = "on_off"


@dataclass(frozen=True)
class MotorState:
    position: float = 0.0
    velocity: float = 0.0
    current: float = 0.0
    temperature: float = 0.0
    reached: bool = False


@dataclass(frozen=True)
class TelemetrySupport:
    position: bool = True
    velocity: bool = True
    current: bool = True
    temperature: bool = True


FULL_TELEMETRY = TelemetrySupport()

NO_TELEMETRY = TelemetrySupport(position=False, velocity=False, current=False, temperature=False)


_DEFAULT_REACH_TOLERANCES: dict[ControlMode, float] = {
    ControlMode.POSITION: 1.0,
    ControlMode.VELOCITY: 5.0,
}


class MotorDriver(abc.ABC):
    def __init__(self, name: str, can_id: int) -> None:
        self.name = name
        self.can_id = can_id
        self._state = MotorState()

    @property
    def state(self) -> MotorState:
        return self._state

    @property
    def telemetry(self) -> TelemetrySupport:
        return FULL_TELEMETRY

    @abc.abstractmethod
    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        """目標値を CAN メッセージにエンコードする。"""

    @abc.abstractmethod
    def decode_feedback(self, msg: can.Message) -> MotorState:
        """CAN メッセージからフィードバックをデコードする。"""

    def update_state(self, msg: can.Message) -> MotorState:
        self._state = self.decode_feedback(msg)
        return self._state

    @abc.abstractmethod
    def matches_feedback(self, msg: can.Message) -> bool:
        """受信した CAN メッセージがこのモータのフィードバックかどうか判定する。"""

    def matches_info(self, msg: can.Message) -> bool:
        return False

    def update_info(self, msg: can.Message) -> None:  # noqa: B027
        """自己申告フレームを受けて内部状態を更新する。既定は何もしない。"""

    def default_tolerance(self, mode: ControlMode) -> float:
        return _DEFAULT_REACH_TOLERANCES.get(mode, math.inf)

    def is_target_reached(
        self,
        target: float,
        mode: ControlMode,
        *,
        tolerance: float | None = None,
    ) -> bool:
        tol = self.default_tolerance(mode) if tolerance is None else tolerance
        if math.isinf(tol):
            return True

        observed = self._observed_for(mode)
        if observed is None:
            return True
        return abs(observed - target) <= tol

    def feedback_position(self) -> float:
        return self._state.position

    def _observed_for(self, mode: ControlMode) -> float | None:
        if mode is ControlMode.POSITION:
            return self.feedback_position()
        if mode is ControlMode.VELOCITY:
            return self._state.velocity
        if mode is ControlMode.CURRENT:
            return self._state.current
        return None

    def has_thermal_warning(self, temp_warning_c: float) -> bool:
        if not self.telemetry.temperature:
            return False
        return self._state.temperature >= temp_warning_c

    def has_thermal_fault(self, temp_critical_c: float) -> bool:
        if not self.telemetry.temperature:
            return False
        return self._state.temperature >= temp_critical_c

    def has_overcurrent_warning(self) -> bool:
        return False

    def is_fault(self) -> bool:
        return False

    def has_on_off_control(self) -> bool:
        return False

    def is_energized(self) -> bool | None:
        return None

    def firmware_confirmed(self) -> bool | None:
        return None

    def health_detail(self) -> str | None:
        return None

    def initialization_steps(self) -> list[tuple[can.Message, float]]:
        """起動時に送る ``(message, delay_after_seconds)``。既定は初期化不要。

        励磁の有効化はここに含めない (activation_steps を使う)。
        """
        return []

    def reinitialization_steps(self) -> list[tuple[can.Message, float]]:
        """``initialization_steps()`` のうち **電源断で失われる設定だけ**。既定は空。

        再励磁は起動時の手順を丸ごと送り直せない —— ``set_zero_on_start`` が載って
        いるドライバでは、零点確定で合わせた原点をその場の姿勢へ書き換えてしまう。
        呼び出し側は無励磁だと分かっているモータへしか送らない。
        """
        return []

    def activation_steps(self, *, after_set_zero: bool = False) -> list[tuple[can.Message, float]]:
        return []

    def deactivation_steps(self) -> list[tuple[can.Message, float]]:
        return []

    def origin_capture_steps(self) -> list[tuple[can.Message, float]]:
        return []

    def supports_origin_capture(self) -> bool:
        return bool(self.deactivation_steps()) and bool(self.origin_capture_steps())

    def requires_fresh_feedback_for_activation(self) -> bool:
        return False

    def activation_block_reason(self) -> str | None:
        """励磁してはならない理由。無ければ None。

        **「フィードバックが届かない」とは別の軸の判断である。** あちらは
        「まだ分からない」なので待てば解ける可能性があるが、こちらは
        「構成が食い違っている」ので待っても解けない。分けてあるのは、
        後者を前者のタイムアウトへ紛れ込ませると、原因が「通信が遅い」に
        見えてしまうため。

        既定は None (理由なし)。**ここで報告する食い違いは、放置すると
        「指令どおり動いたのに機構が別の場所へ行く」形でしか現れないもの**に
        限ること —— 動作に影響しない差異まで励磁拒否へ倒すと、操縦者は
        機体を動かす手段を失う。
        """
        return None

    def configuration_probe_messages(self) -> list[can.Message]:
        """励磁前に確認する設定のうち、**まだ読めていない**ぶんの問い合わせ。

        `CANManager._confirm_configuration` が空リストになるまで送り直す ——
        「読めなければ通す」でゲートを緩めず、取りこぼしは再試行で解くための口。
        機構を動かすフレームを返してはならない (`feedback_probe_message` と同じ)。
        """
        return []

    def feedback_probe_message(self) -> can.Message | None:
        return None

    def emergency_stop_message(self) -> can.Message | None:
        return None
