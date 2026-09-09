from __future__ import annotations

import logging
import math
import struct

import can
import pytest

from lib.drivers.base import ControlMode
from lib.drivers.dm3520 import (
    _RANGE_WRITE_ATTEMPTS,
    Dm3520CtrlMode,
    Dm3520Driver,
    Dm3520Error,
)
from tests.feedback_frames import dm3520_feedback, feed_dm3520


def _config_response(drv: Dm3520Driver, register: int, value: float) -> can.Message:
    """0x7FF への読み出しに対する応答フレーム (マニュアル「Read Parameters」節)。

    **状態フィードバックと同じ MST_ID で返る**ので、実物と同じ形で組み立てる。
    """
    data = struct.pack("<HBBf", drv.can_id, Dm3520Driver.CONFIG_READ, register, value)
    return can.Message(arbitration_id=drv.master_id, data=data, is_extended_id=False)


def _probe_registers(drv: Dm3520Driver) -> list[int]:
    """まだ読めていないレンジの読み返しフレームが指すレジスタ番号。"""
    return [bytes(msg.data)[3] for msg in drv.configuration_probe_messages()]


def _confirm_ranges(
    drv: Dm3520Driver,
    *,
    p_max: float | None = None,
    v_max: float | None = None,
    t_max: float | None = None,
) -> None:
    """3 つのレンジの読み返し応答を実機から届いたことにする (既定は食い違いなし)。"""
    for register, reported in (
        (Dm3520Driver.REG_P_MAX, drv.p_max if p_max is None else p_max),
        (Dm3520Driver.REG_V_MAX, drv.v_max if v_max is None else v_max),
        (Dm3520Driver.REG_T_MAX, drv.t_max if t_max is None else t_max),
    ):
        drv.matches_feedback(_config_response(drv, register, reported))


def _driver(**kwargs: object) -> Dm3520Driver:
    params: dict = {"master_id": 0x11}
    params.update(kwargs)
    return Dm3520Driver("slide", 0x05, **params)  # type: ignore[arg-type]


class TestConstruction:
    @pytest.mark.parametrize("can_id", [0x00, 0x10, 0x13, 0xFF, 0x100])
    def test_can_id_out_of_range_is_rejected(self, can_id: int) -> None:
        with pytest.raises(ValueError, match="can_id"):
            Dm3520Driver("slide", can_id)

    def test_enabled_feedback_is_never_read_as_a_config_response(self) -> None:
        drv = Dm3520Driver("slide", 0x03, master_id=0x13)

        enabled = dm3520_feedback(drv, position=-drv.p_max, error=int(Dm3520Error.ENABLED))

        assert enabled.data[0] == 0x13
        assert drv.matches_feedback(enabled) is True

    def test_mit_mode_is_not_supported(self) -> None:
        with pytest.raises(ValueError, match=r"mit|ControlMode"):
            Dm3520Driver("slide", 1, mode="mit")

    @pytest.mark.parametrize("key", ["p_max", "v_max", "t_max"])
    def test_zero_mapping_range_is_rejected(self, key: str) -> None:
        with pytest.raises(ValueError, match=key):
            Dm3520Driver("slide", 1, **{key: 0.0})  # type: ignore[arg-type]

    def test_limit_speed_is_capped_by_v_max(self) -> None:
        drv = Dm3520Driver("slide", 1, limit_speed=100.0, v_max=45.0)

        assert drv.limit_speed == 45.0


