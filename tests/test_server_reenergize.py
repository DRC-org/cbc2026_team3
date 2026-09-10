from __future__ import annotations

import asyncio
import contextlib
import struct
import time
from typing import ClassVar
from unittest.mock import AsyncMock

import can
import pytest
from aiohttp.test_utils import TestClient, TestServer

from lib.axis_sync import MotorSpec, SyncGroup
from lib.can_manager import CANManager
from lib.control.sync_monitor import SyncMonitor
from lib.control.target_refresh import QueryDrivenTargetRefresher
from lib.drivers.base import ControlMode
from lib.drivers.edulite05 import Edulite05Driver
from lib.manual import ManualController
from lib.sequence.engine import Sequence, step
from lib.sequence.motors import MotorGroup, MotorHandle
from lib.sequence.positions import load_position_table
from lib.server import _ENERGIZE_GRACE_S
from tests.fake_can import (
    direct_runner,
    keep_feedback_fresh,
    mock_bus,
    mock_can_manager,
    mock_motor,
    set_motors,
)
from tests.feedback_frames import feed_edulite
from tests.server_fixtures import RecordingClient, ServerFixture, wait_until

_ROBOT_NAMES = ("main_hand", "sub_hand")


def _target_value(msg: can.Message) -> float:
    _param_id, value = struct.unpack("<Hxxf", msg.data)
    return value


class _EmptySequence(Sequence):
    """ステップを 1 つも持たないシーケンス。"""


def _count_e_stop_broadcasts(sent: list[can.Message]) -> int:
    return sum(
        1
        for msg in sent
        if msg.arbitration_id == 0x0FF and bytes(msg.data) == bytes(3) and not msg.is_extended_id
    )


def _build_fixture() -> ServerFixture:
    fx = ServerFixture.build()
    for name in _ROBOT_NAMES:
        fx.add_robot(name, _EmptySequence(name))
    return fx


def _can_manager_with_dropped_motor() -> tuple[CANManager, Edulite05Driver]:
    can_manager = mock_can_manager(bus_name="can_edulite")
    can_manager.send = AsyncMock()
    dropped = Edulite05Driver("dropped", can_id=1)
    set_motors(can_manager, {"dropped": dropped})
    feed_edulite(dropped, position=0.5, mode_state=0)
    keep_feedback_fresh(can_manager)
    return can_manager, dropped


def _dropped_fixture() -> tuple[ServerFixture, Edulite05Driver]:
    can_manager, dropped = _can_manager_with_dropped_motor()
    fx = ServerFixture.build()
    fx.add_robot("main_hand", _EmptySequence("main_hand"), can_manager)
    fx.add_robot("sub_hand", _EmptySequence("sub_hand"))
    return fx, dropped


def _manual_fixture() -> ServerFixture:
    table = load_position_table(
        {
            "axes": {
                "axis": {
                    "unit": "rad",
                    "command_unit": "rad",
                    "manual": {"min": -1.0, "max": 1.0, "steps": [0.1]},
                    "motors": {"m1": {"scale": 1.0}},
                },
            },
            "positions": {},
        },
        source="<test>",
    )
    motor = mock_motor("m1")
    motor.is_energized.return_value = False
    can_manager = mock_can_manager({"m1": motor})
    group = MotorGroup()
    group.add(MotorHandle("m1", can_manager.motors["m1"], can_manager))
    manual = ManualController(group, table)

    fx = ServerFixture.build()
    fx.add_robot("main_hand", _EmptySequence("main_hand"), can_manager, manual=manual)
    fx.add_robot("sub_hand", _EmptySequence("sub_hand"))
    return fx


class TestUnknownOrMissingRobot:
    async def test_unknown_robot_is_ignored(self) -> None:
        fx = _build_fixture()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])
        await fx.command({"type": "reenergize_motors", "robot": "no_such_robot"})
        assert not fx.has_pending_reenergize("no_such_robot")
        await asyncio.sleep(0)
        fx.can_manager("main_hand").activate_motors.assert_not_called()

    async def test_missing_robot_is_ignored(self) -> None:
        fx = _build_fixture()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])
        await fx.command({"type": "reenergize_motors"})
        assert not fx.has_pending_reenergize(None)  # type: ignore[arg-type]
        await asyncio.sleep(0)
        fx.can_manager("main_hand").activate_motors.assert_not_called()


