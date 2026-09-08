from __future__ import annotations

import time

import can
from aiohttp.test_utils import TestClient, TestServer

from lib.can_manager import CANManager
from lib.config_schema import DEFAULT_HEALTH
from lib.drivers.base import ControlMode
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import M3508Driver
from lib.sequence.engine import Sequence, step
from lib.server import _FIRMWARE_INFO_GRACE_S
from tests.fake_can import deliver_frame, mark_feedback_at
from tests.feedback_frames import generic_feedback, generic_info, m3508_feedback
from tests.server_fixtures import ServerFixture, wait_until

_BUS = "can_generic"


class _DummySequence(Sequence):
    def __init__(self, name: str) -> None:
        super().__init__(name)

    @step("ノーオペ")
    async def noop(self) -> None:
        return None


def _build_can_manager(*, bus_channel: str) -> tuple[CANManager, can.Bus]:
    mgr = CANManager()
    bus = can.Bus(interface="virtual", channel=bus_channel, receive_own_messages=False)
    mgr.add_bus(_BUS, bus, channel=bus_channel)
    return mgr, bus


def _feed_alive(mgr: CANManager, motor: GenericDriver) -> None:
    deliver_frame(mgr, _BUS, generic_feedback(motor, position=0.0))


class TestFirmwareUnconfirmedMotorsAreVisible:
    async def test_猶予を過ぎても未受信なら_safety_に載る(self) -> None:
        fx = ServerFixture.build()
        mgr, bus = _build_can_manager(bus_channel="vfw0")
        motor = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
        mgr.add_motor(_BUS, motor)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            _feed_alive(mgr, motor)
            fx.expire_firmware_grace()
            reported = await wait_until(
                lambda: (
                    fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"]
                    == ["gripper"]
                )
            )
            assert reported, "INFO 未受信のモータが safety に載っていない"

        bus.shutdown()

    async def test_猶予の間は報告しない(self) -> None:
        fx = ServerFixture.build()
        mgr, bus = _build_can_manager(bus_channel="vfw1")
        motor = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
        mgr.add_motor(_BUS, motor)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            _feed_alive(mgr, motor)
            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == []

        bus.shutdown()

    async def test_info_を受けたモータは載らない(self) -> None:
        fx = ServerFixture.build()
        mgr, bus = _build_can_manager(bus_channel="vfw2")
        motor = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
        mgr.add_motor(_BUS, motor)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            _feed_alive(mgr, motor)
            deliver_frame(mgr, _BUS, generic_info(motor, firmware_version=1))
            fx.expire_firmware_grace()

            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == []

        bus.shutdown()

    async def test_info_を送らないドライバは対象外(self) -> None:
        fx = ServerFixture.build()
        mgr, bus = _build_can_manager(bus_channel="vfw3")
        motor = M3508Driver("y_axis_r", can_id=1)
        mgr.add_motor(_BUS, motor)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            deliver_frame(mgr, _BUS, m3508_feedback(motor, angle_raw=0))
            fx.expire_firmware_grace()

            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == []

        bus.shutdown()

    async def test_他ロボットへ巻き添えを出さない(self) -> None:
        fx = ServerFixture.build()
        mgr_main, bus_main = _build_can_manager(bus_channel="vfw4")
        mgr_sub, bus_sub = _build_can_manager(bus_channel="vfw5")
        motor_main = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
        motor_sub = GenericDriver("wall", 0x41, control_type=ControlMode.POSITION)
        mgr_main.add_motor(_BUS, motor_main)
        mgr_sub.add_motor(_BUS, motor_sub)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr_main)
        fx.add_robot("sub_hand", _DummySequence("sub_hand"), mgr_sub)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            _feed_alive(mgr_main, motor_main)
            _feed_alive(mgr_sub, motor_sub)
            deliver_frame(mgr_sub, _BUS, generic_info(motor_sub, firmware_version=1))
            fx.expire_firmware_grace()

            reported = await wait_until(
                lambda: (
                    fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"]
                    == ["gripper"]
                )
            )
            assert reported, "main_hand 側の未受信が safety に載っていない"
            assert fx.state_message("sub_hand")["safety"]["firmware_unconfirmed_motors"] == []

        bus_main.shutdown()
        bus_sub.shutdown()

    async def test_dry_run_では出さない(self) -> None:
        fx = ServerFixture.build(dry_run=True)
        mgr, bus = _build_can_manager(bus_channel="vfw6")
        motor = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
        mgr.add_motor(_BUS, motor)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            _feed_alive(mgr, motor)
            fx.expire_firmware_grace()

            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == []

        bus.shutdown()


