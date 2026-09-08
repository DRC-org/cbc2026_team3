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

    def test_set_zero_on_start_appends_zero_command(self) -> None:
        drv = _driver(set_zero_on_start=True)

        codes = [bytes(msg.data)[-1] for msg, _ in drv.initialization_steps()]

        assert codes[-1] == 0xFE

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
