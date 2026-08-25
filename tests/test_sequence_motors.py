from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import can
import pytest

from lib.drivers.base import ControlMode, MotorDriver, MotorState
from lib.sequence.engine import Sequence, step
from lib.sequence.motors import (
    EStopActiveError,
    MotorGroup,
    MotorHandle,
    build_motor_group,
)


class _FakeDriver(MotorDriver):
    """CAN プロトコルに依存せず encode/state だけを差し替えられるテスト用ドライバ。"""

    def __init__(self, name: str = "m1", can_id: int = 1) -> None:
        super().__init__(name, can_id)
        self.encoded: list[tuple[ControlMode, float]] = []

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        self.encoded.append((mode, value))
        return can.Message(arbitration_id=0x100 + self.can_id, data=bytes(8), is_extended_id=False)

    def decode_feedback(self, msg: can.Message) -> MotorState:  # pragma: no cover
        return self._state

    def matches_feedback(self, msg: can.Message) -> bool:  # pragma: no cover
        return False

    def set_observed(self, **kwargs: float) -> None:
        self._state = MotorState(**kwargs)


def _make_can_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.send = AsyncMock()
    return mgr


def _make_handle(**kwargs) -> tuple[MotorHandle, _FakeDriver, MagicMock]:
    driver = _FakeDriver()
    mgr = _make_can_manager()
    handle = MotorHandle("m1", driver, mgr, **kwargs)
    return handle, driver, mgr


class TestMotorHandleSend:
    async def test_set_position_encodes_and_sends(self) -> None:
        handle, driver, mgr = _make_handle()

        await handle.set_position(120.0)

        assert driver.encoded == [(ControlMode.POSITION, 120.0)]
        mgr.send.assert_awaited_once()
        sent_name, sent_msg = mgr.send.await_args.args
        assert sent_name == "m1"
        assert sent_msg.arbitration_id == 0x101

    async def test_set_velocity_current_duty(self) -> None:
        handle, driver, mgr = _make_handle()

        await handle.set_velocity(50.0)
        await handle.set_current(500.0)
        await handle.set_duty(0.3)

        assert driver.encoded == [
            (ControlMode.VELOCITY, 50.0),
            (ControlMode.CURRENT, 500.0),
            (ControlMode.DUTY, 0.3),
        ]
        assert mgr.send.await_count == 3

    async def test_last_target_is_recorded(self) -> None:
        handle, _driver, _mgr = _make_handle()

        assert handle.has_target is False
        assert handle.target is None
        assert handle.mode is None

        await handle.set_position(90.0)

        assert handle.has_target is True
        assert handle.target == 90.0
        assert handle.mode is ControlMode.POSITION

    def test_state_delegates_to_driver(self) -> None:
        handle, driver, _mgr = _make_handle()
        driver.set_observed(position=12.0)
        assert handle.state.position == 12.0

    def test_name_and_driver_exposed(self) -> None:
        handle, driver, _mgr = _make_handle()
        assert handle.name == "m1"
        assert handle.driver is driver


class TestMotorHandleEStop:
    async def test_set_position_rejected_while_estop_active(self) -> None:
        active = True
        handle, driver, mgr = _make_handle(is_estop_active=lambda: active)

        with pytest.raises(EStopActiveError):
            await handle.set_position(10.0)

        assert driver.encoded == []
        mgr.send.assert_not_awaited()

    async def test_all_setters_rejected_while_estop_active(self) -> None:
        handle, _driver, mgr = _make_handle(is_estop_active=lambda: True)

        for coro in (
            handle.set_velocity(1.0),
            handle.set_current(1.0),
            handle.set_duty(0.1),
        ):
            with pytest.raises(EStopActiveError):
                await coro

        mgr.send.assert_not_awaited()

    async def test_send_allowed_when_estop_released(self) -> None:
        active = True

        def is_active() -> bool:
            return active

        handle, driver, mgr = _make_handle(is_estop_active=is_active)
        active = False

        await handle.set_position(10.0)

        assert driver.encoded == [(ControlMode.POSITION, 10.0)]
        mgr.send.assert_awaited_once()

    async def test_estop_rejection_does_not_update_target(self) -> None:
        handle, _driver, _mgr = _make_handle(is_estop_active=lambda: True)

        with pytest.raises(EStopActiveError):
            await handle.set_position(10.0)

        assert handle.has_target is False


