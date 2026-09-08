from __future__ import annotations

import struct

import can
import pytest

from lib.drivers.base import ControlMode
from lib.drivers.generic import CommandType, GenericDriver
from tests.feedback_frames import (
    feed_generic,
    feed_generic_info,
    generic_feedback,
    generic_info,
)

_RESERVED_BITS = 0xE0


class TestCanIdRange:
    @pytest.mark.parametrize("can_id", [0x01, 0x05, 0x7F, 0xFE])
    def test_ids_in_range_are_accepted(self, can_id: int):
        assert GenericDriver("m", can_id).can_id == can_id

    @pytest.mark.parametrize("can_id", [-1, 0x00, 0xFF, 0x100, 0x1FF])
    def test_ids_out_of_range_are_rejected(self, can_id: int):
        with pytest.raises(ValueError, match="can_id"):
            GenericDriver("m", can_id)


class TestBuildCanId:
    def test_build_can_id(self):
        assert GenericDriver.build_can_id(CommandType.E_STOP, 0x01) == 0x001
        assert GenericDriver.build_can_id(CommandType.SET_TARGET, 0x01) == 0x101
        assert GenericDriver.build_can_id(CommandType.SET_PARAM, 0x10) == 0x210
        assert GenericDriver.build_can_id(CommandType.FEEDBACK, 0x01) == 0x301
        assert GenericDriver.build_can_id(CommandType.E_STOP, 0xFF) == 0x0FF

    def test_e_stop_outranks_every_other_frame(self):
        dev = 0x7F
        estop = GenericDriver.build_can_id(CommandType.E_STOP, dev)
        for other in (
            CommandType.SET_TARGET,
            CommandType.SET_PARAM,
            CommandType.FEEDBACK,
            CommandType.INFO,
        ):
            assert estop < GenericDriver.build_can_id(other, dev)
        assert GenericDriver.build_can_id(CommandType.E_STOP, 0xFF) < GenericDriver.build_can_id(
            CommandType.SET_TARGET, 0x00
        )


class TestParseCanId:
    def test_parse_can_id(self):
        cmd, dev = GenericDriver.parse_can_id(0x101)
        assert cmd == CommandType.SET_TARGET
        assert dev == 0x01

        cmd, dev = GenericDriver.parse_can_id(0x301)
        assert cmd == CommandType.FEEDBACK
        assert dev == 0x01

        cmd, dev = GenericDriver.parse_can_id(0x0FF)
        assert cmd == CommandType.E_STOP
        assert dev == 0xFF


class TestEncodeTarget:
    def setup_method(self):
        self.drv = GenericDriver("test_motor", 0x01)

    def test_encode_target_position(self):
        msg = self.drv.encode_target(ControlMode.POSITION, 90.0)
        assert msg.arbitration_id == 0x101
        assert msg.is_extended_id is False
        assert len(msg.data) == 3
        assert msg.data[0] == 0
        assert struct.unpack_from("<h", msg.data, 1)[0] == 900

    def test_encode_target_duty(self):
        msg = self.drv.encode_target(ControlMode.DUTY, 0.75)
        assert msg.data[0] == 2
        assert struct.unpack_from("<h", msg.data, 1)[0] == 7500

    def test_encode_target_on_off(self):
        msg = self.drv.encode_target(ControlMode.ON_OFF, 1.0)
        assert msg.arbitration_id == 0x101
        assert len(msg.data) == 3
        assert msg.data[0] == 3
        assert struct.unpack_from("<h", msg.data, 1)[0] == 1

        msg = self.drv.encode_target(ControlMode.ON_OFF, 0.0)
        assert msg.data[0] == 3
        assert struct.unpack_from("<h", msg.data, 1)[0] == 0

    def test_encode_target_keeps_sign(self):
        msg = self.drv.encode_target(ControlMode.DUTY, -0.75)
        assert struct.unpack_from("<h", msg.data, 1)[0] == -7500

    def test_encode_target_saturates_instead_of_wrapping(self):
        msg = self.drv.encode_target(ControlMode.POSITION, 1e6)
        assert struct.unpack_from("<h", msg.data, 1)[0] == 32767

        msg = self.drv.encode_target(ControlMode.POSITION, -1e6)
        assert struct.unpack_from("<h", msg.data, 1)[0] == -32768

    def test_encode_target_rounds_instead_of_truncating(self):
        msg = self.drv.encode_target(ControlMode.POSITION, 5.55)
        assert struct.unpack_from("<h", msg.data, 1)[0] == 56

        msg = self.drv.encode_target(ControlMode.POSITION, -5.55)
        assert struct.unpack_from("<h", msg.data, 1)[0] == -56

        msg = self.drv.encode_target(ControlMode.DUTY, 0.3)
        assert struct.unpack_from("<h", msg.data, 1)[0] == 3000

    def test_encode_target_rejects_nan(self):
        msg = self.drv.encode_target(ControlMode.POSITION, float("nan"))
        assert struct.unpack_from("<h", msg.data, 1)[0] == 0


