from __future__ import annotations

import time

import can

from lib.can_manager import CANManager
from lib.config_schema import HealthThresholds
from lib.drivers.base import ControlMode
from lib.drivers.generic import GenericDriver
from lib.sequence.engine import Sequence, step
from tests.fake_can import deliver_frame, mark_feedback_at
from tests.feedback_frames import generic_feedback
from tests.server_fixtures import ServerFixture

_BUS = "can_generic"
_TIMEOUT_MS = 500.0


class _DummySequence(Sequence):
    def __init__(self) -> None:
        super().__init__("sensor_seq")

    @step("ノーオペ")
    async def noop(self) -> None:
        return None


def _build(
    *, channel: str, dry_run: bool = False
) -> tuple[ServerFixture, CANManager, GenericDriver, can.Bus]:
    fx = ServerFixture.build(
        health=HealthThresholds(feedback_timeout_ms=_TIMEOUT_MS), dry_run=dry_run
    )
    mgr = CANManager()
    bus = can.Bus(interface="virtual", channel=channel, receive_own_messages=False)
    sensor = GenericDriver("origin_sensor", can_id=0x44, control_type=ControlMode.POSITION)
    mgr.add_bus(_BUS, bus, channel=channel)
    mgr.add_sensor(_BUS, sensor)

    fx.add_robot("main_hand", _DummySequence(), mgr)
    return fx, mgr, sensor, bus


def _sensors(fx: ServerFixture) -> dict:
    return fx.state_message("main_hand")["sensors"]


class TestSensorStateInBroadcast:
    async def test_contact_is_reported_as_active(self) -> None:
        fx, mgr, sensor, bus = _build(channel="vsensor_active")
        try:
            deliver_frame(mgr, _BUS, generic_feedback(sensor, sensor=True))
            assert _sensors(fx) == {"origin_sensor": {"active": True, "stale": False}}
        finally:
            bus.shutdown()

    async def test_release_is_reported_as_inactive(self) -> None:
        fx, mgr, sensor, bus = _build(channel="vsensor_release")
        try:
            deliver_frame(mgr, _BUS, generic_feedback(sensor, sensor=True))
            deliver_frame(mgr, _BUS, generic_feedback(sensor, sensor=False))
            assert _sensors(fx) == {"origin_sensor": {"active": False, "stale": False}}
        finally:
            bus.shutdown()

    async def test_never_received_is_stale(self) -> None:
        fx, _mgr, _sensor, bus = _build(channel="vsensor_unheard")
        try:
            assert _sensors(fx)["origin_sensor"]["stale"] is True
        finally:
            bus.shutdown()

    async def test_stale_uses_configured_threshold(self) -> None:
        fx, mgr, sensor, bus = _build(channel="vsensor_threshold")
        try:
            deliver_frame(mgr, _BUS, generic_feedback(sensor, sensor=True))
            mark_feedback_at(mgr, "origin_sensor", time.time() - _TIMEOUT_MS / 1000.0 * 2)
            assert _sensors(fx)["origin_sensor"] == {"active": True, "stale": True}
        finally:
            bus.shutdown()

    async def test_sensors_are_not_listed_as_motors(self) -> None:
        fx, mgr, sensor, bus = _build(channel="vsensor_notmotor")
        try:
            deliver_frame(mgr, _BUS, generic_feedback(sensor, sensor=True))
            message = fx.state_message("main_hand")
            assert message["motors"] == {}
            assert "origin_sensor" in message["sensors"]
        finally:
            bus.shutdown()

    async def test_dry_run_reports_sensors_without_feedback(self) -> None:
        fx, _mgr, _sensor, bus = _build(channel="vsensor_dryrun", dry_run=True)
        try:
            state = _sensors(fx)["origin_sensor"]
            assert state["stale"] is False
            assert isinstance(state["active"], bool)
        finally:
            bus.shutdown()
