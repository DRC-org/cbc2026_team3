from __future__ import annotations

import struct

import can
import pytest

from lib.drivers.base import ControlMode, MotorState
from lib.drivers.m3508 import GEAR_RATIO, M3508Driver


class TestEncodeCurrentCommand:
    def setup_method(self) -> None:
        self.driver = M3508Driver("test_motor", can_id=1)

    def test_encode_current_command(self) -> None:
        msg = self.driver.encode_target(ControlMode.CURRENT, 5000)
        assert msg.arbitration_id == 0x200
        assert msg.is_extended_id is False
        values = struct.unpack(">hhhh", msg.data)
        assert values[0] == 5000
        assert values[1] == 0
        assert values[2] == 0
        assert values[3] == 0

    def test_encode_current_command_negative(self) -> None:
        msg = self.driver.encode_target(ControlMode.CURRENT, -10000)
        values = struct.unpack(">hhhh", msg.data)
        assert values[0] == -10000

    def test_encode_current_command_clamp(self) -> None:
        msg_over = self.driver.encode_target(ControlMode.CURRENT, 20000)
        values_over = struct.unpack(">hhhh", msg_over.data)
        assert values_over[0] == 16384

        msg_under = self.driver.encode_target(ControlMode.CURRENT, -20000)
        values_under = struct.unpack(">hhhh", msg_under.data)
        assert values_under[0] == -16384


class TestEncodeCurrentCommandMotor3:
    """モータ ID 3 の場合、バイト 4-5 にスロットされることを確認。"""

    def test_encode_motor3_slot(self) -> None:
        driver = M3508Driver("motor3", can_id=3)
        msg = driver.encode_target(ControlMode.CURRENT, 1000)
        values = struct.unpack(">hhhh", msg.data)
        assert values[0] == 0
        assert values[1] == 0
        assert values[2] == 1000
        assert values[3] == 0


class TestEncodeCurrentCommandInvalidMode:
    def test_velocity_mode_raises(self) -> None:
        driver = M3508Driver("test", can_id=1)
        with pytest.raises(ValueError, match="CURRENT"):
            driver.encode_target(ControlMode.VELOCITY, 100)


class TestDecodeFeedback:
    def setup_method(self) -> None:
        self.driver = M3508Driver("test_motor", can_id=1)

    def test_decode_feedback(self) -> None:
        angle_raw = 4096
        rpm_raw = 1000
        current_raw = 500
        temp_raw = 40
        data = struct.pack(">hhhBB", angle_raw, rpm_raw, current_raw, temp_raw, 0)
        msg = can.Message(arbitration_id=0x201, data=data, is_extended_id=False)

        state = self.driver.decode_feedback(msg)
        assert isinstance(state, MotorState)
        assert state.position == pytest.approx(4096 / 8191 * 360, abs=0.1)
        assert state.velocity == pytest.approx(1000.0)
        assert state.current == pytest.approx(500.0)
        assert state.temperature == pytest.approx(40.0)

    def test_decode_feedback_negative_rpm(self) -> None:
        data = struct.pack(">hhhBB", 0, -3000, -200, 25, 0)
        msg = can.Message(arbitration_id=0x201, data=data, is_extended_id=False)

        state = self.driver.decode_feedback(msg)
        assert state.velocity == pytest.approx(-3000.0)
        assert state.current == pytest.approx(-200.0)


class TestMatchesFeedback:
    def test_matches_feedback(self) -> None:
        driver = M3508Driver("test", can_id=2)
        msg = can.Message(arbitration_id=0x202, data=bytes(8), is_extended_id=False)
        assert driver.matches_feedback(msg) is True

    def test_matches_feedback_wrong_id(self) -> None:
        driver = M3508Driver("test", can_id=2)
        msg = can.Message(arbitration_id=0x201, data=bytes(8), is_extended_id=False)
        assert driver.matches_feedback(msg) is False

        msg_unrelated = can.Message(arbitration_id=0x100, data=bytes(8), is_extended_id=False)
        assert driver.matches_feedback(msg_unrelated) is False


class TestEncodeCurrentFrame:
    def test_encode_current_frame(self) -> None:
        msg = M3508Driver.encode_current_frame([1000, -2000, 3000, -4000])
        assert msg.arbitration_id == 0x200
        assert msg.is_extended_id is False
        values = struct.unpack(">hhhh", msg.data)
        assert values == (1000, -2000, 3000, -4000)

    def test_encode_current_frame_clamp(self) -> None:
        msg = M3508Driver.encode_current_frame([20000, -20000, 0, 0])
        values = struct.unpack(">hhhh", msg.data)
        assert values[0] == 16384
        assert values[1] == -16384


