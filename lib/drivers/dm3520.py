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
        # v_max は復号レンジなので単位違い。実機では発火せず、正しい上限は実測待ちのため据え置く。
        self.limit_speed = min(float(limit_speed), self.v_max)
        self.set_zero_on_start = bool(set_zero_on_start)

        self.error_code = int(Dm3520Error.DISABLED)
        self._feedback_received = False
        # 載っていないレジスタは「まだ読めていない」であって「一致した」ではない。
        self._reported_ranges: dict[int, float] = {}

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
        """今の位置を原点として書き込む (特殊コマンド)。

        **必ず無励磁にしてから送る。** 順序は
        `CANManager.capture_origin_via_set_zero` が保証する。
        """
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

    def _expected_ranges(self) -> tuple[tuple[int, str, float], ...]:
        """励磁前に実機と突き合わせるレンジ (レジスタ, 名前, config の値)。

        重さで区別せず 3 つとも載せる —— 現実の壊れ方 (電源断で出荷値へ戻る) では
        同時にずれるので、レジスタごとの効き方の対応表を誰も覚え続けられない。
        """
        return (
            (self.REG_P_MAX, "p_max", self.p_max),
            (self.REG_V_MAX, "v_max", self.v_max),
            (self.REG_T_MAX, "t_max", self.t_max),
        )

    def _record_config_response(self, msg: can.Message) -> None:
        """設定応答から実機の固定小数点レンジを控える (実機を知る唯一の口)。"""
        data = msg.data
        if data[2] != self.CONFIG_READ:
            return
        register = data[3]
        if register not in {reg for reg, _, _ in self._expected_ranges()}:
            return
        self._reported_ranges[register] = struct.unpack("<f", bytes(data[4:8]))[0]

    def matches_feedback(self, msg: can.Message) -> bool:
        if msg.is_extended_id or len(msg.data) != 8:
            return False
        if msg.arbitration_id != self.master_id:
            return False
        if self._is_config_response(msg):
            self._record_config_response(msg)
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
        """無励磁化 → 制御モード設定 (→ 原点確定)。

        !!! **ここで p_max を書いてはならない。一度入れて実機を壊しかけた** !!!
        書き終わるまでの窓で復号レンジが食い違い、12.5 のフィードバックを 1000 で
        復号した 80 倍の位置が ``activation_steps`` の保持目標に入る (2026-09-09 に
        機構がリミットスイッチを踏み越えた)。正しい向きは「書いて直す」ではなく
        「読み返して食い違いを検出し、励磁を拒む」で、再試行は
        `configuration_probe_messages()` が持つ。
        """
        steps = list(self.reinitialization_steps())
        if self.set_zero_on_start:
            # 原点は励磁が落ちても生き残るので、再励磁で送ると合わせた原点を壊すだけ
            steps.append((self.encode_set_zero(), 0.2))
        return steps

    def reinitialization_steps(self) -> list[tuple[can.Message, float]]:
        """無励磁化 → 制御モード設定。**電源断で失われるぶんだけ。**

        物理非常停止は本機の電源を数秒落とし、CTRL_MODE はフラッシュに残らないので
        復帰した個体は MIT モードで立つ —— 書き直さないと `0x100` が解釈されず
        「励磁を名乗るのにトルクが出ない」になる (`is_fault()` は掛からない)。
        控えてあるレンジをここで捨てるのは、同じ電源断で戻った p_max を起動時の値で
        「一致している」と答えてしまうため。何が揮発するかを知るのはここだけである。
        """
        self._reported_ranges.clear()
        return [
            (self.encode_disable(), 0.05),
            (self.encode_ctrl_mode(self._CONTROL_TO_CTRL_MODE[self.mode]), 0.05),
        ]

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

    def deactivation_steps(self) -> list[tuple[can.Message, float]]:
        """原点を切り直す前に無励磁へ落とす。

        待ちは `initialization_steps()` の先頭の `disable` と同じ 0.05 秒。
        """
        return [(self.encode_disable(), 0.05)]

    def origin_capture_steps(self) -> list[tuple[can.Message, float]]:
        """今の位置を原点として書き込む (特殊コマンド 0xFE)。

        待ちが `disable` より長いのは、`initialization_steps()` の
        `set_zero_on_start` と揃えているため。

        TODO(未修正): **ここで確定した原点は、物理緊急停止で本機の電源が数秒
        落ちるたびに黙って無効になる** —— 本機は電源投入時に位置が 0.0rad へ
        固定されるので、復帰時点で内部の原点がその瞬間の姿勢へ作り直される。
        PC 側に検出手段が無く、UI にもログにもヘルスにも出ない。当面は
        「物理緊急停止を踏んだら零点確定をやり直す」運用で受ける。詳細は
        `docs/checks_and_health.md` の「零点確定」節。
        """
        return [(self.encode_set_zero(), 0.2)]

    def configuration_probe_messages(self) -> list[can.Message]:
        """まだ読めていないレンジの読み返し (0x15/0x16/0x17)。

        取りこぼしは再試行で解く —— ゲートの既定を「通す」にして解くと、応答 1 通の
        取りこぼしで事故の経路が丸ごと復活する。
        """
        return [
            self.encode_read_register(register)
            for register, _, _ in self._expected_ranges()
            if register not in self._reported_ranges
        ]

    def _range_problems(self) -> tuple[list[str], list[str]]:
        """(未確認のレンジ, 食い違っているレンジ)。

        励磁ゲートとヘルスが同じここを読む —— 2 箇所に書くと「止めているのに画面は
        平常」が作れる。
        """
        unconfirmed: list[str] = []
        mismatched: list[str] = []
        for register, label, expected in self._expected_ranges():
            reported = self._reported_ranges.get(register)
            if reported is None:
                unconfirmed.append(f"{label} (レジスタ {register:#04x})")
            elif not math.isclose(reported, expected, rel_tol=1e-6):
                mismatched.append(f"{label} 実機 {reported:g} / config {expected:g}")
        return unconfirmed, mismatched

    def activation_block_reason(self) -> str | None:
        """レンジが確認できない・config と食い違うなら励磁を止める。

        !!! **「書いて直す」の代わりである。一度書いて実機を壊しかけた** !!!
        書き終わるまでの窓でレンジが食い違い、12.5 で送られた位置を 1000 で復号した
        80 倍の値が `activation_steps` の保持目標に書かれて機構端まで走った
        (2026-09-09)。**未確認でも止める** —— 素通しにすると応答 1 通の取りこぼしで
        この経路が丸ごと復活する。文面を分けるのは手当てが逆だから (応答が無い =
        電源・配線 / 食い違う = config か実機のレジスタ)。
        """
        unconfirmed, mismatched = self._range_problems()
        if mismatched:
            return (
                f"実機と config の固定小数点レンジが食い違っています ({', '.join(mismatched)})。"
                "**電源断でフラッシュの出荷値へ戻ったか、config を書き換えたのに実機へ"
                "反映していないかのどちらかです。** フィードバックが比例倍で読めるので、"
                "レジスタ 0x15/0x16/0x17 を config の値へ書き直してから起動し直してください"
            )
        if unconfirmed:
            return (
                f"実機の固定小数点レンジを読み返せません ({', '.join(unconfirmed)})。"
                "レジスタ読み返しの応答が 1 通も届いていないので、フィードバックの"
                "解釈が config どおりかを確かめられません。**電源・CAN 配線を"
                "確認してください** (レンジが食い違ったまま励磁すると、比例倍で読めた"
                "位置がそのまま保持目標に書かれて機構が飛びます)"
            )
        return None

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

    def health_detail(self) -> str | None:
        """励磁を止めている理由を操縦者へ渡す。

        ログと起動時の「有効化できなかったモータ名」にしか出ないと配線不良と区別が
        付かない。**`is_fault()` へ入れてはならない** —— ドライバの異常報告ではなく
        PC 側が安全側へ倒した判断なので、FAULT ではなく WARNING で出す。
        """
        unconfirmed, mismatched = self._range_problems()
        if mismatched:
            return (
                f"固定小数点レンジ 食い違い ({', '.join(mismatched)}) のため励磁しません。"
                "実機のレジスタ 0x15/0x16/0x17 か config のどちらかを直してください"
            )
        if unconfirmed:
            return (
                f"固定小数点レンジ 未確認 ({', '.join(unconfirmed)} の読み返し応答が"
                "未受信) のため励磁しません。電源・CAN 配線を確認してください"
            )
        return None

    def error_label(self) -> str | None:
        return _ERROR_LABELS.get(self.error_code)

    _RPM_TO_RAD_PER_S = 2.0 * math.pi / 60.0

    def default_tolerance(self, mode: ControlMode) -> float:
        if mode is ControlMode.POSITION:
            return math.radians(super().default_tolerance(mode))
        if mode is ControlMode.VELOCITY:
            return super().default_tolerance(mode) * self._RPM_TO_RAD_PER_S
        return super().default_tolerance(mode)