class TestTargetFrames:
    def test_position_command_uses_0x100_offset_and_two_floats(self) -> None:
        drv = _driver(limit_speed=3.0)

        msg = drv.encode_target(ControlMode.POSITION, 1.25)

        assert msg.arbitration_id == 0x100 + 0x05
        assert msg.is_extended_id is False
        assert len(msg.data) == 8
        p_des, v_des = struct.unpack("<ff", msg.data)
        assert p_des == pytest.approx(1.25)
        assert v_des == pytest.approx(3.0)

    def test_velocity_command_uses_0x200_offset_and_one_float(self) -> None:
        drv = _driver(mode=ControlMode.VELOCITY, limit_speed=5.0)

        msg = drv.encode_target(ControlMode.VELOCITY, 2.0)

        assert msg.arbitration_id == 0x200 + 0x05
        assert len(msg.data) == 4
        assert struct.unpack("<f", msg.data)[0] == pytest.approx(2.0)

    def test_position_is_clamped_to_p_max(self) -> None:
        drv = _driver(p_max=10.0)

        p_des, _ = struct.unpack("<ff", drv.encode_target(ControlMode.POSITION, 99.0).data)

        assert p_des == pytest.approx(10.0)

    def test_velocity_is_clamped_to_limit_speed(self) -> None:
        drv = _driver(mode=ControlMode.VELOCITY, limit_speed=2.0)

        speed = struct.unpack("<f", drv.encode_target(ControlMode.VELOCITY, 99.0).data)[0]

        assert speed == pytest.approx(2.0)

    def test_current_mode_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            _driver().encode_target(ControlMode.CURRENT, 1.0)


class TestSpecialCommands:
    @pytest.mark.parametrize(
        ("build", "code"),
        [
            (Dm3520Driver.encode_enable, 0xFC),
            (Dm3520Driver.encode_disable, 0xFD),
            (Dm3520Driver.encode_set_zero, 0xFE),
        ],
    )
    def test_special_command_frame(self, build: object, code: int) -> None:
        drv = _driver()

        msg = build(drv)

        assert msg.arbitration_id == 0x05
        assert msg.is_extended_id is False
        assert bytes(msg.data) == bytes([0xFF] * 7 + [code])

    def test_ctrl_mode_write_targets_the_config_id(self) -> None:
        drv = _driver()

        msg = drv.encode_ctrl_mode(Dm3520CtrlMode.POSITION_VELOCITY)

        assert msg.arbitration_id == 0x7FF
        can_id, op, reg, value = struct.unpack("<HBBI", msg.data)
        assert (can_id, op, reg, value) == (0x05, 0x55, 0x0A, 2)


class TestFeedbackDecode:
    def test_position_velocity_torque_and_temperature(self) -> None:
        drv = _driver(p_max=12.5, v_max=40.0, t_max=10.0)

        state = drv.update_state(
            dm3520_feedback(drv, position=1.5, velocity=-2.0, torque=0.5, t_mos=30, t_rotor=41)
        )

        assert state.position == pytest.approx(1.5, abs=1e-3)
        assert state.velocity == pytest.approx(-2.0, abs=0.05)
        assert state.current == pytest.approx(0.5, abs=0.01)
        assert state.temperature == 41.0

    def test_position_uses_p_max_not_v_max(self) -> None:
        drv = _driver(p_max=12.5, v_max=40.0)

        state = drv.update_state(dm3520_feedback(drv, position=12.5))

        assert state.position == pytest.approx(12.5, abs=1e-3)

    def test_error_nibble_is_captured(self) -> None:
        drv = _driver()

        feed_dm3520(drv, error=int(Dm3520Error.OVERCURRENT))

        assert drv.error_code == Dm3520Error.OVERCURRENT
        assert drv.is_fault() is True
        assert drv.has_overcurrent_warning() is True
        assert "過電流" in (drv.error_label() or "")

    @pytest.mark.parametrize("error", [int(Dm3520Error.DISABLED), int(Dm3520Error.ENABLED)])
    def test_disabled_and_enabled_are_not_faults(self, error: int) -> None:
        drv = _driver()

        feed_dm3520(drv, error=error)

        assert drv.is_fault() is False

    def test_energized_only_when_error_nibble_says_enabled(self) -> None:
        drv = _driver()

        feed_dm3520(drv, error=int(Dm3520Error.ENABLED))
        assert drv.is_energized() is True

        feed_dm3520(drv, error=int(Dm3520Error.DISABLED))
        assert drv.is_energized() is False
        assert drv.is_fault() is False

        feed_dm3520(drv, error=int(Dm3520Error.COMM_LOSS))
        assert drv.is_energized() is False

    def test_unreceived_feedback_is_energized_none(self) -> None:
        drv = _driver()

        assert drv.is_energized() is None

        feed_dm3520(drv, error=int(Dm3520Error.DISABLED))
        assert drv.is_energized() is False

    def test_comm_loss_is_a_fault(self) -> None:
        drv = _driver()

        feed_dm3520(drv, error=int(Dm3520Error.COMM_LOSS))

        assert drv.is_fault() is True