class TestExclusionGates:
    async def test_rejected_while_motor_check_running(self) -> None:
        fx = _build_fixture()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])
        gate = asyncio.Event()

        async def _never_finishes() -> None:
            await gate.wait()

        fx.set_motor_check_task(asyncio.create_task(_never_finishes()))
        client = RecordingClient()
        fx.attach_clients(client)
        try:
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"}, requester=client)
            await asyncio.sleep(0)
            fx.can_manager("main_hand").activate_motors.assert_not_called()
            rejected = client.of_type("command_rejected")
            assert len(rejected) == 1
            assert rejected[0]["command"] == "reenergize_motors"
            assert "動作確認" in rejected[0]["reason"]
        finally:
            gate.set()

    async def test_rejected_while_e_stop_reactivation_in_flight(self) -> None:
        fx = _build_fixture()
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        fx.can_manager("sub_hand").activate_motors = AsyncMock(return_value=[])
        client = RecordingClient()
        fx.attach_clients(client)
        try:
            await fx.command({"type": "e_stop"})
            await fx.command({"type": "e_stop_release"})
            await asyncio.sleep(0)
            assert fx.server._reactivating

            await fx.command({"type": "reenergize_motors", "robot": "sub_hand"}, requester=client)
            fx.can_manager("sub_hand").activate_motors.assert_not_called()
            rejected = client.of_type("command_rejected")
            assert len(rejected) == 1
            assert "進行中" in rejected[0]["reason"]
        finally:
            gate.set()
            await fx.wait_reactivation()

    async def test_second_press_is_rejected_while_first_still_running(self) -> None:
        fx, _dropped = _dropped_fixture()
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        client = RecordingClient()
        fx.attach_clients(client)
        try:
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
            await asyncio.sleep(0)
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"}, requester=client)
            await asyncio.sleep(0)
            rejected = client.of_type("command_rejected")
            assert len(rejected) == 1
            assert "処理中" in rejected[0]["reason"]
        finally:
            gate.set()
            await fx.wait_reenergize("main_hand")


class TestTargetsRobotOnly:
    async def test_only_named_robot_is_activated(self) -> None:
        fx, _dropped = _dropped_fixture()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])
        fx.can_manager("sub_hand").activate_motors = AsyncMock(return_value=[])

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        main = fx.can_manager("main_hand")
        main.activate_motors.assert_awaited_once()
        assert main.activate_motors.await_args.kwargs["only"] == {"dropped"}
        fx.can_manager("sub_hand").activate_motors.assert_not_called()

    async def test_nothing_is_sent_when_no_motor_is_dropped(self) -> None:
        fx = _build_fixture()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        fx.can_manager("main_hand").activate_motors.assert_not_called()


class TestFailureIsReported:
    async def test_motors_that_fail_to_activate_appear_in_safety(self) -> None:
        fx, _driver = _dropped_fixture()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=["dropped"])

        app = fx.create_app()
        async with TestClient(TestServer(app)):
            await asyncio.sleep(_ENERGIZE_GRACE_S + 0.1)
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
            await fx.wait_reenergize("main_hand")

            assert fx.state_message("main_hand")["safety"]["unenergized_motors"] == []

            await asyncio.sleep(_ENERGIZE_GRACE_S + 0.1)
            assert fx.state_message("main_hand")["safety"]["unenergized_motors"] == ["dropped"]
            assert fx.state_message("sub_hand")["safety"]["unenergized_motors"] == []

    async def test_activate_motors_exception_reports_only_set(self) -> None:
        fx = ServerFixture.build()

        rotate_r = mock_motor("rotate_r")
        rotate_r.is_energized.return_value = False
        rotate_l = mock_motor("rotate_l")
        rotate_l.is_energized.return_value = True
        gripper = mock_motor("gripper")
        gripper.is_energized.return_value = True

        can_manager = mock_can_manager(
            {"rotate_r": rotate_r, "rotate_l": rotate_l, "gripper": gripper}
        )
        can_manager.activate_motors = AsyncMock(side_effect=RuntimeError("CAN 送信失敗"))
        keep_feedback_fresh(can_manager)

        group = SyncGroup(
            name="rotate",
            members=(
                MotorSpec(name="rotate_r", scale=1.0, offset=0.0),
                MotorSpec(name="rotate_l", scale=-1.0, offset=0.0),
            ),
            tolerance=5.0,
        )
        monitor = SyncMonitor(
            [group],
            {"rotate_r": rotate_r, "rotate_l": rotate_l},  # type: ignore[arg-type]
            last_feedback_at=lambda _name: time.time(),
        )

        fx.add_robot("main_hand", _EmptySequence("main_hand"), can_manager, sync_monitors=[monitor])
        fx.add_robot("sub_hand", _EmptySequence("sub_hand"))

        app = fx.create_app()
        async with TestClient(TestServer(app)):
            await asyncio.sleep(_ENERGIZE_GRACE_S + 0.1)
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
            await fx.wait_reenergize("main_hand")
            await asyncio.sleep(_ENERGIZE_GRACE_S + 0.1)

            # 相方 `rotate_l` は励磁されたままなので報告しない (ラッチには居る)。
            safety = fx.state_message("main_hand")["safety"]
            assert safety["unenergized_motors"] == ["rotate_r"]
            assert safety["unresponsive_motors"] == []


class TestPreviouslyInactiveMotorsAreRetried:
    async def test_only_includes_previously_inactive_motor(self) -> None:
        fx = _build_fixture()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])
        fx.server.set_initial_inactive_motors("main_hand", ["m_startup"])

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        can_manager = fx.can_manager("main_hand")
        can_manager.activate_motors.assert_awaited_once()
        assert can_manager.activate_motors.await_args.kwargs["only"] == {"m_startup"}


