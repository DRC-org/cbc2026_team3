"""Damiao DM3520-1EC ドライバのプロトコル層。

情報源は `DM-S3519-1EC User Manual`。**実機が手元に無い段階で書いているので、
ここで固定できるのは「マニュアルの記述どおりに組み立て・解釈しているか」まで**である。
p_max / v_max / t_max の実値のような、実機のレジスタを読まないと分からないものは
config で与える形にしてあり、そのずれは動作確認シーケンスが検出する。
"""

from __future__ import annotations

import math
import struct

import can
import pytest

from lib.drivers.base import ControlMode
from lib.drivers.dm3520 import Dm3520CtrlMode, Dm3520Driver, Dm3520Error
from tests.feedback_frames import dm3520_feedback, feed_dm3520


def _driver(**kwargs: object) -> Dm3520Driver:
    params: dict = {"master_id": 0x11}
    params.update(kwargs)
    return Dm3520Driver("slide", 0x05, **params)  # type: ignore[arg-type]


class TestConstruction:
    @pytest.mark.parametrize("can_id", [0x00, 0x10, 0x13, 0xFF, 0x100])
    def test_can_id_out_of_range_is_rejected(self, can_id: int) -> None:
        """**フィードバックには CAN ID の下位 4bit しか載らない。**

        0x10 / 0x13 は実機の出荷値だが、下位 4bit が 0x00 / 0x03 と重なるため
        「2 台目のフィードバックが 1 台目の状態を上書きする」構成を書けてしまう。
        レジスタ 0x08 を書き換えて 0x01..0x0F へ寄せる運用にする。
        """
        with pytest.raises(ValueError, match="can_id"):
            Dm3520Driver("slide", can_id)

    def test_enabled_feedback_is_never_read_as_a_config_response(self) -> None:
        """ESC_ID を下位ニブルに閉じたことの効き目を、境界そのもので確かめる。

        パラメータ応答は D0 = ESC_ID、フィードバックは D0 = エラー<<4 | ID下位4bit。
        両者が一致するには エラー == ESC_ID>>4 が要るので、ESC_ID <= 0x0F なら
        「エラー == 0 = 無励磁」に限られる。**励磁して運転している間は起こらない。**
        位置は生値が最も小さくなる端 (-p_max) に置き、D1/D2 の側の条件も
        わざと成立させたうえで見る。
        """
        drv = Dm3520Driver("slide", 0x03, master_id=0x13)

        enabled = dm3520_feedback(drv, position=-drv.p_max, error=int(Dm3520Error.ENABLED))

        assert enabled.data[0] == 0x13  # ESC_ID が 0x13 なら一致してしまう並び
        assert drv.matches_feedback(enabled) is True

    def test_mit_mode_is_not_supported(self) -> None:
        with pytest.raises(ValueError, match=r"mit|ControlMode"):
            Dm3520Driver("slide", 1, mode="mit")

    @pytest.mark.parametrize("key", ["p_max", "v_max", "t_max"])
    def test_zero_mapping_range_is_rejected(self, key: str) -> None:
        # 0 を通すと uint_to_float が全域 0 を返し、位置が常に 0 として読める。
        # 「動いていないように見えるモータ」になり、到達判定は永久に成立しない
        with pytest.raises(ValueError, match=key):
            Dm3520Driver("slide", 1, **{key: 0.0})  # type: ignore[arg-type]

    def test_limit_speed_is_capped_by_v_max(self) -> None:
        # v_max を超える速度上限は物理的に出ない。指令に載せると
        # 「指定した速度で動かない」だけが残り、原因が config から読めない
        drv = Dm3520Driver("slide", 1, limit_speed=100.0, v_max=45.0)

        assert drv.limit_speed == 45.0


