from __future__ import annotations

import struct
from enum import IntEnum

import can

from lib.drivers.base import CheckContext, ControlMode, MotorDriver, MotorState

_MODE_MAP = {
    ControlMode.POSITION: 0,
    ControlMode.VELOCITY: 1,
    ControlMode.DUTY: 2,
}

# 動作確認の detail に出す単位。操縦者は config に書いた値の単位しか知らない
_DISPLAY_UNITS = {
    ControlMode.POSITION: "deg",
    ControlMode.VELOCITY: "rpm",
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
# 基板上のセンサ入力 (タッチセンサ等)。予約ビットの bit6 を割り当てる。
# センサは自分のデバイス ID で FEEDBACK を送るので、1 枚に何個載っていてもビットは 1 つ。
# サーボのフレームに相乗りさせていた頃は「センサだけの基板」が成立しなかった
_FLAG_SENSOR = 0x40

# デバイス ID の範囲 (仕様書 §2.2)。0x00 は「DIP 設定忘れ」、0xFF は E_STOP
# ブロードキャストの予約なので、どちらも個別デバイスの ID として使ってはならない。
_DEVICE_ID_MIN = 0x01
_DEVICE_ID_MAX = 0xFE

# E_STOP 解除フレームのマジックバイト (仕様書 §3.5)。
# 1 バイトの値だけで安全装置が解除されるのを避けるため、ファームは Byte1/Byte2 の
# 一致も要求する。バス上のビット化けや無関係なフレームで解除されてはならない。
_E_STOP_CLEAR_MAGIC = (0x5A, 0xA5)


class GenericDriver(MotorDriver):
    """自作モータドライバ(DC モータ/サーボ)用の汎用 CAN ドライバ。"""

    def __init__(
        self,
        name: str,
        can_id: int,
        *,
        control_type: ControlMode = ControlMode.POSITION,
    ) -> None:
        # 範囲外の can_id は静かに壊れる。特に 0xFF は activation_steps() が
        # 緊急停止**解除**フレームを 0x7FF (ブロードキャスト) へ送ることになり、
        # 共有 can_generic バス上の全基板のラッチをまとめて外す。
        # 0x100 以上はコマンド種別のビットを侵食し、SET_TARGET が FEEDBACK として
        # 読まれるフレームになる (何も駆動せず永久に STALE)。
        # 0x00 はファームが駆動を拒否する ID で、実行時に bit5 で分かるが遅い。
        if not _DEVICE_ID_MIN <= can_id <= _DEVICE_ID_MAX:
            raise ValueError(
                f"can_id は {_DEVICE_ID_MIN:#04x}〜{_DEVICE_ID_MAX:#04x} の範囲"
                f"(0x00=未設定 / 0xFF=E_STOP ブロードキャストの予約): {can_id}"
            )
        super().__init__(name, can_id)
        # フィードバック Byte7 の bit1/bit2 は MotorState に持たせず、ドライバ側で保持する
        # (MotorState は frozen dataclass で他ドライバ共通のため、汎用化を避けて専用属性に分離)
        self._overcurrent_flag: bool = False
        self._overheat_flag: bool = False
        self._e_stop_flag: bool = False
        self._watchdog_flag: bool = False
        self._unconfigured_id_flag: bool = False
        self._sensor_flag: bool = False
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
        self._sensor_flag = bool(flags & _FLAG_SENSOR)
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

    @property
    def sensor_active(self) -> bool:
        """このデバイスのセンサ入力が入っているか (FEEDBACK bit6, 仕様書 §5.2)。

        基板のセンサは 1 個ずつ独立した CAN デバイスとして FEEDBACK を送るので、
        「何番のセンサか」はこのドライバのモータ名と can_id が表す。

        原点合わせ用の入力で、**異常ではない**。is_fault() にも check_safety_error()
        にも入れない。ここに入れると、センサに触れているだけでヘルスが FAULT になり
        動作確認もシーケンスも止まる (原点合わせは「触れさせる」操作なので必ず起きる)。

        基板は状態を報告するだけで、判断は PC 側が持つ (仕様書 §5.2)。
        """
        return self._sensor_flag

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

    def check_command(self, *, magnitude: float = 0.1) -> tuple[can.Message, CheckContext]:
        # 位置指令は絶対値なので、実際に動くはずの量は「現在位置との差」になる。
        # reference を省くと、既に目標位置に居るモータが動かないまま合格する
        reference = self._observed_for(self.control_type)
        context = CheckContext(
            mode=self.control_type,
            target=float(magnitude),
            reference=0.0 if reference is None else reference,
            display_unit=_DISPLAY_UNITS.get(self.control_type, ""),
        )
        return self.encode_target(self.control_type, magnitude), context

    def evaluate_check_result(self, context: CheckContext) -> tuple[bool, str | None]:
        if context.mode is ControlMode.DUTY:
            # duty 指令を使うのは自作 DC モタドラだけで、その基板はエンコーダも
            # 電流センスも持たない (仕様書 §8)。FEEDBACK の velocity は常に 0 で、
            # 「回ったかどうか」を自動判定する手段が 1 つも存在しない。
            # 以前はここで velocity を見ており、実機では必ず「回転検出なし」で
            # 落ちるうえ、原因が配線不良にしか見えない失敗を出していた。
            return False, (
                "duty 指令はフィードバックが無く自動判定できない "
                f"({self.name} は motor_check.magnitude: 0 で除外し、"
                "config/checklist.yaml の指差喚呼で目視確認すること)"
            )

        passed, detail = self.evaluate_tracking(context)

        if context.mode is not ControlMode.POSITION:
            return (True, self._overflow_note()) if passed else (False, detail)

        # 位置決めの行き過ぎ・整定中はファームの到達フラグ (§3.2 bit0) が持つ
        if passed and self._state.reached:
            return True, self._overflow_note()
        if detail is None:
            detail = (
                f"目標 {context.display(context.target)}, "
                f"観測 {context.display(self._state.position)}"
            )
        return False, f"{detail} (reached={self._state.reached})"

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
