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
from lib.health import BusHealth, HealthSnapshot, MotorHealth
from tests.fake_can import deliver_frame, mark_bus_off, mark_feedback_at
from tests.fake_drivers import HealthFlagDriver
from tests.test_can_manager import _direct_runner


def _make_virtual_bus(channel: str) -> can.Bus:
    return can.Bus(interface="virtual", channel=channel, receive_own_messages=False)


@pytest.fixture
def mgr_with_motors():
    """共通 fixture: 1 バス + 1 モータの CANManager を返す。"""
    mgr = CANManager(run_blocking=_direct_runner())
    bus = _make_virtual_bus("vhealth0")
    motor = HealthFlagDriver("m1", 1)
    mgr.add_bus("bus0", bus, channel="vhealth0")
    mgr.add_motor("bus0", motor)
    yield mgr, motor
    bus.shutdown()


class TestCANManagerHealth:
    def test_initial_snapshot_all_stale(self, mgr_with_motors) -> None:
        # 受信ゼロの初期状態では全モータ STALE、バスは OK のはず
        mgr, _ = mgr_with_motors
        snap = mgr.health()
        assert isinstance(snap, HealthSnapshot)
        assert len(snap.buses) == 1
        assert len(snap.motors) == 1
        assert snap.buses[0].state is BusHealth.OK
        assert snap.motors[0].state is MotorHealth.STALE
        assert snap.motors[0].last_feedback_at is None

    def test_health_snapshot_structure(self, mgr_with_motors) -> None:
        # WS 配信で使う dataclass の基本フィールドが揃っていることを担保
        mgr, _ = mgr_with_motors
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
        # 受信ループがフィードバック鮮度を進め、十分新しければ OK 判定
        mgr, motor = mgr_with_motors
        feedback_msg = can.Message(arbitration_id=0x200 + motor.can_id, data=bytes(8))

        call_count = 0

        def recv_side_effect(timeout: float):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return feedback_msg
            raise asyncio.CancelledError

        bus = mgr._buses["bus0"]
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
        # last_rx_at が timeout を超えていると STALE
        mgr, motor = mgr_with_motors
        mark_feedback_at(mgr, motor.name, time.time() - 1.0)
        snap = mgr.health(thresholds=replace(DEFAULT_HEALTH, feedback_timeout_ms=100.0))
        assert snap.motors[0].state is MotorHealth.STALE

    def test_thermal_warning(self, mgr_with_motors) -> None:
        # 受信は新鮮 + 温度 WARNING フラグ → MotorHealth.WARNING
        mgr, motor = mgr_with_motors
        deliver_frame(mgr, "bus0", motor.feedback_message())
        motor.thermal_warning = True
        snap = mgr.health(thresholds=replace(DEFAULT_HEALTH, feedback_timeout_ms=500.0))
        assert snap.motors[0].state is MotorHealth.WARNING
        # overall は DEGRADED に正規化される (health.py の _MOTOR_TO_BUS_SEVERITY 参照)
        assert snap.overall is BusHealth.DEGRADED

    def test_thermal_fault(self, mgr_with_motors) -> None:
        # 温度 FAULT フラグは STALE/WARNING より優先される
        mgr, motor = mgr_with_motors
        deliver_frame(mgr, "bus0", motor.feedback_message())
        motor.thermal_fault = True
        snap = mgr.health(thresholds=replace(DEFAULT_HEALTH, feedback_timeout_ms=500.0))
        assert snap.motors[0].state is MotorHealth.FAULT
        assert snap.overall is BusHealth.DOWN

    def test_overcurrent_warning(self, mgr_with_motors) -> None:
        mgr, motor = mgr_with_motors
        deliver_frame(mgr, "bus0", motor.feedback_message())
        motor.overcurrent = True
        snap = mgr.health(thresholds=replace(DEFAULT_HEALTH, feedback_timeout_ms=500.0))
        assert snap.motors[0].state is MotorHealth.WARNING

    def test_is_fault_takes_priority(self, mgr_with_motors) -> None:
        # is_fault() True は最優先で FAULT
        mgr, motor = mgr_with_motors
        deliver_frame(mgr, "bus0", motor.feedback_message())
        motor.fault = True
        motor.thermal_warning = True  # 同時に warning でも FAULT 維持
        snap = mgr.health(thresholds=replace(DEFAULT_HEALTH, feedback_timeout_ms=500.0))
        assert snap.motors[0].state is MotorHealth.FAULT

    async def test_send_failure_increments_tx_error_and_degrades(self) -> None:
        # bus.send が CanError を投げると tx_error_count が増え、しきい値以上で DEGRADED
        mgr = CANManager()
        bus = MagicMock()
        bus.send.side_effect = can.CanError("simulated tx failure")
        motor = HealthFlagDriver("m1", 1)
        mgr.add_bus("bus0", bus, channel="vhealth-fail")
        mgr.add_motor("bus0", motor)

        msg = can.Message(arbitration_id=0x100, data=bytes(8))
        # 互換性維持のため例外は再 raise されるはず
        for _ in range(3):
            with pytest.raises(can.CanError):
                await mgr.send_to_bus("bus0", msg)

        assert mgr._tx_error_count["bus0"] == 3

        # しきい値 2 で DEGRADED 判定
        snap = mgr.health(thresholds=replace(DEFAULT_HEALTH, tx_error_threshold=2))
        assert snap.buses[0].state is BusHealth.DEGRADED
        assert snap.buses[0].tx_error_count == 3

    def test_bus_off_marks_down(self, mgr_with_motors) -> None:
        mgr, _ = mgr_with_motors
        mark_bus_off(mgr, "bus0")
        snap = mgr.health()
        assert snap.buses[0].state is BusHealth.DOWN
        assert snap.buses[0].bus_off is True
        assert snap.overall is BusHealth.DOWN

    async def test_send_success_records_last_tx_at(self) -> None:
        # 送信成功時は _last_tx_at が更新され、tx_error_count は据え置き
        mgr = CANManager(run_blocking=_direct_runner())
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
        """握り潰したフレームは rx_error_count としてヘルスに現れなければならない。

        ヘルス配信が「全部 OK」と言い続けるなら、握り潰しはバグの隠蔽と同じになる。
        """
        mgr = CANManager(run_blocking=_direct_runner())
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
        # デコードできなかったフレームを受信扱いしないので、モータは STALE のまま出る
        assert snap.motors[0].state is MotorHealth.STALE
        # バスの判定自体は変えない。フレームを解釈できない機器が同じバスに相乗りする
        # (メインハンドとサブハンドは can_edulite / can_generic を物理的に共有する)
        # だけで DEGRADED になると、本物の送信障害の警告まで信用されなくなる
        assert snap.buses[0].state is BusHealth.OK