class TestStaleTargetIsClearedBeforeActivation:
    def _build(self) -> tuple[ServerFixture, dict[str, Edulite05Driver], dict[str, MotorHandle]]:
        can_manager = mock_can_manager(bus_name="can_edulite")
        can_manager.send = AsyncMock()
        can_manager.activate_motors = AsyncMock(return_value=[])

        dropped = Edulite05Driver("dropped", can_id=1)
        healthy = Edulite05Driver("healthy", can_id=2)
        set_motors(can_manager, {"dropped": dropped, "healthy": healthy})

        dropped_handle = MotorHandle("dropped", dropped, can_manager)
        healthy_handle = MotorHandle("healthy", healthy, can_manager)
        refresher = QueryDrivenTargetRefresher(
            [dropped_handle, healthy_handle], can_manager, is_estop_active=lambda: False
        )

        fx = ServerFixture.build()
        fx.add_robot(
            "main_hand", _EmptySequence("main_hand"), can_manager, target_refreshers=[refresher]
        )
        fx.add_robot("sub_hand", _EmptySequence("sub_hand"))

        return (
            fx,
            {"dropped": dropped, "healthy": healthy},
            {
                "dropped": dropped_handle,
                "healthy": healthy_handle,
            },
        )

    async def test_dropped_motor_loses_stale_explicit_target(self) -> None:
        fx, drivers, handles = self._build()
        await handles["dropped"].set_target(ControlMode.POSITION, 4.0)
        feed_edulite(drivers["dropped"], position=0.5, mode_state=0)
        assert drivers["dropped"].is_energized() is False

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        assert handles["dropped"].has_target is False

    async def test_dropped_motor_loses_stale_idle_latch(self) -> None:
        fx, drivers, _handles = self._build()
        can_manager = fx.can_manager("main_hand")
        (refresher,) = fx.server._robots["main_hand"].target_refreshers

        feed_edulite(drivers["dropped"], position=3.0, mode_state=2)
        await refresher.step()
        latched = [c.args[1] for c in can_manager.send.await_args_list if c.args[0] == "dropped"][
            -1
        ]
        assert _target_value(latched) == pytest.approx(3.0, abs=0.05)

        feed_edulite(drivers["dropped"], position=1.0, mode_state=0)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        can_manager.send.reset_mock()
        await refresher.step()
        resent = [c.args[1] for c in can_manager.send.await_args_list if c.args[0] == "dropped"]
        assert len(resent) == 1
        assert _target_value(resent[0]) == pytest.approx(1.0, abs=0.05)

    async def test_healthy_motor_on_same_refresher_is_untouched(self) -> None:
        fx, drivers, handles = self._build()
        feed_edulite(drivers["healthy"], position=0.0, mode_state=2)
        await handles["healthy"].set_target(ControlMode.POSITION, 7.0)

        feed_edulite(drivers["dropped"], position=0.5, mode_state=0)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        assert handles["healthy"].target == 7.0

    async def test_already_energized_motor_keeps_its_target(self) -> None:
        fx, drivers, handles = self._build()
        feed_edulite(drivers["dropped"], position=0.0, mode_state=2)
        await handles["dropped"].set_target(ControlMode.POSITION, 1.2)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        assert handles["dropped"].target == 1.2

    async def test_activation_is_scoped_to_dropped_motors_only(self) -> None:
        fx, drivers, _handles = self._build()
        feed_edulite(drivers["healthy"], position=0.0, mode_state=2)
        feed_edulite(drivers["dropped"], position=0.5, mode_state=0)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        can_manager = fx.can_manager("main_hand")
        can_manager.activate_motors.assert_awaited_once()
        assert can_manager.activate_motors.await_args.kwargs["only"] == {"dropped"}


