from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from lib.can_manager import CANManager
from lib.control.target_refresh import GenericTargetRefresher
from lib.drivers.base import ControlMode, MotorState
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import M3508Driver
from lib.health import BusHealth, MotorHealth
from lib.sequence.engine import Sequence, step
from lib.sequence.motors import MotorGroup, MotorHandle
from tests.fake_can import mock_can_manager, set_motors
from tests.fake_health import ok_health_snapshot
from tests.feedback_frames import feed_generic, feed_m3508
from tests.server_fixtures import ServerFixture, recv_type


class DummySequence(Sequence):
    def __init__(self) -> None:
        super().__init__("test_seq")
        self.triggered = False

    @step("待機ステップ", require_trigger=True)
    async def wait_step(self) -> None:
        self.triggered = True


def _build_fixture() -> ServerFixture:
    fx = ServerFixture.build()
    fx.add_robot(
        "main_hand",
        DummySequence(),
        mock_can_manager(
            {"m3508_1": MotorState(position=1500.0, velocity=0.0, current=0.2, temperature=35.0)},
            bus_name="generic_bus",
        ),
    )
    return fx


class TestStateMessageFormat:
    async def test_state_message_format(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            await fx.publish_state()

            msg = await recv_type(ws, "state")
            assert msg is not None
            assert msg["type"] == "state"
            assert msg["robot"] == "main_hand"
            assert msg["sequence"] == "test_seq"
            assert "current_step" in msg
            assert "step_index" in msg
            assert "total_steps" in msg
            assert "waiting_trigger" in msg
            assert "motors" in msg
            assert "m3508_1" in msg["motors"]

            motor_data = msg["motors"]["m3508_1"]
            assert motor_data["pos"] == 1500.0
            assert motor_data["vel"] == 0.0
            assert motor_data["torque"] == 0.2
            assert motor_data["temp"] == 35.0

            await ws.close()


class TestTriggerCommand:
    async def test_trigger_command(self) -> None:
        fx = _build_fixture()
        fx.enter_match()
        seq = fx.sequence("main_hand")
        app = fx.create_app()

        task = asyncio.create_task(seq.run())
        await asyncio.sleep(0.05)
        assert seq.waiting_trigger is True

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "trigger", "robot": "main_hand"})
            await asyncio.sleep(0.05)

            assert seq.waiting_trigger is False
            await ws.close()

        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


class TestEStopCommand:
    async def test_e_stop_command(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "e_stop"})
            await asyncio.sleep(0.05)
            await ws.close()

        fx.can_manager("main_hand").send_to_bus.assert_called()


class TestUnknownCommandIgnored:
    async def test_unknown_command_ignored(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "totally_unknown_command"})
            await asyncio.sleep(0.05)

            assert not ws.closed

            await fx.publish_state()
            msg = await recv_type(ws, "state")
            assert msg is not None

            await ws.close()


class TestEStopSetsActiveState:
    async def test_e_stop_sets_active_state(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "e_stop"})
            await asyncio.sleep(0.05)

            assert fx.e_stop_active is True

            msg = await recv_type(ws, "e_stop_state")
            assert msg is not None
            assert msg["active"] is True

            await ws.close()


class TestEStopRelease:
    async def test_e_stop_release(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            await ws.send_json({"type": "e_stop"})
            await asyncio.sleep(0.05)
            msg = await recv_type(ws, "e_stop_state")
            assert msg is not None
            assert msg["active"] is True

            await ws.send_json({"type": "e_stop_release"})
            await asyncio.sleep(0.05)

            assert fx.e_stop_active is False

            found = False
            for _ in range(20):
                try:
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=0.1)
                except TimeoutError:
                    break
                if msg.get("type") == "e_stop_state" and msg.get("active") is False:
                    found = True
                    break
            assert found, "e_stop_state active=false メッセージが配信されなかった"

            await ws.close()