class TestMotorHandleTargetSink:
    async def test_sink_replaces_can_send(self) -> None:
        calls: list[tuple[ControlMode, float]] = []

        async def sink(mode: ControlMode, value: float) -> None:
            calls.append((mode, value))

        handle, driver, mgr = _make_handle(target_sink=sink)

        await handle.set_position(45.0)

        assert calls == [(ControlMode.POSITION, 45.0)]
        assert driver.encoded == []
        mgr.send.assert_not_awaited()

    async def test_sink_still_records_target(self) -> None:
        async def sink(mode: ControlMode, value: float) -> None:
            return None

        handle, driver, _mgr = _make_handle(target_sink=sink)
        await handle.set_position(45.0)
        driver.set_observed(position=45.0)

        assert handle.target == 45.0
        assert await handle.wait_reached(timeout=0.05) is True

    async def test_sink_blocked_by_estop(self) -> None:
        calls: list[tuple[ControlMode, float]] = []

        async def sink(mode: ControlMode, value: float) -> None:
            calls.append((mode, value))

        handle, _driver, _mgr = _make_handle(target_sink=sink, is_estop_active=lambda: True)

        with pytest.raises(EStopActiveError):
            await handle.set_position(45.0)

        assert calls == []


class TestMotorHandleWaitReached:
    async def test_returns_true_when_already_reached(self) -> None:
        handle, driver, _mgr = _make_handle()
        await handle.set_position(10.0)
        driver.set_observed(position=10.2)

        assert await handle.wait_reached(timeout=0.05) is True

    async def test_returns_false_on_timeout(self) -> None:
        handle, driver, _mgr = _make_handle()
        await handle.set_position(10.0)
        driver.set_observed(position=100.0)

        assert await handle.wait_reached(timeout=0.05) is False

    async def test_returns_true_when_reached_later(self) -> None:
        handle, driver, _mgr = _make_handle()
        await handle.set_position(10.0)
        driver.set_observed(position=100.0)

        async def arrive() -> None:
            await asyncio.sleep(0.03)
            driver.set_observed(position=10.0)

        task = asyncio.create_task(arrive())
        try:
            assert await handle.wait_reached(timeout=1.0) is True
        finally:
            await task

    async def test_explicit_tolerance(self) -> None:
        handle, driver, _mgr = _make_handle()
        await handle.set_position(10.0)
        driver.set_observed(position=13.0)

        assert await handle.wait_reached(tolerance=5.0, timeout=0.05) is True
        assert await handle.wait_reached(tolerance=0.5, timeout=0.05) is False

    async def test_no_target_is_reached(self) -> None:
        handle, _driver, _mgr = _make_handle()
        assert await handle.wait_reached(timeout=0.05) is True

    async def test_clear_target(self) -> None:
        handle, driver, _mgr = _make_handle()
        await handle.set_position(10.0)
        driver.set_observed(position=100.0)

        handle.clear_target()

        assert handle.has_target is False
        assert await handle.wait_reached(timeout=0.05) is True


