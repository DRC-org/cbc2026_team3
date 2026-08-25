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


# FEEDBACK Byte7 の状態フラグ (仕様書 §3.2)
_FLAG_REACHED = 0x01
_FLAG_OVERCURRENT = 0x02
_FLAG_OVERHEAT = 0x04
_FLAG_E_STOP = 0x08
_FLAG_WATCHDOG = 0x10
_FLAG_UNCONFIGURED_ID = 0x20

# E_STOP 解除フレームのマジックバイト (仕様書 §3.5)。
# 1 バイトの値だけで安全装置が解除されるのを避けるため、ファームは Byte1/Byte2 の
# 一致も要求する。バス上のビット化けや無関係なフレームで解除されてはならない。
_E_STOP_CLEAR_MAGIC = (0x5A, 0xA5)


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
        self._e_stop_flag: bool = False
        self._watchdog_flag: bool = False
        self._unconfigured_id_flag: bool = False
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

    @staticmethod
    def try_parse_can_id(arbitration_id: int) -> tuple[CommandType, int] | None:
        """解析できない ID では例外の代わりに None を返す parse_can_id。

        parse_can_id は「自分が組み立てた ID を解析し直す」用途で例外を投げたままにし
        (誤った ID を黙って通すと原因調査が困難になる)、バス上の他人のフレームを
        ふるいにかける受信経路だけをこちらに分ける。
        """
        try:
            return GenericDriver.parse_can_id(arbitration_id)
        except ValueError:
            # 仕様書 §2.1 の予約コマンド種別 (0b100/0b101/0b110) は CommandType に無い
            return None

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

    @staticmethod
    def encode_e_stop_clear(device_id: int = 0xFF) -> can.Message:
        """緊急停止ラッチの解除フレーム (仕様書 §3.5)。

        ファーム側は緊急停止をラッチし、解除フレームを受け取るまで SET_TARGET で
        駆動しない。解除しない限り復旧できないので、起動時と緊急停止解除時に送る。
        """
        data = bytearray(8)
        data[0] = 0x01
        data[1], data[2] = _E_STOP_CLEAR_MAGIC
        return can.Message(
            arbitration_id=GenericDriver.build_can_id(CommandType.E_STOP, device_id),
            data=bytes(data),
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
            reached=bool(flags & _FLAG_REACHED),
        )

    def update_state(self, msg: can.Message) -> MotorState:
        # decode_feedback は純粋関数のまま保ち、副作用 (フラグ保持) はここで処理する
        # bit0 (到達) は MotorState.reached に反映、bit1 以上はドライバ属性に保持
        flags = msg.data[7]
        self._overcurrent_flag = bool(flags & _FLAG_OVERCURRENT)
        self._overheat_flag = bool(flags & _FLAG_OVERHEAT)
        self._e_stop_flag = bool(flags & _FLAG_E_STOP)
        self._watchdog_flag = bool(flags & _FLAG_WATCHDOG)
        self._unconfigured_id_flag = bool(flags & _FLAG_UNCONFIGURED_ID)
        return super().update_state(msg)

    def matches_feedback(self, msg: can.Message) -> bool:
        # 自分宛でないフレームの解釈失敗はここで握りつぶす。CANManager._receive_loop は
        # 例外を捕捉しないため、ここから例外を投げると想定外のフレーム 1 通で
        # そのバスの受信ループタスクごと死に、バス上の全モータが永久に STALE になる。
        # 判定できないフレームを「自分宛ではない」として無視する方が明らかに安全。
        if msg.is_extended_id:
            # 本プロトコルは Standard Frame のみ (仕様書 §1)。
            # 他プロトコルが同一バスに相乗りしても壊れないよう、ID 解析前に弾く
            return False

        parsed = self.try_parse_can_id(msg.arbitration_id)
        if parsed is None:
            return False

        cmd, dev = parsed
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
    @property
    def e_stop_active(self) -> bool:
        """ファーム側の緊急停止ラッチが有効か (FEEDBACK bit3)。"""
        return self._e_stop_flag

    @property
    def watchdog_active(self) -> bool:
        """コマンドウォッチドッグ作動中か (FEEDBACK bit4, 仕様書 §5.1)。"""
        return self._watchdog_flag

    @property
    def device_id_unconfigured(self) -> bool:
        """DIP スイッチのデバイス ID が未設定 (0x00) か (FEEDBACK bit5)。"""
        return self._unconfigured_id_flag

    def has_overcurrent_warning(self) -> bool:
        return self._overcurrent_flag

    def is_fault(self) -> bool:
        # 過熱は復帰不能リスクが高いので FAULT 扱い (シーケンス停止対象)。
        # デバイス ID 未設定は基板の設定ミスで駆動自体が拒否される状態であり、
        # 試合前に必ず気付く必要があるため同じく FAULT にする。
        # 緊急停止中 (bit3) とウォッチドッグ作動中 (bit4) は正常な安全動作なので含めない
        return self._overheat_flag or self._unconfigured_id_flag

    def check_safety_error(self) -> str | None:
        # 駆動が拒否される状態のまま動作確認しても必ず失敗し、しかも原因が
        # 「動かなかった」としか出ないため、先に理由を明示して打ち切る
        if self._unconfigured_id_flag:
            return "デバイス ID 未設定 (基板の DIP スイッチを確認)"
        if self._e_stop_flag:
            return "緊急停止中 (解除してから動作確認すること)"
        if self._watchdog_flag:
            return "コマンドウォッチドッグ作動中 (CAN 通信途絶を確認すること)"
        return None

    # ------------------------------------------------------------------ #
    #  励磁 (緊急停止ラッチの解除)
    # ------------------------------------------------------------------ #

    def activation_steps(self) -> list[tuple[can.Message, float]]:
        """緊急停止ラッチを解除する (仕様書 §3.5)。

        本機に励磁の概念はないが、緊急停止はファーム側でラッチされるため、
        解除フレームを送らない限り SET_TARGET を受け付けない。これを
        activation_steps に載せることで、起動時 (initialize_motors) と
        サーバの緊急停止解除 (e_stop_release → activate_motors) の両方から送られる。

        ブロードキャストではなく自分の device_id 宛に送るのは、共有バス上の
        他ロボットのモータまで巻き添えで解除しないため。

        解除後の目標値はファーム側が 0 から始める (§3.5) ので、実測角を知らなくても
        飛び出さない。よって requires_fresh_feedback_for_activation は既定の False。
        """
        return [(self.encode_e_stop_clear(self.can_id), 0.0)]

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
