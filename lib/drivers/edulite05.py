from __future__ import annotations

import math
import struct
from enum import IntEnum, IntFlag
from typing import ClassVar

import can

from lib.drivers.base import CheckContext, ControlMode, MotorDriver, MotorState


class Edulite05RunMode(IntEnum):
    MIT = 0
    POSITION = 1
    VELOCITY = 2
    CURRENT = 3


class Edulite05Fault(IntFlag):
    NONE = 0
    UNDERVOLTAGE = 1
    OVERCURRENT = 2
    OVERTEMP = 4
    MAG_ENCODER = 8
    HALL = 16
    UNCALIBRATED = 32


class Edulite05Driver(MotorDriver):
    """RobStride EDULITE 05 CAN 2.0B extended-frame driver."""

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
        self.mode_state = 0
        self.fault_bits = Edulite05Fault.NONE

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

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        param_id = self._TARGET_PARAM.get(mode)
        if param_id is None:
            raise ValueError(f"Edulite05Driver は {mode} モードをサポートしていません")
        if mode is ControlMode.POSITION:
            value = self._clamp(value, self.POS_MIN, self.POS_MAX)
        elif mode is ControlMode.VELOCITY:
            value = self._clamp(value, -self.limit_speed, self.limit_speed)
        else:
            value = self._clamp(value, -self.limit_current, self.limit_current)
        return self.encode_write_param_float(param_id, value)

    def encode_enable(self) -> can.Message:
        return self._message(self.COMM_TYPE_ENABLE, bytes(8))

    def encode_disable(self, *, clear_fault: bool = False) -> can.Message:
        return self._message(self.COMM_TYPE_DISABLE, bytes([int(clear_fault)]) + bytes(7))

    def encode_set_zero(self) -> can.Message:
        return self._message(self.COMM_TYPE_SET_ZERO, b"\x01" + bytes(7))

    def initialization_steps(self) -> list[tuple[can.Message, float]]:
        steps = [
            (self.encode_disable(), 0.05),
            (self.encode_run_mode(self._CONTROL_TO_RUN_MODE[self.mode]), 0.05),
            (self.encode_write_param_float(self.PARAM_LIMIT_SPD, self.limit_speed), 0.05),
            (self.encode_write_param_float(self.PARAM_LIMIT_CUR, self.limit_current), 0.05),
        ]
        if self.mode is ControlMode.POSITION:
            steps.append((self.encode_write_param_float(self.PARAM_LOC_KP, self.position_kp), 0.05))
        if self.set_zero_on_start:
            steps.append((self.encode_set_zero(), 0.2))
        return steps

    def activation_steps(self, *, after_set_zero: bool = False) -> list[tuple[can.Message, float]]:
        """現在値を目標に書いてから励磁する。

        本機は enable した瞬間に PARAM_LOC_REF (位置モード) へ追従を始めるため、
        目標を書かずに励磁するとアームが原点へ全速で飛ぶ。位置モードでは直前に
        受信したフィードバックの実測角を、それ以外のモードでは静止を意味する 0 を
        目標として書いてから enable する。

        after_set_zero は「直前に set_zero を送った」経路向け。原点が付け替わった
        後のフィードバックはまだ届いておらず、手元の実測角は旧原点基準のままなので、
        保持目標に使うと原点の差分だけアームが動く。新原点そのものである 0 を書く。
        起動時 (initialize_motors) と緊急停止復帰 (activate_motors) は set_zero を
        挟まないか新しいフィードバックを待ってから来るため、既定は実測角のままでよい。
        """
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
        # 位置モードの保持目標は実測角そのもの。MotorState の初期値 0.0 を実測角と
        # 取り違えると原点へ飛ぶため、フィードバック未受信では励磁させない。
        # set_zero_on_start で原点が変わった直後の値も同じ理由で使えない。
        return self.mode is ControlMode.POSITION

    def feedback_probe_message(self) -> can.Message | None:
        # disable は無励磁を保ったままフィードバック応答を返させられる唯一のフレーム。
        # clear_fault=False なので障害フラグを握り潰す心配もない。
        return self.encode_disable()

    def decode_feedback(self, msg: can.Message) -> MotorState:
        if not self.matches_feedback(msg):
            raise ValueError("対象モータの EDULITE 05 フィードバックではありません")
        if len(msg.data) != 8:
            raise ValueError("EDULITE 05 フィードバックは 8 byte 必須です")

        _comm_type, data_area2, _dest_id = self.parse_can_id(msg.arbitration_id)
        # 参照実装準拠。mode_state の正確な bit 範囲は実機応答で要確認。
        self.mode_state = (data_area2 >> 14) & 0x03
        self.fault_bits = Edulite05Fault((data_area2 >> 8) & 0x3F)
        pos_raw, vel_raw, torque_raw, temp_raw = struct.unpack(">HHHH", msg.data)
        return MotorState(
            position=self.uint16_to_float(pos_raw, self.POS_MIN, self.POS_MAX),
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

    def has_overcurrent_warning(self) -> bool:
        return bool(self.fault_bits & Edulite05Fault.OVERCURRENT)

    def is_fault(self) -> bool:
        return self.fault_bits != Edulite05Fault.NONE

    # 本機の速度は rad/s だが、yaml の magnitude も操縦者への表示も共通単位の rpm で扱う
    _RPM_TO_RAD_PER_S = 2.0 * math.pi / 60.0

    def default_tolerance(self, mode: ControlMode) -> float:
        # 共通既定値 (deg / rpm) を本機のフィードバック単位へ換算するだけに留める。
        # ここに独自の数値を書くと共通既定値を直しても本機だけ古い値のまま残る
        if mode is ControlMode.POSITION:
            return math.radians(super().default_tolerance(mode))
        if mode is ControlMode.VELOCITY:
            return super().default_tolerance(mode) * self._RPM_TO_RAD_PER_S
        return super().default_tolerance(mode)

    def prepare_check_steps(self) -> list[tuple[can.Message, float]]:
        """設定 → 保持目標 → 励磁。起動経路と同じ手順をそのまま合成する。

        励磁手順をここに書き写すと、片方だけ直したときに「保持目標を書かずに
        enable してアームが原点へ飛ぶ」が動作確認経路だけで再発する。
        run_mode も設定値のまま使う。ここで POSITION を強制すると、動作確認後に
        戻す経路が無いぶん速度指令で運用する機体の run_mode が書き換わったまま残る。
        """
        return self.initialization_steps() + self.activation_steps(
            after_set_zero=self.set_zero_on_start
        )

    def check_safety_error(self) -> str | None:
        # 電流モードの指令は [A]、本機のフィードバックはトルク [Nm] で次元が違うため、
        # 指令に追従したかを判定する術がない。「動かなかった」と区別できない合否を
        # 出すより、動作確認の対象外だと明示して打ち切る。
        if self.mode is ControlMode.CURRENT:
            return (
                "EDULITE 05 電流モードは動作確認の対象外 "
                "(指令[A] とフィードバック[Nm] は次元が違う)"
            )
        if self.is_fault():
            return f"EDULITE 05 fault=0x{int(self.fault_bits):02X}"
        if self._state.temperature >= 60.0:
            return f"EDULITE 05 過温 {self._state.temperature:.1f}C"
        return None

    def requires_fresh_feedback_for_check(self) -> bool:
        return True

    def emergency_stop_message(self) -> can.Message:
        return self.encode_disable()

    def check_command(self, *, magnitude: float = 5.0) -> tuple[can.Message, CheckContext]:
        # 動作確認は運用と同じ run_mode のまま行う。位置設定の機体へ速度指令を送っても
        # 無反応にしかならず、逆に run_mode を書き換えると運用時の指令が効かなくなる。
        if self.mode is ControlMode.VELOCITY:
            # magnitude は共通単位の rpm。encode_target と同じクランプを context にも
            # 効かせないと、limit_speed で頭打ちになった瞬間に必ず不合格になる。
            target = self._clamp(
                magnitude * self._RPM_TO_RAD_PER_S, -self.limit_speed, self.limit_speed
            )
            context = CheckContext(
                mode=ControlMode.VELOCITY,
                target=target,
                reference=self._state.velocity,
                # 内部単位は rad/s だが、yaml の magnitude と揃わないと操縦者が原因を追えない
                display_scale=1.0 / self._RPM_TO_RAD_PER_S,
                display_unit="rpm",
            )
            return self.encode_target(ControlMode.VELOCITY, target), context
        if self.mode is not ControlMode.POSITION:
            raise ValueError(f"Edulite05Driver は {self.mode} モードの動作確認に対応していません")

        target = self._clamp(
            self._state.position + math.radians(magnitude), self.POS_MIN, self.POS_MAX
        )
        context = CheckContext(
            mode=ControlMode.POSITION,
            target=target,
            reference=self._state.position,
            display_scale=180.0 / math.pi,
            display_unit="deg",
        )
        return self.encode_target(ControlMode.POSITION, target), context

    def evaluate_check_result(self, context: CheckContext) -> tuple[bool, str | None]:
        return self.evaluate_tracking(context)

    def reset_after_check(self) -> can.Message:
        # prepare_check_steps が run_mode を設定値のまま使うので、戻す作業は無く
        # 無励磁化だけで原状に復帰する
        return self.encode_disable()
