"""Damiao DM3520-1EC ドライバ (DM-S3519-1EC ギヤードモータ用) の CAN 2.0A 実装。

情報源は `DM-S3519-1EC User Manual`「Control Protocol Description」節。
**このドライバだけ、フィードバックが問い合わせ駆動である。** 自分の CAN ID 宛の
フレームを受けたときにしか状態を返さないので、PC 側が定期的に問い合わせないと
1 通も届かない (`lib/control/target_refresh.py` の ``Dm3520TargetRefresher``)。

M3508 と違い位置ループはドライバ内蔵 (Position Velocity Mode = 三重ループ) なので、
PC 側 PID は持たない。この点は EDULITE 05 と同じ扱いになる。

--- 専用バス (can_dm3520) を立てる理由 ----------------------------------------
本機は 11bit 標準 ID を**帯で**使う。ID の下位 8bit だけを見て自分宛かを判定し、
上位 3bit は「どの制御モードの指令か」を表す (マニュアル同節)。

    0x000 + ID : MIT モードの指令 / 特殊コマンド (enable / disable / set zero)
    0x100 + ID : 位置速度モードの指令
    0x200 + ID : 速度モードの指令
    0x7FF      : パラメータ読み書き (対象は D0/D1 の CAN ID で選ぶ)
    MST_ID     : フィードバックとパラメータ応答 (本機 → PC)

**既存 3 本のうち 2 本は物理的に相乗り不可**である。
  - can_generic は `E_STOP=0x000+ID` / `SET_TARGET=0x100+ID` / `SET_PARAM=0x200+ID`
    (docs/motor_driver_can_protocol.md §2) で、3 帯とも上と重なる。自作基板宛の
    目標値がそのまま本機への位置指令として解釈される
  - can_m3508 は C620 のフィードバックが `0x201`〜`0x204`。本機から見るとこれは
    `0x200 + ID` すなわち**速度指令**で、C620 の角度・回転数がそのまま float32 の
    速度目標として解釈される。しかも発生源はモータ自身なので、PC 側を止めても続く

can_edulite だけは相乗りできる (EDULITE 05 が 29bit 拡張 ID 専用で本機は 11bit 標準
ID 専用。ID 空間そのものが分かれる) が、**専用バスを立てる方を採った**。相乗りは
「本機のファームが拡張フレームを確実に無視する」ことに寄りかかっており、それは
マニュアルに書かれていない。既存の `can0/can1/can2` を捨てて serial 固定名にしたのと
同じ理由 —— バス 1 本 = ドライバ種別 1 つを崩さない —— でもある。
"""

from __future__ import annotations

import math
import struct
from enum import IntEnum
from typing import ClassVar

import can

from lib.drivers.base import CheckContext, ControlMode, MotorDriver, MotorState


class Dm3520CtrlMode(IntEnum):
    """CTRL_MODE レジスタ (0x0A) に書く制御モード番号。"""

    MIT = 1
    POSITION_VELOCITY = 2
    VELOCITY = 3


class Dm3520Error(IntEnum):
    """FEEDBACK D0 の上位 4bit。0 と 1 は正常状態、5 以上が異常。"""

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