class TestStateIncludesEStopActive:
    async def test_state_includes_e_stop_active(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            await fx.publish_state()
            msg = await recv_type(ws, "state")
            assert msg is not None
            assert "e_stop_active" in msg
            assert msg["e_stop_active"] is False

            await ws.close()


def _fault_health_snapshot(mgr: CANManager):
    snap = ok_health_snapshot(mgr)
    for motor in snap.motors:
        motor.state = MotorHealth.FAULT
    snap.overall = BusHealth.DOWN
    return snap


class TestStateIncludesRunning:
    async def test_state_includes_running(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            await fx.publish_state()
            msg = await recv_type(ws, "state")
            assert msg is not None
            assert msg["running"] is False

            await ws.close()

    async def test_state_running_true_while_sequence_runs(self) -> None:
        fx = _build_fixture()
        fx.enter_match()
        seq = fx.sequence("main_hand")
        app = fx.create_app()

        task = asyncio.create_task(seq.run())
        await asyncio.sleep(0.05)

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await fx.publish_state()
            msg = await recv_type(ws, "state")
            assert msg is not None
            assert msg["running"] is True
            await ws.close()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestHealthChangeIncludesRobot:
    async def test_health_change_includes_robot(self) -> None:
        fx = _build_fixture()
        can_mgr = fx.can_manager("main_hand")
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            await fx.publish_state()
            can_mgr.health.side_effect = lambda **_kwargs: _fault_health_snapshot(can_mgr)
            await fx.publish_state()

            msg = await recv_type(ws, "health_change")
            assert msg is not None
            assert msg["robot"] == "main_hand"
            assert msg["target"] == "motor:m3508_1"

            await ws.close()


def _telemetry_fixture(*, dry_run: bool = False) -> ServerFixture:
    fx = ServerFixture.build(dry_run=dry_run)
    mgr = mock_can_manager(["y_axis_r"], bus_name="can_m3508")

    y_axis_r = M3508Driver("y_axis_r", 1)
    gripper = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
    conveyor = GenericDriver("conveyor", 0x80, control_type=ControlMode.DUTY)
    feed_m3508(y_axis_r, deg=90.0, rpm=120, current=200, temp=42)
    feed_generic(gripper, position=5.0, reached=True)
    feed_generic(conveyor)

    set_motors(mgr, {"y_axis_r": y_axis_r, "gripper": gripper, "conveyor": conveyor})
    fx.add_robot("main_hand", DummySequence(), mgr)
    return fx


class TestUnmeasuredTelemetryIsNull:
    async def _motors(self, fx: ServerFixture) -> dict:
        app = fx.create_app()
        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await fx.publish_state()
            msg = await recv_type(ws, "state")
            assert msg is not None
            await ws.close()
            return msg["motors"]

    async def test_dc_board_carries_no_numbers_at_all(self) -> None:
        motors = await self._motors(_telemetry_fixture())

        assert motors["conveyor"] == {
            "pos": None,
            "vel": None,
            "torque": None,
            "temp": None,
            "command": None,
            "command_mode": None,
        }

    async def test_servo_board_carries_position_only(self) -> None:
        motors = await self._motors(_telemetry_fixture())

        assert motors["gripper"]["pos"] == pytest.approx(5.0)
        assert motors["gripper"]["vel"] is None
        assert motors["gripper"]["torque"] is None
        assert motors["gripper"]["temp"] is None

    async def test_m3508_carries_all_four(self) -> None:
        motors = await self._motors(_telemetry_fixture())

        assert motors["y_axis_r"]["pos"] == pytest.approx(90.0, abs=0.1)
        assert motors["y_axis_r"]["vel"] == pytest.approx(120.0)
        assert motors["y_axis_r"]["torque"] is not None
        assert motors["y_axis_r"]["temp"] == pytest.approx(42.0)

    async def test_dry_run_follows_the_same_rule(self) -> None:
        motors = await self._motors(_telemetry_fixture(dry_run=True))

        assert motors["conveyor"]["pos"] is None
        assert motors["conveyor"]["vel"] is None
        assert motors["conveyor"]["torque"] is None
        assert motors["conveyor"]["temp"] is None
        assert motors["gripper"]["pos"] is not None, "机上で位置の描画を確かめられない"
        assert motors["gripper"]["temp"] is None
        assert motors["y_axis_r"]["temp"] is not None


def _command_fixture() -> tuple[ServerFixture, MotorGroup, GenericTargetRefresher]:
    fx = ServerFixture.build()
    mgr = mock_can_manager(["conveyor"], bus_name="can_generic")

    conveyor = GenericDriver("conveyor", 0x80, control_type=ControlMode.DUTY)
    gripper = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
    set_motors(mgr, {"conveyor": conveyor, "gripper": gripper})

    group = MotorGroup()
    for driver in (conveyor, gripper):
        group.add(MotorHandle(driver.name, driver, mgr, is_estop_active=lambda: fx.e_stop_active))

    sequence = DummySequence()
    sequence.bind_motors(group)
    refresher = GenericTargetRefresher(list(group.handles))
    fx.add_robot("main_hand", sequence, mgr, target_refreshers=[refresher])
    return fx, group, refresher


class TestCommandValue:
    async def test_duty_command_appears_with_its_mode(self) -> None:
        fx, group, _ = _command_fixture()
        await group["conveyor"].set_target(ControlMode.DUTY, 0.3)

        motor = fx.state_message("main_hand")["motors"]["conveyor"]
        assert motor["command"] == pytest.approx(0.3)
        assert motor["command_mode"] == "duty"
        assert motor["pos"] is None

    async def test_never_commanded_motor_is_null(self) -> None:
        fx, _group, _ = _command_fixture()

        motor = fx.state_message("main_hand")["motors"]["gripper"]
        assert motor["command"] is None
        assert motor["command_mode"] is None

    async def test_e_stop_clears_the_command(self) -> None:
        fx, group, _ = _command_fixture()
        await group["conveyor"].set_target(ControlMode.DUTY, 0.3)

        await fx.activate_e_stop(reason="テスト")

        motor = fx.state_message("main_hand")["motors"]["conveyor"]
        assert motor["command"] is None, "停止中なのに指令が出続けているように見える"
        assert motor["command_mode"] is None

    async def test_motor_outside_the_group_is_null(self) -> None:
        fx, _group, _ = _command_fixture()
        mgr = fx.can_manager("main_hand")
        spare = GenericDriver("spare_valve", 0x81, control_type=ControlMode.ON_OFF)
        set_motors(mgr, {**mgr.motors, "spare_valve": spare})

        motor = fx.state_message("main_hand")["motors"]["spare_valve"]
        assert motor["command"] is None
        assert motor["command_mode"] is None

    async def test_robot_without_bound_motors_does_not_raise(self) -> None:
        fx = ServerFixture.build()
        mgr = mock_can_manager(["conveyor"], bus_name="can_generic")
        set_motors(
            mgr, {"conveyor": GenericDriver("conveyor", 0x80, control_type=ControlMode.DUTY)}
        )
        fx.add_robot("main_hand", DummySequence(), mgr)

        motor = fx.state_message("main_hand")["motors"]["conveyor"]
        assert motor["command"] is None
        assert motor["command_mode"] is None
