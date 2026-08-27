from __future__ import annotations

import math

import pytest

from lib.drivers.base import ControlMode
from lib.drivers.edulite05 import Edulite05Driver
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import M3508Driver
from tests.fake_drivers import StubFeedbackDriver
from tests.feedback_frames import feed_edulite, feed_generic, feed_m3508


class TestDefaultTolerance:
    def test_position_default_is_one_degree(self) -> None:
        driver = GenericDriver("g", 1)
        assert driver.default_tolerance(ControlMode.POSITION) == 1.0

    def test_velocity_default_is_five_rpm(self) -> None:
        driver = GenericDriver("g", 1)
        assert driver.default_tolerance(ControlMode.VELOCITY) == 5.0

    def test_open_loop_modes_have_no_finite_tolerance(self) -> None:
        driver = GenericDriver("g", 1)
        assert math.isinf(driver.default_tolerance(ControlMode.CURRENT))
        assert math.isinf(driver.default_tolerance(ControlMode.DUTY))

    def test_edulite_position_tolerance_is_radians(self) -> None:
        driver = Edulite05Driver("e", 1)
        assert driver.default_tolerance(ControlMode.POSITION) == math.radians(1.0)

    def test_edulite_velocity_tolerance_is_rad_per_sec(self) -> None:
        driver = Edulite05Driver("e", 1)
        assert driver.default_tolerance(ControlMode.VELOCITY) == pytest.approx(
            5.0 * 2.0 * math.pi / 60.0
        )


class TestBaseIsTargetReached:
    def test_position_within_tolerance(self) -> None:
        driver = StubFeedbackDriver("b", 1)
        driver.set_observed(position=10.5)
        assert driver.is_target_reached(10.0, ControlMode.POSITION) is True

    def test_position_outside_tolerance(self) -> None:
        driver = StubFeedbackDriver("b", 1)
        driver.set_observed(position=15.0)
        assert driver.is_target_reached(10.0, ControlMode.POSITION) is False

    def test_velocity_within_tolerance(self) -> None:
        driver = StubFeedbackDriver("b", 1)
        driver.set_observed(velocity=1004.0)
        assert driver.is_target_reached(1000.0, ControlMode.VELOCITY) is True

    def test_velocity_outside_tolerance(self) -> None:
        driver = StubFeedbackDriver("b", 1)
        driver.set_observed(velocity=1010.0)
        assert driver.is_target_reached(1000.0, ControlMode.VELOCITY) is False

    def test_explicit_tolerance_overrides_default(self) -> None:
        driver = StubFeedbackDriver("b", 1)
        driver.set_observed(position=15.0)
        assert driver.is_target_reached(10.0, ControlMode.POSITION, tolerance=10.0) is True

    def test_open_loop_modes_are_always_reached(self) -> None:
        driver = StubFeedbackDriver("b", 1)
        driver.set_observed(current=0.0)
        assert driver.is_target_reached(5000.0, ControlMode.CURRENT) is True
        assert driver.is_target_reached(0.5, ControlMode.DUTY) is True


class TestGenericIsTargetReached:
    def test_position_requires_reached_flag(self) -> None:
        driver = GenericDriver("g", 1)
        feed_generic(driver, position=10.0)
        assert driver.is_target_reached(10.0, ControlMode.POSITION) is False

    def test_position_reached_flag_and_within_tolerance(self) -> None:
        driver = GenericDriver("g", 1)
        feed_generic(driver, position=10.0, reached=True)
        assert driver.is_target_reached(10.0, ControlMode.POSITION) is True

    def test_position_reached_flag_but_far_from_target(self) -> None:
        driver = GenericDriver("g", 1)
        feed_generic(driver, position=30.0, reached=True)
        assert driver.is_target_reached(10.0, ControlMode.POSITION) is False

    def test_velocity_uses_base_comparison(self) -> None:
        driver = GenericDriver("g", 1)
        feed_generic(driver, velocity=100.0)
        assert driver.is_target_reached(102.0, ControlMode.VELOCITY) is True
        assert driver.is_target_reached(120.0, ControlMode.VELOCITY) is False


class TestM3508IsTargetReached:
    """M3508 は多回転累積角で判定する (詳細は tests/drivers/test_m3508.py)。"""

    def test_position_uses_multi_turn_position_not_wrapped_angle(self) -> None:
        driver = M3508Driver("m", 1)
        # 起動後 1 フレーム目は差分を取れないので累積角は 0 のまま。
        # 単回転角が 10deg でも 360deg 目標には到達しない
        feed_m3508(driver, deg=10.0)
        assert driver.is_target_reached(360.0, ControlMode.POSITION) is False


class TestFeedbackPosition:
    """位置偏差監視用の共通 API。ドライバ種別によらず同じ意味の値が得られること。"""

    def test_base_driver_returns_state_position(self) -> None:
        driver = StubFeedbackDriver("b", 1)
        driver.set_observed(position=12.5)
        assert driver.feedback_position() == pytest.approx(12.5)

    def test_generic_returns_state_position_in_degrees(self) -> None:
        driver = GenericDriver("g", 1)
        feed_generic(driver, position=42.0)
        assert driver.feedback_position() == pytest.approx(driver.state.position)
        assert driver.feedback_position() == pytest.approx(42.0)

    def test_edulite_returns_state_position_in_radians(self) -> None:
        driver = Edulite05Driver("e", 1)
        feed_edulite(driver, position=1.25)
        assert driver.feedback_position() == pytest.approx(driver.state.position)
        assert driver.feedback_position() == pytest.approx(1.25)

    def test_m3508_returns_multi_turn_position_not_wrapped_angle(self) -> None:
        driver = M3508Driver("m", 1)
        feed_m3508(driver, deg=10.0)
        assert driver.feedback_position() == pytest.approx(driver.multi_turn_position)
        assert driver.feedback_position() != pytest.approx(driver.state.position)
