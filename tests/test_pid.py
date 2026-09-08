from __future__ import annotations

import math

import pytest

from lib.control.pid import PIDController

M3508_CURRENT_MIN = -16384.0
M3508_CURRENT_MAX = 16384.0


class TestProportional:
    def test_output_is_proportional_to_error(self) -> None:
        pid = PIDController(kp=2.0)
        assert pid.update(setpoint=10.0, measurement=4.0, dt=0.01) == pytest.approx(12.0)

    def test_negative_error_gives_negative_output(self) -> None:
        pid = PIDController(kp=2.0)
        assert pid.update(setpoint=0.0, measurement=5.0, dt=0.01) == pytest.approx(-10.0)

    def test_zero_error_gives_zero_output(self) -> None:
        pid = PIDController(kp=2.0)
        assert pid.update(setpoint=3.0, measurement=3.0, dt=0.01) == pytest.approx(0.0)


class TestIntegral:
    def test_integral_accumulates_error_over_time(self) -> None:
        pid = PIDController(kp=0.0, ki=10.0)
        first = pid.update(setpoint=1.0, measurement=0.0, dt=0.1)
        second = pid.update(setpoint=1.0, measurement=0.0, dt=0.1)
        assert first == pytest.approx(1.0)
        assert second == pytest.approx(2.0)

    def test_integral_respects_variable_dt(self) -> None:
        pid = PIDController(kp=0.0, ki=10.0)
        pid.update(setpoint=1.0, measurement=0.0, dt=0.1)
        assert pid.update(setpoint=1.0, measurement=0.0, dt=0.2) == pytest.approx(3.0)

    def test_integral_removes_steady_state_error(self) -> None:

        def simulate(pid: PIDController) -> float:
            position = 0.0
            for _ in range(2000):
                output = pid.update(setpoint=1.0, measurement=position, dt=0.01)
                position += (output - 0.4) * 0.01
            return position

        assert abs(simulate(PIDController(kp=0.5)) - 1.0) > 0.1
        assert simulate(PIDController(kp=0.5, ki=2.0)) == pytest.approx(1.0, abs=0.01)

    def test_integral_not_accumulated_when_ki_is_zero(self) -> None:
        pid = PIDController(kp=1.0, ki=0.0)
        for _ in range(100):
            pid.update(setpoint=1.0, measurement=0.0, dt=0.01)
        assert pid.integral == pytest.approx(0.0)


class TestDerivative:
    def test_derivative_opposes_measurement_change(self) -> None:
        pid = PIDController(kp=0.0, kd=1.0)
        pid.update(setpoint=0.0, measurement=0.0, dt=0.1)
        assert pid.update(setpoint=0.0, measurement=1.0, dt=0.1) == pytest.approx(-10.0)

    def test_no_derivative_on_first_update(self) -> None:
        pid = PIDController(kp=0.0, kd=1.0)
        assert pid.update(setpoint=0.0, measurement=5.0, dt=0.1) == pytest.approx(0.0)

    def test_derivative_damps_oscillation(self) -> None:
        def simulate(kd: float) -> float:
            pid = PIDController(kp=20.0, kd=kd)
            position = 0.0
            velocity = 0.0
            overshoot = 0.0
            for _ in range(500):
                output = pid.update(setpoint=1.0, measurement=position, dt=0.01)
                velocity += output * 0.01
                position += velocity * 0.01
                overshoot = max(overshoot, position - 1.0)
            return overshoot

        assert simulate(kd=2.0) < simulate(kd=0.0)


class TestDerivativeKick:
    def test_setpoint_step_does_not_spike_output(self) -> None:
        pid = PIDController(kp=1.0, kd=100.0)
        pid.update(setpoint=0.0, measurement=0.0, dt=0.01)
        output = pid.update(setpoint=1.0, measurement=0.0, dt=0.01)
        assert output == pytest.approx(1.0)


class TestOutputClamp:
    def test_output_clamped_to_limits(self) -> None:
        pid = PIDController(kp=1000.0, output_min=M3508_CURRENT_MIN, output_max=M3508_CURRENT_MAX)
        assert pid.update(setpoint=100.0, measurement=0.0, dt=0.01) == pytest.approx(
            M3508_CURRENT_MAX
        )
        assert pid.update(setpoint=-100.0, measurement=0.0, dt=0.01) == pytest.approx(
            M3508_CURRENT_MIN
        )

    def test_asymmetric_limits(self) -> None:
        pid = PIDController(kp=1.0, output_min=-2.0, output_max=5.0)
        assert pid.update(setpoint=100.0, measurement=0.0, dt=0.01) == pytest.approx(5.0)
        assert pid.update(setpoint=-100.0, measurement=0.0, dt=0.01) == pytest.approx(-2.0)

    def test_default_limits_are_unbounded(self) -> None:
        pid = PIDController(kp=1.0)
        assert pid.update(setpoint=1e9, measurement=0.0, dt=0.01) == pytest.approx(1e9)

    def test_invalid_limits_raise(self) -> None:
        with pytest.raises(ValueError, match="output_min"):
            PIDController(kp=1.0, output_min=5.0, output_max=-5.0)