class TestManualJogOriginIsReset:
    _POSITIONS: ClassVar[dict] = {
        "axes": {
            "test_axis": {
                "unit": "rad",
                "command_unit": "rad",
                "manual": {"min": -12.0, "max": 12.0, "steps": [1.0]},
                "motors": {"dropped": {"scale": 1.0}},
            },
            "other_axis": {
                "unit": "rad",
                "command_unit": "rad",
                "manual": {"min": -12.0, "max": 12.0, "steps": [1.0]},
                "motors": {"healthy": {"scale": 1.0}},
            },
        },
        "positions": {},
    }

    def _build(self) -> tuple[ServerFixture, dict[str, Edulite05Driver], ManualController]:
        can_manager = mock_can_manager(bus_name="can_edulite")
        can_manager.send = AsyncMock()
        can_manager.activate_motors = AsyncMock(return_value=[])

        dropped = Edulite05Driver("dropped", can_id=1)
        healthy = Edulite05Driver("healthy", can_id=2)
        set_motors(can_manager, {"dropped": dropped, "healthy": healthy})

        handles = {
            name: MotorHandle(name, driver, can_manager)
            for name, driver in (("dropped", dropped), ("healthy", healthy))
        }
        refresher = QueryDrivenTargetRefresher(
            list(handles.values()), can_manager, is_estop_active=lambda: False
        )

        group = MotorGroup()
        for handle in handles.values():
            group.add(handle)
        table = load_position_table(self._POSITIONS, source="<test>")
        manual = ManualController(group, table)

        fx = ServerFixture.build()
        fx.add_robot(
            "main_hand",
            _EmptySequence("main_hand"),
            can_manager,
            target_refreshers=[refresher],
            manual=manual,
        )
        fx.add_robot("sub_hand", _EmptySequence("sub_hand"))
        return fx, {"dropped": dropped, "healthy": healthy}, manual

    async def test_origin_is_dropped_when_motor_was_unenergized(self) -> None:
        fx, drivers, manual = self._build()
        await manual.set_value("test_axis", 5.0)
        feed_edulite(drivers["dropped"], position=1.0, mode_state=0)
        feed_edulite(drivers["healthy"], position=0.0, mode_state=2)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        feed_edulite(drivers["dropped"], position=1.0, mode_state=2)
        assert await manual.jog("test_axis", 0.5) == pytest.approx(1.5, abs=0.01)

    async def test_unrelated_axis_keeps_its_origin(self) -> None:
        fx, drivers, manual = self._build()
        await manual.set_value("test_axis", 5.0)
        await manual.set_value("other_axis", 7.0)
        feed_edulite(drivers["dropped"], position=1.0, mode_state=0)
        feed_edulite(drivers["healthy"], position=2.0, mode_state=2)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        assert await manual.jog("other_axis", 0.5) == pytest.approx(7.5, abs=0.01)

    async def test_origin_is_kept_when_nothing_was_dropped(self) -> None:
        fx, drivers, manual = self._build()
        await manual.set_value("test_axis", 5.0)
        feed_edulite(drivers["dropped"], position=2.0, mode_state=2)
        feed_edulite(drivers["healthy"], position=0.0, mode_state=2)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        assert await manual.jog("test_axis", 0.5) == pytest.approx(5.5, abs=0.01)


class TestPairedAxisIsExpandedToPartner:
    def _build(
        self,
    ) -> tuple[ServerFixture, dict[str, Edulite05Driver], dict[str, MotorHandle]]:
        can_manager = mock_can_manager(bus_name="can_edulite")
        can_manager.send = AsyncMock()
        can_manager.activate_motors = AsyncMock(return_value=[])
        can_manager.last_feedback_at = lambda _name: time.time()

        rotate_r = Edulite05Driver("rotate_r", can_id=1)
        rotate_l = Edulite05Driver("rotate_l", can_id=2)
        gripper = Edulite05Driver("gripper", can_id=3)
        set_motors(can_manager, {"rotate_r": rotate_r, "rotate_l": rotate_l, "gripper": gripper})

        handle_r = MotorHandle("rotate_r", rotate_r, can_manager)
        handle_l = MotorHandle("rotate_l", rotate_l, can_manager)
        handle_g = MotorHandle("gripper", gripper, can_manager)
        refresher = QueryDrivenTargetRefresher(
            [handle_r, handle_l, handle_g], can_manager, is_estop_active=lambda: False
        )

        group = SyncGroup(
            name="rotate",
            members=(
                MotorSpec(name="rotate_r", scale=1.0, offset=0.0),
                MotorSpec(name="rotate_l", scale=-1.0, offset=0.0),
            ),
            tolerance=5.0,
        )
        monitor = SyncMonitor(
            [group],
            {"rotate_r": rotate_r, "rotate_l": rotate_l},  # type: ignore[arg-type]
            last_feedback_at=lambda _name: time.time(),
        )

        fx = ServerFixture.build()
        fx.add_robot(
            "main_hand",
            _EmptySequence("main_hand"),
            can_manager,
            target_refreshers=[refresher],
            sync_monitors=[monitor],
        )
        fx.add_robot("sub_hand", _EmptySequence("sub_hand"))

        return (
            fx,
            {"rotate_r": rotate_r, "rotate_l": rotate_l, "gripper": gripper},
            {"rotate_r": handle_r, "rotate_l": handle_l, "gripper": handle_g},
        )

    async def test_activation_includes_healthy_partner(self) -> None:
        fx, drivers, _handles = self._build()
        feed_edulite(drivers["rotate_l"], position=0.0, mode_state=2)
        feed_edulite(drivers["rotate_r"], position=0.5, mode_state=0)
        feed_edulite(drivers["gripper"], position=0.0, mode_state=2)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        can_manager = fx.can_manager("main_hand")
        can_manager.activate_motors.assert_awaited_once()
        assert can_manager.activate_motors.await_args.kwargs["only"] == {"rotate_r", "rotate_l"}

    async def test_healthy_partners_target_is_also_cleared(self) -> None:
        fx, drivers, handles = self._build()
        feed_edulite(drivers["rotate_l"], position=0.0, mode_state=2)
        await handles["rotate_l"].set_target(ControlMode.POSITION, 7.0)
        feed_edulite(drivers["rotate_r"], position=0.5, mode_state=0)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        assert handles["rotate_l"].has_target is False

    async def test_unpaired_motor_is_not_expanded(self) -> None:
        fx, drivers, _handles = self._build()
        feed_edulite(drivers["rotate_l"], position=0.0, mode_state=2)
        feed_edulite(drivers["rotate_r"], position=0.5, mode_state=2)
        feed_edulite(drivers["gripper"], position=0.0, mode_state=0)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        can_manager = fx.can_manager("main_hand")
        can_manager.activate_motors.assert_awaited_once()
        assert can_manager.activate_motors.await_args.kwargs["only"] == {"gripper"}

    async def test_nothing_dropped_yields_empty_target(self) -> None:
        fx, drivers, _handles = self._build()
        feed_edulite(drivers["rotate_l"], position=0.0, mode_state=2)
        feed_edulite(drivers["rotate_r"], position=0.5, mode_state=2)
        feed_edulite(drivers["gripper"], position=0.0, mode_state=2)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        fx.can_manager("main_hand").activate_motors.assert_not_called()