class TestMotorGroup:
    def _group(self) -> tuple[MotorGroup, dict[str, _FakeDriver], MagicMock]:
        mgr = _make_can_manager()
        drivers = {
            "lift_motor": _FakeDriver("lift_motor", 1),
            "arm_joint": _FakeDriver("arm_joint", 2),
        }
        group = build_motor_group(mgr, drivers)
        return group, drivers, mgr

    def test_attribute_access(self) -> None:
        group, drivers, _mgr = self._group()
        assert isinstance(group.lift_motor, MotorHandle)
        assert group.lift_motor.driver is drivers["lift_motor"]

    def test_unknown_attribute_lists_available_motors(self) -> None:
        group, _drivers, _mgr = self._group()
        with pytest.raises(AttributeError) as excinfo:
            _ = group.no_such_motor
        message = str(excinfo.value)
        assert "no_such_motor" in message
        assert "lift_motor" in message
        assert "arm_joint" in message

    def test_getitem_and_contains(self) -> None:
        group, _drivers, _mgr = self._group()
        assert group["arm_joint"].name == "arm_joint"
        assert "arm_joint" in group
        assert "missing" not in group
        with pytest.raises(KeyError):
            _ = group["missing"]

    def test_iteration_and_names(self) -> None:
        group, _drivers, _mgr = self._group()
        assert list(group) == ["lift_motor", "arm_joint"]
        assert group.names == ("lift_motor", "arm_joint")
        assert len(group) == 2
        assert [h.name for h in group.handles] == ["lift_motor", "arm_joint"]
        assert [name for name, _h in group.items()] == ["lift_motor", "arm_joint"]

    async def test_send_through_group(self) -> None:
        group, drivers, mgr = self._group()
        await group.lift_motor.set_current(300.0)

        assert drivers["lift_motor"].encoded == [(ControlMode.CURRENT, 300.0)]
        assert mgr.send.await_args.args[0] == "lift_motor"

    async def test_wait_all_reached_true(self) -> None:
        group, drivers, _mgr = self._group()
        await group.lift_motor.set_position(10.0)
        await group.arm_joint.set_position(20.0)
        drivers["lift_motor"].set_observed(position=10.0)
        drivers["arm_joint"].set_observed(position=20.0)

        assert await group.wait_all_reached(timeout=0.05) is True

    async def test_wait_all_reached_false_when_one_lags(self) -> None:
        group, drivers, _mgr = self._group()
        await group.lift_motor.set_position(10.0)
        await group.arm_joint.set_position(20.0)
        drivers["lift_motor"].set_observed(position=10.0)
        drivers["arm_joint"].set_observed(position=200.0)

        assert await group.wait_all_reached(timeout=0.05) is False

    async def test_wait_all_reached_ignores_motors_without_target(self) -> None:
        group, drivers, _mgr = self._group()
        await group.lift_motor.set_position(10.0)
        drivers["lift_motor"].set_observed(position=10.0)
        drivers["arm_joint"].set_observed(position=999.0)

        assert await group.wait_all_reached(timeout=0.05) is True

    async def test_estop_and_sink_propagate_from_builder(self) -> None:
        mgr = _make_can_manager()
        drivers = {"lift_motor": _FakeDriver("lift_motor", 1)}
        group = build_motor_group(mgr, drivers, is_estop_active=lambda: True)

        with pytest.raises(EStopActiveError):
            await group.lift_motor.set_position(1.0)


class _UnboundSequence(Sequence):
    def __init__(self) -> None:
        super().__init__("unbound")
        self.executed: list[str] = []

    @step("動く")
    async def move(self) -> None:
        self.executed.append("move")


class _BoundSequence(Sequence):
    def __init__(self) -> None:
        super().__init__("bound")
        self.reached: bool | None = None

    @step("持ち上げ")
    async def lift(self) -> None:
        await self.motors.lift_motor.set_position(100.0)
        self.reached = await self.wait_all_reached(timeout=0.05)


class TestSequenceMotorBinding:
    async def test_unbound_sequence_runs_as_before(self) -> None:
        seq = _UnboundSequence()
        await seq.run()
        assert seq.executed == ["move"]

    def test_unbound_sequence_constructor_signature_unchanged(self) -> None:
        seq = _UnboundSequence()
        assert seq.name == "unbound"
        assert seq.has_motors is False

    def test_accessing_motors_unbound_raises(self) -> None:
        seq = _UnboundSequence()
        with pytest.raises(RuntimeError) as excinfo:
            _ = seq.motors
        assert "unbound" in str(excinfo.value)

    async def test_wait_all_reached_unbound_raises(self) -> None:
        seq = _UnboundSequence()
        with pytest.raises(RuntimeError):
            await seq.wait_all_reached(timeout=0.01)

    async def test_bound_sequence_can_drive_motors(self) -> None:
        mgr = _make_can_manager()
        driver = _FakeDriver("lift_motor", 1)
        group = build_motor_group(mgr, {"lift_motor": driver})

        seq = _BoundSequence()
        seq.bind_motors(group)
        assert seq.has_motors is True

        driver.set_observed(position=100.0)
        await seq.run()

        assert driver.encoded == [(ControlMode.POSITION, 100.0)]
        assert seq.reached is True
