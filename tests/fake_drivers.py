from __future__ import annotations

import can

from lib.drivers.base import ControlMode, MotorDriver, MotorState


class StubFeedbackDriver(MotorDriver):
    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        return can.Message(arbitration_id=0x100 + self.can_id, data=bytes(8), is_extended_id=False)

    def decode_feedback(self, msg: can.Message) -> MotorState:
        return self._state

    def matches_feedback(self, msg: can.Message) -> bool:
        return False

    def set_observed(self, **kwargs: float) -> None:
        self._state = MotorState(**kwargs)


class HealthFlagDriver(MotorDriver):
    FEEDBACK_ID_BASE = 0x200

    def __init__(self, name: str, can_id: int) -> None:
        super().__init__(name, can_id)
        self.thermal_warning = False
        self.thermal_fault = False
        self.overcurrent = False
        self.fault = False

    def feedback_message(self) -> can.Message:
        return can.Message(arbitration_id=self.FEEDBACK_ID_BASE + self.can_id, data=bytes(8))

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        return can.Message(arbitration_id=0x100 + self.can_id, data=bytes(8))

    def decode_feedback(self, msg: can.Message) -> MotorState:
        return self._state

    def matches_feedback(self, msg: can.Message) -> bool:
        return msg.arbitration_id == self.FEEDBACK_ID_BASE + self.can_id

    def has_thermal_warning(self, temp_warning_c: float) -> bool:
        return self.thermal_warning

    def has_thermal_fault(self, temp_critical_c: float) -> bool:
        return self.thermal_fault

    def has_overcurrent_warning(self) -> bool:
        return self.overcurrent

    def is_fault(self) -> bool:
        return self.fault
