from __future__ import annotations

import can
from aiohttp.test_utils import TestClient, TestServer

from lib.can_manager import CANManager
from lib.health import BusHealth
from lib.sequence.engine import Sequence, step
from tests.fake_can import deliver_frame, mark_bus_off
from tests.fake_drivers import HealthFlagDriver
from tests.server_fixtures import ServerFixture, collect_types, drain, recv_type


class _DummySequence(Sequence):
    def __init__(self) -> None:
        super().__init__("test_seq")

    @step("ノーオペ")
    async def noop(self) -> None:
        return None


def _build_fixture_with_motors(
    *,
    bus_channel: str = "vsrvhealth0",
    fresh_feedback: bool = True,
) -> tuple[ServerFixture, CANManager, HealthFlagDriver, can.Bus]:
    fx = ServerFixture.build()
    mgr = CANManager()
    bus = can.Bus(interface="virtual", channel=bus_channel, receive_own_messages=False)
    motor = HealthFlagDriver("m1", 1)
    mgr.add_bus("bus0", bus, channel=bus_channel)
    mgr.add_motor("bus0", motor)

    if fresh_feedback:
        deliver_frame(mgr, "bus0", motor.feedback_message())

    fx.add_robot("main_hand", _DummySequence(), mgr)
    return fx, mgr, motor, bus


class TestHealthEndpointEmptyRobots:
    async def test_health_endpoint_empty_robots(self) -> None:
        fx = ServerFixture.build()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/health")
            assert resp.status == 200
            data = await resp.json()
            assert data["overall"] == "ok"
            assert data["robots"] == {}


class TestHealthEndpointReturns200WhenOk:
    async def test_health_endpoint_returns_200_when_ok(self) -> None:
        fx, _, _, bus = _build_fixture_with_motors(bus_channel="vsrvhealth_ok")
        try:
            app = fx.create_app()

            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/health")
                assert resp.status == 200
                data = await resp.json()
                assert data["overall"] == "ok"
                assert "main_hand" in data["robots"]
                snap = data["robots"]["main_hand"]
                assert snap["overall"] == "ok"
                assert isinstance(snap["buses"], list)
                assert isinstance(snap["motors"], list)
                assert snap["buses"][0]["name"] == "bus0"
                assert snap["motors"][0]["name"] == "m1"
                assert snap["motors"][0]["state"] == "ok"
        finally:
            bus.shutdown()


class TestHealthEndpointReturns503WhenDegraded:
    async def test_health_endpoint_returns_503_when_degraded(self) -> None:
        fx, _, _, bus = _build_fixture_with_motors(
            bus_channel="vsrvhealth_deg", fresh_feedback=False
        )
        try:
            app = fx.create_app()

            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/health")
                assert resp.status == 503
                data = await resp.json()
                assert data["overall"] == "degraded"
                snap = data["robots"]["main_hand"]
                assert snap["motors"][0]["state"] == "stale"
        finally:
            bus.shutdown()


class TestHealthEndpointReturns503WhenDown:
    async def test_health_endpoint_returns_503_when_down(self) -> None:
        fx, mgr, _, bus = _build_fixture_with_motors(bus_channel="vsrvhealth_down")
        try:
            mark_bus_off(mgr, "bus0")
            app = fx.create_app()

            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/health")
                assert resp.status == 503
                data = await resp.json()
                assert data["overall"] == "down"
                snap = data["robots"]["main_hand"]
                assert snap["buses"][0]["state"] == "down"
                assert snap["buses"][0]["bus_off"] is True
        finally:
            bus.shutdown()


class TestStateMessageIncludesHealth:
    async def test_state_message_includes_health(self) -> None:
        fx, _, _, bus = _build_fixture_with_motors(bus_channel="vsrvhealth_state")
        try:
            msg = fx.state_message("main_hand")
            assert "health" in msg
            health = msg["health"]
            assert health["overall"] == "ok"
            assert isinstance(health["buses"], list)
            assert isinstance(health["motors"], list)
            assert health["buses"][0]["name"] == "bus0"
            assert health["motors"][0]["name"] == "m1"
            assert msg["type"] == "state"
            assert msg["robot"] == "main_hand"
            assert "motors" in msg
            assert "e_stop_active" in msg
        finally:
            bus.shutdown()


class TestHealthChangeEventPushedOnStateTransition:
    async def test_health_change_event_pushed_on_state_transition(self) -> None:
        fx, _, motor, bus = _build_fixture_with_motors(bus_channel="vsrvhealth_change")
        try:
            app = fx.create_app()

            async with TestClient(TestServer(app)) as client:
                ws = await client.ws_connect("/ws")

                await fx.publish_state()

                await drain(ws, limit=5)

                motor.fault = True

                await fx.publish_state()

                changes = await collect_types(ws, {"health_change"}, tries=20)
                assert any(
                    msg["target"] == "motor:m1" and msg["to"] == "fault" for msg in changes
                ), "motor m1 ok→fault の health_change が配信されなかった"

                await ws.close()
        finally:
            bus.shutdown()


class TestHealthCheckCommandTriggersBroadcast:
    async def test_health_check_command_triggers_broadcast(self) -> None:
        fx, _, _, bus = _build_fixture_with_motors(bus_channel="vsrvhealth_cmd")
        try:
            app = fx.create_app()

            async with TestClient(TestServer(app)) as client:
                ws = await client.ws_connect("/ws")

                await drain(ws, limit=5)

                await ws.send_json({"type": "health_check"})

                msg = await recv_type(ws, "state", tries=20, timeout=0.1)
                assert msg is not None and "health" in msg, (
                    "health_check に対する state 配信が来なかった"
                )

                await ws.close()
        finally:
            bus.shutdown()


class TestHealthComputationFailure:
    async def test_exception_is_reported_as_down(self) -> None:
        fx, mgr, _motor, bus = _build_fixture_with_motors()
        try:

            def _boom(**_kwargs):
                raise RuntimeError("health 計算が壊れた")

            mgr.health = _boom  # type: ignore[method-assign]

            snap = fx.health("main_hand")

            assert snap.overall is BusHealth.DOWN
            assert snap.detail is not None
        finally:
            bus.shutdown()

    async def test_exception_makes_endpoint_return_503(self) -> None:
        fx, mgr, _motor, bus = _build_fixture_with_motors()
        try:

            def _boom(**_kwargs):
                raise RuntimeError("health 計算が壊れた")

            mgr.health = _boom  # type: ignore[method-assign]
            app = fx.create_app()

            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/health")
                assert resp.status == 503
                data = await resp.json()
                assert data["overall"] == "down"
        finally:
            bus.shutdown()

    async def test_non_snapshot_return_is_reported_as_down(self) -> None:
        fx, mgr, _motor, bus = _build_fixture_with_motors()
        try:
            mgr.health = lambda **_kwargs: None  # type: ignore[method-assign, assignment]

            snap = fx.health("main_hand")

            assert snap.overall is BusHealth.DOWN
            assert snap.detail is not None
        finally:
            bus.shutdown()

    async def test_failure_detail_reaches_state_message(self) -> None:
        fx, mgr, _motor, bus = _build_fixture_with_motors()
        try:

            def _boom(**_kwargs):
                raise RuntimeError("health 計算が壊れた")

            mgr.health = _boom  # type: ignore[method-assign]

            state = fx.state_message("main_hand")

            assert state["health"]["overall"] == "down"
            assert state["health"]["detail"]
        finally:
            bus.shutdown()