class TestFeedbackMatching:
    def test_matches_own_master_id(self) -> None:
        drv = _driver()

        assert drv.matches_feedback(dm3520_feedback(drv)) is True

    def test_other_master_id_is_ignored(self) -> None:
        drv = _driver()

        assert drv.matches_feedback(dm3520_feedback(drv, master_id=0x12)) is False

    def test_other_motor_on_the_same_master_id_is_ignored(self) -> None:
        drv = _driver()

        assert drv.matches_feedback(dm3520_feedback(drv, can_id_nibble=0x06)) is False

    def test_extended_frame_is_ignored(self) -> None:
        drv = _driver()
        msg = can.Message(arbitration_id=0x11, data=bytes(8), is_extended_id=True)

        assert drv.matches_feedback(msg) is False

    def test_param_write_echo_is_not_feedback(self) -> None:
        drv = _driver()
        echo = can.Message(
            arbitration_id=drv.master_id,
            data=struct.pack("<HBBI", drv.can_id, 0x55, 0x0A, 2),
            is_extended_id=False,
        )

        assert drv.matches_feedback(echo) is False

    def test_param_read_response_is_not_feedback(self) -> None:
        drv = _driver()
        resp = can.Message(
            arbitration_id=drv.master_id,
            data=struct.pack("<HBBI", drv.can_id, 0x33, 0x15, 0),
            is_extended_id=False,
        )

        assert drv.matches_feedback(resp) is False

    def test_decode_rejects_frames_it_does_not_own(self) -> None:
        drv = _driver()

        with pytest.raises(ValueError):
            drv.decode_feedback(dm3520_feedback(drv, master_id=0x12))


