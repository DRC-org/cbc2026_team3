from __future__ import annotations

import math
import struct
from enum import IntEnum
from typing import ClassVar

import can

from lib.drivers.base import ControlMode, MotorDriver, MotorState


class Dm3520CtrlMode(IntEnum):
    MIT = 1
    POSITION_VELOCITY = 2
    VELOCITY = 3


class Dm3520Error(IntEnum):
    DISABLED = 0x0
    ENABLED = 0x1
    SENSOR_READ = 0x5
    MOTOR_PARAM_READ = 0x6
    OVERVOLTAGE = 0x8
    UNDERVOLTAGE = 0x9
    OVERCURRENT = 0xA
    MOS_OVERTEMP = 0xB
    COIL_OVERTEMP = 0xC
    COMM_LOSS = 0xD
    OVERLOAD = 0xE


_FIRST_ERROR_CODE = 0x5

_ERROR_LABELS: dict[int, str] = {
    Dm3520Error.SENSOR_READ: "センサ読み取りエラー",
    Dm3520Error.MOTOR_PARAM_READ: "モータパラメータ読み取りエラー",
    Dm3520Error.OVERVOLTAGE: "過電圧",
    Dm3520Error.UNDERVOLTAGE: "低電圧",
    Dm3520Error.OVERCURRENT: "過電流",
    Dm3520Error.MOS_OVERTEMP: "MOS 過熱",
    Dm3520Error.COIL_OVERTEMP: "コイル過熱",
    Dm3520Error.COMM_LOSS: "通信途絶 (ドライバの TIMEOUT が満了)",
    Dm3520Error.OVERLOAD: "過負荷",
}


