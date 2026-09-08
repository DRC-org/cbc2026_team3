from __future__ import annotations

import asyncio
import struct
import time
from dataclasses import replace
from unittest.mock import MagicMock, patch

import can
import pytest

from lib.can_manager import CANManager
from lib.config_schema import DEFAULT_HEALTH
from lib.drivers.base import ControlMode
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import M3508Driver
from lib.health import BusHealth, HealthSnapshot, MotorHealth
from tests.fake_can import deliver_frame, direct_runner, mark_bus_off, mark_feedback_at
from tests.fake_clock import FakeClock
from tests.fake_drivers import HealthFlagDriver
from tests.feedback_frames import generic_feedback, generic_info, m3508_feedback


def _make_virtual_bus(channel: str) -> can.Bus:
    return can.Bus(interface="virtual", channel=channel, receive_own_messages=False)


@pytest.fixture
def mgr_with_motors():
    mgr = CANManager(run_blocking=direct_runner())
    bus = _make_virtual_bus("vhealth0")
    motor = HealthFlagDriver("m1", 1)
    mgr.add_bus("bus0", bus, channel="vhealth0")
    mgr.add_motor("bus0", motor)
    yield mgr, motor, bus
    bus.shutdown()


class TestCANManagerHealth:
    def test_initial_snapshot_all_stale(self, mgr_with_motors) -> None:
        mgr, _, _ = mgr_with_motors
        snap = mgr.health()
        assert isinstance(snap, HealthSnapshot)
        assert len(snap.buses) == 1
        assert len(snap.motors) == 1
        assert snap.buses[0].state is BusHealth.OK
        assert snap.motors[0].state is MotorHealth.STALE
        assert snap.motors[0].last_feedback_at is None

    def test_health_snapshot_structure(self, mgr_with_motors) -> None:
        mgr, _, _ = mgr_with_motors
        snap = mgr.health()
        assert isinstance(snap.timestamp, float)
        assert isinstance(snap.overall, BusHealth)
        assert isinstance(snap.buses, list)
        assert isinstance(snap.motors, list)
        assert snap.buses[0].name == "bus0"
        assert snap.buses[0].channel == "vhealth0"
        assert snap.motors[0].name == "m1"
        assert snap.motors[0].bus == "bus0"

    async def test_receive_records_last_rx_and_marks_ok(self, mgr_with_motors) -> None:
        mgr, motor, bus = mgr_with_motors
        feedback_msg = can.Message(arbitration_id=0x200 + motor.can_id, data=bytes(8))

        call_count = 0

        def recv_side_effect(timeout: float):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return feedback_msg
            raise asyncio.CancelledError

        with (
            patch.object(bus, "recv", side_effect=recv_side_effect),
            pytest.raises(asyncio.CancelledError),
        ):
            await mgr._receive_loop("bus0")

        snap = mgr.health(thresholds=replace(DEFAULT_HEALTH, feedback_timeout_ms=500.0))
        assert snap.motors[0].state is MotorHealth.OK
        assert snap.motors[0].last_feedback_at is not None
        assert snap.motors[0].feedback_age_ms is not None
        assert snap.motors[0].feedback_age_ms < 500.0

    def test_feedback_timeout_transitions_to_stale(self, mgr_with_motors) -> None:
        mgr, motor, _ = mgr_with_motors
        mark_feedback_at(mgr, motor.name, time.time() - 1.0)
        snap = mgr.health(thresholds=replace(DEFAULT_HEALTH, feedback_timeout_ms=100.0))
        assert snap.motors[0].state is MotorHealth.STALE

    def test_thermal_warning(self, mgr_with_motors) -> None:
        mgr, motor, _ = mgr_with_motors
        deliver_frame(mgr, "bus0", motor.feedback_message())
        motor.thermal_warning = True
        snap = mgr.health(thresholds=replace(DEFAULT_HEALTH, feedback_timeout_ms=500.0))
        assert snap.motors[0].state is MotorHealth.WARNING
        assert snap.overall is BusHealth.DEGRADED

    def test_thermal_fault(self, mgr_with_motors) -> None:
        mgr, motor, _ = mgr_with_motors
        deliver_frame(mgr, "bus0", motor.feedback_message())
        motor.thermal_fault = True
        snap = mgr.health(thresholds=replace(DEFAULT_HEALTH, feedback_timeout_ms=500.0))
        assert snap.motors[0].state is MotorHealth.FAULT
        assert snap.overall is BusHealth.DOWN

    def test_overcurrent_warning(self, mgr_with_motors) -> None:
        mgr, motor, _ = mgr_with_motors
        deliver_frame(mgr, "bus0", motor.feedback_message())
        motor.overcurrent = True
        snap = mgr.health(thresholds=replace(DEFAULT_HEALTH, feedback_timeout_ms=500.0))
        assert snap.motors[0].state is MotorHealth.WARNING

    def test_is_fault_takes_priority(self, mgr_with_motors) -> None:
        mgr, motor, _ = mgr_with_motors
        deliver_frame(mgr, "bus0", motor.feedback_message())
        motor.fault = True
        motor.thermal_warning = True
        snap = mgr.health(thresholds=replace(DEFAULT_HEALTH, feedback_timeout_ms=500.0))
        assert snap.motors[0].state is MotorHealth.FAULT

    async def test_send_failure_increments_tx_error_and_degrades(self) -> None:
        mgr = CANManager()
        bus = MagicMock()
        bus.send.side_effect = can.CanError("simulated tx failure")
        motor = HealthFlagDriver("m1", 1)
        mgr.add_bus("bus0", bus, channel="vhealth-fail")
        mgr.add_motor("bus0", motor)

        msg = can.Message(arbitration_id=0x100, data=bytes(8))
        for _ in range(3):
            with pytest.raises(can.CanError):
                await mgr.send_to_bus("bus0", msg)

        assert mgr._tx_error_count["bus0"] == 3

        snap = mgr.health(thresholds=replace(DEFAULT_HEALTH, tx_error_threshold=2))
        assert snap.buses[0].state is BusHealth.DEGRADED
        assert snap.buses[0].tx_error_count == 3

    async def test_degraded_clears_once_sending_recovers(self) -> None:
        mgr = CANManager(run_blocking=direct_runner())
        bus = MagicMock()
        mgr.add_bus("bus0", bus)
        mgr.add_motor("bus0", HealthFlagDriver("m1", 1))
        thresholds = replace(DEFAULT_HEALTH, tx_error_threshold=16)
        msg = can.Message(arbitration_id=0x100, data=bytes(8))

        bus.send.side_effect = can.CanError("相手が電源を失って ACK が返らない")
        for _ in range(5):
            with pytest.raises(can.CanError):
                await mgr.send_to_bus("bus0", msg)
        assert mgr.health(thresholds=thresholds).buses[0].state is BusHealth.DEGRADED

        bus.send.side_effect = None
        for _ in range(64):
            await mgr.send_to_bus("bus0", msg)

        snap = mgr.health(thresholds=thresholds)
        assert snap.buses[0].state is BusHealth.OK
        assert snap.buses[0].tx_error_count == 5

    # 実機の DM3520 の MST_ID 0x11 は CAN_ERR_TRX|CAN_ERR_TX_TIMEOUT と同じ値で、
    # 通常フレームとして扱うと本物のフィードバックと区別が付かなくなる。
    async def test_error_frame_marks_bus_off_and_is_not_delivered_to_motors(self) -> None:
        mgr = CANManager(run_blocking=direct_runner())
        bus = MagicMock()
        motor = HealthFlagDriver("m1", 1)
        mgr.add_bus("bus0", bus)
        mgr.add_motor("bus0", motor)

        bus_off_frame = can.Message(
            arbitration_id=motor.FEEDBACK_ID_BASE + motor.can_id | 0x40,
            data=bytes(8),
            is_error_frame=True,
        )
        bus.recv.side_effect = [bus_off_frame, asyncio.CancelledError()]
        with pytest.raises(asyncio.CancelledError):
            await mgr._receive_loop("bus0")

        snap = mgr.health()
        assert snap.buses[0].bus_off is True
        assert snap.buses[0].state is BusHealth.DOWN
        assert mgr.last_feedback_at("m1") is None

    async def test_bus_off_clears_once_traffic_returns(self) -> None:
        mgr = CANManager(run_blocking=direct_runner())
        bus = MagicMock()
        mgr.add_bus("bus0", bus)
        mgr.add_motor("bus0", HealthFlagDriver("m1", 1))
        mark_bus_off(mgr, "bus0")

        await mgr.send_to_bus("bus0", can.Message(arbitration_id=0x100, data=bytes(8)))

        assert mgr.health().buses[0].bus_off is False

    def test_bus_off_marks_down(self, mgr_with_motors) -> None:
        mgr, _, _ = mgr_with_motors
        mark_bus_off(mgr, "bus0")
        snap = mgr.health()
        assert snap.buses[0].state is BusHealth.DOWN
        assert snap.buses[0].bus_off is True
        assert snap.overall is BusHealth.DOWN

    async def test_send_success_records_last_tx_at(self) -> None:
        mgr = CANManager(run_blocking=direct_runner())
        bus = MagicMock()
        motor = HealthFlagDriver("m1", 1)
        mgr.add_bus("bus0", bus)
        mgr.add_motor("bus0", motor)

        before = time.time()
        msg = can.Message(arbitration_id=0x100, data=bytes(8))
        await mgr.send_to_bus("bus0", msg)

        assert mgr._last_tx_at["bus0"] >= before
        assert mgr._tx_error_count["bus0"] == 0

    async def test_frame_decode_failure_surfaces_as_rx_error_count(self) -> None:
        mgr = CANManager(run_blocking=direct_runner())
        bus = MagicMock()
        motor = HealthFlagDriver("m1", 1)
        motor.decode_feedback = MagicMock(  # type: ignore[method-assign]
            side_effect=struct.error("DLC 不足")
        )
        mgr.add_bus("bus0", bus, channel="vhealth-rx")
        mgr.add_motor("bus0", motor)

        feedback_msg = can.Message(arbitration_id=0x200 + motor.can_id, data=bytes(4))
        queue = [feedback_msg, feedback_msg]

        def recv_side_effect(timeout: float):
            if queue:
                return queue.pop(0)
            raise asyncio.CancelledError

        bus.recv.side_effect = recv_side_effect
        with pytest.raises(asyncio.CancelledError):
            await mgr._receive_loop("bus0")

        snap = mgr.health()
        assert snap.buses[0].rx_error_count == 2
        assert snap.motors[0].state is MotorHealth.STALE
        assert snap.buses[0].state is BusHealth.OK


