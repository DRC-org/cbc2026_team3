from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

import can

from lib.drivers.base import (
    NO_TELEMETRY,
    ControlMode,
    MotorDriver,
    MotorState,
    TelemetrySupport,
)

_MODE_MAP = {
    ControlMode.POSITION: 0,
    ControlMode.VELOCITY: 1,
    ControlMode.DUTY: 2,
    ControlMode.ON_OFF: 3,
}


class CommandType(IntEnum):
    E_STOP = 0
    SET_TARGET = 1
    SET_PARAM = 2
    FEEDBACK = 3
    INFO = 4


_FLAG_REACHED = 0x01
_FLAG_E_STOP = 0x02
_FLAG_WATCHDOG = 0x04
_FLAG_UNCONFIGURED_ID = 0x08
_FLAG_SENSOR = 0x10
_FLAG_NEVER_COMMANDED = 0x20

_DEVICE_ID_MIN = 0x01
_DEVICE_ID_MAX = 0xFE

_FEEDBACK_MIN_LENGTH = 1
_INFO_MIN_LENGTH = 3

_ANGLE_SCALE = 10
_DUTY_SCALE = 10000
_RATE_SCALE = 10
_PLAIN_SCALE = 1

_TARGET_SCALE = {
    ControlMode.POSITION: _ANGLE_SCALE,
    ControlMode.VELOCITY: _ANGLE_SCALE,
    ControlMode.DUTY: _DUTY_SCALE,
    ControlMode.ON_OFF: _PLAIN_SCALE,
}


@dataclass(frozen=True)
class InfoFrame:
    firmware_version: int
    board_kind: int
    slot_kind: int
    angle_range_deg: float | None


_ANGLE_RANGE_EPSILON = 0.05

_POSITION_ONLY_TELEMETRY = TelemetrySupport(
    position=True, velocity=False, current=False, temperature=False
)


def _to_raw(value: float, scale: int) -> int:
    if value != value:
        return 0
    scaled = round(value * scale)
    return max(-32768, min(32767, scaled))


_E_STOP_CLEAR_MAGIC = (0x5A, 0xA5)


