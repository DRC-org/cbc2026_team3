from __future__ import annotations

import math

import pytest

from lib.control.trajectory import TrapezoidalProfile

V_MAX = 60.0
A_MAX = 400.0
DT = 0.005

TRIANGLE_BOUNDARY = V_MAX**2 / A_MAX

TRAVEL_DISTANCES = [0.5, 1.5, 5.0, 15.0, 50.0]


def _profile(*, max_velocity: float = V_MAX, max_acceleration: float = A_MAX) -> TrapezoidalProfile:
    return TrapezoidalProfile(max_velocity=max_velocity, max_acceleration=max_acceleration)


def _run(profile: TrapezoidalProfile, ticks: int, dt: float = DT) -> list[tuple[float, float]]:
    return [profile.advance(dt) for _ in range(ticks)]


def _ticks_to_settle(profile: TrapezoidalProfile, dt: float, limit: int = 100_000) -> int:
    for tick in range(1, limit + 1):
        profile.advance(dt)
        if profile.done:
            return tick
    raise AssertionError("プロファイルが収束しませんでした")


class TestConstruction:
    @pytest.mark.parametrize("bad", [0.0, -1.0])
    def test_rejects_non_positive_max_velocity(self, bad: float) -> None:
        with pytest.raises(ValueError):
            _profile(max_velocity=bad)

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    def test_rejects_non_positive_max_acceleration(self, bad: float) -> None:
        with pytest.raises(ValueError):
            _profile(max_acceleration=bad)

    def test_starts_at_rest_and_done(self) -> None:
        profile = _profile()
        assert profile.position == 0.0
        assert profile.velocity == 0.0
        assert profile.done


class TestReset:
    def test_reset_sticks_to_the_given_position_at_rest(self) -> None:
        profile = _profile()
        profile.retarget(20.0)
        _run(profile, 20)

        profile.reset(3.25)

        assert profile.position == 3.25
        assert profile.velocity == 0.0
        assert profile.done
        assert profile.advance(DT) == (3.25, 0.0)

    def test_retarget_keeps_the_current_velocity(self) -> None:
        profile = _profile()
        profile.retarget(50.0)
        _run(profile, 20)
        moving = profile.velocity
        assert moving > 0.0

        profile.retarget(40.0)

        assert profile.velocity == moving


class TestVelocityLimit:
    @pytest.mark.parametrize("distance", TRAVEL_DISTANCES)
    def test_velocity_never_exceeds_the_limit(self, distance: float) -> None:
        profile = _profile()
        profile.retarget(distance)
        for _, velocity in _run(profile, 2000):
            assert abs(velocity) <= V_MAX + 1e-9

    def test_velocity_limit_holds_in_the_negative_direction(self) -> None:
        profile = _profile()
        profile.retarget(-50.0)
        for _, velocity in _run(profile, 2000):
            assert abs(velocity) <= V_MAX + 1e-9

    def test_long_move_actually_reaches_the_velocity_limit(self) -> None:
        profile = _profile()
        profile.retarget(50.0)
        peak = max(abs(v) for _, v in _run(profile, 2000))
        assert peak == pytest.approx(V_MAX, rel=1e-3)


class TestAccelerationLimit:
    @pytest.mark.parametrize("distance", TRAVEL_DISTANCES)
    def test_velocity_step_is_bounded(self, distance: float) -> None:
        profile = _profile()
        profile.retarget(distance)
        previous = 0.0
        for _, velocity in _run(profile, 2000):
            assert abs(velocity - previous) <= A_MAX * DT + 1e-9
            previous = velocity

    def test_bound_scales_with_dt(self) -> None:
        profile = _profile()
        profile.retarget(50.0)
        dt = 0.02
        previous = 0.0
        for _, velocity in _run(profile, 500, dt=dt):
            assert abs(velocity - previous) <= A_MAX * dt + 1e-9
            previous = velocity


class TestConvergence:
    @pytest.mark.parametrize("distance", TRAVEL_DISTANCES)
    def test_settles_exactly_on_the_target(self, distance: float) -> None:
        profile = _profile()
        profile.retarget(distance)
        _run(profile, 4000)
        assert profile.position == distance
        assert profile.velocity == 0.0
        assert profile.done

    def test_settles_from_a_non_zero_start(self) -> None:
        profile = _profile()
        profile.reset(-12.5)
        profile.retarget(7.5)
        _run(profile, 4000)
        assert profile.position == 7.5
        assert profile.velocity == 0.0


class TestNoOvershoot:
    @pytest.mark.parametrize("distance", TRAVEL_DISTANCES)
    def test_forward_move_never_crosses_the_target(self, distance: float) -> None:
        profile = _profile()
        profile.retarget(distance)
        previous = 0.0
        for position, _ in _run(profile, 4000):
            assert position <= distance
            assert position >= previous
            previous = position
        assert previous == distance

    @pytest.mark.parametrize("distance", TRAVEL_DISTANCES)
    def test_backward_move_never_crosses_the_target(self, distance: float) -> None:
        profile = _profile()
        profile.retarget(-distance)
        previous = 0.0
        for position, _ in _run(profile, 4000):
            assert position >= -distance
            assert position <= previous
            previous = position
        assert previous == -distance

    @pytest.mark.parametrize("distance", TRAVEL_DISTANCES)
    def test_velocity_never_reverses_during_a_move(self, distance: float) -> None:
        profile = _profile()
        profile.retarget(distance)
        for _, velocity in _run(profile, 4000):
            assert velocity >= 0.0

    @pytest.mark.parametrize("dt", [0.001, 0.005, 0.02])
    def test_holds_for_coarser_control_periods(self, dt: float) -> None:
        profile = _profile()
        profile.retarget(15.0)
        for position, _ in _run(profile, 20_000, dt=dt):
            assert position <= 15.0
        assert profile.position == 15.0