class TestAntiWindup:
    def test_integral_frozen_while_saturated(self) -> None:
        pid = PIDController(kp=1.0, ki=100.0, output_min=-10.0, output_max=10.0)
        for _ in range(100):
            pid.update(setpoint=100.0, measurement=0.0, dt=0.01)
        assert pid.integral == pytest.approx(0.0)

    def test_recovers_immediately_after_constraint_released(self) -> None:
        pid = PIDController(kp=1.0, ki=100.0, output_min=-10.0, output_max=10.0)
        for _ in range(200):
            pid.update(setpoint=100.0, measurement=0.0, dt=0.01)
        assert pid.update(setpoint=100.0, measurement=200.0, dt=0.01) == pytest.approx(-10.0)

    def test_integral_still_grows_while_unsaturated(self) -> None:
        pid = PIDController(kp=1.0, ki=1.0, output_min=-100.0, output_max=100.0)
        pid.update(setpoint=1.0, measurement=0.0, dt=0.1)
        assert pid.integral == pytest.approx(0.1)

    def test_integral_can_unwind_out_of_saturation(self) -> None:
        pid = PIDController(kp=0.0, ki=1.0, output_min=-10.0, output_max=10.0)
        for _ in range(200):
            pid.update(setpoint=100.0, measurement=0.0, dt=0.1)
        saturated_integral = pid.integral
        pid.update(setpoint=0.0, measurement=100.0, dt=0.1)
        assert pid.integral < saturated_integral

    def test_integral_limit_caps_integral_contribution(self) -> None:
        pid = PIDController(kp=0.0, ki=2.0, integral_limit=5.0)
        for _ in range(1000):
            output = pid.update(setpoint=10.0, measurement=0.0, dt=0.01)
        assert output == pytest.approx(5.0)
        assert pid.integral == pytest.approx(2.5)

    def test_negative_integral_limit_raises(self) -> None:
        with pytest.raises(ValueError, match="integral_limit"):
            PIDController(kp=1.0, integral_limit=-1.0)


class TestDeadBand:
    def test_no_proportional_output_inside_dead_band(self) -> None:
        pid = PIDController(kp=10.0, dead_band=0.5)
        assert pid.update(setpoint=1.0, measurement=0.6, dt=0.01) == pytest.approx(0.0)

    def test_output_resumes_outside_dead_band(self) -> None:
        pid = PIDController(kp=10.0, dead_band=0.5)
        assert pid.update(setpoint=1.0, measurement=0.0, dt=0.01) == pytest.approx(10.0)

    def test_integral_frozen_inside_dead_band(self) -> None:
        pid = PIDController(kp=0.0, ki=10.0, dead_band=0.5)
        pid.update(setpoint=1.0, measurement=0.0, dt=0.1)
        held = pid.integral
        for _ in range(10):
            pid.update(setpoint=1.0, measurement=0.7, dt=0.1)
        assert pid.integral == pytest.approx(held)

    def test_integral_term_still_holds_load_inside_dead_band(self) -> None:
        pid = PIDController(kp=0.0, ki=10.0, dead_band=0.5)
        held = pid.update(setpoint=1.0, measurement=0.0, dt=0.1)
        assert held == pytest.approx(1.0)
        assert pid.update(setpoint=1.0, measurement=0.7, dt=0.1) == pytest.approx(held)

    def test_negative_dead_band_raises(self) -> None:
        with pytest.raises(ValueError, match="dead_band"):
            PIDController(kp=1.0, dead_band=-0.1)


class TestNonPositiveDt:
    def test_zero_dt_returns_previous_output(self) -> None:
        pid = PIDController(kp=1.0, ki=10.0, kd=1.0)
        previous = pid.update(setpoint=1.0, measurement=0.0, dt=0.1)
        assert pid.update(setpoint=5.0, measurement=3.0, dt=0.0) == pytest.approx(previous)

    def test_zero_dt_does_not_change_state(self) -> None:
        pid = PIDController(kp=1.0, ki=10.0, kd=1.0)
        pid.update(setpoint=1.0, measurement=0.0, dt=0.1)
        integral = pid.integral
        pid.update(setpoint=5.0, measurement=3.0, dt=0.0)
        assert pid.integral == pytest.approx(integral)
        assert pid.update(setpoint=1.0, measurement=0.0, dt=0.1) == pytest.approx(3.0)

    def test_negative_dt_returns_previous_output(self) -> None:
        pid = PIDController(kp=1.0)
        previous = pid.update(setpoint=1.0, measurement=0.0, dt=0.1)
        assert pid.update(setpoint=9.0, measurement=0.0, dt=-0.05) == pytest.approx(previous)

    def test_zero_dt_before_first_update_returns_zero(self) -> None:
        pid = PIDController(kp=1.0)
        assert pid.update(setpoint=1.0, measurement=0.0, dt=0.0) == pytest.approx(0.0)