class TestInfoDoesNotRefreshFeedbackAge:
    @pytest.fixture
    def mgr_with_servo(self):
        mgr = CANManager(run_blocking=direct_runner())
        bus = _make_virtual_bus("vinfo0")
        motor = GenericDriver("gripper", 0x40, expected_angle_range_deg=270.0)
        mgr.add_bus("bus0", bus, channel="vinfo0")
        mgr.add_motor("bus0", motor)
        yield mgr, motor
        bus.shutdown()

    def test_info_is_delivered_without_touching_age(self, mgr_with_servo) -> None:
        mgr, motor = mgr_with_servo
        deliver_frame(mgr, "bus0", generic_info(motor, firmware_version=2, angle_range_deg=270.0))

        assert motor.info is not None
        assert motor.info.angle_range_deg == pytest.approx(270.0)
        assert mgr.last_feedback_at("gripper") is None

    def test_info_does_not_rescue_a_stale_motor(self, mgr_with_servo) -> None:
        mgr, motor = mgr_with_servo
        mark_feedback_at(mgr, "gripper", time.time() - 1.0)

        deliver_frame(mgr, "bus0", generic_info(motor, firmware_version=2, angle_range_deg=270.0))

        snap = mgr.health(thresholds=replace(DEFAULT_HEALTH, feedback_timeout_ms=100.0))
        assert snap.motors[0].state is MotorHealth.STALE

    def test_feedback_still_refreshes_age(self, mgr_with_servo) -> None:
        mgr, motor = mgr_with_servo
        deliver_frame(mgr, "bus0", generic_feedback(motor, position=1.0))

        assert mgr.last_feedback_at("gripper") is not None