class Dm3520Driver(MotorDriver):
    MIT_CMD_BASE = 0x000
    POSITION_CMD_BASE = 0x100
    VELOCITY_CMD_BASE = 0x200
    CONFIG_FRAME_ID = 0x7FF

    CONFIG_READ = 0x33
    CONFIG_WRITE = 0x55
    CONFIG_SAVE = 0xAA

    SPECIAL_ENABLE = 0xFC
    SPECIAL_DISABLE = 0xFD
    SPECIAL_SET_ZERO = 0xFE

    REG_MST_ID = 0x07
    REG_ESC_ID = 0x08
    REG_TIMEOUT = 0x09
    REG_CTRL_MODE = 0x0A
    REG_P_MAX = 0x15
    REG_V_MAX = 0x16
    REG_T_MAX = 0x17

    _POS_BITS = 16
    _VEL_BITS = 12
    _TORQUE_BITS = 12

    _CONTROL_TO_CTRL_MODE: ClassVar[dict[ControlMode, Dm3520CtrlMode]] = {
        ControlMode.POSITION: Dm3520CtrlMode.POSITION_VELOCITY,
        ControlMode.VELOCITY: Dm3520CtrlMode.VELOCITY,
    }

    def __init__(
        self,
        name: str,
        can_id: int,
        *,
        master_id: int = 0x00,
        mode: ControlMode | str = ControlMode.POSITION,
        limit_speed: float = 2.0,
        p_max: float = 12.566,
        v_max: float = 45.0,
        t_max: float = 10.0,
        set_zero_on_start: bool = False,
    ) -> None:
        super().__init__(name, can_id)
        if not 0x01 <= can_id <= 0x0F:
            raise ValueError(
                f"can_id (ESC_ID) は 0x01..0x0F の範囲で指定してください: {can_id:#x} "
                "(フィードバックには下位 4bit しか載らないため。レジスタ 0x08 を書き換えること)"
            )
        if not 0 <= master_id <= 0x7FF:
            raise ValueError("master_id (MST_ID) は 0x000..0x7FF の範囲で指定してください")

        self.master_id = master_id
        self.mode = ControlMode(mode) if isinstance(mode, str) else mode
        if self.mode not in self._CONTROL_TO_CTRL_MODE:
            raise ValueError(f"Dm3520Driver は {self.mode} モードをサポートしていません")

        for label, value in (("p_max", p_max), ("v_max", v_max), ("t_max", t_max)):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{label} は正の有限値で指定してください: {value!r}")
        self.p_max = float(p_max)
        self.v_max = float(v_max)
        self.t_max = float(t_max)

        if not math.isfinite(limit_speed) or limit_speed <= 0:
            raise ValueError("limit_speed は正の有限値で指定してください")
        self.limit_speed = min(float(limit_speed), self.v_max)
        self.set_zero_on_start = bool(set_zero_on_start)

        self.error_code = int(Dm3520Error.DISABLED)
        self._feedback_received = False

    @staticmethod
    def _clamp(value: float, min_val: float, max_val: float) -> float:
        return min(max(float(value), min_val), max_val)

    @staticmethod
    def uint_to_float(raw: int, max_abs: float, bits: int) -> float:
        span = float((1 << bits) - 1)
        return raw * (2.0 * max_abs) / span - max_abs

    def _standard(self, arbitration_id: int, data: bytes) -> can.Message:
        return can.Message(arbitration_id=arbitration_id, data=data, is_extended_id=False)

    def _special_command(self, command: int) -> can.Message:
        return self._standard(self.MIT_CMD_BASE + self.can_id, bytes([0xFF] * 7 + [command]))

    def encode_enable(self) -> can.Message:
        return self._special_command(self.SPECIAL_ENABLE)

    def encode_disable(self) -> can.Message:
        return self._special_command(self.SPECIAL_DISABLE)

    def encode_set_zero(self) -> can.Message:
        return self._special_command(self.SPECIAL_SET_ZERO)

    def encode_write_register_u32(self, register: int, value: int) -> can.Message:
        data = struct.pack("<HBBI", self.can_id, self.CONFIG_WRITE, register, value)
        return self._standard(self.CONFIG_FRAME_ID, data)

    def encode_read_register(self, register: int) -> can.Message:
        data = struct.pack("<HBBI", self.can_id, self.CONFIG_READ, register, 0)
        return self._standard(self.CONFIG_FRAME_ID, data)

    def encode_ctrl_mode(self, ctrl_mode: Dm3520CtrlMode | int) -> can.Message:
        return self.encode_write_register_u32(self.REG_CTRL_MODE, int(ctrl_mode))

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        if mode is ControlMode.POSITION:
            position = self._clamp(value, -self.p_max, self.p_max)
            data = struct.pack("<ff", position, self.limit_speed)
            return self._standard(self.POSITION_CMD_BASE + self.can_id, data)

        if mode is ControlMode.VELOCITY:
            speed = self._clamp(value, -self.limit_speed, self.limit_speed)
            return self._standard(self.VELOCITY_CMD_BASE + self.can_id, struct.pack("<f", speed))

        raise ValueError(f"Dm3520Driver は {mode} モードをサポートしていません")

    def _is_config_response(self, msg: can.Message) -> bool:
        data = msg.data
        return (
            data[0] == (self.can_id & 0xFF)
            and data[1] == ((self.can_id >> 8) & 0xFF)
            and data[2] in (self.CONFIG_READ, self.CONFIG_WRITE)
        )

    def matches_feedback(self, msg: can.Message) -> bool:
        if msg.is_extended_id or len(msg.data) != 8:
            return False
        if msg.arbitration_id != self.master_id:
            return False
        if self._is_config_response(msg):
            return False
        return (msg.data[0] & 0x0F) == (self.can_id & 0x0F)

    def decode_feedback(self, msg: can.Message) -> MotorState:
        if not self.matches_feedback(msg):
            raise ValueError("対象モータの DM3520 フィードバックではありません")

        data = msg.data
        self.error_code = (data[0] >> 4) & 0x0F
        self._feedback_received = True

        pos_raw = (data[1] << 8) | data[2]
        vel_raw = (data[3] << 4) | (data[4] >> 4)
        torque_raw = ((data[4] & 0x0F) << 8) | data[5]
        t_mos = float(data[6])
        t_rotor = float(data[7])

        return MotorState(
            position=self.uint_to_float(pos_raw, self.p_max, self._POS_BITS),
            velocity=self.uint_to_float(vel_raw, self.v_max, self._VEL_BITS),
            current=self.uint_to_float(torque_raw, self.t_max, self._TORQUE_BITS),
            temperature=max(t_mos, t_rotor),
        )

    def initialization_steps(self) -> list[tuple[can.Message, float]]:
        steps = [
            (self.encode_disable(), 0.05),
            (self.encode_ctrl_mode(self._CONTROL_TO_CTRL_MODE[self.mode]), 0.05),
        ]
        if self.set_zero_on_start:
            steps.append((self.encode_set_zero(), 0.2))
        return steps

    def activation_steps(self, *, after_set_zero: bool = False) -> list[tuple[can.Message, float]]:
        hold = (
            self._state.position
            if self.mode is ControlMode.POSITION and not after_set_zero
            else 0.0
        )
        return [
            (self.encode_target(self.mode, hold), 0.05),
            (self.encode_enable(), 0.1),
        ]

    def requires_fresh_feedback_for_activation(self) -> bool:
        return self.mode is ControlMode.POSITION

    def feedback_probe_message(self) -> can.Message | None:
        return self.encode_disable()

    def idle_target_value(self) -> float:
        return self._state.position if self.mode is ControlMode.POSITION else 0.0

    def emergency_stop_message(self) -> can.Message:
        return self.encode_disable()

    def is_fault(self) -> bool:
        return self.error_code >= _FIRST_ERROR_CODE

    def is_energized(self) -> bool | None:
        if not self._feedback_received:
            return None
        return self.error_code == Dm3520Error.ENABLED

    def has_overcurrent_warning(self) -> bool:
        return self.error_code == Dm3520Error.OVERCURRENT

    def error_label(self) -> str | None:
        return _ERROR_LABELS.get(self.error_code)

    _RPM_TO_RAD_PER_S = 2.0 * math.pi / 60.0

    def default_tolerance(self, mode: ControlMode) -> float:
        if mode is ControlMode.POSITION:
            return math.radians(super().default_tolerance(mode))
        if mode is ControlMode.VELOCITY:
            return super().default_tolerance(mode) * self._RPM_TO_RAD_PER_S
        return super().default_tolerance(mode)
