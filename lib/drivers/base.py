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

    def feedback_probe_message(self) -> can.Message | None:
        return None

    def emergency_stop_message(self) -> can.Message | None:
        return None
