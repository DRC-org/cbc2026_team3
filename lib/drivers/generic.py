from __future__ import annotations

import struct
from enum import IntEnum

import can

from lib.drivers.base import CheckContext, ControlMode, MotorDriver, MotorState

_MODE_MAP = {
    ControlMode.POSITION: 0,
    ControlMode.VELOCITY: 1,
    ControlMode.DUTY: 2,
    ControlMode.ON_OFF: 3,
}

# 動作確認の detail に出す単位。操縦者は config に書いた値の単位しか知らない
_DISPLAY_UNITS = {
    ControlMode.POSITION: "deg",
    ControlMode.VELOCITY: "rpm",
}


class CommandType(IntEnum):
    """CAN ID 上位 3bit (仕様書 §2.1)。

    **値は CAN の調停順 (小さいほど優先) に合わせてある。** 止めるためのフレームが
    目標値やフィードバックに追い越されてはならない。0b101-0b111 は予約。
    """

    E_STOP = 0
    SET_TARGET = 1
    SET_PARAM = 2
    FEEDBACK = 3
    INFO = 4


# FEEDBACK Byte0 の状態フラグ (仕様書 §3.2)。
# **頭から詰める。** 空きを挟むと、報告できる項目が増えたときに「途中に空いている
# ビットがあるのに末尾へ足す」ことになり、対応表が読みにくくなる
_FLAG_REACHED = 0x01
_FLAG_E_STOP = 0x02
_FLAG_WATCHDOG = 0x04
_FLAG_UNCONFIGURED_ID = 0x08
# 基板上のセンサ入力 (タッチセンサ等)。
# センサは自分のデバイス ID で FEEDBACK を送るので、1 枚に何個載っていてもビットは 1 つ
_FLAG_SENSOR = 0x10
# **電源投入後まだ SET_TARGET を 1 通も受けていない** (仕様書 §3.2 / §5.4)。
# これが無いと基板の再起動が PC から見えない。サーボ基板は起動時に config.h の
# 初期角へ駆動するので、試合中の瞬断は「機構が勝手に飛ぶ」形で現れるのに、
# ウォッチドッグのビットは「一度でも受けた後の満了」でしか立たない
_FLAG_NEVER_COMMANDED = 0x20
# bit6-7 は予約

# デバイス ID の範囲 (仕様書 §2.2)。0x00 は「DIP 設定忘れ」、0xFF は E_STOP
# ブロードキャストの予約なので、どちらも個別デバイスの ID として使ってはならない。
_DEVICE_ID_MIN = 0x01
_DEVICE_ID_MAX = 0xFE

# 固定小数点の単位 (仕様書 §4)。**CAN 上を流れる数値はすべて int16 で、float は
# 1 バイトも流れない。** float32 をやめたのは NaN の防御をプロトコル全体から消すため。
# NaN は比較がすべて false になるのでクランプも範囲チェックも素通りし、一度内部へ
# 入ると「無言で止まったモータ」になる。整数ならその失敗クラスごと存在しない
_ANGLE_SCALE = 10  # 0.1deg
_DUTY_SCALE = 10000  # duty -1.0 .. +1.0
_RATE_SCALE = 10  # 0.1deg/s
_PLAIN_SCALE = 1  # ms など

#: 制御タイプごとの目標値の単位 (仕様書 §4)
_TARGET_SCALE = {
    ControlMode.POSITION: _ANGLE_SCALE,
    ControlMode.VELOCITY: _ANGLE_SCALE,
    ControlMode.DUTY: _DUTY_SCALE,
    # on_off は 0 と非 0 の区別しか使わないのでスケールは掛けない。
    # 掛けると 1 が 10000 になり、この基板では症状が出ないままファームと単位が
    # 食い違う (「0 でなければ ON」なのでどちらでも動いてしまう)
    ControlMode.ON_OFF: _PLAIN_SCALE,
}