class TestTargetFrames:
    """指令フレームの ID とペイロード (マニュアル「Control Protocol Description」節)。"""

    def test_position_command_uses_0x100_offset_and_two_floats(self) -> None:
        drv = _driver(limit_speed=3.0)

        msg = drv.encode_target(ControlMode.POSITION, 1.25)

        assert msg.arbitration_id == 0x100 + 0x05
        assert msg.is_extended_id is False
        assert len(msg.data) == 8
        p_des, v_des = struct.unpack("<ff", msg.data)
        assert p_des == pytest.approx(1.25)
        # v_des は目標速度ではなく移動中の速度上限
        assert v_des == pytest.approx(3.0)

    def test_velocity_command_uses_0x200_offset_and_one_float(self) -> None:
        drv = _driver(mode=ControlMode.VELOCITY, limit_speed=5.0)

        msg = drv.encode_target(ControlMode.VELOCITY, 2.0)

        assert msg.arbitration_id == 0x200 + 0x05
        assert len(msg.data) == 4
        assert struct.unpack("<f", msg.data)[0] == pytest.approx(2.0)

    def test_position_is_clamped_to_p_max(self) -> None:
        # p_max を超えた位置はドライバ側で頭打ちになり、フィードバックも張り付く。
        # 「送ったのに途中で止まる」としか見えないので、送る前に丸める
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
    """enable / disable / set zero は ESC_ID 宛の 0xFF 7 連 + 種別。"""

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

        # 16bit / 12bit の量子化ぶんだけ誤差が出る
        assert state.position == pytest.approx(1.5, abs=1e-3)
        assert state.velocity == pytest.approx(-2.0, abs=0.05)
        assert state.current == pytest.approx(0.5, abs=0.01)
        # ドライバ MOS とコイルの高い方。低い方を採ると、先に焼ける側を見逃す
        assert state.temperature == 41.0

    def test_position_uses_p_max_not_v_max(self) -> None:
        """**レンジの取り違えはデコード層でしか捕まえられない。**

        位置に v_max を当てると、指令どおり動いても位置が比例倍で読め、
        到達判定が永久に成立しない。症状は「動いたのに到達しない」だけになる。
        """
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
        # 無励磁は正常な状態。異常にすると起動直後から FAULT のまま復帰しない
        drv = _driver()

        feed_dm3520(drv, error=error)

        assert drv.is_fault() is False

    def test_energized_only_when_error_nibble_says_enabled(self) -> None:
        """**無励磁は `is_fault()` に掛からないので、別に読めなければ見えない。**

        本機は指令フレームを無励磁のまま受理して黙って捨てる。ドライバの TIMEOUT や
        電源の瞬断で励磁が外れると、PC は 20Hz で位置指令を送り続け、フィードバックも
        正常に届き、ヘルスも OK のまま —— 操縦者に見えるのは「指令しても動かない」だけで、
        原因を示す表示がどこにも無い。実機で実際にこの状態が起きた。
        """
        drv = _driver()

        feed_dm3520(drv, error=int(Dm3520Error.ENABLED))
        assert drv.is_energized() is True

        feed_dm3520(drv, error=int(Dm3520Error.DISABLED))
        assert drv.is_energized() is False
        assert drv.is_fault() is False

        # 異常で自ら励磁を切った状態も「励磁されていない」
        feed_dm3520(drv, error=int(Dm3520Error.COMM_LOSS))
        assert drv.is_energized() is False

    def test_comm_loss_is_a_fault(self) -> None:
        """ドライバの TIMEOUT が満了して自分で励磁を切った状態。

        拾わないと「機体は止まっているのに UI は平常のまま」になる。
        """
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
        """**MST_ID は複数台で共有されうる。**

        見分ける手掛かりは D0 下位 4bit (送り主の CAN ID 下位 4bit) だけなので、
        ここを見ないと 2 台目のフィードバックが 1 台目の状態を上書きする。
        """
        drv = _driver()

        assert drv.matches_feedback(dm3520_feedback(drv, can_id_nibble=0x06)) is False

    def test_extended_frame_is_ignored(self) -> None:
        drv = _driver()
        msg = can.Message(arbitration_id=0x11, data=bytes(8), is_extended_id=True)

        assert drv.matches_feedback(msg) is False

    def test_param_write_echo_is_not_feedback(self) -> None:
        """**パラメータ書き込みの応答も MST_ID で返ってくる。**

        起動時の CTRL_MODE 書き込みは必ず 1 通の応答を生む。これを状態として
        取り込むと、``activation_steps`` が保持目標に使う実測角がその瞬間だけ
        嘘になり、励磁した瞬間にモータが飛ぶ。
        """
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
        """**順序が逆だと危ない。**

        モード切替はドライバ内部の指令値をクリアするので、励磁したまま切り替えると
        p_des が 0 に落ちた状態で位置ループが走る。
        """
        drv = _driver()

        steps = [msg for msg, _ in drv.initialization_steps()]

        assert bytes(steps[0].data)[-1] == 0xFD
        assert steps[1].arbitration_id == 0x7FF

    def test_set_zero_on_start_appends_zero_command(self) -> None:
        drv = _driver(set_zero_on_start=True)

        codes = [bytes(msg.data)[-1] for msg, _ in drv.initialization_steps()]

        assert codes[-1] == 0xFE

    def test_activation_writes_measured_position_before_enable(self) -> None:
        """**保持目標を書かずに enable すると機構が原点へ飛ぶ。**

        モード切替で p_des が 0 になっているので、そのまま励磁すると
        ラックがストローク端まで走る。
        """
        drv = _driver()
        feed_dm3520(drv, position=2.0)

        steps = drv.activation_steps()

        p_des, _ = struct.unpack("<ff", steps[0][0].data)
        assert p_des == pytest.approx(2.0, abs=1e-3)
        assert bytes(steps[1][0].data)[-1] == 0xFC

    def test_activation_after_set_zero_holds_the_new_origin(self) -> None:
        """原点を付け替えた直後の実測角は旧原点基準。

        保持目標に使うと、原点の差分だけ機構が動く。
        """
        drv = _driver()
        feed_dm3520(drv, position=2.0)

        steps = drv.activation_steps(after_set_zero=True)

        p_des, _ = struct.unpack("<ff", steps[0][0].data)
        assert p_des == pytest.approx(0.0)

    def test_position_mode_requires_fresh_feedback(self) -> None:
        # MotorState の初期値 0.0rad を実測角と取り違えると原点へ飛ぶ
        assert _driver().requires_fresh_feedback_for_activation() is True

    def test_velocity_mode_does_not_require_fresh_feedback(self) -> None:
        assert _driver(mode=ControlMode.VELOCITY).requires_fresh_feedback_for_activation() is False

    def test_probe_is_the_disable_frame(self) -> None:
        """無励磁を保ったまま状態を返させられる唯一のフレーム。"""
        drv = _driver()

        assert bytes(drv.feedback_probe_message().data)[-1] == 0xFD

    def test_emergency_stop_disables(self) -> None:
        drv = _driver()

        assert bytes(drv.emergency_stop_message().data)[-1] == 0xFD