class TestReanchorSurfacesInHealth:
    def test_再アンカーしたモータは詳細付きのWARNINGになる(self) -> None:
        clock = FakeClock()
        mgr = CANManager(run_blocking=direct_runner())
        bus = _make_virtual_bus("vhealth_reanchor")
        motor = M3508Driver("y_axis_r", 1, time_source=clock)
        mgr.add_bus("bus0", bus, channel="vhealth_reanchor")
        mgr.add_motor("bus0", motor)

        try:
            deliver_frame(mgr, "bus0", m3508_feedback(motor, angle_raw=8000))
            clock.advance(1.0)
            deliver_frame(mgr, "bus0", m3508_feedback(motor, angle_raw=4108))

            info = next(m for m in mgr.health().motors if m.name == "y_axis_r")
            assert info.state is MotorHealth.WARNING, "再アンカーが平常として扱われている"
            assert info.detail is not None, "再アンカーの理由がどこにも出ていない"
        finally:
            bus.shutdown()

    def test_平常時は詳細もWARNINGも出さない(self) -> None:
        clock = FakeClock()
        mgr = CANManager(run_blocking=direct_runner())
        bus = _make_virtual_bus("vhealth_quiet")
        motor = M3508Driver("y_axis_r", 1, time_source=clock)
        mgr.add_bus("bus0", bus, channel="vhealth_quiet")
        mgr.add_motor("bus0", motor)

        try:
            deliver_frame(mgr, "bus0", m3508_feedback(motor, angle_raw=8000))
            clock.advance(0.001)
            deliver_frame(mgr, "bus0", m3508_feedback(motor, angle_raw=8100))

            info = next(m for m in mgr.health().motors if m.name == "y_axis_r")
            assert info.state is MotorHealth.OK
            assert info.detail is None
        finally:
            bus.shutdown()