class TestReset:
    def test_reset_clears_integral(self) -> None:
        pid = PIDController(kp=0.0, ki=10.0)
        for _ in range(10):
            pid.update(setpoint=1.0, measurement=0.0, dt=0.1)
        pid.reset()
        assert pid.integral == pytest.approx(0.0)
        assert pid.update(setpoint=1.0, measurement=0.0, dt=0.1) == pytest.approx(1.0)

    def test_reset_clears_derivative_history(self) -> None:
        pid = PIDController(kp=0.0, kd=1.0)
        pid.update(setpoint=0.0, measurement=0.0, dt=0.1)
        pid.reset()
        assert pid.update(setpoint=0.0, measurement=100.0, dt=0.1) == pytest.approx(0.0)

    def test_reset_clears_last_output(self) -> None:
        pid = PIDController(kp=1.0)
        pid.update(setpoint=1.0, measurement=0.0, dt=0.1)
        pid.reset()
        assert pid.last_output == pytest.approx(0.0)
        assert pid.update(setpoint=1.0, measurement=0.0, dt=0.0) == pytest.approx(0.0)


class TestAttributeAccess:
    def test_output_limits_are_read_at_each_update(self) -> None:
        pid = PIDController(kp=1000.0, output_min=M3508_CURRENT_MIN, output_max=M3508_CURRENT_MAX)
        pid.output_min = -2000.0
        pid.output_max = 2000.0
        assert pid.update(setpoint=100.0, measurement=0.0, dt=0.01) == pytest.approx(2000.0)
        assert pid.update(setpoint=-100.0, measurement=0.0, dt=0.01) == pytest.approx(-2000.0)

    def test_gains_are_read_at_each_update(self) -> None:
        pid = PIDController(kp=1.0)
        pid.kp = 3.0
        assert pid.update(setpoint=1.0, measurement=0.0, dt=0.01) == pytest.approx(3.0)

    def test_last_output_tracks_latest_result(self) -> None:
        pid = PIDController(kp=1.0, output_min=-2.0, output_max=2.0)
        output = pid.update(setpoint=100.0, measurement=0.0, dt=0.01)
        assert pid.last_output == pytest.approx(output)
        assert pid.last_output == pytest.approx(2.0)


class TestDefaults:
    def test_defaults_are_p_only_and_unbounded(self) -> None:
        pid = PIDController(kp=1.0)
        assert pid.ki == pytest.approx(0.0)
        assert pid.kd == pytest.approx(0.0)
        assert pid.output_min == -math.inf
        assert pid.output_max == math.inf


class TestFeedforward:
    def test_default_is_zero_and_changes_nothing(self) -> None:
        plain = PIDController(kp=2.0)
        explicit = PIDController(kp=2.0)

        assert plain.update(setpoint=10.0, measurement=4.0, dt=0.01) == pytest.approx(
            explicit.update(setpoint=10.0, measurement=4.0, dt=0.01, feedforward=0.0)
        )

    def test_feedforward_is_added_to_output(self) -> None:
        pid = PIDController(kp=2.0)

        assert pid.update(
            setpoint=10.0, measurement=4.0, dt=0.01, feedforward=5.0
        ) == pytest.approx(17.0)

    def test_output_is_clamped_including_feedforward(self) -> None:
        pid = PIDController(kp=2.0, output_min=-100.0, output_max=100.0)

        output = pid.update(setpoint=10.0, measurement=0.0, dt=0.01, feedforward=500.0)

        assert output == pytest.approx(100.0)

    def test_feedforward_survives_dead_band(self) -> None:
        pid = PIDController(kp=2.0, dead_band=5.0)

        output = pid.update(setpoint=1.0, measurement=0.0, dt=0.01, feedforward=30.0)

        assert output == pytest.approx(30.0)

    def test_integral_does_not_grow_while_saturated_by_feedforward(self) -> None:
        pid = PIDController(kp=1.0, ki=1.0, output_min=-1000.0, output_max=1000.0)

        for _ in range(5):
            pid.update(setpoint=10.0, measurement=0.0, dt=0.01, feedforward=1000.0)

        assert pid.integral == pytest.approx(0.0)

    def test_integral_grows_without_saturation(self) -> None:
        pid = PIDController(kp=1.0, ki=1.0, output_min=-1000.0, output_max=1000.0)

        for _ in range(5):
            pid.update(setpoint=10.0, measurement=0.0, dt=0.01, feedforward=0.0)

        assert pid.integral > 0.0