class TestReactivationSettlesPendingReenergize:
    async def test_reactivate_activate_motors_never_overlaps_the_pending_one(self) -> None:
        fx, _dropped = _dropped_fixture()
        order: list[str] = []
        reenergize_gate = asyncio.Event()

        async def _activate(**_kwargs: object) -> list[str]:
            if not order:
                order.append("reenergize_start")
                try:
                    await reenergize_gate.wait()
                finally:
                    order.append("reenergize_done")
            else:
                order.append("reactivate")
            return []

        fx.can_manager("main_hand").activate_motors = _activate
        fx.can_manager("sub_hand").activate_motors = AsyncMock(return_value=[])

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await asyncio.sleep(0)
        assert order == ["reenergize_start"]

        await fx.command({"type": "e_stop"})
        await fx.command({"type": "e_stop_release"})
        assert not fx.server._e_stop_active

        await fx.wait_reactivation()
        await fx.wait_reenergize("main_hand")

        assert order == ["reenergize_start", "reenergize_done", "reactivate"]


class TestReactivationDoesNotWaitForeverOnAStuckReenergize:
    # 居座る旧タスクは CancelledError を握り潰すコルーチンで作る。run_in_executor で
    # スレッドへ入った呼び出しは cancel() の瞬間に CancelledError が返るため
    # asyncio.wait がタイムアウトへ一度も到達せず (実測 0.000s)、timeout=None でも通る。
    async def test_release_proceeds_when_the_old_task_refuses_to_cancel(self) -> None:
        fx, _dropped = _dropped_fixture()
        release_first_call = asyncio.Event()
        calls: list[str] = []

        async def _stubborn_activate(**_kwargs: object) -> list[str]:
            calls.append("main_hand")
            if len(calls) == 1:
                while not release_first_call.is_set():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.sleep(0.01)
            return []

        fx.can_manager("main_hand").activate_motors = _stubborn_activate
        fx.can_manager("sub_hand").activate_motors = AsyncMock(return_value=[])

        try:
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
            await asyncio.sleep(0)
            assert calls == ["main_hand"]

            await fx.command({"type": "e_stop"})
            await fx.command({"type": "e_stop_release"})
            await fx.wait_reactivation(timeout=3.0)

            assert calls == ["main_hand", "main_hand"], "解除の励磁が先へ進んでいない"
            fx.can_manager("sub_hand").activate_motors.assert_awaited_once()
        finally:
            release_first_call.set()
            await fx.wait_reenergize("main_hand")


class TestMotorCheckDeniedWhileReenergizeInFlight:
    async def test_denied_while_pending(self) -> None:
        fx, _dropped = _dropped_fixture()
        fx.set_motor_check_sequence(_EmptySequence("motor_check"))
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        try:
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
            await asyncio.sleep(0)

            assert await fx.start_motor_check() is False
            assert "main_hand" in (fx.motor_check_error() or "")
        finally:
            gate.set()
            await fx.wait_reenergize("main_hand")

    async def test_allowed_once_reenergize_finishes(self) -> None:
        fx = _build_fixture()
        fx.set_motor_check_sequence(_EmptySequence("motor_check"))
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        assert await fx.start_motor_check() is True


class TestSetOperationModeDeniedWhileReenergizeInFlight:
    async def test_denied_while_pending(self) -> None:
        fx = _manual_fixture()
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        client = RecordingClient()
        fx.attach_clients(client)
        try:
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
            await asyncio.sleep(0)

            await fx.command(
                {"type": "set_operation_mode", "robot": "main_hand", "mode": "manual"},
                requester=client,
            )
            assert fx.operation_mode("main_hand") == "sequence"
            rejected = client.of_type("command_rejected")
            assert len(rejected) == 1
            assert "再励磁" in rejected[0]["reason"]
        finally:
            gate.set()
            await fx.wait_reenergize("main_hand")

    async def test_allowed_once_reenergize_finishes(self) -> None:
        fx = _manual_fixture()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        await fx.command({"type": "set_operation_mode", "robot": "main_hand", "mode": "manual"})
        assert fx.operation_mode("main_hand") == "manual"