class TestIdleTarget:
    """目標を持たない間に書き続ける値 (``QueryDrivenTargetRefresher`` が使う)。"""

    def test_position_mode_holds_the_measured_position(self) -> None:
        drv = _driver()
        feed_dm3520(drv, position=1.75)

        assert drv.idle_target_value() == pytest.approx(1.75, abs=1e-3)

    def test_velocity_mode_holds_stop(self) -> None:
        """速度モードで「今の速度を保て」を書くと、止まらないまま回り続ける。"""
        drv = _driver(mode=ControlMode.VELOCITY)
        feed_dm3520(drv, velocity=3.0)

        assert drv.idle_target_value() == 0.0


class TestTolerance:
    def test_position_tolerance_is_the_common_default_in_radians(self) -> None:
        # 共通既定値 (1deg) を本機の単位へ換算するだけ。ここに独自の数値を書くと
        # 共通既定値を直しても本機だけ古い値のまま残る
        assert _driver().default_tolerance(ControlMode.POSITION) == pytest.approx(math.radians(1.0))

    def test_velocity_tolerance_is_the_common_default_in_rad_per_s(self) -> None:
        assert _driver().default_tolerance(ControlMode.VELOCITY) == pytest.approx(
            5.0 * 2.0 * math.pi / 60.0
        )