class TestStartupSequence:
    def test_initialization_disables_then_writes_ctrl_mode(self) -> None:
        drv = _driver()

        steps = [msg for msg, _ in drv.initialization_steps()]

        assert bytes(steps[0].data)[-1] == 0xFD
        assert steps[1].arbitration_id == 0x7FF

    def test_初期化で_p_max_を書かない(self) -> None:
        """**ここで p_max を書くと機構が飛ぶ。一度入れて実機を壊しかけた。**

        p_max はフラッシュへ保存されず電源断で出荷値へ戻るので、CTRL_MODE と同じく
        書き直せばよいと考えたのが誤り。**書き終わるまでの窓で復号レンジが食い違う** ——
        ドライバが 12.5 で送ったフィードバックを config の 1000 で復号すると位置が
        80 倍に読め、直後の `activation_steps` がそれを「現在角」として保持目標に
        書く。機構は 80 倍先へ走り、リミットスイッチを踏み越えて機構端まで行く
        (2026-09-09 に実機で発生)。

        正しい向きは「書いて直す」ではなく「食い違いを検出して止める」なので、
        **このテストは書き込みが再び足されたときに落ちる。**
        """
        drv = _driver(p_max=1000.0)

        writes = [
            bytes(msg.data)[3]
            for msg, _ in drv.initialization_steps()
            if msg.arbitration_id == 0x7FF and bytes(msg.data)[2] == 0x55
        ]

        assert Dm3520Driver.REG_P_MAX not in writes
        assert writes == [Dm3520Driver.REG_CTRL_MODE]

    def test_初期化に読み返しを並べない(self) -> None:
        """**静的な list は「応答が届いたらやめる」を表現できない。** 再試行は
        `configuration_probe_messages()` が持つ。"""
        drv = _driver()

        reads = [
            bytes(msg.data)[3]
            for msg, _ in drv.initialization_steps()
            if msg.arbitration_id == 0x7FF and bytes(msg.data)[2] == 0x33
        ]

        assert reads == []

    def test_まだ読めていないレンジだけを読み返す(self) -> None:
        """**読めた項目を落として返すから、空になった時点で最終判断できる。**"""
        drv = _driver(p_max=1000.0)

        assert _probe_registers(drv) == [
            Dm3520Driver.REG_P_MAX,
            Dm3520Driver.REG_V_MAX,
            Dm3520Driver.REG_T_MAX,
        ]

        drv.matches_feedback(_config_response(drv, Dm3520Driver.REG_P_MAX, 1000.0))

        assert _probe_registers(drv) == [Dm3520Driver.REG_V_MAX, Dm3520Driver.REG_T_MAX]

        _confirm_ranges(drv)

        assert _probe_registers(drv) == []

    def test_読み返せていないうちは励磁しない(self) -> None:
        """**「読めていない」を「問題なし」へ倒すと、応答 1 通の取りこぼしで事故の
        経路が丸ごと復活する。** 取りこぼしは再試行で解く。"""
        drv = _driver(p_max=1000.0)

        reason = drv.activation_block_reason()

        assert reason is not None
        # 手当てが逆なので、食い違いと同じ文言にしてはならない (こちらは電源・配線)
        assert "応答が 1 通も届いていない" in reason
        assert "配線" in reason

    @pytest.mark.parametrize(
        ("register", "label"),
        [
            (Dm3520Driver.REG_P_MAX, "p_max"),
            (Dm3520Driver.REG_V_MAX, "v_max"),
            (Dm3520Driver.REG_T_MAX, "t_max"),
        ],
    )
    def test_レンジが食い違うと励磁を止める(self, register: int, label: str) -> None:
        """**比例倍で読める状態のまま励磁すると、保持目標が桁ごとずれる** (実機では
        80 倍の位置を保持目標に書いて機構端まで走った)。t_max も同型で、
        `guard.stall_torque` の実効値がずれる。"""
        drv = _driver(p_max=1000.0, v_max=200.0, t_max=10.0)
        _confirm_ranges(drv, **{label: 12.5})

        reason = drv.activation_block_reason()

        assert reason is not None
        assert label in reason
        assert "12.5" in reason
        # 応答は返ってきているので、電源・配線を疑わせてはならない
        assert "配線" not in reason

    def test_全レンジが一致していれば止めない(self) -> None:
        drv = _driver(p_max=1000.0, v_max=200.0, t_max=10.0)

        _confirm_ranges(drv)

        assert drv.activation_block_reason() is None

    def test_読み返しの応答をフィードバックとして取り込まない(self) -> None:
        """**応答もフィードバックと同じ MST_ID で返る。** 取り込むと実測角が嘘になる。"""
        drv = _driver(p_max=1000.0)

        assert drv.matches_feedback(_config_response(drv, Dm3520Driver.REG_P_MAX, 1000.0)) is False

    def test_set_zero_on_start_appends_zero_command(self) -> None:
        drv = _driver(set_zero_on_start=True)

        codes = [bytes(msg.data)[-1] for msg, _ in drv.initialization_steps()]

        assert codes[-1] == 0xFE

    def test_再初期化は電源断で失われるぶんだけを送る(self) -> None:
        """**再励磁のたびに `SET_ZERO` を送り直してはならない。**

        原点は励磁が落ちても生き残るので書き直す理由が無く、送れば零点確定で
        合わせた原点をその場の姿勢へ書き換えてしまう。一方 CTRL_MODE は
        フラッシュに残らないので、電源が落ちた個体には書き直しが要る。
        """
        drv = _driver(set_zero_on_start=True)

        codes = [bytes(msg.data)[-1] for msg, _ in drv.reinitialization_steps()]

        assert 0xFE not in codes, "再励磁で原点を書き換えてはならない"
        assert codes == [0xFD, 0x00]  # disable → CTRL_MODE 書き込み

    def test_起動時の手順は再初期化に原点確定を足したもの(self) -> None:
        """2 つのリストを別々に並べると、片方だけへレジスタを足した状態が作れる。"""
        drv = _driver(set_zero_on_start=True)

        initialization = [
            (msg.arbitration_id, bytes(msg.data)) for msg, _ in drv.initialization_steps()
        ]
        reinitialization = [
            (msg.arbitration_id, bytes(msg.data)) for msg, _ in drv.reinitialization_steps()
        ]

        assert initialization[: len(reinitialization)] == reinitialization
        assert len(initialization) == len(reinitialization) + 1

    def test_再初期化は控えてあるレンジを捨てる(self) -> None:
        """**電源断は CTRL_MODE と同時に p_max も出荷値へ戻す。**

        起動時に読んだ 1000 が残っていると `activation_block_reason()` は
        「一致している」と答え、2026-09-09 に機構を壊しかけた 80 倍の経路が
        再励磁のたびに復活する。捨てれば読み返しが再開する。
        """
        drv = _driver(p_max=1000.0)
        _confirm_ranges(drv)
        assert _probe_registers(drv) == []
        assert drv.activation_block_reason() is None

        drv.reinitialization_steps()

        assert _probe_registers(drv) == [
            Dm3520Driver.REG_P_MAX,
            Dm3520Driver.REG_V_MAX,
            Dm3520Driver.REG_T_MAX,
        ]
        assert drv.activation_block_reason() is not None

    def test_activation_writes_measured_position_before_enable(self) -> None:
        drv = _driver()
        feed_dm3520(drv, position=2.0)

        steps = drv.activation_steps()

        p_des, _ = struct.unpack("<ff", steps[0][0].data)
        assert p_des == pytest.approx(2.0, abs=1e-3)
        assert bytes(steps[1][0].data)[-1] == 0xFC

    def test_activation_after_set_zero_holds_the_new_origin(self) -> None:
        drv = _driver()
        feed_dm3520(drv, position=2.0)

        steps = drv.activation_steps(after_set_zero=True)

        p_des, _ = struct.unpack("<ff", steps[0][0].data)
        assert p_des == pytest.approx(0.0)

    def test_position_mode_requires_fresh_feedback(self) -> None:
        assert _driver().requires_fresh_feedback_for_activation() is True

    def test_velocity_mode_does_not_require_fresh_feedback(self) -> None:
        assert _driver(mode=ControlMode.VELOCITY).requires_fresh_feedback_for_activation() is False

    def test_probe_is_the_disable_frame(self) -> None:
        drv = _driver()

        assert bytes(drv.feedback_probe_message().data)[-1] == 0xFD

    def test_emergency_stop_disables(self) -> None:
        drv = _driver()

        assert bytes(drv.emergency_stop_message().data)[-1] == 0xFD