class TestManualTargetDeniedWhileReenergizeInFlight:
    async def _enter_manual(self, fx: ServerFixture) -> None:
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])
        await fx.command({"type": "set_operation_mode", "robot": "main_hand", "mode": "manual"})
        assert fx.operation_mode("main_hand") == "manual"

    async def _start_slow_reenergize(self, fx: ServerFixture) -> asyncio.Event:
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await asyncio.sleep(0)
        return gate

    async def test_manual_jog_denied_while_pending(self) -> None:
        fx = _manual_fixture()
        await self._enter_manual(fx)
        gate = await self._start_slow_reenergize(fx)
        client = RecordingClient()
        fx.attach_clients(client)
        try:
            await fx.command(
                {"type": "manual_jog", "robot": "main_hand", "axis": "axis", "delta": 0.1},
                requester=client,
            )
            rejected = client.of_type("command_rejected")
            assert len(rejected) == 1
            assert rejected[0]["command"] == "manual_jog"
            assert "再励磁" in rejected[0]["reason"]
        finally:
            gate.set()
            await fx.wait_reenergize("main_hand")

    async def test_manual_move_denied_while_pending(self) -> None:
        fx = _manual_fixture()
        await self._enter_manual(fx)
        gate = await self._start_slow_reenergize(fx)
        client = RecordingClient()
        fx.attach_clients(client)
        try:
            await fx.command(
                {"type": "manual_move", "robot": "main_hand", "axis": "axis", "position": "home"},
                requester=client,
            )
            rejected = client.of_type("command_rejected")
            assert len(rejected) == 1
            assert rejected[0]["command"] == "manual_move"
            assert "再励磁" in rejected[0]["reason"]
        finally:
            gate.set()
            await fx.wait_reenergize("main_hand")

    async def test_manual_set_denied_while_pending(self) -> None:
        fx = _manual_fixture()
        await self._enter_manual(fx)
        gate = await self._start_slow_reenergize(fx)
        client = RecordingClient()
        fx.attach_clients(client)
        try:
            await fx.command(
                {"type": "manual_set", "robot": "main_hand", "axis": "axis", "value": 0.2},
                requester=client,
            )
            rejected = client.of_type("command_rejected")
            assert len(rejected) == 1
            assert rejected[0]["command"] == "manual_set"
            assert "再励磁" in rejected[0]["reason"]
        finally:
            gate.set()
            await fx.wait_reenergize("main_hand")

    async def test_manual_jog_allowed_once_reenergize_finishes(self) -> None:
        fx = _manual_fixture()
        await self._enter_manual(fx)
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        client = RecordingClient()
        fx.attach_clients(client)
        await fx.command(
            {"type": "manual_jog", "robot": "main_hand", "axis": "axis", "delta": 0.1},
            requester=client,
        )
        assert client.of_type("command_rejected") == []


class TestEStopDuringReenergize:
    async def test_activation_gets_a_live_e_stop_abort_hook(self) -> None:
        fx, _dropped = _dropped_fixture()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        activate = fx.can_manager("main_hand").activate_motors
        activate.assert_awaited_once()
        should_abort = activate.await_args.kwargs["should_abort"]
        assert should_abort() is False
        await fx.activate_e_stop(reason="再励磁の直後に機体が異常な動きを始めた")
        assert should_abort() is True

    async def test_stop_broadcast_is_resent_after_activation(self) -> None:
        sent: list[can.Message] = []
        mgr = CANManager(run_blocking=direct_runner())
        bus = mock_bus()
        bus.send.side_effect = lambda msg: sent.append(msg)
        mgr.add_bus("can_edulite", bus)
        dropped = Edulite05Driver("dropped", can_id=1)
        mgr.add_motor("can_edulite", dropped)
        feed_edulite(dropped, position=0.5, mode_state=0)

        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        mgr.activate_motors = _slow_activate  # type: ignore[method-assign]

        fx = ServerFixture.build()
        fx.add_robot("main_hand", _EmptySequence("main_hand"), mgr)
        fx.add_robot("sub_hand", _EmptySequence("sub_hand"))

        try:
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
            await asyncio.sleep(0)

            await fx.activate_e_stop(reason="再励磁の在飛中に押した")
            before = _count_e_stop_broadcasts(sent)
            assert before >= 1, "緊急停止そのものが停止フレームを出していない"
        finally:
            gate.set()
            await fx.wait_reenergize("main_hand")

        assert _count_e_stop_broadcasts(sent) > before, (
            "再励磁の完了後に停止フレームが送り直されていない"
            " (中断判定をすり抜けた enable が停止より後に届いたまま残る)"
        )


class _HoldingSequence(Sequence):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @step("解放されるまで待つ")
    async def hold(self) -> None:
        self.entered.set()
        await self.release.wait()


