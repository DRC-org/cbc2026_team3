from __future__ import annotations

import struct

import can
import pytest

from lib.drivers.base import ControlMode, MotorState
from lib.drivers.m3508 import GEAR_RATIO, M3508Driver
from tests.fake_clock import FakeClock
from tests.feedback_frames import feed_m3508


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
    def setup_method(self) -> None:
        self.driver = M3508Driver("test_motor", can_id=1)

    def _feed(self, *, current: int = 0, temp: int = 25) -> None:
        feed_m3508(self.driver, angle_raw=0, current=current, temp=temp)

    def test_thermal_warning_below_threshold(self) -> None:
        self._feed(temp=60)
        assert self.driver.has_thermal_warning(temp_warning_c=65) is False

    def test_thermal_warning_at_threshold(self) -> None:
        self._feed(temp=65)
        assert self.driver.has_thermal_warning(temp_warning_c=65) is True

    def test_thermal_fault_at_critical(self) -> None:
        self._feed(temp=80)
        assert self.driver.has_thermal_fault(temp_critical_c=80) is True
        self._feed(temp=79)
        assert self.driver.has_thermal_fault(temp_critical_c=80) is False

    def test_overcurrent_warning_above_threshold(self) -> None:
        self._feed(current=18500)
        assert self.driver.has_overcurrent_warning() is True

    def test_overcurrent_warning_negative_above_threshold(self) -> None:
        self._feed(current=-19000)
        assert self.driver.has_overcurrent_warning() is True

    def test_overcurrent_warning_within_limit(self) -> None:
        self._feed(current=15000)
        assert self.driver.has_overcurrent_warning() is False

    def test_is_fault_default_false(self) -> None:
        self._feed(temp=200, current=20000)
        assert self.driver.is_fault() is False


class TestMultiTurn:
    def setup_method(self) -> None:
        self.driver = M3508Driver("lift", can_id=1)

    def _feed_angle(self, angle_raw: int) -> None:
        feed_m3508(self.driver, angle_raw=angle_raw)

    @staticmethod
    def _deg(counts: float) -> float:
        return counts / 8192 * 360.0

    def test_initial_position_is_zero_before_any_feedback(self) -> None:
        assert self.driver.multi_turn_position == pytest.approx(0.0)

    def test_first_feedback_becomes_origin(self) -> None:
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
        self._feed_angle(8000)
        self._feed_angle(200)
        assert self.driver.multi_turn_position == pytest.approx(self._deg(392), abs=1e-6)

    def test_wraparound_backward(self) -> None:
        self._feed_angle(200)
        self._feed_angle(8000)
        assert self.driver.multi_turn_position == pytest.approx(self._deg(-392), abs=1e-6)

    def test_multiple_revolutions_forward(self) -> None:
        self._feed_angle(0)
        for raw in (2000, 4000, 6000, 8000, 1808, 3808):
            self._feed_angle(raw)
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

        self._feed_angle(6096)
        assert self.driver.multi_turn_position == pytest.approx(self._deg(1000), abs=1e-6)

    def test_single_turn_position_still_reported_in_degrees(self) -> None:
        self._feed_angle(4096)
        assert self.driver.state.position == pytest.approx(4096 / 8191 * 360, abs=0.1)


class TestMultiTurnTargetReached:
    def setup_method(self) -> None:
        self.driver = M3508Driver("lift", can_id=1)

    def _feed(self, angle_raw: int, *, velocity: int = 0, current: int = 0) -> None:
        feed_m3508(self.driver, angle_raw=angle_raw, rpm=velocity, current=current)

    def _spin_two_turns(self) -> None:
        for raw in (0, 2048, 4096, 6144, 0, 2048, 4096, 6144, 0):
            self._feed(raw)
        assert self.driver.multi_turn_position == pytest.approx(720.0)
        assert self.driver.state.position == pytest.approx(0.0)

    def test_reached_at_multi_turn_target(self) -> None:
        self._spin_two_turns()
        assert self.driver.is_target_reached(720.0, ControlMode.POSITION) is True

    def test_wrapped_angle_matching_target_is_not_reached(self) -> None:
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
        self._spin_two_turns()
        self._feed(0, current=0)
        assert self.driver.is_target_reached(5000.0, ControlMode.CURRENT) is True

    def test_velocity_mode_still_uses_feedback_rpm(self) -> None:
        self._feed(0, velocity=1000)
        assert self.driver.is_target_reached(1002.0, ControlMode.VELOCITY) is True
        assert self.driver.is_target_reached(1020.0, ControlMode.VELOCITY) is False


class TestFeedbackPosition:
    def setup_method(self) -> None:
        self.driver = M3508Driver("lift", can_id=1)

    def _feed_angle(self, angle_raw: int) -> None:
        feed_m3508(self.driver, angle_raw=angle_raw)

    def test_returns_multi_turn_position(self) -> None:
        self._feed_angle(0)
        self._feed_angle(2048)
        assert self.driver.feedback_position() == pytest.approx(self.driver.multi_turn_position)
        assert self.driver.feedback_position() == pytest.approx(90.0)

    def test_continuous_across_wraparound(self) -> None:
        for raw in (0, 2048, 4096, 6144, 0):
            self._feed_angle(raw)
        assert self.driver.state.position == pytest.approx(0.0)
        assert self.driver.feedback_position() == pytest.approx(360.0)

    def test_negative_direction_is_continuous(self) -> None:
        for raw in (0, 6144, 4096, 2048, 0):
            self._feed_angle(raw)
        assert self.driver.feedback_position() == pytest.approx(-360.0)