class TestHealth:
    """ヘルスチェック判定 (Phase 6 段階②)。"""

    def setup_method(self) -> None:
        self.driver = M3508Driver("test_motor", can_id=1)

    def _feed(self, *, current: int = 0, temp: int = 25) -> None:
        # フィードバックフレームを 1 つ流して内部 state を更新する補助
        data = struct.pack(">hhhBB", 0, 0, current, temp, 0)
        msg = can.Message(arbitration_id=0x201, data=data, is_extended_id=False)
        self.driver.update_state(msg)

    def test_thermal_warning_below_threshold(self) -> None:
        self._feed(temp=60)
        assert self.driver.has_thermal_warning(temp_warning_c=65, temp_critical_c=80) is False

    def test_thermal_warning_at_threshold(self) -> None:
        self._feed(temp=65)
        assert self.driver.has_thermal_warning(temp_warning_c=65, temp_critical_c=80) is True

    def test_thermal_fault_at_critical(self) -> None:
        self._feed(temp=80)
        assert self.driver.has_thermal_fault(temp_critical_c=80) is True
        # critical 未満なら fault ではない
        self._feed(temp=79)
        assert self.driver.has_thermal_fault(temp_critical_c=80) is False

    def test_overcurrent_warning_above_threshold(self) -> None:
        # しきい値 18000 mA を超える電流で警告
        self._feed(current=18500)
        assert self.driver.has_overcurrent_warning() is True

    def test_overcurrent_warning_negative_above_threshold(self) -> None:
        # 逆方向の電流暴走も検出 (絶対値判定)
        self._feed(current=-19000)
        assert self.driver.has_overcurrent_warning() is True

    def test_overcurrent_warning_within_limit(self) -> None:
        self._feed(current=15000)
        assert self.driver.has_overcurrent_warning() is False

    def test_is_fault_default_false(self) -> None:
        # M3508 には明示的な fault フラグがないので常に False
        self._feed(temp=200, current=20000)
        assert self.driver.is_fault() is False


class TestMotorCheck:
    """アクチュエータ動作確認 API (Phase 6 段階⑦)。"""

    def setup_method(self) -> None:
        self.driver = M3508Driver("test_motor", can_id=1)

    def _feed(self, *, velocity: int, current: int = 0, temp: int = 25) -> None:
        # M3508 フィードバックの velocity 符号は電流符号と一致する想定
        data = struct.pack(">hhhBB", 0, velocity, current, temp, 0)
        msg = can.Message(arbitration_id=0x201, data=data, is_extended_id=False)
        self.driver.update_state(msg)

    def test_check_command_uses_specified_magnitude(self) -> None:
        msg, context = self.driver.check_command(magnitude=500.0)
        assert msg.arbitration_id == 0x200
        assert msg.is_extended_id is False
        values = struct.unpack(">hhhh", msg.data)
        # can_id=1 → スロット 0 に 500 mA 投入
        assert values[0] == 500
        assert values[1] == 0
        assert context["target"] == pytest.approx(500.0)
        assert context["mode"] == "current"

    def test_check_command_negative_magnitude(self) -> None:
        msg, context = self.driver.check_command(magnitude=-500.0)
        values = struct.unpack(">hhhh", msg.data)
        assert values[0] == -500
        assert context["target"] == pytest.approx(-500.0)

    def test_evaluate_passed_when_velocity_sign_matches(self) -> None:
        _, context = self.driver.check_command(magnitude=500.0)
        # 電流指令と同符号の rpm がフィードバック → PASSED
        self._feed(velocity=300)
        passed, detail = self.driver.evaluate_check_result(self.driver.state, context)
        assert passed is True
        assert detail is None

    def test_evaluate_failed_when_velocity_sign_mismatch(self) -> None:
        _, context = self.driver.check_command(magnitude=500.0)
        # 電流指令は正だが rpm が逆方向 → FAILED
        self._feed(velocity=-300)
        passed, detail = self.driver.evaluate_check_result(self.driver.state, context)
        assert passed is False
        assert detail is not None

    def test_evaluate_failed_when_velocity_near_zero(self) -> None:
        _, context = self.driver.check_command(magnitude=500.0)
        # |rpm| < 50 は「回転検出なし」
        self._feed(velocity=10)
        passed, detail = self.driver.evaluate_check_result(self.driver.state, context)
        assert passed is False
        assert detail is not None
        assert "回転" in detail

    def test_reset_after_check_sends_zero_current(self) -> None:
        msg = self.driver.reset_after_check()
        values = struct.unpack(">hhhh", msg.data)
        assert values[0] == 0
        assert values[1] == 0
        assert values[2] == 0
        assert values[3] == 0