class TestSequenceCommandsDeniedWhileReenergizeInFlight:
    async def _start_slow_reenergize(self, fx: ServerFixture) -> asyncio.Event:
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await asyncio.sleep(0)
        return gate

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"type": "sequence_start"}, "シーケンスを開始"),
            ({"type": "sequence_jump", "step_index": 0}, "ステップ移動"),
            ({"type": "trigger"}, "トリガー"),
        ],
    )
    async def test_denied_while_pending(self, payload: dict, expected: str) -> None:
        fx, _dropped = _dropped_fixture()
        fx.enter_match()
        gate = await self._start_slow_reenergize(fx)
        client = RecordingClient()
        fx.attach_clients(client)
        try:
            await fx.command({**payload, "robot": "main_hand"}, requester=client)

            rejected = client.of_type("command_rejected")
            assert len(rejected) == 1
            assert "再励磁の処理中" in rejected[0]["reason"]
            assert expected in rejected[0]["reason"]
        finally:
            gate.set()
            await fx.wait_reenergize("main_hand")

    async def test_other_robot_is_not_blocked(self) -> None:
        fx, _dropped = _dropped_fixture()
        fx.enter_match()
        gate = await self._start_slow_reenergize(fx)
        client = RecordingClient()
        fx.attach_clients(client)
        try:
            await fx.command({"type": "sequence_start", "robot": "sub_hand"}, requester=client)
            assert client.of_type("command_rejected") == []
        finally:
            gate.set()
            await fx.wait_reenergize("main_hand")

    async def test_allowed_once_reenergize_finishes(self) -> None:
        fx, _dropped = _dropped_fixture()
        fx.enter_match()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])
        client = RecordingClient()
        fx.attach_clients(client)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")
        await fx.command({"type": "sequence_start", "robot": "main_hand"}, requester=client)

        assert client.of_type("command_rejected") == []

    async def test_sequence_start_does_not_block_reenergize(self) -> None:
        can_manager, _dropped = _can_manager_with_dropped_motor()
        can_manager.activate_motors = AsyncMock(return_value=[])
        sequence = _HoldingSequence("main_hand")
        fx = ServerFixture.build()
        fx.add_robot("main_hand", sequence, can_manager)
        fx.add_robot("sub_hand", _EmptySequence("sub_hand"))
        fx.enter_match()
        client = RecordingClient()
        fx.attach_clients(client)

        runner = asyncio.create_task(sequence.run_forever())
        try:
            await fx.command({"type": "sequence_start", "robot": "main_hand"})
            await asyncio.wait_for(sequence.entered.wait(), timeout=1.0)
            assert sequence.is_running

            await fx.command({"type": "reenergize_motors", "robot": "main_hand"}, requester=client)
            await fx.wait_reenergize("main_hand")

            assert client.of_type("command_rejected") == []
            can_manager.activate_motors.assert_awaited_once()
        finally:
            sequence.release.set()
            runner.cancel()


class TestInFlightIsBroadcast:
    async def test_flag_follows_the_task_lifetime(self) -> None:
        fx, _dropped = _dropped_fixture()
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate

        assert fx.state_message("main_hand")["safety"]["reenergizing"] is False

        try:
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
            await asyncio.sleep(0)
            assert fx.state_message("main_hand")["safety"]["reenergizing"] is True
            assert fx.state_message("sub_hand")["safety"]["reenergizing"] is False
        finally:
            gate.set()
            await fx.wait_reenergize("main_hand")

        assert fx.state_message("main_hand")["safety"]["reenergizing"] is False