class TestTerminalBehaviour:
    def test_stays_pinned_after_arrival(self) -> None:
        profile = _profile()
        profile.retarget(5.0)
        _run(profile, 1000)
        assert profile.done

        for position, velocity in _run(profile, 400):
            assert position == 5.0
            assert velocity == 0.0

    def test_last_approach_does_not_oscillate(self) -> None:
        profile = _profile()
        profile.retarget(0.5)
        samples = _run(profile, 400)
        signs = {math.copysign(1.0, v) for _, v in samples if v != 0.0}
        assert signs == {1.0}


class TestRetarget:
    def test_forward_retarget_keeps_velocity_continuous(self) -> None:
        profile = _profile()
        profile.retarget(50.0)
        previous = 0.0
        for tick in range(2000):
            if tick == 40:
                profile.retarget(12.0)
            _, velocity = profile.advance(DT)
            assert abs(velocity - previous) <= A_MAX * DT + 1e-9
            previous = velocity
        assert profile.position == 12.0

    def test_reverse_retarget_keeps_velocity_continuous(self) -> None:
        profile = _profile()
        profile.retarget(50.0)
        previous = 0.0
        for tick in range(4000):
            if tick == 40:
                profile.retarget(-30.0)
            _, velocity = profile.advance(DT)
            assert abs(velocity - previous) <= A_MAX * DT + 1e-9
            previous = velocity
        assert profile.position == -30.0
        assert profile.velocity == 0.0

    def test_巡航中にすぐ手前へ再ターゲットしても目標を通り過ぎない(self) -> None:
        profile = _profile()
        profile.retarget(1000.0)
        position = 0.0
        velocity = 0.0
        for _ in range(2000):
            position, velocity = profile.advance(DT)
            if velocity >= V_MAX:
                break
        assert velocity == pytest.approx(V_MAX)

        target = position + 0.1
        profile.retarget(target)

        for _ in range(200):
            position, _velocity = profile.advance(DT)
            assert position <= target + 1e-9, "着地した目標を通り過ぎた"

        assert position == pytest.approx(target)

    def test_repeated_retarget_keeps_velocity_continuous(self) -> None:
        profile = _profile()
        previous = 0.0
        target = 0.0
        for tick in range(600):
            target += 0.4 if tick < 300 else -0.4
            profile.retarget(target)
            _, velocity = profile.advance(DT)
            assert abs(velocity - previous) <= A_MAX * DT + 1e-9
            assert abs(velocity) <= V_MAX + 1e-9
            previous = velocity


class TestNonPositiveDt:
    @pytest.mark.parametrize("bad_dt", [0.0, -0.005])
    def test_state_is_untouched(self, bad_dt: float) -> None:
        profile = _profile()
        profile.retarget(20.0)
        _run(profile, 30)
        position, velocity = profile.position, profile.velocity

        assert profile.advance(bad_dt) == (position, velocity)
        assert profile.position == position
        assert profile.velocity == velocity

    def test_next_normal_tick_continues_from_the_same_state(self) -> None:
        reference = _profile()
        reference.retarget(20.0)
        _run(reference, 30)
        expected = reference.advance(DT)

        profile = _profile()
        profile.retarget(20.0)
        _run(profile, 30)
        profile.advance(0.0)
        profile.advance(-1.0)

        assert profile.advance(DT) == expected


class TestProfileTiming:
    def test_trapezoidal_move_matches_theory(self) -> None:
        distance = 50.0
        assert distance > TRIANGLE_BOUNDARY
        expected = V_MAX / A_MAX + distance / V_MAX

        profile = _profile()
        profile.retarget(distance)
        elapsed = _ticks_to_settle(profile, DT) * DT

        assert elapsed == pytest.approx(expected, rel=0.03)

    def test_triangular_move_matches_theory(self) -> None:
        distance = 5.0
        assert distance < TRIANGLE_BOUNDARY
        expected = 2.0 * math.sqrt(distance / A_MAX)

        profile = _profile()
        profile.retarget(distance)
        samples = _run(profile, 4000)
        peak = max(v for _, v in samples)
        elapsed = next(i for i, (p, v) in enumerate(samples, start=1) if p == distance and v == 0.0)

        assert peak < V_MAX
        assert peak == pytest.approx(math.sqrt(A_MAX * distance), rel=0.05)
        assert elapsed * DT == pytest.approx(expected, rel=0.05)

    def test_timing_is_independent_of_the_control_period(self) -> None:
        expected = V_MAX / A_MAX + 50.0 / V_MAX
        for dt in (0.001, 0.005, 0.01):
            profile = _profile()
            profile.retarget(50.0)
            assert _ticks_to_settle(profile, dt) * dt == pytest.approx(expected, rel=0.05)