class TestMultiTurn:
    """多回転累積角 (リフト軸の位置制御用)。"""

    def setup_method(self) -> None:
        self.driver = M3508Driver("lift", can_id=1)

    def _feed_angle(self, angle_raw: int) -> None:
        data = struct.pack(">HhhBB", angle_raw, 0, 0, 25, 0)
        msg = can.Message(arbitration_id=0x201, data=data, is_extended_id=False)
        self.driver.update_state(msg)

    @staticmethod
    def _deg(counts: float) -> float:
        return counts / 8192 * 360.0

    def test_initial_position_is_zero_before_any_feedback(self) -> None:
        assert self.driver.multi_turn_position == pytest.approx(0.0)

    def test_first_feedback_becomes_origin(self) -> None:
        # 起動位置を原点にすることで、目標 0 が「電源投入時の姿勢維持」を意味する
        self._feed_angle(3000)
        assert self.driver.multi_turn_position == pytest.approx(0.0)

    def test_accumulates_forward_within_one_turn(self) -> None:
        self._feed_angle(1000)
        self._feed_angle(3048)
        assert self.driver.multi_turn_position == pytest.approx(self._deg(2048), abs=1e-6)

    def test_accumulates_backward_within_one_turn(self) -> None:
        self._feed_angle(3048)
        self._feed_angle(1000)
        assert self.driver.multi_turn_position == pytest.approx(self._deg(-2048), abs=1e-6)

    def test_wraparound_forward(self) -> None:
        # 8000 → 200 は 0 を跨いだ正転 (+392 counts)
        self._feed_angle(8000)
        self._feed_angle(200)
        assert self.driver.multi_turn_position == pytest.approx(self._deg(392), abs=1e-6)

    def test_wraparound_backward(self) -> None:
        # 200 → 8000 は 0 を跨いだ逆転 (-392 counts)
        self._feed_angle(200)
        self._feed_angle(8000)
        assert self.driver.multi_turn_position == pytest.approx(self._deg(-392), abs=1e-6)

    def test_multiple_revolutions_forward(self) -> None:
        self._feed_angle(0)
        for raw in (2000, 4000, 6000, 8000, 1808, 3808):
            self._feed_angle(raw)
        # 2000 counts x 6 = 12000 counts (1 回転と少し)
        assert self.driver.multi_turn_position == pytest.approx(self._deg(12000), abs=1e-6)

    def test_multiple_revolutions_backward(self) -> None:
        self._feed_angle(0)
        for raw in (6192, 4192, 2192, 192, 6384):
            self._feed_angle(raw)
        assert self.driver.multi_turn_position == pytest.approx(self._deg(-10000), abs=1e-6)

    def test_reset_origin_makes_current_position_zero(self) -> None:
        self._feed_angle(1000)
        self._feed_angle(5096)
        assert self.driver.multi_turn_position != pytest.approx(0.0)

        self.driver.reset_multi_turn_origin()
        assert self.driver.multi_turn_position == pytest.approx(0.0)

        # 原点リセット後も累積は継続する
        self._feed_angle(6096)
        assert self.driver.multi_turn_position == pytest.approx(self._deg(1000), abs=1e-6)

    def test_single_turn_position_still_reported_in_degrees(self) -> None:
        # decode_feedback の position は 0〜360 のまま (既存 API の互換性)
        self._feed_angle(4096)
        assert self.driver.state.position == pytest.approx(4096 / 8191 * 360, abs=0.1)