class TestUnmeasuredTemperature:
    def _mgr(self, motor, channel: str) -> tuple[CANManager, can.Bus]:
        mgr = CANManager(run_blocking=direct_runner())
        bus = _make_virtual_bus(channel)
        mgr.add_bus("bus0", bus, channel=channel)
        mgr.add_motor("bus0", motor)
        return mgr, bus

    def test_dc_board_reports_no_temperature(self) -> None:
        motor = GenericDriver("conveyor", 0x80, control_type=ControlMode.DUTY)
        mgr, bus = self._mgr(motor, "vtemp_dc")
        try:
            deliver_frame(mgr, "bus0", generic_feedback(motor))

            info = next(m for m in mgr.health().motors if m.name == "conveyor")
            assert info.state is MotorHealth.OK
            assert info.temperature is None, "測っていない 0.0℃ が配信されている"
        finally:
            bus.shutdown()

    def test_m3508_still_reports_its_temperature(self) -> None:
        motor = M3508Driver("y_axis_r", 1)
        mgr, bus = self._mgr(motor, "vtemp_m3508")
        try:
            deliver_frame(mgr, "bus0", m3508_feedback(motor, angle_raw=0, temp=42))

            info = next(m for m in mgr.health().motors if m.name == "y_axis_r")
            assert info.temperature == pytest.approx(42.0)
        finally:
            bus.shutdown()


class TestMayAffectWorkpiece:
    def _mgr_with_bus(self, channel: str) -> tuple[CANManager, can.Bus]:
        mgr = CANManager(run_blocking=direct_runner())
        bus = _make_virtual_bus(channel)
        mgr.add_bus("bus0", bus, channel=channel)
        return mgr, bus

    def test_on_offのモータが載っていればTrue(self) -> None:
        mgr, bus = self._mgr_with_bus("vworkpiece_on_off")
        try:
            mgr.add_motor("bus0", GenericDriver("valve_1", 0x10, control_type=ControlMode.ON_OFF))
            assert mgr.health().buses[0].may_affect_workpiece is True
        finally:
            bus.shutdown()

    def test_on_off以外だけならFalse(self) -> None:
        mgr, bus = self._mgr_with_bus("vworkpiece_position")
        try:
            mgr.add_motor("bus0", GenericDriver("servo_1", 0x40, control_type=ControlMode.POSITION))
            mgr.add_motor("bus0", M3508Driver("y_axis_l", 1))
            assert mgr.health().buses[0].may_affect_workpiece is False
        finally:
            bus.shutdown()

    def test_モータの無いバスはFalse(self) -> None:
        mgr, bus = self._mgr_with_bus("vworkpiece_empty")
        try:
            assert mgr.health().buses[0].may_affect_workpiece is False
        finally:
            bus.shutdown()

    def test_on_offと他ドライバが混在してもTrue(self) -> None:
        mgr, bus = self._mgr_with_bus("vworkpiece_mixed")
        try:
            mgr.add_motor("bus0", M3508Driver("y_axis_l", 1))
            mgr.add_motor("bus0", GenericDriver("valve_1", 0x10, control_type=ControlMode.ON_OFF))
            assert mgr.health().buses[0].may_affect_workpiece is True
        finally:
            bus.shutdown()