class TestDecodeFeedback:
    def setup_method(self):
        self.drv = GenericDriver("test_motor", 0x01)

    def test_decode_feedback(self):
        data = bytearray([0x00])
        data.extend(struct.pack("<h", 1800))

        msg = can.Message(arbitration_id=0x301, data=bytes(data), is_extended_id=False)
        state = self.drv.decode_feedback(msg)

        assert state.position == pytest.approx(180.0)
        assert state.reached is False

    def test_decode_feedback_without_position(self):
        msg = can.Message(arbitration_id=0x301, data=bytes([0x00]), is_extended_id=False)
        state = self.drv.decode_feedback(msg)
        assert state.position == pytest.approx(0.0)

    def test_decode_feedback_ignores_trailing_bytes(self):
        data = bytearray([0x00])
        data.extend(struct.pack("<h", 1800))
        data.extend(b"\xff\xff\xff")
        msg = can.Message(arbitration_id=0x301, data=bytes(data), is_extended_id=False)
        state = self.drv.decode_feedback(msg)
        assert state.position == pytest.approx(180.0)
        assert state.current == pytest.approx(0.0)
        assert state.temperature == pytest.approx(0.0)
        assert state.current == pytest.approx(0.0)
        assert state.temperature == pytest.approx(0.0)

    def test_decode_feedback_with_flags(self):
        data = bytearray([0b00000001])
        data.extend(struct.pack("<h", 0))

        msg = can.Message(arbitration_id=0x301, data=bytes(data), is_extended_id=False)
        assert self.drv.decode_feedback(msg).reached is True

        data[0] = 0b00000101
        msg = can.Message(arbitration_id=0x301, data=bytes(data), is_extended_id=False)
        assert self.drv.decode_feedback(msg).reached is True


class TestMatchesFeedback:
    def setup_method(self):
        self.drv = GenericDriver("test_motor", 0x01)

    def test_matches_feedback(self):
        msg = can.Message(arbitration_id=0x301, data=bytes(8), is_extended_id=False)
        assert self.drv.matches_feedback(msg) is True

    def test_matches_feedback_wrong_device(self):
        msg = can.Message(arbitration_id=0x302, data=bytes(3), is_extended_id=False)
        assert self.drv.matches_feedback(msg) is False

        msg_target = can.Message(arbitration_id=0x101, data=bytes(3), is_extended_id=False)
        assert self.drv.matches_feedback(msg_target) is False

    def test_状態フラグすら無いフレームは自分宛にしない(self):
        empty = can.Message(arbitration_id=0x301, data=b"", is_extended_id=False)

        assert self.drv.matches_feedback(empty) is False

    def test_状態フラグだけの_FEEDBACK_は自分宛として受ける(self):
        minimal = generic_feedback(self.drv)
        assert len(minimal.data) == 1

        assert self.drv.matches_feedback(minimal) is True

    def test_短すぎる_INFO_も自分宛にしない(self):
        short = can.Message(arbitration_id=0x401, data=bytes(2), is_extended_id=False)

        assert self.drv.matches_info(short) is False
        exact = can.Message(arbitration_id=0x401, data=bytes(3), is_extended_id=False)
        assert self.drv.matches_info(exact) is True


