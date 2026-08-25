from __future__ import annotations

import struct
from enum import IntEnum

import can

from lib.drivers.base import ControlMode, MotorDriver, MotorState

_MODE_MAP = {
    ControlMode.POSITION: 0,
    ControlMode.VELOCITY: 1,
    ControlMode.DUTY: 2,
}


class CommandType(IntEnum):
    SET_TARGET = 0
    FEEDBACK = 1
    SET_MODE = 2
    SET_PARAM = 3
    E_STOP = 7


class GenericDriver(MotorDriver):
    """自作モータドライバ(DC モータ/サーボ)用の汎用 CAN ドライバ。"""

    # 動作確認の判定許容値 (control_type 別)
    # POSITION: 0.1deg 単位フィードバック → 1.0deg 余裕
    # VELOCITY: 5rpm 程度のリップルを許容
    # DUTY: |velocity| > 10rpm で「回った」と見なす
    _CHECK_POSITION_TOLERANCE_DEG = 1.0
    _CHECK_VELOCITY_TOLERANCE_RPM = 5.0
    _CHECK_DUTY_ROTATION_RPM = 10.0

    def __init__(
        self,
        name: str,
        can_id: int,
        *,
        control_type: ControlMode = ControlMode.POSITION,
    ) -> None:
        super().__init__(name, can_id)
        # フィードバック Byte7 の bit1/bit2 は MotorState に持たせず、ドライバ側で保持する
        # (MotorState は frozen dataclass で他ドライバ共通のため、汎用化を避けて専用属性に分離)
        self._overcurrent_flag: bool = False
        self._overheat_flag: bool = False
        # 動作確認や reset の指令を出す制御モード。config から渡される値で上書き可能。
        self.control_type: ControlMode = control_type

    # ---- CAN ID ユーティリティ ----

    @staticmethod
    def build_can_id(command_type: CommandType, device_id: int) -> int:
        return (int(command_type) << 8) | device_id

    @staticmethod
    def parse_can_id(arbitration_id: int) -> tuple[CommandType, int]:
        command_type = CommandType((arbitration_id >> 8) & 0x07)
        device_id = arbitration_id & 0xFF
        return command_type, device_id

    # ---- 送信フレーム生成 ----

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        data = bytearray(8)
        data[0] = _MODE_MAP[mode]
        struct.pack_into("<f", data, 2, value)
        return can.Message(
            arbitration_id=self.build_can_id(CommandType.SET_TARGET, self.can_id),
            data=bytes(data),
            is_extended_id=False,
        )

    def encode_set_mode(self, mode: ControlMode) -> can.Message:
        data = bytearray(8)
        data[0] = _MODE_MAP[mode]
        return can.Message(
            arbitration_id=self.build_can_id(CommandType.SET_MODE, self.can_id),
            data=bytes(data),
            is_extended_id=False,
        )

    @staticmethod
    def encode_e_stop() -> can.Message:
        return can.Message(
            arbitration_id=0x7FF,
            data=bytes(8),
            is_extended_id=False,
        )

    # ---- 受信フレーム解析 ----

    def decode_feedback(self, msg: can.Message) -> MotorState:
        d = msg.data
        raw_pos = struct.unpack_from("<h", d, 0)[0]
        raw_vel = struct.unpack_from("<h", d, 2)[0]
        raw_cur = struct.unpack_from("<h", d, 4)[0]
        temp = d[6]
        flags = d[7]
        return MotorState(
            position=raw_pos * 0.1,
            velocity=float(raw_vel),
            current=float(raw_cur),
            temperature=float(temp),
            reached=bool(flags & 0x01),
        )

    def update_state(self, msg: can.Message) -> MotorState:
        # decode_feedback は純粋関数のまま保ち、副作用 (フラグ保持) はここで処理する
        # bit0 (到達) は MotorState.reached に反映、bit1=過電流 / bit2=過熱 はドライバ属性に保持
        flags = msg.data[7]
        self._overcurrent_flag = bool(flags & 0x02)
        self._overheat_flag = bool(flags & 0x04)
        return super().update_state(msg)

    def matches_feedback(self, msg: can.Message) -> bool:
        cmd, dev = self.parse_can_id(msg.arbitration_id)
        return cmd == CommandType.FEEDBACK and dev == self.can_id

    # ------------------------------------------------------------------ #
    #  目標到達判定
    # ------------------------------------------------------------------ #
    def default_tolerance(self, mode: ControlMode) -> float:
        if mode is ControlMode.POSITION:
            return self._CHECK_POSITION_TOLERANCE_DEG
        if mode is ControlMode.VELOCITY:
            return self._CHECK_VELOCITY_TOLERANCE_RPM
        return super().default_tolerance(mode)

    def is_target_reached(
        self,
        target: float,
        mode: ControlMode,
        *,
        tolerance: float | None = None,
    ) -> bool:
        # 自作モタドラはフィードバック bit0 に到達フラグを持つ。
        # 位置決めは行き過ぎ・オーバーシュートを含むため、ファームの到達判定を優先する
        if mode is ControlMode.POSITION and not self._state.reached:
            return False
        return super().is_target_reached(target, mode, tolerance=tolerance)

    # ------------------------------------------------------------------ #
    #  ヘルスチェック判定
    # ------------------------------------------------------------------ #
    def has_overcurrent_warning(self) -> bool:
        return self._overcurrent_flag

    def is_fault(self) -> bool:
        # 過熱は復帰不能リスクが高いので FAULT 扱い (シーケンス停止対象)
        return self._overheat_flag

    # ------------------------------------------------------------------ #
    #  動作確認 (Phase 6 段階⑦)
    # ------------------------------------------------------------------ #

    def check_command(self, *, magnitude: float = 0.1) -> tuple[can.Message, dict]:
        msg = self.encode_target(self.control_type, magnitude)
        context = {"target": float(magnitude), "mode": self.control_type.value}
        return msg, context

    def evaluate_check_result(
        self,
        state: MotorState,
        context: dict,
        *,
        tolerance: float | None = None,
    ) -> tuple[bool, str | None]:
        target = context["target"]
        mode = context["mode"]

        if mode == ControlMode.POSITION.value:
            tol = tolerance if tolerance is not None else self._CHECK_POSITION_TOLERANCE_DEG
            position_ok = state.reached and abs(state.position - target) <= tol
            if position_ok:
                return True, self._overflow_note()
            return False, (
                f"目標 {target:.2f}deg, 観測 {state.position:.2f}deg (reached={state.reached})"
            )

        if mode == ControlMode.VELOCITY.value:
            tol = tolerance if tolerance is not None else self._CHECK_VELOCITY_TOLERANCE_RPM
            if abs(state.velocity - target) <= tol:
                return True, self._overflow_note()
            return False, (f"目標 {target:.1f}rpm, 観測 {state.velocity:.1f}rpm")

        if mode == ControlMode.DUTY.value:
            if abs(state.velocity) > self._CHECK_DUTY_ROTATION_RPM:
                return True, self._overflow_note()
            return False, (
                f"回転検出なし (target duty={target:.2f}, velocity={state.velocity:.1f}rpm)"
            )

        # 未知の制御モード (将来拡張時のフォールバック)
        return False, f"未対応の制御モード: {mode}"

    def reset_after_check(self) -> can.Message:
        return self.encode_target(self.control_type, 0.0)

    def _overflow_note(self) -> str | None:
        """過電流/過熱フラグが立っている場合の注釈を返す (PASSED でも残す)。"""
        notes: list[str] = []
        if self._overcurrent_flag:
            notes.append("過電流フラグあり")
        if self._overheat_flag:
            notes.append("過熱フラグあり")
        return ", ".join(notes) if notes else None