class TestEStopReactivationSharesTheReenergizeGate:
    @staticmethod
    async def _hold_reactivation(fx: ServerFixture) -> asyncio.Event:
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        await fx.command({"type": "e_stop"})
        await fx.command({"type": "e_stop_release"})
        await asyncio.sleep(0)
        assert fx.server._reactivating
        return gate

    async def test_sequence_start_is_rejected(self) -> None:
        fx = _build_fixture()
        fx.enter_match()
        client = RecordingClient()
        fx.attach_clients(client)
        gate = await self._hold_reactivation(fx)
        try:
            await fx.command({"type": "sequence_start", "robot": "main_hand"}, requester=client)
            rejected = client.of_type("command_rejected")
            assert len(rejected) == 1
            assert rejected[0]["command"] == "sequence_start"
            assert not fx.sequence("main_hand").is_running
        finally:
            gate.set()
            await fx.wait_reactivation()

    async def test_motor_check_start_is_rejected(self) -> None:
        fx = _build_fixture()
        fx.set_motor_check_sequence(_EmptySequence("motor_check"))
        gate = await self._hold_reactivation(fx)
        try:
            assert await fx.start_motor_check() is False
            assert "再励磁" in (fx.motor_check_error() or "")
        finally:
            gate.set()
            await fx.wait_reactivation()

    async def test_switching_to_manual_is_rejected(self) -> None:
        fx = _manual_fixture()
        client = RecordingClient()
        fx.attach_clients(client)
        gate = await self._hold_reactivation(fx)
        try:
            await fx.command(
                {"type": "set_operation_mode", "robot": "main_hand", "mode": "manual"},
                requester=client,
            )
            rejected = client.of_type("command_rejected")
            assert len(rejected) == 1
            assert rejected[0]["command"] == "set_operation_mode"
            assert fx.operation_mode("main_hand") == "sequence"
        finally:
            gate.set()
            await fx.wait_reactivation()

    async def test_switching_back_to_sequence_is_allowed(self) -> None:
        fx = _manual_fixture()
        await fx.command({"type": "set_operation_mode", "robot": "main_hand", "mode": "manual"})
        assert fx.operation_mode("main_hand") == "manual"
        client = RecordingClient()
        fx.attach_clients(client)
        gate = await self._hold_reactivation(fx)
        try:
            await fx.command(
                {"type": "set_operation_mode", "robot": "main_hand", "mode": "sequence"},
                requester=client,
            )
            assert client.of_type("command_rejected") == []
            assert fx.operation_mode("main_hand") == "sequence"
        finally:
            gate.set()
            await fx.wait_reactivation()

    async def test_unknown_robot_is_not_denied(self) -> None:
        fx = _build_fixture()
        fx.enter_match()
        client = RecordingClient()
        fx.attach_clients(client)
        gate = await self._hold_reactivation(fx)
        try:
            await fx.command({"type": "sequence_start", "robot": "bogus"}, requester=client)
            assert client.of_type("command_rejected") == []
        finally:
            gate.set()
            await fx.wait_reactivation()

    async def test_safety_reports_reenergizing(self) -> None:
        fx = _build_fixture()
        gate = await self._hold_reactivation(fx)
        try:
            assert fx.state_message("main_hand")["safety"]["reenergizing"] is True
            assert fx.state_message("sub_hand")["safety"]["reenergizing"] is True
        finally:
            gate.set()
            await fx.wait_reactivation()

        assert fx.state_message("main_hand")["safety"]["reenergizing"] is False


class TestEStopReleaseIsNotReentrant:
    @staticmethod
    def _alive_reactivations(fx: ServerFixture) -> int:
        return sum(1 for task in fx.server._reactivate_tasks if not task.done())

    async def test_second_release_settles_the_first(self) -> None:
        fx = _build_fixture()
        gate = asyncio.Event()
        started = 0

        async def _slow_activate(**_kwargs: object) -> list[str]:
            nonlocal started
            started += 1
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        fx.can_manager("sub_hand").activate_motors = AsyncMock(return_value=[])

        await fx.command({"type": "e_stop"})
        await fx.command({"type": "e_stop_release"})
        await asyncio.sleep(0)
        assert fx.server._reactivating

        await fx.command({"type": "e_stop"})
        try:
            await fx.command({"type": "e_stop_release"})

            assert await wait_until(lambda: self._alive_reactivations(fx) == 1), (
                "1 本目が畳まれていない (2 本の activate_motors が並走する)"
            )
        finally:
            gate.set()
            await fx.wait_reactivation()

        assert started >= 2

    async def test_the_settle_is_not_on_the_release_handlers_path(self) -> None:
        fx = _build_fixture()
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        fx.can_manager("sub_hand").activate_motors = AsyncMock(return_value=[])

        try:
            await fx.command({"type": "e_stop"})
            await fx.command({"type": "e_stop_release"})
            await asyncio.sleep(0)
            first = next(iter(fx.server._reactivate_tasks))
            assert not first.done()

            await fx.command({"type": "e_stop"})
            await fx.command({"type": "e_stop_release"})
            assert not first.done(), "解除の受理が前回の畳み込みを待っている"
            assert self._alive_reactivations(fx) == 2

            assert await wait_until(first.done), "1 本目が畳まれていない"
            assert self._alive_reactivations(fx) == 1
        finally:
            gate.set()
            await fx.wait_reactivation()

    async def test_release_proceeds_when_the_old_reactivation_refuses_to_cancel(self) -> None:
        fx = _build_fixture()
        release_first_call = asyncio.Event()
        calls: list[str] = []

        async def _stubborn_activate(**_kwargs: object) -> list[str]:
            calls.append("main_hand")
            if len(calls) == 1:
                while not release_first_call.is_set():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.sleep(0.01)
            return []

        fx.can_manager("main_hand").activate_motors = _stubborn_activate
        fx.can_manager("sub_hand").activate_motors = AsyncMock(return_value=[])

        try:
            await fx.command({"type": "e_stop"})
            await fx.command({"type": "e_stop_release"})
            assert await wait_until(lambda: calls == ["main_hand"])

            await fx.command({"type": "e_stop"})
            await fx.command({"type": "e_stop_release"})

            assert await wait_until(lambda: calls == ["main_hand", "main_hand"]), (
                "畳めない旧タスクを待って解除の励磁が進んでいない"
            )
        finally:
            release_first_call.set()
            with contextlib.suppress(TimeoutError):
                await fx.wait_reactivation(timeout=2.0)