class TestEncodeEStop:
    def test_encode_e_stop(self):
        msg = GenericDriver.encode_e_stop()
        assert msg.arbitration_id == 0x0FF
        assert msg.is_extended_id is False
        assert msg.data == bytes(3)


class TestHealth:
    def setup_method(self):
        self.drv = GenericDriver("test_motor", 0x01)

    def _feed(self, **kwargs: object) -> None:
        feed_generic(self.drv, **kwargs)  # type: ignore[arg-type]

    def test_initial_flags_are_clear(self):
        assert self.drv.has_overcurrent_warning() is False
        assert self.drv.is_fault() is False

    def test_reserved_sensor_bytes_never_raise_warnings(self):
        feed_generic(self.drv, position=0.0, flags=_RESERVED_BITS, reserved=b"\xff\xff\xff")
        assert self.drv.has_overcurrent_warning() is False
        assert self.drv.has_thermal_warning(temp_warning_c=65.0) is False
        assert self.drv.has_thermal_fault(temp_critical_c=80.0) is False
        assert self.drv.is_fault() is False

    def test_sensor_and_reserved_bits_do_not_affect_health(self):
        self._feed(sensor=True, flags=_RESERVED_BITS)
        assert self.drv.has_overcurrent_warning() is False
        assert self.drv.is_fault() is False


class TestSensorInput:
    def setup_method(self):
        self.drv = GenericDriver("origin_sensor", 0x02)

    def _feed(self, **kwargs: object) -> None:
        feed_generic(self.drv, **kwargs)  # type: ignore[arg-type]

    def test_initially_inactive(self):
        assert self.drv.sensor_active is False

    def test_sensor_bit_reports_input(self):
        self._feed(sensor=True)
        assert self.drv.sensor_active is True

    def test_clears_when_released(self):
        self._feed(sensor=True)
        self._feed()
        assert self.drv.sensor_active is False

    def test_reserved_bits_are_not_the_sensor(self):
        self._feed(flags=_RESERVED_BITS)
        assert self.drv.sensor_active is False

    def test_contact_is_not_an_abnormality(self):
        self._feed(sensor=True)
        assert self.drv.is_fault() is False
        assert self.drv.has_overcurrent_warning() is False

    def test_latch_keeps_a_contact_that_is_already_over(self):
        self._feed(sensor=True)
        self._feed()

        assert self.drv.sensor_active is False
        assert self.drv.consume_sensor_latch() is True

    def test_latch_clears_on_read(self):
        self._feed(sensor=True)
        self._feed()

        assert self.drv.consume_sensor_latch() is True
        assert self.drv.consume_sensor_latch() is False

    def test_latch_never_answers_weaker_than_the_current_state(self):
        self._feed(sensor=True)

        assert self.drv.consume_sensor_latch() is True
        assert self.drv.consume_sensor_latch() is True

    def test_latch_does_not_disturb_the_current_state(self):
        self._feed(sensor=True)
        self.drv.consume_sensor_latch()

        assert self.drv.sensor_active is True

    def test_does_not_disturb_other_flags(self):
        self._feed(e_stop=True, watchdog=True, sensor=True)
        assert self.drv.sensor_active is True
        assert self.drv.e_stop_active is True
        assert self.drv.watchdog_active is True
        assert self.drv.device_id_unconfigured is False


class TestMatchesFeedbackRobustness:
    def setup_method(self):
        self.drv = GenericDriver("test_motor", 0x01)

    @pytest.mark.parametrize("command_type", [0b100, 0b101, 0b110])
    def test_reserved_command_type_returns_false(self, command_type: int):
        msg = can.Message(
            arbitration_id=(command_type << 8) | 0x01,
            data=bytes(8),
            is_extended_id=False,
        )
        assert self.drv.matches_feedback(msg) is False

    def test_extended_frame_returns_false(self):
        msg = can.Message(arbitration_id=0x301, data=bytes(8), is_extended_id=True)
        assert self.drv.matches_feedback(msg) is False

    def test_try_parse_can_id_returns_none_for_reserved(self):
        assert GenericDriver.try_parse_can_id((0b101 << 8) | 0x01) is None
        assert GenericDriver.try_parse_can_id(0x301) == (CommandType.FEEDBACK, 0x01)

    def test_parse_can_id_still_raises_for_reserved(self):
        with pytest.raises(ValueError):
            GenericDriver.parse_can_id((0b101 << 8) | 0x01)