class TestFixedPointRangeRewrite:
    """**物理非常停止はドライバの電源を数秒落とし、レンジは出荷値へ戻る。**

    戻ったままでは `activation_block_reason()` が励磁を拒み続け、再励磁ボタンでは
    絶対に解けない (2026-09-09 に 1 日で 3 回踏んだ)。そこで励磁の手前で config の
    値を書き、**読み返して確かめてから**励磁へ進む。
    """

    @staticmethod
    def _frames(messages: list[can.Message], kind: int) -> list[tuple[int, float]]:
        """(レジスタ番号, 載っている float) を種別 (0x55 / 0x33) で絞る。"""
        return [
            (bytes(msg.data)[3], struct.unpack("<f", bytes(msg.data)[4:8])[0])
            for msg in messages
            if msg.arbitration_id == Dm3520Driver.CONFIG_FRAME_ID and bytes(msg.data)[2] == kind
        ]

    def _confirm_loop(
        self,
        drv: Dm3520Driver,
        device: dict[int, float],
        *,
        accept_writes: bool = True,
        rounds: int = 8,
    ) -> list[can.Message]:
        """`CANManager._confirm_configuration` と同じ回し方 (空になるまで送る)。

        `accept_writes=False` は「レンジ外の値を書いたので実機が元の値を返す」。
        """
        sent: list[can.Message] = []
        for _ in range(rounds):
            probes = drv.configuration_probe_messages()
            if not probes:
                break
            sent.extend(probes)
            for msg in probes:
                data = bytes(msg.data)
                if data[2] == Dm3520Driver.CONFIG_WRITE and accept_writes:
                    device[data[3]] = struct.unpack("<f", data[4:8])[0]
                elif data[2] == Dm3520Driver.CONFIG_READ:
                    drv.matches_feedback(_config_response(drv, data[3], device[data[3]]))
        return sent

    @staticmethod
    def _device(p_max: float, drv: Dm3520Driver) -> dict[int, float]:
        return {
            Dm3520Driver.REG_P_MAX: p_max,
            Dm3520Driver.REG_V_MAX: drv.v_max,
            Dm3520Driver.REG_T_MAX: drv.t_max,
        }

    def test_出荷値へ戻っていたら書き直して励磁へ進む(self) -> None:
        drv = _driver(p_max=1000.0, v_max=200.0, t_max=10.0)
        device = self._device(12.5, drv)

        sent = self._confirm_loop(drv, device)

        assert self._frames(sent, Dm3520Driver.CONFIG_WRITE) == [(Dm3520Driver.REG_P_MAX, 1000.0)]
        assert device[Dm3520Driver.REG_P_MAX] == pytest.approx(1000.0)
        assert drv.activation_block_reason() is None

    def test_書いた事実を一致と数えない(self) -> None:
        """**レンジ外を書くと実機は元の値をそのまま返す** (実機で確認済み)。
        書けたかどうかは読み返しでしか分からないので、書いた register は控えから
        外して問い合わせ直す。"""
        drv = _driver(p_max=1000.0, v_max=200.0, t_max=10.0)
        _confirm_ranges(drv, p_max=12.5)

        first = drv.configuration_probe_messages()

        assert self._frames(first, Dm3520Driver.CONFIG_WRITE) == [(Dm3520Driver.REG_P_MAX, 1000.0)]
        assert drv.activation_block_reason() is not None, "書いただけで通してはならない"
        assert self._frames(drv.configuration_probe_messages(), Dm3520Driver.CONFIG_READ) == [
            (Dm3520Driver.REG_P_MAX, 0.0)
        ]

    def test_書き直しても戻らなければ励磁しない(self) -> None:
        """ゲートは残す。守っているのは「比例倍に読めた位置がそのまま保持目標に
        書かれて機構が可動端へ走る」で、実際に踏んでいる (2026-09-09)。"""
        drv = _driver(p_max=1000.0, v_max=200.0, t_max=10.0)
        device = self._device(12.5, drv)

        sent = self._confirm_loop(drv, device, accept_writes=False)

        writes = self._frames(sent, Dm3520Driver.CONFIG_WRITE)
        assert writes == [(Dm3520Driver.REG_P_MAX, 1000.0)] * _RANGE_WRITE_ATTEMPTS, (
            "1 通落ちただけで諦めず、かつ無限には書かない"
        )
        reason = drv.activation_block_reason()
        assert reason is not None
        assert "p_max" in reason

    def test_フラッシュへ保存しない(self) -> None:
        """**寿命は約 1 万回。電源が入るたびに焼けば確実に潰れる。**
        揮発は承知のうえで毎回書き直すのが正解なので、`0xAA` を送ってはならない。"""
        drv = _driver(p_max=1000.0, v_max=200.0, t_max=10.0)
        device = self._device(12.5, drv)

        sent = [msg for msg, _ in drv.initialization_steps()]
        sent += self._confirm_loop(drv, device)

        assert self._frames(sent, Dm3520Driver.CONFIG_SAVE) == []
        assert all(bytes(msg.data)[2] != Dm3520Driver.CONFIG_SAVE for msg in sent)

    def test_書き直しをログに残す(self, caplog: pytest.LogCaptureFixture) -> None:
        """無言で直すと、電源が落ちたこと自体が誰にも見えなくなる。"""
        drv = _driver(p_max=1000.0, v_max=200.0, t_max=10.0)
        device = self._device(12.5, drv)

        with caplog.at_level(logging.INFO, logger="lib.drivers.dm3520"):
            self._confirm_loop(drv, device)

        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert "slide" in message
        assert "p_max" in message
        assert "12.5" in message and "1000" in message

    def test_一致しているレンジは書かない(self) -> None:
        drv = _driver(p_max=1000.0, v_max=200.0, t_max=10.0)
        device = self._device(1000.0, drv)

        sent = self._confirm_loop(drv, device)

        assert self._frames(sent, Dm3520Driver.CONFIG_WRITE) == []
        assert drv.activation_block_reason() is None

    def test_再初期化のあとはもう一度書き直す(self) -> None:
        """**電源断は何度でも起きる。** 諦めた回数を持ち越すと、2 回目の物理非常停止で
        書き直しが 1 通も出ない。"""
        drv = _driver(p_max=1000.0, v_max=200.0, t_max=10.0)
        device = self._device(12.5, drv)
        self._confirm_loop(drv, device, accept_writes=False)

        drv.reinitialization_steps()
        sent = self._confirm_loop(drv, device)

        assert self._frames(sent, Dm3520Driver.CONFIG_WRITE) == [(Dm3520Driver.REG_P_MAX, 1000.0)]
        assert drv.activation_block_reason() is None