# 異常はここより上の符号。0 (無励磁) と 1 (励磁) だけが正常なので境界は 1 本で足りる
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
    """Damiao DM3520-1EC ドライバ。標準 ID・1Mbps。"""

    # ---- フレーム ID ----
    MIT_CMD_BASE = 0x000
    POSITION_CMD_BASE = 0x100
    VELOCITY_CMD_BASE = 0x200
    CONFIG_FRAME_ID = 0x7FF

    # ---- 0x7FF の D2 (操作種別) ----
    CONFIG_READ = 0x33
    CONFIG_WRITE = 0x55
    CONFIG_SAVE = 0xAA

    # ---- 特殊コマンド (D0-D6 = 0xFF, D7 = 種別) ----
    SPECIAL_ENABLE = 0xFC
    SPECIAL_DISABLE = 0xFD
    SPECIAL_SET_ZERO = 0xFE

    # ---- レジスタアドレス (マニュアル「Register Address」節) ----
    REG_MST_ID = 0x07
    REG_ESC_ID = 0x08
    REG_TIMEOUT = 0x09
    REG_CTRL_MODE = 0x0A
    REG_P_MAX = 0x15
    REG_V_MAX = 0x16
    REG_T_MAX = 0x17

    # フィードバックの固定小数点ビット幅 (マニュアル「Feedback Frame」節)
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
            # **フィードバックには CAN ID の下位 4bit しか載らない** (マニュアル
            # 「Feedback Frame」節の D0)。MST_ID を共有する 2 台を見分ける手掛かりは
            # それだけなので、下位 4bit が重なる ID (0x01 と 0x11 など) を許すと
            # 「2 台目のフィードバックが 1 台目の状態を上書きする」構成が config に
            # 書けてしまう。0x01..0x0F に閉じれば「ID が違う = 下位 4bit も違う」が
            # 構造的に成立する。
            #
            # 副次的に、パラメータ応答との取り違え余地も無励磁時だけに閉じ込められる。
            # 応答は D0 = ESC_ID、フィードバックは D0 = エラー<<4 | ID下位4bit なので、
            # 両者が一致するには エラー == ESC_ID>>4 が要る。ESC_ID <= 0x0F なら
            # これは「エラー == 0 = 無励磁」に限られ、励磁して運転している間は起こらない
            # (0x10..0x1F を許すと「エラー == 1 = 励磁中」、つまり通常運転中に成立する)。
            #
            # 実機の出荷値がこの範囲外なら、レジスタ 0x08 を書き換えて 0xAA で保存する。
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

    # ------------------------------------------------------------------ #
    #  固定小数点 <-> 実数
    # ------------------------------------------------------------------ #
    # マニュアル「Feedback Frame」節: 位置・速度・トルクは [-max, +max] を
    # 符号なし整数へ線形写像して運ぶ。CAN 上に float は 1 バイトも流れない。

    @staticmethod
    def _clamp(value: float, min_val: float, max_val: float) -> float:
        return min(max(float(value), min_val), max_val)

    @staticmethod
    def uint_to_float(raw: int, max_abs: float, bits: int) -> float:
        span = float((1 << bits) - 1)
        return raw * (2.0 * max_abs) / span - max_abs

    # ------------------------------------------------------------------ #
    #  送信フレームの組み立て
    # ------------------------------------------------------------------ #

    def _standard(self, arbitration_id: int, data: bytes) -> can.Message:
        return can.Message(arbitration_id=arbitration_id, data=data, is_extended_id=False)

    def _special_command(self, command: int) -> can.Message:
        """enable / disable / set zero。宛先は ESC_ID そのもの。

        制御モードに依らず同じ ID で受け付ける (マニュアルの調整アシスタントが
        送っているのと同じ形)。0xFF が 7 バイト並ぶことでモード指令と区別される。
        """
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
            # 位置速度モード: p_des [rad] と v_des [rad/s] の float32 2 つ。
            # v_des は「移動中の速度上限」であって目標速度ではない (マニュアル同節)
            position = self._clamp(value, -self.p_max, self.p_max)
            data = struct.pack("<ff", position, self.limit_speed)
            return self._standard(self.POSITION_CMD_BASE + self.can_id, data)

        if mode is ControlMode.VELOCITY:
            speed = self._clamp(value, -self.limit_speed, self.limit_speed)
            return self._standard(self.VELOCITY_CMD_BASE + self.can_id, struct.pack("<f", speed))

        raise ValueError(f"Dm3520Driver は {mode} モードをサポートしていません")

    # ------------------------------------------------------------------ #
    #  受信
    # ------------------------------------------------------------------ #

    def _is_config_response(self, msg: can.Message) -> bool:
        """0x7FF へのパラメータ読み書きの応答か。

        **応答もフィードバックと同じ MST_ID で返ってくる** (マニュアル
        「CAN Configuration Commands」節)。起動時の CTRL_MODE 書き込みは必ず
        1 通の応答を生むので、これを状態フィードバックとして解釈すると
        ``activation_steps`` が保持目標に使う実測角がその瞬間だけ嘘になり、
        励磁した瞬間にモータが飛ぶ。

        判別は D0/D1 = 対象 CAN ID (リトルエンディアン) かつ D2 = 操作種別。
        状態フィードバックがこの形に一致するには、無励磁 (D0 上位 4bit = 0) で
        かつ位置の生値が 0x0033 / 0x0055 相当 —— すなわち可動範囲の最も負の端 ——
        に居る必要がある。電源投入位置が 0.0rad である本機では起こらない。
        """
        data = msg.data
        return (
            data[0] == (self.can_id & 0xFF)
            and data[1] == ((self.can_id >> 8) & 0xFF)
            and data[2] in (self.CONFIG_READ, self.CONFIG_WRITE)
        )

    def matches_feedback(self, msg: can.Message) -> bool:
        if msg.is_extended_id or len(msg.data) != 8:
            # 本機は標準フレームしか送らない。拡張 ID をここで落としておくと、
            # 将来バスを相乗りさせた場合でも ID 空間が交わらない
            return False
        if msg.arbitration_id != self.master_id:
            return False
        if self._is_config_response(msg):
            return False
        # D0 の下位 4bit は送り主の CAN ID の下位 4bit (マニュアル「Feedback Frame」節)。
        # MST_ID を共有する複数台を見分ける唯一の手掛かりなので必ず突き合わせる
        return (msg.data[0] & 0x0F) == (self.can_id & 0x0F)

    def decode_feedback(self, msg: can.Message) -> MotorState:
        if not self.matches_feedback(msg):
            raise ValueError("対象モータの DM3520 フィードバックではありません")

        data = msg.data
        self.error_code = (data[0] >> 4) & 0x0F

        pos_raw = (data[1] << 8) | data[2]
        vel_raw = (data[3] << 4) | (data[4] >> 4)
        torque_raw = ((data[4] & 0x0F) << 8) | data[5]
        t_mos = float(data[6])
        t_rotor = float(data[7])

        return MotorState(
            position=self.uint_to_float(pos_raw, self.p_max, self._POS_BITS),
            velocity=self.uint_to_float(vel_raw, self.v_max, self._VEL_BITS),
            # EDULITE 05 と同じく、トルク [Nm] を current フィールドに載せる。
            # 本機も相電流を報告しないので、電流として使える値はどこにも無い
            current=self.uint_to_float(torque_raw, self.t_max, self._TORQUE_BITS),
            # ドライバ MOS とコイルの 2 点。**高い方**を採るのは、どちらが先に
            # 保護温度へ達しても操縦者に見えるようにするため
            temperature=max(t_mos, t_rotor),
        )

    # ------------------------------------------------------------------ #
    #  起動・励磁
    # ------------------------------------------------------------------ #

    def initialization_steps(self) -> list[tuple[can.Message, float]]:
        """無励磁化 → 制御モード設定 (→ 原点確定)。

        CTRL_MODE はフラッシュへ保存されず電源断で失われる (マニュアル「Mode
        Switching」節) ため、**起動のたびに書く**。モード切替の副作用として
        ドライバ内部の指令値 (位置・速度・トルク) はクリアされるので、この後の
        ``activation_steps`` が実測角を書き直す順序でなければならない。
        """
        steps = [
            (self.encode_disable(), 0.05),
            (self.encode_ctrl_mode(self._CONTROL_TO_CTRL_MODE[self.mode]), 0.05),
        ]
        if self.set_zero_on_start:
            steps.append((self.encode_set_zero(), 0.2))
        return steps

    def activation_steps(self, *, after_set_zero: bool = False) -> list[tuple[can.Message, float]]:
        """現在角を目標に書いてから励磁する (EDULITE 05 と同じ理由)。

        位置速度モードで目標を書かずに enable すると、モード切替でクリアされた
        p_des = 0.0rad へ向かって機構が全速で戻る。ラックアンドピニオンでは
        そのままストローク端まで走るので、実測角を確認できないうちは励磁しない。

        ``after_set_zero`` は直前に SET_ZERO を送った経路向け。原点が付け替わった
        後のフィードバックはまだ届いておらず、手元の実測角は旧原点基準のままなので、
        新原点そのものである 0 を書く。
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
        # MotorState の初期値 0.0rad を実測角と取り違えると、機構は「原点へ戻る」
        # 動作として飛ぶ。無励磁のまま残すほうが必ず安全
        return self.mode is ControlMode.POSITION

    def feedback_probe_message(self) -> can.Message | None:
        """無励磁を保ったまま状態を返させられる唯一のフレーム。

        本機のフィードバックは問い合わせ駆動で、自分宛のフレームを受けたときに
        しか返らない。disable は無励磁を無励磁のままにするだけなので、励磁前の
        問い合わせに使っても機構は動かない。
        """
        return self.encode_disable()

    def idle_target_value(self) -> float:
        """目標を持たない間に書き続ける指令値。

        ``Dm3520TargetRefresher`` がこれをラッチして毎周期送り直すことで、操縦者が
        何も操作していない間もフィードバックが届き続ける (本機のフィードバックは
        問い合わせ駆動で、送らなければ 1 通も来ない)。**指令として無害である**
        ことが要点 —— 位置モードなら「今居る場所を保て」、速度モードなら「止まれ」
        で、どちらも新しい動きを作らない。
        """
        return self._state.position if self.mode is ControlMode.POSITION else 0.0

    def emergency_stop_message(self) -> can.Message:
        return self.encode_disable()

    # ------------------------------------------------------------------ #
    #  ヘルス
    # ------------------------------------------------------------------ #

    def is_fault(self) -> bool:
        return self.error_code >= _FIRST_ERROR_CODE

    def is_energized(self) -> bool:
        """FEEDBACK D0 の上位 4bit が `ENABLED` のときだけ励磁されている。

        本機は指令フレームを無励磁のまま受理して黙って捨てる。ドライバの TIMEOUT
        (通信途絶保護) や電源の瞬断で励磁が外れると、PC は 20Hz で位置指令を送り続け、
        フィードバックも正常に届き続けるのに機構だけが 1mm も動かない。
        `is_fault()` は 0x5 以上しか見ないのでここには掛からず、モータのヘルスは
        OK のまま —— **画面のどこにも現れない**。それを見えるようにするための判定。
        """
        return self.error_code == Dm3520Error.ENABLED

    def has_overcurrent_warning(self) -> bool:
        return self.error_code == Dm3520Error.OVERCURRENT

    def error_label(self) -> str | None:
        return _ERROR_LABELS.get(self.error_code)

    # ------------------------------------------------------------------ #
    #  到達判定・動作確認
    # ------------------------------------------------------------------ #

    _RPM_TO_RAD_PER_S = 2.0 * math.pi / 60.0

    def default_tolerance(self, mode: ControlMode) -> float:
        # 共通既定値 (deg / rpm) を本機のフィードバック単位 (rad / rad/s) へ
        # 換算するだけに留める。独自の数値を書くと共通既定値を直しても本機だけ
        # 古い値のまま残る (EDULITE 05 と同じ方針)
        if mode is ControlMode.POSITION:
            return math.radians(super().default_tolerance(mode))
        if mode is ControlMode.VELOCITY:
            return super().default_tolerance(mode) * self._RPM_TO_RAD_PER_S
        return super().default_tolerance(mode)

    def prepare_check_steps(self) -> list[tuple[can.Message, float]]:
        """設定 → 保持目標 → 励磁。起動経路をそのまま合成する。

        励磁手順を書き写すと、片方だけ直したときに「保持目標を書かずに enable して
        機構が原点へ飛ぶ」が動作確認経路だけで再発する。
        """
        return self.initialization_steps() + self.activation_steps(
            after_set_zero=self.set_zero_on_start
        )

    def requires_fresh_feedback_for_check(self) -> bool:
        return True

    def check_safety_error(self) -> str | None:
        if self.is_fault():
            label = self.error_label() or f"0x{self.error_code:X}"
            return f"DM3520 異常: {label}"
        if self._state.temperature >= 60.0:
            return f"DM3520 過温 {self._state.temperature:.1f}C"
        return None

    def check_command(self, *, magnitude: float = 5.0) -> tuple[can.Message, CheckContext]:
        """運用と同じ制御モードのまま微小量を指令する。

        **この確認は p_max の誤りも同時に検出する。** p_max はフィードバックの
        固定小数点レンジで、実機のレジスタ (0x15) と config がずれていると位置が
        比例倍で読めるため、指令した量だけ動いても追従判定を通らない。
        """
        if self.mode is ControlMode.VELOCITY:
            # magnitude は共通単位の rpm。encode_target と同じクランプを context にも
            # 効かせないと、limit_speed で頭打ちになった瞬間に必ず不合格になる
            target = self._clamp(
                magnitude * self._RPM_TO_RAD_PER_S, -self.limit_speed, self.limit_speed
            )
            context = CheckContext(
                mode=ControlMode.VELOCITY,
                target=target,
                reference=self._state.velocity,
                display_scale=1.0 / self._RPM_TO_RAD_PER_S,
                display_unit="rpm",
            )
            return self.encode_target(ControlMode.VELOCITY, target), context

        target = self._clamp(
            self._state.position + math.radians(magnitude), -self.p_max, self.p_max
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
        # prepare_check_steps が CTRL_MODE を設定値のまま使うので、戻す作業は無く
        # 無励磁化だけで原状に復帰する
        return self.encode_disable()