class TestEncodeEStopClear:
    def test_encode_e_stop_clear_broadcast(self):
        msg = GenericDriver.encode_e_stop_clear()
        assert msg.arbitration_id == 0x0FF
        assert msg.is_extended_id is False
        assert msg.data[0] == 0x01
        assert msg.data[1] == 0x5A
        assert msg.data[2] == 0xA5
        assert len(msg.data) == 3

    def test_encode_e_stop_clear_specific_device(self):
        msg = GenericDriver.encode_e_stop_clear(0x03)
        assert msg.arbitration_id == GenericDriver.build_can_id(CommandType.E_STOP, 0x03)
        assert msg.data[0] == 0x01

    def test_encode_e_stop_is_unchanged(self):
        msg = GenericDriver.encode_e_stop()
        assert msg.arbitration_id == 0x0FF
        assert msg.data == bytes(3)


class TestActivationSteps:
    def test_activation_steps_sends_e_stop_clear_to_own_device(self):
        drv = GenericDriver("test_motor", 0x05)
        steps = drv.activation_steps()

        assert len(steps) == 1
        msg, delay = steps[0]
        assert msg.arbitration_id == GenericDriver.build_can_id(CommandType.E_STOP, 0x05)
        assert msg.data[0] == 0x01
        assert msg.data[1] == 0x5A
        assert msg.data[2] == 0xA5
        assert delay == pytest.approx(0.0)

    def test_activation_does_not_require_fresh_feedback(self):
        drv = GenericDriver("test_motor", 0x05)
        assert drv.requires_fresh_feedback_for_activation() is False


class TestSafetyStatusFlags:
    def setup_method(self):
        self.drv = GenericDriver("test_motor", 0x01)

    def _feed(self, **kwargs: object) -> None:
        feed_generic(self.drv, **kwargs)  # type: ignore[arg-type]

    def test_initial_flags_are_clear(self):
        assert self.drv.e_stop_active is False
        assert self.drv.watchdog_active is False
        assert self.drv.device_id_unconfigured is False

    def test_e_stop_flag(self):
        self._feed(e_stop=True)
        assert self.drv.e_stop_active is True
        assert self.drv.is_fault() is False

    def test_watchdog_flag(self):
        self._feed(watchdog=True)
        assert self.drv.watchdog_active is True
        assert self.drv.is_fault() is False

    def test_unconfigured_device_id_is_fault(self):
        self._feed(unconfigured_id=True)
        assert self.drv.device_id_unconfigured is True
        assert self.drv.is_fault() is True

        self._feed()
        assert self.drv.is_fault() is False

    def test_flags_clear_on_recovery(self):
        self._feed(e_stop=True, watchdog=True, unconfigured_id=True)
        assert self.drv.is_fault() is True

        self._feed()
        assert self.drv.e_stop_active is False
        assert self.drv.watchdog_active is False
        assert self.drv.device_id_unconfigured is False
        assert self.drv.is_fault() is False


class TestInfoFrame:
    def test_decodes_servo_range(self):
        driver = GenericDriver("gripper", 0x40)
        feed_generic_info(driver, firmware_version=2, angle_range_deg=270.0)

        assert driver.info is not None
        assert driver.info.firmware_version == 2
        assert driver.info.angle_range_deg == pytest.approx(270.0)

    def test_absent_range_is_none_not_zero(self):
        driver = GenericDriver("conveyor", 0x80, control_type=ControlMode.DUTY)
        feed_generic_info(driver, firmware_version=1, board_kind=2)

        assert driver.info is not None
        assert driver.info.angle_range_deg is None

    def test_no_info_yet_is_none(self):
        assert GenericDriver("gripper", 0x40).info is None

    def test_matches_only_own_info_frames(self):
        driver = GenericDriver("gripper", 0x40)
        other = GenericDriver("wall_f", 0x41)

        assert driver.matches_info(generic_info(driver)) is True
        assert driver.matches_info(generic_feedback(driver)) is False
        assert driver.matches_info(generic_info(other)) is False

    def test_info_frame_is_not_feedback(self):
        driver = GenericDriver("gripper", 0x40)
        assert driver.matches_feedback(generic_info(driver)) is False