class GenericDriver(MotorDriver):
    def __init__(
        self,
        name: str,
        can_id: int,
        *,
        control_type: ControlMode = ControlMode.POSITION,
        expected_firmware: int | None = None,
        expected_angle_range_deg: float | None = None,
    ) -> None:
        if not _DEVICE_ID_MIN <= can_id <= _DEVICE_ID_MAX:
            raise ValueError(
                f"can_id は {_DEVICE_ID_MIN:#04x}〜{_DEVICE_ID_MAX:#04x} の範囲"
                f"(0x00=未設定 / 0xFF=E_STOP ブロードキャストの予約): {can_id}"
            )
        super().__init__(name, can_id)
        self._e_stop_flag: bool = False
        self._watchdog_flag: bool = False
        self._unconfigured_id_flag: bool = False
        self._sensor_flag: bool = False
        self._sensor_contacts: int = 0
        self._never_commanded_flag: bool = False
        self.control_type: ControlMode = control_type
        self._info: InfoFrame | None = None
        self._expected_firmware = expected_firmware
        self._expected_angle_range_deg = expected_angle_range_deg

    @property
    def telemetry(self) -> TelemetrySupport:
        if self.control_type is ControlMode.POSITION:
            return _POSITION_ONLY_TELEMETRY
        return NO_TELEMETRY

    @staticmethod
    def build_can_id(command_type: CommandType, device_id: int) -> int:
        return (int(command_type) << 8) | device_id

    @staticmethod
    def parse_can_id(arbitration_id: int) -> tuple[CommandType, int]:
        command_type = CommandType((arbitration_id >> 8) & 0x07)
        device_id = arbitration_id & 0xFF
        return command_type, device_id

    @staticmethod
    def try_parse_can_id(arbitration_id: int) -> tuple[CommandType, int] | None:
        try:
            return GenericDriver.parse_can_id(arbitration_id)
        except ValueError:
            return None

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        data = bytearray(3)
        data[0] = _MODE_MAP[mode]
        struct.pack_into("<h", data, 1, _to_raw(value, _TARGET_SCALE[mode]))
        return can.Message(
            arbitration_id=self.build_can_id(CommandType.SET_TARGET, self.can_id),
            data=bytes(data),
            is_extended_id=False,
        )

    @staticmethod
    def encode_e_stop() -> can.Message:
        return can.Message(
            arbitration_id=GenericDriver.build_can_id(CommandType.E_STOP, 0xFF),
            data=bytes(3),
            is_extended_id=False,
        )

    @staticmethod
    def encode_e_stop_clear(device_id: int = 0xFF) -> can.Message:
        data = bytearray(3)
        data[0] = 0x01
        data[1], data[2] = _E_STOP_CLEAR_MAGIC
        return can.Message(
            arbitration_id=GenericDriver.build_can_id(CommandType.E_STOP, device_id),
            data=bytes(data),
            is_extended_id=False,
        )

    def decode_feedback(self, msg: can.Message) -> MotorState:
        d = msg.data
        flags = d[0]
        position = struct.unpack_from("<h", d, 1)[0] / _ANGLE_SCALE if len(d) >= 3 else 0.0
        return MotorState(position=position, reached=bool(flags & _FLAG_REACHED))

    def update_state(self, msg: can.Message) -> MotorState:
        # デコードを先に通す。解釈できないフレームで接触を数えると、読み手は
        # その差分だけを見るので「触れていないのに触れた」と読む
        state = super().update_state(msg)
        flags = msg.data[0]
        self._e_stop_flag = bool(flags & _FLAG_E_STOP)
        self._watchdog_flag = bool(flags & _FLAG_WATCHDOG)
        self._unconfigured_id_flag = bool(flags & _FLAG_UNCONFIGURED_ID)
        sensor = bool(flags & _FLAG_SENSOR)
        if sensor and not self._sensor_flag:
            # 数えるのは立ち上がり (OFF→ON) だけ。ON の間ずっと数えると、
            # 触れ続けている 1 回の接触が 100Hz で増え続け、回数が意味を失う
            self._sensor_contacts += 1
        self._sensor_flag = sensor
        self._never_commanded_flag = bool(flags & _FLAG_NEVER_COMMANDED)
        return state

    def matches_feedback(self, msg: can.Message) -> bool:
        if msg.is_extended_id:
            return False

        parsed = self.try_parse_can_id(msg.arbitration_id)
        if parsed is None:
            return False

        cmd, dev = parsed
        if cmd != CommandType.FEEDBACK or dev != self.can_id:
            return False

        return len(msg.data) >= _FEEDBACK_MIN_LENGTH

    def decode_info(self, msg: can.Message) -> InfoFrame:
        d = msg.data
        angle_range = struct.unpack_from("<h", d, 3)[0] / _ANGLE_SCALE if len(d) >= 5 else None
        return InfoFrame(
            firmware_version=d[0],
            board_kind=d[1],
            slot_kind=d[2],
            angle_range_deg=angle_range,
        )

    def update_info(self, msg: can.Message) -> None:
        self._info = self.decode_info(msg)

    def matches_info(self, msg: can.Message) -> bool:
        if msg.is_extended_id:
            return False

        parsed = self.try_parse_can_id(msg.arbitration_id)
        if parsed is None:
            return False

        cmd, dev = parsed
        if cmd != CommandType.INFO or dev != self.can_id:
            return False

        return len(msg.data) >= _INFO_MIN_LENGTH

    @property
    def info(self) -> InfoFrame | None:
        return self._info

    def firmware_confirmed(self) -> bool | None:
        return self._info is not None

    @property
    def info_mismatch(self) -> str | None:
        info = self._info
        if info is None:
            return None

        expected_fw = self._expected_firmware
        if expected_fw is not None and info.firmware_version != expected_fw:
            return (
                f"ファーム版が不一致 (期待 {expected_fw} / 申告 {info.firmware_version})。"
                "焼き忘れの可能性"
            )

        expected_range = self._expected_angle_range_deg
        if expected_range is None:
            return None

        if info.angle_range_deg is None:
            return (
                f"サーボ可動レンジが申告されていない (期待 {expected_range:g}deg)。"
                "可動レンジ以前のファームが焼かれている"
            )

        if abs(info.angle_range_deg - expected_range) > _ANGLE_RANGE_EPSILON:
            return (
                f"サーボ可動レンジが不一致 (期待 {expected_range:g}deg / "
                f"申告 {info.angle_range_deg:g}deg)。180/270 の取り違えの可能性"
            )
        return None

    def is_target_reached(
        self,
        target: float,
        mode: ControlMode,
        *,
        tolerance: float | None = None,
    ) -> bool:
        if mode is ControlMode.POSITION and not self._state.reached:
            return False
        return super().is_target_reached(target, mode, tolerance=tolerance)

    @property
    def e_stop_active(self) -> bool:
        return self._e_stop_flag

    @property
    def watchdog_active(self) -> bool:
        return self._watchdog_flag

    @property
    def device_id_unconfigured(self) -> bool:
        return self._unconfigured_id_flag

    @property
    def never_commanded(self) -> bool:
        return self._never_commanded_flag

    @property
    def sensor_active(self) -> bool:
        return self._sensor_flag

    @property
    def sensor_contact_count(self) -> int:
        # 読んでも消えない単調カウンタ。零点確定とリミットスイッチ保護が同じ
        # センサを読むので、「読むと消える」ラッチだと先に読んだ側が相手のぶんまで
        # 消し、観測周期より狭い ON 区間を取りこぼす。読み手は自分で基準値を控え、
        # その差だけを見る
        return self._sensor_contacts

    def is_fault(self) -> bool:
        return self._unconfigured_id_flag or self.info_mismatch is not None

    def has_on_off_control(self) -> bool:
        return self.control_type is ControlMode.ON_OFF

    def activation_steps(self, *, after_set_zero: bool = False) -> list[tuple[can.Message, float]]:
        return [(self.encode_e_stop_clear(self.can_id), 0.0)]