class TestWrapInferenceAcrossFeedbackGap:
    def setup_method(self) -> None:
        self.clock = FakeClock()
        self.driver = M3508Driver("y_axis_r", can_id=1, time_source=self.clock)

    def _feed(self, angle_raw: int, *, rpm: int = 0, after_s: float = 0.001) -> None:
        self.clock.advance(after_s)
        feed_m3508(self.driver, angle_raw=angle_raw, rpm=rpm)

    @staticmethod
    def _deg(counts: float) -> float:
        return counts / 8192 * 360.0

    def test_平常の1ms間隔では従来どおり折り返しを推定する(self) -> None:
        self._feed(8000)
        self._feed(100)

        assert self.driver.multi_turn_position == pytest.approx(self._deg(292), abs=1e-6)
        assert self.driver.origin_trusted is True
        assert self.driver.health_detail() is None

    def test_長い窓を跨いだ差分は折り返しを推定せず累積しない(self) -> None:
        self._feed(8000)
        self._feed(4108, after_s=1.0)

        assert self.driver.multi_turn_position == pytest.approx(0.0)
        assert self.driver.origin_trusted is False
        assert self.driver.reanchor_count == 1

    def test_再アンカーはヘルスの詳細として報告される(self) -> None:
        self._feed(8000)
        self._feed(4108, after_s=1.0)

        detail = self.driver.health_detail()
        assert detail is not None
        assert "原点" in detail

    def test_高速回転なら短い窓でも折り返しを推定しない(self) -> None:
        self._feed(8000, rpm=3000)
        self._feed(4108, rpm=3000, after_s=0.02)

        assert self.driver.origin_trusted is False

    def test_低速なら同じ窓でも折り返しを推定する(self) -> None:
        self._feed(8000, rpm=100)
        self._feed(100, rpm=100, after_s=0.02)

        assert self.driver.multi_turn_position == pytest.approx(self._deg(292), abs=1e-6)
        assert self.driver.origin_trusted is True

    def test_rpmが両端で0でも長すぎる窓は信じない(self) -> None:
        self._feed(8000, rpm=0)
        self._feed(4108, rpm=0, after_s=0.2)

        assert self.driver.origin_trusted is False

    def test_原点確定で信頼が戻る(self) -> None:
        self._feed(8000)
        self._feed(4108, after_s=1.0)
        assert self.driver.origin_trusted is False

        self.driver.reset_multi_turn_origin()

        assert self.driver.origin_trusted is True
        assert self.driver.health_detail() is None

    def test_受信復帰だけでは信頼は戻らない(self) -> None:
        self._feed(8000)
        self._feed(4108, after_s=1.0)

        for _ in range(50):
            self._feed(4108)

        assert self.driver.origin_trusted is False


class TestWrapInferenceWhenFramesAreDropped:
    CRUISE_RPM = 1834
    COUNTS_PER_MS = 250

    def setup_method(self) -> None:
        self.clock = FakeClock()
        self.driver = M3508Driver("y_axis_r", can_id=1, time_source=self.clock)
        self.stamp = 5000.0
        self.angle = 0

    def _feed(self, *, elapsed_ms: float, processed_after_ms: float) -> None:
        self.angle += int(self.COUNTS_PER_MS * elapsed_ms)
        self.stamp += elapsed_ms / 1000.0
        self.clock.advance(processed_after_ms / 1000.0)
        feed_m3508(
            self.driver,
            angle_raw=self.angle % 8192,
            rpm=self.CRUISE_RPM,
            timestamp=self.stamp,
        )

    def test_捨てられた窓は処理間隔が詰まっていても折り返しを推定しない(self) -> None:
        for _ in range(5):
            self._feed(elapsed_ms=1, processed_after_ms=1)
        before = self.driver.multi_turn_position

        self._feed(elapsed_ms=30, processed_after_ms=1)

        advanced = self.driver.multi_turn_position - before
        assert advanced == pytest.approx(0.0), (
            f"捨てられた窓に折り返し推定を当てている (累積角が {advanced:.1f}deg 動いた)"
        )
        assert self.driver.reanchor_count == 1
        assert self.driver.origin_trusted is False

    def test_取りこぼしの報告はヘルスに出る(self) -> None:
        self._feed(elapsed_ms=1, processed_after_ms=1)
        self._feed(elapsed_ms=30, processed_after_ms=1)

        detail = self.driver.health_detail()
        assert detail is not None
        assert "原点" in detail

    def test_滞留しているだけで取りこぼしが無ければ推定を続ける(self) -> None:
        self._feed(elapsed_ms=1, processed_after_ms=1)
        self._feed(elapsed_ms=1, processed_after_ms=30)

        assert self.driver.reanchor_count == 0
        assert self.driver.origin_trusted is True

    def test_時刻を持たないフレームでは処理時刻で測る(self) -> None:
        driver = M3508Driver("y_axis_r", can_id=1, time_source=self.clock)
        feed_m3508(driver, angle_raw=8000, rpm=self.CRUISE_RPM)
        self.clock.advance(1.0)
        feed_m3508(driver, angle_raw=4108, rpm=self.CRUISE_RPM)

        assert driver.origin_trusted is False

    def test_時刻が巻き戻ったら処理時刻へ落ちる(self) -> None:
        feed_m3508(self.driver, angle_raw=0, rpm=self.CRUISE_RPM, timestamp=5000.0)
        self.clock.advance(1.0)
        feed_m3508(self.driver, angle_raw=4108, rpm=self.CRUISE_RPM, timestamp=4999.0)

        assert self.driver.origin_trusted is False
