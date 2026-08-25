from __future__ import annotations

import math
import struct

import can
import pytest

from lib.drivers.base import ControlMode, MotorDriver, MotorState
from lib.drivers.edulite05 import Edulite05Driver
from lib.drivers.generic import CommandType, GenericDriver
from lib.drivers.m3508 import M3508Driver


class _BaseDriver(MotorDriver):
    """基底実装をそのまま使う最小のドライバ (追加 API を一切上書きしない)。"""

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        return can.Message(arbitration_id=1, data=bytes(8))

    def decode_feedback(self, msg: can.Message) -> MotorState:
        return MotorState()

    def matches_feedback(self, msg: can.Message) -> bool:
        return False


def _feed_generic(
    driver: GenericDriver,
    *,
    position: float = 0.0,
    velocity: float = 0.0,
    flags: int = 0,
) -> None:
    data = bytearray(8)
    struct.pack_into("<h", data, 0, round(position * 10))
    struct.pack_into("<h", data, 2, int(velocity))
    data[7] = flags
    msg = can.Message(
        arbitration_id=GenericDriver.build_can_id(CommandType.FEEDBACK, driver.can_id),
        data=bytes(data),
        is_extended_id=False,
    )
    driver.update_state(msg)


class TestDefaultTolerance:
    def test_position_default_matches_generic_check_tolerance(self) -> None:
        driver = GenericDriver("g", 1)
        assert driver.default_tolerance(ControlMode.POSITION) == 1.0

    def test_velocity_default_matches_generic_check_tolerance(self) -> None:
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
        driver = _BaseDriver("b", 1)
        driver._state = driver._state.__class__(position=10.5)
        assert driver.is_target_reached(10.0, ControlMode.POSITION) is True

    def test_position_outside_tolerance(self) -> None:
        driver = _BaseDriver("b", 1)
        driver._state = driver._state.__class__(position=15.0)
        assert driver.is_target_reached(10.0, ControlMode.POSITION) is False

    def test_velocity_within_tolerance(self) -> None:
        driver = _BaseDriver("b", 1)
        driver._state = driver._state.__class__(velocity=1004.0)
        assert driver.is_target_reached(1000.0, ControlMode.VELOCITY) is True

    def test_velocity_outside_tolerance(self) -> None:
        driver = _BaseDriver("b", 1)
        driver._state = driver._state.__class__(velocity=1010.0)
        assert driver.is_target_reached(1000.0, ControlMode.VELOCITY) is False

    def test_explicit_tolerance_overrides_default(self) -> None:
        driver = _BaseDriver("b", 1)
        driver._state = driver._state.__class__(position=15.0)
        assert driver.is_target_reached(10.0, ControlMode.POSITION, tolerance=10.0) is True

    def test_open_loop_modes_are_always_reached(self) -> None:
        driver = _BaseDriver("b", 1)
        driver._state = driver._state.__class__(current=0.0)
        assert driver.is_target_reached(5000.0, ControlMode.CURRENT) is True
        assert driver.is_target_reached(0.5, ControlMode.DUTY) is True


class TestGenericIsTargetReached:
    def test_position_requires_reached_flag(self) -> None:
        driver = GenericDriver("g", 1)
        _feed_generic(driver, position=10.0, flags=0x00)
        assert driver.is_target_reached(10.0, ControlMode.POSITION) is False

    def test_position_reached_flag_and_within_tolerance(self) -> None:
        driver = GenericDriver("g", 1)
        _feed_generic(driver, position=10.0, flags=0x01)
        assert driver.is_target_reached(10.0, ControlMode.POSITION) is True

    def test_position_reached_flag_but_far_from_target(self) -> None:
        driver = GenericDriver("g", 1)
        _feed_generic(driver, position=30.0, flags=0x01)
        assert driver.is_target_reached(10.0, ControlMode.POSITION) is False

    def test_velocity_uses_base_comparison(self) -> None:
        driver = GenericDriver("g", 1)
        _feed_generic(driver, velocity=100.0, flags=0x00)
        assert driver.is_target_reached(102.0, ControlMode.VELOCITY) is True
        assert driver.is_target_reached(120.0, ControlMode.VELOCITY) is False


class TestBackwardCompatibility:
    def test_minimal_driver_subclass_still_works(self) -> None:
        """既存の mock 相当 (追加 API を実装しない派生クラス) が壊れないこと。"""
        driver = _BaseDriver("x", 1)
        assert driver.is_target_reached(0.0, ControlMode.POSITION) is True


class TestM3508IsTargetReached:
    """M3508 は多回転累積角で判定する (詳細は tests/drivers/test_m3508.py)。"""

    def test_position_uses_multi_turn_position_not_wrapped_angle(self) -> None:
        driver = M3508Driver("m", 1)
        driver._state = MotorState(position=10.0)
        # 単回転角だけ書き換えても累積角は 0 のままなので到達しない
        assert driver.is_target_reached(360.0, ControlMode.POSITION) is False