class TestSensorSlotsAreCovered:
    def _sensor_only(self, *, bus_channel: str) -> tuple[CANManager, can.Bus, GenericDriver]:
        mgr, bus = _build_can_manager(bus_channel=bus_channel)
        sensor = GenericDriver("origin_sensor", 0x44, control_type=ControlMode.POSITION)
        mgr.add_sensor(_BUS, sensor)
        return mgr, bus, sensor

    async def test_info_未受信のセンサが_safety_に載る(self) -> None:
        fx = ServerFixture.build()
        mgr, bus, sensor = self._sensor_only(bus_channel="vfwc")
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            deliver_frame(mgr, _BUS, generic_feedback(sensor))
            fx.expire_firmware_grace()
            reported = await wait_until(
                lambda: (
                    fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"]
                    == ["origin_sensor"]
                )
            )
            assert reported, "INFO 未受信のセンサが safety に載っていない"

        bus.shutdown()

    async def test_info_を受けたセンサは載らない(self) -> None:
        fx = ServerFixture.build()
        mgr, bus, sensor = self._sensor_only(bus_channel="vfwd")
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            deliver_frame(mgr, _BUS, generic_feedback(sensor))
            deliver_frame(mgr, _BUS, generic_info(sensor, firmware_version=1))
            fx.expire_firmware_grace()

            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == []

        bus.shutdown()

    async def test_途絶えたセンサは対象外(self) -> None:
        fx = ServerFixture.build()
        mgr, bus, sensor = self._sensor_only(bus_channel="vfwe")
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            deliver_frame(mgr, _BUS, generic_feedback(sensor))
            mark_feedback_at(
                mgr,
                "origin_sensor",
                time.time() - (DEFAULT_HEALTH.feedback_timeout_ms / 1000.0) - 0.1,
            )
            fx.expire_firmware_grace()

            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == []

        bus.shutdown()


class TestStaleMotorsAreExcluded:
    async def test_フィードバックが途絶えたモータは対象外(self) -> None:
        fx = ServerFixture.build()
        mgr, bus = _build_can_manager(bus_channel="vfw7")
        motor = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
        mgr.add_motor(_BUS, motor)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            _feed_alive(mgr, motor)
            mark_feedback_at(
                mgr,
                "gripper",
                time.time() - (DEFAULT_HEALTH.feedback_timeout_ms / 1000.0) - 0.1,
            )
            fx.expire_firmware_grace()

            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == []

        bus.shutdown()

    async def test_一度もフィードバックが来ていないモータは対象外(self) -> None:
        fx = ServerFixture.build()
        mgr, bus = _build_can_manager(bus_channel="vfw8")
        motor = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
        mgr.add_motor(_BUS, motor)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            fx.expire_firmware_grace()

            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == []

        bus.shutdown()

    async def test_鮮度が生きていれば対象(self) -> None:
        fx = ServerFixture.build()
        mgr, bus = _build_can_manager(bus_channel="vfw9")
        motor = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
        mgr.add_motor(_BUS, motor)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            _feed_alive(mgr, motor)
            mark_feedback_at(
                mgr,
                "gripper",
                time.time() - (DEFAULT_HEALTH.feedback_timeout_ms / 1000.0) + 0.2,
            )
            fx.expire_firmware_grace()

            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == [
                "gripper"
            ]

        bus.shutdown()


class TestGraceIsAnchoredToStartupOnly:
    async def test_試合開始で猶予は置き直されない(self) -> None:
        fx = ServerFixture.build()
        mgr, bus = _build_can_manager(bus_channel="vfwa")
        motor = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
        mgr.add_motor(_BUS, motor)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            _feed_alive(mgr, motor)
            fx.expire_firmware_grace()
            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == [
                "gripper"
            ]

            fx.complete_all_checklists()
            await fx.command({"type": "match_start"})
            assert fx.match.phase.value == "match", "試合開始が通っていない"

            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == [
                "gripper"
            ], "試合開始で猶予が置き直され、報告が消えた"

        bus.shutdown()

    async def test_緊急停止で猶予は置き直されない(self) -> None:
        fx = ServerFixture.build()
        mgr, bus = _build_can_manager(bus_channel="vfwb")
        motor = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
        mgr.add_motor(_BUS, motor)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            _feed_alive(mgr, motor)
            fx.expire_firmware_grace()

            await fx.activate_e_stop(reason="テスト")
            assert fx.e_stop_active

            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == [
                "gripper"
            ], "緊急停止で猶予が置き直され、報告が消えた"

        bus.shutdown()


def test_猶予は_INFO_数周期ぶんに留める() -> None:
    assert _FIRMWARE_INFO_GRACE_S <= 10.0