def _to_raw(value: float, scale: int) -> int:
    """float を固定小数点の int16 へ。範囲外と NaN は飽和させる。

    PC 側は yaml から読んだ float を扱うので、ここが唯一の変換点になる。
    黙って折り返すと +4000deg が負値に化け、基板が逆方向へ動く。
    """
    if value != value:  # NaN
        return 0
    scaled = round(value * scale)
    return max(-32768, min(32767, scaled))


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
        # 0x00 はファームが駆動を拒否する ID で、実行時に FEEDBACK で分かるが遅い。
        if not _DEVICE_ID_MIN <= can_id <= _DEVICE_ID_MAX:
            raise ValueError(
                f"can_id は {_DEVICE_ID_MIN:#04x}〜{_DEVICE_ID_MAX:#04x} の範囲"
                f"(0x00=未設定 / 0xFF=E_STOP ブロードキャストの予約): {can_id}"
            )
        super().__init__(name, can_id)
        # フィードバック Byte7 の到達以外は MotorState に持たせず、ドライバ側で保持する
        # (MotorState は frozen dataclass で他ドライバ共通のため、汎用化を避けて専用属性に分離)
        self._e_stop_flag: bool = False
        self._watchdog_flag: bool = False
        self._unconfigured_id_flag: bool = False
        self._sensor_flag: bool = False
        self._never_commanded_flag: bool = False
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
        """SET_TARGET フレーム (仕様書 §3.1)。Byte0=制御タイプ / Byte1-2=目標値。

        途中に予約バイトを挟まないので DLC=3 で足りる。
        """
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
        """ブロードキャスト緊急停止 (仕様書 §3.5)。

        CAN ID 0x0FF は **他のどのフレームより優先度が高い**。かつては 0x7FF で、
        Standard ID 全 2048 個のうち最も優先度が低かった。
        """
        return can.Message(
            arbitration_id=GenericDriver.build_can_id(CommandType.E_STOP, 0xFF),
            data=bytes(3),
            is_extended_id=False,
        )

    @staticmethod
    def encode_e_stop_clear(device_id: int = 0xFF) -> can.Message:
        """緊急停止ラッチの解除フレーム (仕様書 §3.5)。

        ファーム側は緊急停止をラッチし、解除フレームを受け取るまで SET_TARGET で
        駆動しない。解除しない限り復旧できないので、起動時と緊急停止解除時に送る。
        """
        data = bytearray(3)
        data[0] = 0x01
        data[1], data[2] = _E_STOP_CLEAR_MAGIC
        return can.Message(
            arbitration_id=GenericDriver.build_can_id(CommandType.E_STOP, device_id),
            data=bytes(data),
            is_extended_id=False,
        )

    # ---- 受信フレーム解析 ----

    def decode_feedback(self, msg: can.Message) -> MotorState:
        """FEEDBACK フレーム (仕様書 §3.2)。Byte0=状態フラグ / Byte1-2=位置。

        **DLC は可変。** 位置を持たない基板 (DC・センサ) は状態フラグ 1 バイトだけを
        送る。常に 0 の位置・速度を詰めても、PC には「測ったように見える 0」が届くだけ。
        速度は誰も使っていなかったのでプロトコルから外した。
        """
        d = msg.data
        flags = d[0]
        position = struct.unpack_from("<h", d, 1)[0] / _ANGLE_SCALE if len(d) >= 3 else 0.0
        return MotorState(position=position, reached=bool(flags & _FLAG_REACHED))

    def update_state(self, msg: can.Message) -> MotorState:
        # decode_feedback は純粋関数のまま保ち、副作用 (フラグ保持) はここで処理する
        # 到達は MotorState.reached に反映、それ以外はドライバ属性に保持
        flags = msg.data[0]
        self._e_stop_flag = bool(flags & _FLAG_E_STOP)
        self._watchdog_flag = bool(flags & _FLAG_WATCHDOG)
        self._unconfigured_id_flag = bool(flags & _FLAG_UNCONFIGURED_ID)
        self._sensor_flag = bool(flags & _FLAG_SENSOR)
        self._never_commanded_flag = bool(flags & _FLAG_NEVER_COMMANDED)
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
        # 自作モタドラはフィードバックに到達フラグを持つ。
        # 位置決めは行き過ぎ・オーバーシュートを含むため、ファームの到達判定を優先する
        if mode is ControlMode.POSITION and not self._state.reached:
            return False
        return super().is_target_reached(target, mode, tolerance=tolerance)

    # ------------------------------------------------------------------ #
    #  ヘルスチェック判定
    # ------------------------------------------------------------------ #
    @property
    def e_stop_active(self) -> bool:
        """ファーム側の緊急停止ラッチが有効か (FEEDBACK の緊急停止ビット)。"""
        return self._e_stop_flag

    @property
    def watchdog_active(self) -> bool:
        """コマンドウォッチドッグ作動中か (FEEDBACK のウォッチドッグビット, 仕様書 §5.1)。"""
        return self._watchdog_flag

    @property
    def device_id_unconfigured(self) -> bool:
        """DIP スイッチのデバイス ID が未設定 (0x00) か (FEEDBACK のデバイス ID 未設定ビット)。"""
        return self._unconfigured_id_flag

    @property
    def never_commanded(self) -> bool:
        """電源投入後まだ指令を受けていないか (仕様書 §3.2)。

        **基板の再起動を検出するためにある。** サーボ基板は起動時に config.h の
        初期角へ駆動するので、試合中の瞬断は「機構が勝手に飛ぶ」形で現れる。
        PC は 20Hz で再送しているので 50ms 以内に何事もなかったように復帰し、
        これが無いと再起動そのものがどこにも現れない。
        """
        return self._never_commanded_flag

    @property
    def sensor_active(self) -> bool:
        """このデバイスのセンサ入力が入っているか (FEEDBACK のセンサビット, 仕様書 §5.2)。

        基板のセンサは 1 個ずつ独立した CAN デバイスとして FEEDBACK を送るので、
        「何番のセンサか」はこのドライバのモータ名と can_id が表す。

        原点合わせ用の入力で、**異常ではない**。is_fault() にも check_safety_error()
        にも入れない。ここに入れると、センサに触れているだけでヘルスが FAULT になり
        動作確認もシーケンスも止まる (原点合わせは「触れさせる」操作なので必ず起きる)。

        基板は状態を報告するだけで、判断は PC 側が持つ (仕様書 §5.2)。
        """
        return self._sensor_flag

    def is_fault(self) -> bool:
        # デバイス ID 未設定は基板の設定ミスで駆動自体が拒否される状態であり、
        # 試合前に必ず気付く必要があるため FAULT にする。
        # 緊急停止中とウォッチドッグ作動中は正常な安全動作なので含めない。
        # 過電流・過熱はどちらの基板も検出手段を持たない (仕様書 §3.2)
        return self._unconfigured_id_flag

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
        if context.mode is not ControlMode.POSITION:
            # **自作モタドラが返すのは位置だけ** (仕様書 §3.2)。速度も電流も
            # プロトコルから外れているので、position 以外は「動いたかどうか」を
            # 自動判定する手段が 1 つも存在しない。以前は velocity を見ており、
            # 実機では必ず「回転検出なし」で落ちるうえ、原因が配線不良にしか
            # 見えない失敗を出していた。
            return False, (
                f"{context.mode.value} 指令はフィードバックが無く自動判定できない "
                f"({self.name} は motor_check.magnitude: 0 で除外し、"
                "config/checklist.yaml の指差喚呼で目視確認すること)"
            )

        passed, detail = self.evaluate_tracking(context)

        # 位置決めの行き過ぎ・整定中はファームの到達フラグ (§3.2) が持つ
        if passed and self._state.reached:
            return True, None
        if detail is None:
            detail = (
                f"目標 {context.display(context.target)}, "
                f"観測 {context.display(self._state.position)}"
            )
        return False, f"{detail} (reached={self._state.reached})"

    def reset_after_check(self) -> can.Message:
        return self.encode_target(self.control_type, 0.0)