class TestFirmwareConfirmed:
    def test_before_any_info_is_false(self):
        driver = GenericDriver("gripper", 0x40)
        assert driver.firmware_confirmed() is False

    def test_after_info_is_true(self):
        driver = GenericDriver("gripper", 0x40)
        feed_generic_info(driver, firmware_version=1)
        assert driver.firmware_confirmed() is True


class TestInfoMismatch:
    def _servo(self) -> GenericDriver:
        return GenericDriver("gripper", 0x40, expected_firmware=2, expected_angle_range_deg=270.0)

    def test_matching_info_is_not_fault(self):
        driver = self._servo()
        feed_generic_info(driver, firmware_version=2, angle_range_deg=270.0)

        assert driver.info_mismatch is None
        assert driver.is_fault() is False

    def test_wrong_angle_range_is_fault(self):
        driver = self._servo()
        feed_generic_info(driver, firmware_version=2, angle_range_deg=180.0)

        assert driver.info_mismatch is not None
        assert "180" in driver.info_mismatch
        assert driver.is_fault() is True

    def test_wrong_firmware_is_fault(self):
        driver = self._servo()
        feed_generic_info(driver, firmware_version=1, angle_range_deg=270.0)

        assert driver.info_mismatch is not None
        assert driver.is_fault() is True

    def test_missing_range_is_fault_when_expected(self):
        driver = self._servo()
        feed_generic_info(driver, firmware_version=2)

        assert driver.info_mismatch is not None
        assert driver.is_fault() is True

    def test_no_info_yet_is_not_fault(self):
        driver = self._servo()

        assert driver.info_mismatch is None
        assert driver.is_fault() is False

    def test_no_expectation_never_faults(self):
        driver = GenericDriver("gripper", 0x40)
        feed_generic_info(driver, firmware_version=99, angle_range_deg=1.0)

        assert driver.info_mismatch is None
        assert driver.is_fault() is False

    def test_small_rounding_difference_is_tolerated(self):
        driver = GenericDriver("gripper", 0x40, expected_angle_range_deg=270.04)
        feed_generic_info(driver, angle_range_deg=270.0)

        assert driver.info_mismatch is None


class TestTelemetrySupport:
    def test_servo_board_measures_position_only(self):
        driver = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)

        assert driver.telemetry.position is True
        assert driver.telemetry.velocity is False
        assert driver.telemetry.current is False
        assert driver.telemetry.temperature is False

    def test_dc_board_measures_nothing(self):
        driver = GenericDriver("conveyor", 0x80, control_type=ControlMode.DUTY)

        assert driver.telemetry.position is False
        assert driver.telemetry.velocity is False
        assert driver.telemetry.current is False
        assert driver.telemetry.temperature is False

    def test_solenoid_board_measures_nothing(self):
        driver = GenericDriver("valve_1", 0x81, control_type=ControlMode.ON_OFF)

        assert driver.telemetry.position is False
        assert driver.telemetry.temperature is False

    def test_position_stays_unmeasured_even_after_a_feedback_arrives(self):
        driver = GenericDriver("conveyor", 0x80, control_type=ControlMode.DUTY)
        feed_generic(driver, position=12.0)

        assert driver.state.position == pytest.approx(12.0)
        assert driver.telemetry.position is False

    def test_thermal_judgement_is_skipped_when_temperature_is_unmeasured(self):
        driver = GenericDriver("conveyor", 0x80, control_type=ControlMode.DUTY)

        assert driver.has_thermal_warning(0.0) is False
        assert driver.has_thermal_fault(0.0) is False