class TestMultiTurnTargetReached:
    """到達判定が多回転累積角基準で行われること (位置制御ループと同じ次元)。"""

    def setup_method(self) -> None:
        self.driver = M3508Driver("lift", can_id=1)

    def _feed(self, angle_raw: int, *, velocity: int = 0, current: int = 0) -> None:
        data = struct.pack(">HhhBB", angle_raw, velocity, current, 25, 0)
        msg = can.Message(arbitration_id=0x201, data=data, is_extended_id=False)
        self.driver.update_state(msg)

    def _spin_two_turns(self) -> None:
        """累積角をちょうど +720deg (16384 counts) にし、単回転角は 0 に戻す。"""
        for raw in (0, 2048, 4096, 6144, 0, 2048, 4096, 6144, 0):
            self._feed(raw)
        assert self.driver.multi_turn_position == pytest.approx(720.0)
        assert self.driver.state.position == pytest.approx(0.0)

    def test_reached_at_multi_turn_target(self) -> None:
        self._spin_two_turns()
        assert self.driver.is_target_reached(720.0, ControlMode.POSITION) is True

    def test_wrapped_angle_matching_target_is_not_reached(self) -> None:
        # 累積 720deg の時点で単回転角は 0deg。ラップ角で判定すると誤って到達扱いになる
        self._spin_two_turns()
        assert self.driver.is_target_reached(0.0, ControlMode.POSITION) is False

    def test_not_reached_before_finishing_the_turns(self) -> None:
        for raw in (0, 2048, 4096, 6144, 0):
            self._feed(raw)
        assert self.driver.multi_turn_position == pytest.approx(360.0)
        assert self.driver.is_target_reached(720.0, ControlMode.POSITION) is False

    def test_negative_multi_turn_target(self) -> None:
        for raw in (0, 6144, 4096, 2048, 0, 6144, 4096, 2048, 0):
            self._feed(raw)
        assert self.driver.multi_turn_position == pytest.approx(-720.0)
        assert self.driver.is_target_reached(-720.0, ControlMode.POSITION) is True
        assert self.driver.is_target_reached(720.0, ControlMode.POSITION) is False

    def test_explicit_tolerance_boundary(self) -> None:
        self._spin_two_turns()
        assert self.driver.is_target_reached(730.0, ControlMode.POSITION, tolerance=10.0) is True
        assert self.driver.is_target_reached(730.1, ControlMode.POSITION, tolerance=10.0) is False

    def test_default_tolerance_is_one_degree_at_output_shaft(self) -> None:
        # フィードバックはモータ軸基準なので、既定許容差も減速比分だけ広げる
        assert self.driver.default_tolerance(ControlMode.POSITION) == pytest.approx(GEAR_RATIO)

    def test_default_tolerance_applies_to_multi_turn_error(self) -> None:
        self._spin_two_turns()
        inside = 720.0 + GEAR_RATIO * 0.9
        outside = 720.0 + GEAR_RATIO * 1.1
        assert self.driver.is_target_reached(inside, ControlMode.POSITION) is True
        assert self.driver.is_target_reached(outside, ControlMode.POSITION) is False

    def test_reached_after_origin_reset(self) -> None:
        self._spin_two_turns()
        self.driver.reset_multi_turn_origin()
        assert self.driver.is_target_reached(0.0, ControlMode.POSITION) is True
        assert self.driver.is_target_reached(720.0, ControlMode.POSITION) is False

    def test_current_mode_is_always_reached(self) -> None:
        # 開ループ指令なので目標電流とフィードバックの一致は問わない
        self._spin_two_turns()
        self._feed(0, current=0)
        assert self.driver.is_target_reached(5000.0, ControlMode.CURRENT) is True

    def test_velocity_mode_still_uses_feedback_rpm(self) -> None:
        self._feed(0, velocity=1000)
        assert self.driver.is_target_reached(1002.0, ControlMode.VELOCITY) is True
        assert self.driver.is_target_reached(1020.0, ControlMode.VELOCITY) is False


class TestFeedbackPosition:
    """偏差監視 (左右直結軸) が使う共通 API が多回転累積角を返すこと。"""

    def setup_method(self) -> None:
        self.driver = M3508Driver("lift", can_id=1)

    def _feed_angle(self, angle_raw: int) -> None:
        data = struct.pack(">HhhBB", angle_raw, 0, 0, 25, 0)
        msg = can.Message(arbitration_id=0x201, data=data, is_extended_id=False)
        self.driver.update_state(msg)

    def test_returns_multi_turn_position(self) -> None:
        self._feed_angle(0)
        self._feed_angle(2048)
        assert self.driver.feedback_position() == pytest.approx(self.driver.multi_turn_position)
        assert self.driver.feedback_position() == pytest.approx(90.0)

    def test_continuous_across_wraparound(self) -> None:
        # 単回転角は 0 に戻るが、偏差監視では連続した値でなければならない
        for raw in (0, 2048, 4096, 6144, 0):
            self._feed_angle(raw)
        assert self.driver.state.position == pytest.approx(0.0)
        assert self.driver.feedback_position() == pytest.approx(360.0)

    def test_negative_direction_is_continuous(self) -> None:
        for raw in (0, 6144, 4096, 2048, 0):
            self._feed_angle(raw)
        assert self.driver.feedback_position() == pytest.approx(-360.0)