class TestHealthDetail:
    """励磁を止めた理由を操縦者の画面まで届ける (`MotorHealthInfo.detail`)。

    ログにしか出ないと、操縦者から見て配線不良と区別が付かない。
    """

    def test_未確認は未確認として出す(self) -> None:
        drv = _driver(p_max=1000.0)

        detail = drv.health_detail()

        assert detail is not None
        assert "未確認" in detail
        assert "0x15" in detail

    def test_食い違いは実機と_config_の値を出す(self) -> None:
        """**手当てが逆**なので未確認と同じ文言にしない —— 応答は返っているので、
        電源・配線を疑っても何も見つからない。"""
        drv = _driver(p_max=1000.0, v_max=200.0, t_max=10.0)
        _confirm_ranges(drv, p_max=12.5)

        detail = drv.health_detail()

        assert detail is not None
        assert "食い違" in detail
        assert "12.5" in detail
        assert "1000" in detail

    def test_確認できたら黙る(self) -> None:
        drv = _driver(p_max=1000.0, v_max=200.0, t_max=10.0)

        _confirm_ranges(drv)

        assert drv.health_detail() is None

    def test_励磁拒否を_fault_にしない(self) -> None:
        """**FAULT はドライバが異常を報告したことの印。** PC 側が安全側へ倒した判断を
        そこへ入れると「モータが壊れた」と読める。"""
        drv = _driver(p_max=1000.0)
        _confirm_ranges(drv, p_max=12.5)

        assert drv.health_detail() is not None
        assert drv.is_fault() is False


class TestIdleTarget:
    def test_position_mode_holds_the_measured_position(self) -> None:
        drv = _driver()
        feed_dm3520(drv, position=1.75)

        assert drv.idle_target_value() == pytest.approx(1.75, abs=1e-3)

    def test_velocity_mode_holds_stop(self) -> None:
        drv = _driver(mode=ControlMode.VELOCITY)
        feed_dm3520(drv, velocity=3.0)

        assert drv.idle_target_value() == 0.0


class TestTolerance:
    def test_position_tolerance_is_the_common_default_in_radians(self) -> None:
        assert _driver().default_tolerance(ControlMode.POSITION) == pytest.approx(math.radians(1.0))

    def test_velocity_tolerance_is_the_common_default_in_rad_per_s(self) -> None:
        assert _driver().default_tolerance(ControlMode.VELOCITY) == pytest.approx(
            5.0 * 2.0 * math.pi / 60.0
        )
