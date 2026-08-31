from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

import can

from lib.drivers.base import ControlMode, MotorDriver, MotorState

_MODE_MAP = {
    ControlMode.POSITION: 0,
    ControlMode.VELOCITY: 1,
    ControlMode.DUTY: 2,
    ControlMode.ON_OFF: 3,
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


@dataclass(frozen=True)
class InfoFrame:
    """INFO フレームの中身 (仕様書 §3.4)。基板が 1Hz で自己申告する。

    ``angle_range_deg`` が None なのは **可動レンジを申告しなかった** ことを意味し、
    「レンジ 0deg」とは別物である。混ぜてはならない —— DC 基板と電磁弁基板は
    そもそも角度を持たないので送らないのが正しく、サーボ基板が送ってこないのは
    可動レンジ以前のバージョンが焼かれている証拠になる。0 で埋めると、この 2 つが
    「測ったように見える 0」として同じ顔で届く。
    """

    firmware_version: int
    board_kind: int
    slot_kind: int
    angle_range_deg: float | None


#: 可動レンジの一致とみなす差 [deg]。CAN 上の刻みは 0.1deg (仕様書 §4) なので、
#: それ未満の差は往復の丸めでしか生まれない
_ANGLE_RANGE_EPSILON = 0.05


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
        expected_firmware: int | None = None,
        expected_angle_range_deg: float | None = None,
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
        # INFO (1Hz の自己申告, 仕様書 §3.4)。未受信は None のままで、
        # **未受信を不一致に倒さない** (起動直後は必ず未受信になる)
        self._info: InfoFrame | None = None
        # config に書かれた期待値。**書かなければ照合そのものをしない。**
        # サーボの可動レンジは実物を測る手段が無く、照合できるのは
        # 「ファームに書いた値」と「yaml に書いた値」の一致まで (仕様書 §7.7)
        self._expected_firmware = expected_firmware
        self._expected_angle_range_deg = expected_angle_range_deg

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
        # 自分宛でないフレームの解釈失敗はここで握りつぶす。受信ループ
        # (CANManager._dispatch_frame) は宛先判定とデコードをモータ 1 台単位で
        # 囲うのでバス全体は死なないが、ここから例外を投げるとそのモータ宛の
        # フレームが 1 通落ち、鮮度も進まない (握った件数は rx_error_count に積まれる)。
        # 判定できないフレームは「自分宛ではない」として無視する方が明らかに安全 ——
        # can_generic は 2 台のロボットで物理共有しており、解釈できないフレームが
        # 流れるのは構成上の正常である。
        if msg.is_extended_id:
            # 本プロトコルは Standard Frame のみ (仕様書 §1)。
            # 他プロトコルが同一バスに相乗りしても壊れないよう、ID 解析前に弾く
            return False

        parsed = self.try_parse_can_id(msg.arbitration_id)
        if parsed is None:
            return False

        cmd, dev = parsed
        return cmd == CommandType.FEEDBACK and dev == self.can_id

    def decode_info(self, msg: can.Message) -> InfoFrame:
        """INFO フレーム (仕様書 §3.4)。Byte0=版 / Byte1=基板種別 / Byte2=スロット役割。

        **DLC は可変。** サーボスロットだけが Byte3-4 に可動レンジ [0.1deg] を足す。
        角度を持たない基板 (DC・電磁弁・センサ) は 3 バイトで送るので None になる。
        """
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
        # 判定の作法は matches_feedback と同じ。ここから例外を投げると受信ループごと
        # 死ぬので、解釈できないフレームは「自分宛ではない」として無視する
        if msg.is_extended_id:
            return False

        parsed = self.try_parse_can_id(msg.arbitration_id)
        if parsed is None:
            return False

        cmd, dev = parsed
        return cmd == CommandType.INFO and dev == self.can_id

    @property
    def info(self) -> InfoFrame | None:
        """最後に受け取った自己申告 (仕様書 §3.4)。1 通も来ていなければ None。"""
        return self._info

    @property
    def info_mismatch(self) -> str | None:
        """自己申告が config の期待値と食い違っていれば、その理由を返す。

        **INFO を 1 通も受けていない間は照合しない。** 起動直後は必ず未受信で、
        1Hz なので数秒で埋まる。未受信を不一致に倒すと起動のたびに全サーボが FAULT に
        なり、「いつもの赤」として無視されるようになる。
        """
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
            # 可動レンジを申告しない = それ以前のファーム。**「不明」を一致へ倒すと
            # 焼き忘れの検出そのものが効かなくなる** (仕様書 §3.4)
            return (
                f"サーボ可動レンジが申告されていない (期待 {expected_range:g}deg)。"
                "可動レンジ以前のファームが焼かれている"
            )

        if abs(info.angle_range_deg - expected_range) > _ANGLE_RANGE_EPSILON:
            # **これが 180/270 の取り違えを CAN 越しに見える形にしている唯一の経路。**
            # 実機は指令の 1.5 倍 (または 2/3) 動くが、FEEDBACK が返すのはクランプ後の
            # 指令角なので、この照合が無ければ PC からは正常にしか見えない (仕様書 §7.7)
            return (
                f"サーボ可動レンジが不一致 (期待 {expected_range:g}deg / "
                f"申告 {info.angle_range_deg:g}deg)。180/270 の取り違えの可能性"
            )
        return None

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

        原点合わせ用の入力で、**異常ではない**。is_fault() に入れてはならない。
        ここに入れると、センサに触れているだけでヘルスが FAULT になり動作確認も
        シーケンスも止まる (原点合わせは「触れさせる」操作なので必ず起きる)。

        基板は状態を報告するだけで、判断は PC 側が持つ (仕様書 §5.2)。
        """
        return self._sensor_flag

    def is_fault(self) -> bool:
        # デバイス ID 未設定は基板の設定ミスで駆動自体が拒否される状態であり、
        # 試合前に必ず気付く必要があるため FAULT にする。
        # 緊急停止中とウォッチドッグ作動中は正常な安全動作なので含めない。
        # 過電流・過熱はどちらの基板も検出手段を持たない (仕様書 §3.2)
        #
        # 自己申告の不一致 (焼き忘れ・サーボの型違い) も同じ扱いにする。どちらも
        # **機体は指令どおり動いたようにしか見えない**設定ミスで、ここで FAULT に
        # しないと試合まで誰も気付けない (仕様書 §3.4 / §7.7)
        return self._unconfigured_id_flag or self.info_mismatch is not None

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
