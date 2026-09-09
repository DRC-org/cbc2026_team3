from __future__ import annotations

import logging
import math
import struct
from enum import IntEnum, IntFlag
from typing import ClassVar

import can

from lib.drivers.base import ControlMode, MotorDriver, MotorState

logger = logging.getLogger(__name__)


class Edulite05RunMode(IntEnum):
    MIT = 0
    POSITION = 1
    VELOCITY = 2
    CURRENT = 3


class Edulite05ModeState(IntEnum):
    RESET = 0
    CALIBRATION = 1
    MOTOR = 2


class Edulite05Fault(IntFlag):
    NONE = 0
    UNDERVOLTAGE = 1
    OVERCURRENT = 2
    OVERTEMP = 4
    MAG_ENCODER = 8
    HALL = 16
    UNCALIBRATED = 32


_TURN = 2.0 * math.pi


class Edulite05Driver(MotorDriver):
    POS_MIN, POS_MAX = -12.57, 12.57
    VEL_MIN, VEL_MAX = -50.0, 50.0
    TORQUE_MIN, TORQUE_MAX = -6.0, 6.0
    KP_MIN, KP_MAX = 0.0, 500.0

    COMM_TYPE_FEEDBACK = 0x02
    COMM_TYPE_ENABLE = 0x03
    COMM_TYPE_DISABLE = 0x04
    COMM_TYPE_SET_ZERO = 0x06
    COMM_TYPE_SET_ID = 0x07
    COMM_TYPE_READ_PARAM = 0x11
    COMM_TYPE_WRITE_PARAM = 0x12

    PARAM_RUN_MODE = 0x7005
    PARAM_IQ_REF = 0x7006
    PARAM_SPD_REF = 0x700A
    PARAM_LIMIT_TORQUE = 0x700B
    PARAM_LOC_REF = 0x7016
    PARAM_LIMIT_SPD = 0x7017
    PARAM_LIMIT_CUR = 0x7018
    PARAM_LOC_KP = 0x701E

    _CONTROL_TO_RUN_MODE: ClassVar[dict[ControlMode, Edulite05RunMode]] = {
        ControlMode.POSITION: Edulite05RunMode.POSITION,
        ControlMode.VELOCITY: Edulite05RunMode.VELOCITY,
        ControlMode.CURRENT: Edulite05RunMode.CURRENT,
    }
    _U8_PARAMS: ClassVar[frozenset[int]] = frozenset({PARAM_RUN_MODE})
    _TARGET_PARAM: ClassVar[dict[ControlMode, int]] = {
        ControlMode.POSITION: PARAM_LOC_REF,
        ControlMode.VELOCITY: PARAM_SPD_REF,
        ControlMode.CURRENT: PARAM_IQ_REF,
    }

    def __init__(
        self,
        name: str,
        can_id: int,
        host_id: int = 0xFD,
        *,
        mode: ControlMode | str = ControlMode.POSITION,
        limit_speed: float = 2.0,
        limit_current: float = 5.0,
        position_kp: float = 30.0,
        set_zero_on_start: bool = False,
    ) -> None:
        super().__init__(name, can_id)
        if not 0 <= can_id <= 0xFF:
            raise ValueError("can_id は 0..255 の範囲で指定してください")
        if not 0 <= host_id <= 0xFF:
            raise ValueError("host_id は 0..255 の範囲で指定してください")

        self.host_id = host_id
        self.mode = ControlMode(mode) if isinstance(mode, str) else mode
        if self.mode not in self._CONTROL_TO_RUN_MODE:
            raise ValueError(f"Edulite05Driver は {self.mode} モードをサポートしていません")
        self.limit_speed = self._clamp(limit_speed, 0.0, self.VEL_MAX)
        if not math.isfinite(limit_current) or limit_current < 0:
            raise ValueError("limit_current は有限の0以上で指定してください")
        self.limit_current = float(limit_current)
        self.position_kp = self._clamp(position_kp, self.KP_MIN, self.KP_MAX)
        self.set_zero_on_start = bool(set_zero_on_start)
        self.mode_state: int | None = None
        self.fault_bits = Edulite05Fault.NONE

        self._origin_offset = 0.0
        self._origin_captured = False
        self._target_clamped = False
        self._prev_raw_position: float | None = None
        self._wrap_turns = 0

    @staticmethod
    def _clamp(value: float, min_val: float, max_val: float) -> float:
        return min(max(float(value), min_val), max_val)

    @staticmethod
    def build_can_id(comm_type: int, data_area2: int, dest_id: int) -> int:
        return ((comm_type & 0x1F) << 24) | ((data_area2 & 0xFFFF) << 8) | (dest_id & 0xFF)

    @staticmethod
    def parse_can_id(arbitration_id: int) -> tuple[int, int, int]:
        return (
            (arbitration_id >> 24) & 0x1F,
            (arbitration_id >> 8) & 0xFFFF,
            arbitration_id & 0xFF,
        )

    @classmethod
    def float_to_uint16(cls, value: float, min_val: float, max_val: float) -> int:
        value = cls._clamp(value, min_val, max_val)
        return int((value - min_val) * 65535.0 / (max_val - min_val))

    @staticmethod
    def uint16_to_float(raw: int, min_val: float, max_val: float) -> float:
        return raw * (max_val - min_val) / 65535.0 + min_val

    def _message(
        self, comm_type: int, data: bytes, *, data_area2: int | None = None
    ) -> can.Message:
        if data_area2 is None:
            data_area2 = self.host_id
        return can.Message(
            arbitration_id=self.build_can_id(comm_type, data_area2, self.can_id),
            data=data,
            is_extended_id=True,
        )

    def encode_write_param_float(self, param_id: int, value: float) -> can.Message:
        data = struct.pack("<Hxxf", param_id, float(value))
        return self._message(self.COMM_TYPE_WRITE_PARAM, data)

    def encode_write_param_u8(self, param_id: int, value: int) -> can.Message:
        data = struct.pack("<HxxBxxx", param_id, value)
        return self._message(self.COMM_TYPE_WRITE_PARAM, data)

    def encode_run_mode(self, mode: Edulite05RunMode | int) -> can.Message:
        return self.encode_write_param_u8(self.PARAM_RUN_MODE, int(mode))

    def encode_read_param(self, param_id: int) -> can.Message:
        return self._message(self.COMM_TYPE_READ_PARAM, struct.pack("<Hxxxxxx", param_id))

    def matches_read_param(self, msg: can.Message) -> bool:
        if not msg.is_extended_id or len(msg.data) != 8:
            return False
        comm_type, data_area2, dest_id = self.parse_can_id(msg.arbitration_id)
        return (
            comm_type == self.COMM_TYPE_READ_PARAM
            and (data_area2 & 0xFF) == self.can_id
            and dest_id == self.host_id
        )

    def decode_read_param(self, msg: can.Message) -> tuple[int, float | int]:
        """パラメータ応答を (param_id, 値) にほどく。

        応答フレームは値の型を載せないので、float 4byte と uint8 1byte のどちらで
        詰まっているかはパラメータ ID から決めるしかない。
        """
        if not self.matches_read_param(msg):
            raise ValueError("対象モータの EDULITE 05 パラメータ応答ではありません")
        param_id = struct.unpack_from("<H", msg.data)[0]
        if param_id in self._U8_PARAMS:
            return param_id, msg.data[4]
        return param_id, struct.unpack_from("<f", msg.data, 4)[0]

    @property
    def origin_offset(self) -> float:
        """論理 0 が指す連続化後の生角度 [rad]。ログと診断のための読み出し口。"""
        return self._origin_offset

    def _track_wrap(self, raw: float) -> None:
        """電文値が [0, 360) へ畳まれた跳びを回転数で吸収する。

        電源投入のたびに位置の報告値は [0, 360) へ畳み直される。`rotate` は
        `scale` が逆の 2 台の対で、片側は論理角が正であるかぎり生値が負になり
        電源断のたびに +360deg される。左右に 1 回転の差が生まれ、`SyncMonitor`
        が解除のたびに全体緊急停止を掛け直す。

        「2 つの電文のあいだに軸が半回転以上動かない」ことに立っている。
        通常は 20Hz 以上で受けるので満たし、電源断を跨ぐ窓は `rotate` が無励磁で
        自重で回らないこと (指差喚呼 `rotate_holds`) が人の手で担保する。
        """
        if self._prev_raw_position is not None:
            diff = raw - self._prev_raw_position
            if diff > math.pi:
                self._wrap_turns -= 1
            elif diff < -math.pi:
                self._wrap_turns += 1
        self._prev_raw_position = raw

    def _continuous_position(self) -> float:
        """電文値を連続化した生角度 [rad]。原点も指令もこの座標で持つ。"""
        return self._state.position + self._wrap_turns * _TURN

    def capture_origin_here(self) -> None:
        """今の連続化後の生角度を論理原点として控える。**CAN へは 1 通も出さない。**

        原点をモータ側の `SET_ZERO` で切り直すと生座標そのものが付け替わり、切り直しの
        前後で測った値が混ざる。`rotate` は逆回転ペアで左右の機械ゼロが 175.879deg 違い、
        混ざれば `SyncMonitor` が全体緊急停止を掛ける。PC 側で持てば生座標は動かない。

        控えるのは電文値ではなく連続化後の値である。電文値で控えると、電源投入で
        [0, 360) へ畳まれた後に控え直した瞬間、回転数ぶんの論理位置が生える。
        """
        raw = self._continuous_position()
        # 電源断で機構が動いたかどうかを知る材料はここにしか出ない。
        logger.info(
            "EDULITE 05 の原点を控えました (motor=%s, 生角度=%.4frad, "
            "直前の原点との差=%+.4frad, 控え直し=%s)",
            self.name,
            raw,
            raw - self._origin_offset,
            self._origin_captured,
        )
        self._origin_offset = raw
        self._origin_captured = True

    def feedback_position(self) -> float:
        """論理位置 [rad]。`state.position` は電文どおりの生値のまま残す。"""
        return self._continuous_position() - self._origin_offset

    def _clamp_target(self, mode: ControlMode, requested: float, lo: float, hi: float) -> float:
        clamped = self._clamp(requested, lo, hi)
        if clamped != requested:
            # 20Hz の再送が端に張り付いた同じ指令を送り続けるので、変化の瞬間だけ残す。
            if not self._target_clamped:
                logger.warning(
                    "EDULITE 05 の目標値がレンジ端で頭打ちになりました "
                    "(motor=%s, mode=%s, 生値 %.4f → %.4f, 原点オフセット=%.4frad)",
                    self.name,
                    mode.name,
                    requested,
                    clamped,
                    self._origin_offset,
                )
            self._target_clamped = True
        else:
            self._target_clamped = False
        return clamped

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        """`value` は**論理**座標。生値へ直してから書く。"""
        param_id = self._TARGET_PARAM.get(mode)
        if param_id is None:
            raise ValueError(f"Edulite05Driver は {mode} モードをサポートしていません")
        if mode is ControlMode.POSITION:
            # `POS_MIN`/`POS_MAX` は uint16 の写像レンジであって機構の可動域ではない。
            # 論理値でクランプすると、オフセットを足した生値がレンジ外へ出て折り返す。
            # モータは畳まれた電文座標に居るので、連続化に足した回転数を引いて戻す。
            # 引かずに送ると、モータは指令を 1 回転ぶんの移動として実行する。
            raw = value + self._origin_offset - self._wrap_turns * _TURN
            value = self._clamp_target(mode, raw, self.POS_MIN, self.POS_MAX)
        elif mode is ControlMode.VELOCITY:
            value = self._clamp_target(mode, value, -self.limit_speed, self.limit_speed)
        else:
            value = self._clamp_target(mode, value, -self.limit_current, self.limit_current)
        return self.encode_write_param_float(param_id, value)

    def encode_enable(self) -> can.Message:
        return self._message(self.COMM_TYPE_ENABLE, bytes(8))

    def encode_disable(self, *, clear_fault: bool = False) -> can.Message:
        return self._message(self.COMM_TYPE_DISABLE, bytes([int(clear_fault)]) + bytes(7))

    def encode_set_zero(self) -> can.Message:
        return self._message(self.COMM_TYPE_SET_ZERO, b"\x01" + bytes(7))

    def encode_set_id(self, new_can_id: int) -> can.Message:
        if not 0 <= new_can_id <= 0xFF:
            raise ValueError("new_can_id は 0..255 の範囲で指定してください")
        return self._message(
            self.COMM_TYPE_SET_ID,
            b"\x01" + bytes(7),
            data_area2=((new_can_id & 0xFF) << 8) | self.host_id,
        )

    def initialization_steps(self) -> list[tuple[can.Message, float]]:
        """無励磁化 → 設定の書き込み (→ 原点確定)。

        **`set_zero` は `reinitialization_steps()` へ移してはならない。** 再励磁の
        たびに送ると、零点確定で合わせた原点をその場の姿勢へ書き換える
        (`rotate_r` / `rotate_l` は `set_zero_on_start: true`)。
        """
        steps = list(self.reinitialization_steps())
        if self.set_zero_on_start:
            steps.append((self.encode_set_zero(), 0.2))
        return steps

    def reinitialization_steps(self) -> list[tuple[can.Message, float]]:
        """無励磁化 → 設定の書き込み。**電源断で失われるぶんだけ。**

        `run_mode` / `limit_spd` / `limit_cur` / `loc_kp` はどれも `WRITE_PARAM`
        (0x12) で書く。マニュアルの type 18 は "lost after power failure" なので、
        物理非常停止で電源が落ちた個体は**位置モードですらない**状態で立ち上がる。
        """
        steps = [
            (self.encode_disable(), 0.05),
            (self.encode_run_mode(self._CONTROL_TO_RUN_MODE[self.mode]), 0.05),
            (self.encode_write_param_float(self.PARAM_LIMIT_SPD, self.limit_speed), 0.05),
            (self.encode_write_param_float(self.PARAM_LIMIT_CUR, self.limit_current), 0.05),
        ]
        if self.mode is ControlMode.POSITION:
            steps.append((self.encode_write_param_float(self.PARAM_LOC_KP, self.position_kp), 0.05))
        return steps

    def activation_steps(self, *, after_set_zero: bool = False) -> list[tuple[can.Message, float]]:
        """保持目標は論理座標の実測位置。**原点を PC 側で持つ限り `after_set_zero`
        の有無で変わらない** —— 生座標は原点を控え直しても動かないので、切り直しの
        前に測られた在庫のフィードバックも切り直した後と同じ物理位置を指す。

        モータ側の `SET_ZERO` で切り直したときだけ別で、そこは 0 を書く。旧原点で
        測られた在庫を書くと enable した瞬間に新旧の原点差だけ機構が動き、`rotate`
        はフラッシュの機械ゼロ次第でその差が 3 桁 deg に達する。
        """
        zeroed_in_motor = after_set_zero and not self._origin_captured
        if self.mode is not ControlMode.POSITION or zeroed_in_motor:
            hold = 0.0
        else:
            hold = self.feedback_position()
        return [
            (self.encode_target(self.mode, hold), 0.05),
            (self.encode_enable(), 0.1),
        ]

    def deactivation_steps(self) -> list[tuple[can.Message, float]]:
        return [(self.encode_disable(), 0.05)]

    def origin_capture_steps(self) -> list[tuple[can.Message, float]]:
        return [(self.encode_set_zero(), 0.2)]

    def idle_target_value(self) -> float:
        """20Hz の再送が `encode_target()` へ通す値なので**論理**で返す。

        生のまま返すと `生値 + オフセット` が書かれ続け、機構がオフセットぶん走る。
        """
        return self.feedback_position() if self.mode is ControlMode.POSITION else 0.0

    def requires_fresh_feedback_for_activation(self) -> bool:
        return self.mode is ControlMode.POSITION

    def feedback_probe_message(self) -> can.Message | None:
        return self.encode_disable()

    def decode_feedback(self, msg: can.Message) -> MotorState:
        if not self.matches_feedback(msg):
            raise ValueError("対象モータの EDULITE 05 フィードバックではありません")
        if len(msg.data) != 8:
            raise ValueError("EDULITE 05 フィードバックは 8 byte 必須です")

        _comm_type, data_area2, _dest_id = self.parse_can_id(msg.arbitration_id)
        self.mode_state = (data_area2 >> 14) & 0x03
        self.fault_bits = Edulite05Fault((data_area2 >> 8) & 0x3F)
        pos_raw, vel_raw, torque_raw, temp_raw = struct.unpack(">HHHH", msg.data)
        position = self.uint16_to_float(pos_raw, self.POS_MIN, self.POS_MAX)
        self._track_wrap(position)
        return MotorState(
            position=position,
            velocity=self.uint16_to_float(vel_raw, self.VEL_MIN, self.VEL_MAX),
            current=self.uint16_to_float(torque_raw, self.TORQUE_MIN, self.TORQUE_MAX),
            temperature=temp_raw / 10.0,
        )

    def matches_feedback(self, msg: can.Message) -> bool:
        if not msg.is_extended_id or len(msg.data) != 8:
            return False
        comm_type, data_area2, dest_id = self.parse_can_id(msg.arbitration_id)
        motor_id = data_area2 & 0xFF
        return (
            comm_type == self.COMM_TYPE_FEEDBACK
            and motor_id == self.can_id
            and dest_id == self.host_id
        )

    def is_energized(self) -> bool | None:
        if self.mode_state is None:
            return None
        return self.mode_state == Edulite05ModeState.MOTOR

    def has_overcurrent_warning(self) -> bool:
        return bool(self.fault_bits & Edulite05Fault.OVERCURRENT)

    def is_fault(self) -> bool:
        return self.fault_bits != Edulite05Fault.NONE

    _RPM_TO_RAD_PER_S = 2.0 * math.pi / 60.0

    def default_tolerance(self, mode: ControlMode) -> float:
        if mode is ControlMode.POSITION:
            return math.radians(super().default_tolerance(mode))
        if mode is ControlMode.VELOCITY:
            return super().default_tolerance(mode) * self._RPM_TO_RAD_PER_S
        return super().default_tolerance(mode)

    def emergency_stop_message(self) -> can.Message:
        return self.encode_disable()
