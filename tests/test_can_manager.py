from __future__ import annotations

import asyncio
import struct
import time
from unittest.mock import AsyncMock, MagicMock, patch

import can
import pytest

from lib.can_manager import CANManager
from lib.drivers.base import MotorState
from lib.drivers.generic import CommandType, GenericDriver


def _make_mock_bus() -> MagicMock:
    bus = MagicMock()
    bus.recv.return_value = None
    return bus


def _make_mock_motor(name: str, can_id: int) -> MagicMock:
    motor = MagicMock()
    motor.name = name
    motor.can_id = can_id
    motor.matches_feedback.return_value = False
    motor.update_state.return_value = MotorState()
    motor.initialization_steps.return_value = []
    motor.activation_steps.return_value = []
    motor.requires_fresh_feedback_for_activation.return_value = False
    motor.feedback_probe_message.return_value = None
    return motor


class TestCANManager:
    def test_add_bus_and_motor(self) -> None:
        mgr = CANManager()
        bus = _make_mock_bus()
        motor = _make_mock_motor("m1", 1)

        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)

        assert mgr.get_motor("m1") is motor

    def test_get_motor(self) -> None:
        mgr = CANManager()
        bus = _make_mock_bus()
        motor = _make_mock_motor("drive", 2)

        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)

        assert mgr.get_motor("drive") is motor

    def test_get_motor_not_found(self) -> None:
        mgr = CANManager()
        with pytest.raises(KeyError):
            mgr.get_motor("nonexistent")

    async def test_send_to_correct_bus(self) -> None:
        mgr = CANManager()
        bus0 = _make_mock_bus()
        bus1 = _make_mock_bus()
        motor = _make_mock_motor("m1", 1)

        mgr.add_bus("can0", bus0)
        mgr.add_bus("can1", bus1)
        mgr.add_motor("can0", motor)

        msg = can.Message(arbitration_id=0x200, data=bytes(8))

        with patch("asyncio.get_event_loop") as mock_loop:
            mock_loop.return_value.run_in_executor = AsyncMock()
            await mgr.send("m1", msg)
            mock_loop.return_value.run_in_executor.assert_called_once_with(None, bus0.send, msg)

    async def test_initialize_motors_sends_steps_with_declared_delays(self) -> None:
        mgr = CANManager()
        bus = _make_mock_bus()
        motor = _make_mock_motor("m1", 1)
        first = can.Message(arbitration_id=0x201, data=bytes(8))
        second = can.Message(arbitration_id=0x202, data=bytes(8))
        motor.initialization_steps.return_value = [(first, 0.05), (second, 0.1)]
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)

        with (
            patch.object(mgr, "send", new_callable=AsyncMock) as send,
            patch("lib.can_manager.asyncio.sleep", new_callable=AsyncMock) as sleep,
        ):
            await mgr.initialize_motors()

        assert send.await_args_list[0].args == ("m1", first)
        assert send.await_args_list[1].args == ("m1", second)
        assert [call.args[0] for call in sleep.await_args_list] == [0.05, 0.1]

    async def test_run_initializes_motors_after_starting_receivers(self) -> None:
        mgr = CANManager()
        mgr.add_bus("can0", _make_mock_bus())

        with patch.object(mgr, "initialize_motors", new_callable=AsyncMock) as initialize_motors:
            await mgr.run()

        initialize_motors.assert_awaited_once_with()
        assert len(mgr._tasks) == 1
        await mgr.shutdown()

    async def test_receive_updates_motor_state(self) -> None:
        mgr = CANManager()
        bus = _make_mock_bus()
        motor = _make_mock_motor("m1", 1)
        motor.matches_feedback.return_value = True

        feedback_state = MotorState(position=90.0, velocity=100.0)
        motor.update_state.return_value = feedback_state

        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)

        feedback_msg = can.Message(arbitration_id=0x201, data=bytes(8))

        call_count = 0

        def recv_side_effect(timeout: float) -> can.Message | None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return feedback_msg
            raise asyncio.CancelledError

        bus.recv.side_effect = recv_side_effect

        with patch("asyncio.get_event_loop") as mock_loop:

            async def fake_executor(executor, fn, *args):
                return fn(*args)

            mock_loop.return_value.run_in_executor = AsyncMock(side_effect=fake_executor)

            with pytest.raises(asyncio.CancelledError):
                await mgr._receive_loop("can0")

        motor.matches_feedback.assert_called_once_with(feedback_msg)
        motor.update_state.assert_called_once_with(feedback_msg)

    async def test_state_update_callback(self) -> None:
        mgr = CANManager()
        bus = _make_mock_bus()
        motor = _make_mock_motor("m1", 1)
        motor.matches_feedback.return_value = True

        feedback_state = MotorState(position=45.0)
        motor.update_state.return_value = feedback_state

        callback = MagicMock()
        mgr.set_on_state_update(callback)

        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)

        feedback_msg = can.Message(arbitration_id=0x201, data=bytes(8))
        call_count = 0

        def recv_side_effect(timeout: float) -> can.Message | None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return feedback_msg
            raise asyncio.CancelledError

        bus.recv.side_effect = recv_side_effect

        with patch("asyncio.get_event_loop") as mock_loop:

            async def fake_executor(executor, fn, *args):
                return fn(*args)

            mock_loop.return_value.run_in_executor = AsyncMock(side_effect=fake_executor)

            with pytest.raises(asyncio.CancelledError):
                await mgr._receive_loop("can0")

        callback.assert_called_once_with("m1", feedback_state)

    async def test_shutdown(self) -> None:
        mgr = CANManager()
        bus0 = _make_mock_bus()
        bus1 = _make_mock_bus()

        mgr.add_bus("can0", bus0)
        mgr.add_bus("can1", bus1)

        await mgr.shutdown()

        bus0.shutdown.assert_called_once()
        bus1.shutdown.assert_called_once()


class TestMotorActivation:
    """励磁の有効化は「有効化した瞬間に動かない」ことを保証してからでないと行えない。"""

    def _prepare(self) -> tuple[CANManager, MagicMock]:
        mgr = CANManager()
        mgr.add_bus("can0", _make_mock_bus())
        motor = _make_mock_motor("m1", 1)
        mgr.add_motor("can0", motor)
        return mgr, motor

    async def test_initialize_motors_activates_after_initialization_steps(self) -> None:
        mgr, motor = self._prepare()
        init_msg = can.Message(arbitration_id=0x201, data=bytes(8))
        enable_msg = can.Message(arbitration_id=0x202, data=bytes(8))
        motor.initialization_steps.return_value = [(init_msg, 0.0)]
        motor.activation_steps.return_value = [(enable_msg, 0.0)]

        with patch.object(mgr, "send", new_callable=AsyncMock) as send:
            await mgr.initialize_motors()

        assert [call.args[1] for call in send.await_args_list] == [init_msg, enable_msg]

    async def test_activation_reads_position_after_fresh_feedback_arrives(self) -> None:
        """set_zero 後の原点を反映した実測角でなければ、目標として書いてはいけない。"""
        mgr, motor = self._prepare()
        motor.requires_fresh_feedback_for_activation.return_value = True
        motor.feedback_probe_message.return_value = can.Message(arbitration_id=0x203, data=bytes(8))
        enable_msg = can.Message(arbitration_id=0x202, data=bytes(8))

        # 待機開始前の受信は set_zero 前の可能性があるため、認めてはならない
        mgr._last_rx_at["m1"] = time.time()

        seen_rx_at: list[float | None] = []

        def record_activation() -> list[tuple[can.Message, float]]:
            seen_rx_at.append(mgr._last_rx_at.get("m1"))
            return [(enable_msg, 0.0)]

        motor.activation_steps.side_effect = record_activation

        async def fake_send(name: str, msg: can.Message) -> None:
            # 問い合わせフレームへの応答としてフィードバックが届く状況を模す
            mgr._last_rx_at[name] = time.time()

        with patch.object(mgr, "send", new_callable=AsyncMock, side_effect=fake_send):
            activated = await mgr.activate_motor("m1", feedback_timeout_s=0.5)

        assert activated is True
        assert seen_rx_at and seen_rx_at[0] is not None
        assert seen_rx_at[0] > mgr._last_rx_at["m1"] - 1.0

    async def test_activation_skipped_when_feedback_never_arrives(self) -> None:
        """現在角が分からないまま enable すると原点へ飛ぶため、無励磁のままにする。"""
        mgr, motor = self._prepare()
        motor.requires_fresh_feedback_for_activation.return_value = True
        motor.activation_steps.return_value = [
            (can.Message(arbitration_id=0x202, data=bytes(8)), 0.0)
        ]

        with patch.object(mgr, "send", new_callable=AsyncMock) as send:
            activated = await mgr.activate_motor("m1", feedback_timeout_s=0.05)

        assert activated is False
        motor.activation_steps.assert_not_called()
        assert send.await_count == 0

    async def test_activation_requires_feedback_newer_than_wait_start(self) -> None:
        mgr, motor = self._prepare()
        motor.requires_fresh_feedback_for_activation.return_value = True
        motor.activation_steps.return_value = [
            (can.Message(arbitration_id=0x202, data=bytes(8)), 0.0)
        ]
        mgr._last_rx_at["m1"] = time.time()

        with patch.object(mgr, "send", new_callable=AsyncMock):
            activated = await mgr.activate_motor("m1", feedback_timeout_s=0.05)

        assert activated is False
        motor.activation_steps.assert_not_called()

    async def test_activate_motors_stops_when_abort_requested(self) -> None:
        """緊急停止が再び入ったら、途中でも enable を送ってはならない。"""
        mgr, motor = self._prepare()
        motor.activation_steps.return_value = [
            (can.Message(arbitration_id=0x202, data=bytes(8)), 0.0)
        ]

        with patch.object(mgr, "send", new_callable=AsyncMock) as send:
            await mgr.activate_motors(should_abort=lambda: True)

        assert send.await_count == 0


class TestReceiveLoopRobustness:
    """受信ループは想定外のフレーム 1 通で死んではならない。

    死ぬとそのバスの全モータが永久に STALE になり、試合中は復旧不能になる。
    """

    @staticmethod
    def _drain_recv(bus: MagicMock, messages: list[can.Message]) -> None:
        queue = list(messages)

        def recv_side_effect(timeout: float) -> can.Message | None:
            if queue:
                return queue.pop(0)
            raise asyncio.CancelledError

        bus.recv.side_effect = recv_side_effect

    @staticmethod
    def _feedback_msg(device_id: int, position_dg: int) -> can.Message:
        data = bytearray(8)
        struct.pack_into("<h", data, 0, position_dg)
        return can.Message(
            arbitration_id=GenericDriver.build_can_id(CommandType.FEEDBACK, device_id),
            data=bytes(data),
            is_extended_id=False,
        )

    async def _run_loop(self, mgr: CANManager) -> None:
        with patch("asyncio.get_event_loop") as mock_loop:

            async def fake_executor(executor, fn, *args):
                return fn(*args)

            mock_loop.return_value.run_in_executor = AsyncMock(side_effect=fake_executor)

            with pytest.raises(asyncio.CancelledError):
                await mgr._receive_loop("can0")

    @pytest.mark.parametrize("reserved_command_type", [0b100, 0b101, 0b110])
    async def test_receive_loop_survives_reserved_command_type(
        self, reserved_command_type: int
    ) -> None:
        mgr = CANManager()
        bus = _make_mock_bus()
        motor = GenericDriver("gripper", 0x01)
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)

        bogus = can.Message(
            arbitration_id=(reserved_command_type << 8) | 0x01,
            data=bytes(8),
            is_extended_id=False,
        )
        self._drain_recv(bus, [bogus, self._feedback_msg(0x01, 900)])

        await self._run_loop(mgr)

        # 予約種別のフレームの後でも、続くフィードバックが取り込めていること
        assert motor.state.position == pytest.approx(90.0)

    async def test_receive_loop_survives_extended_frame(self) -> None:
        mgr = CANManager()
        bus = _make_mock_bus()
        motor = GenericDriver("gripper", 0x01)
        mgr.add_bus("can0", bus)
        mgr.add_motor("can0", motor)

        alien = can.Message(arbitration_id=0x12345678, data=bytes(8), is_extended_id=True)
        self._drain_recv(bus, [alien, self._feedback_msg(0x01, 450)])

        await self._run_loop(mgr)

        assert motor.state.position == pytest.approx(45.0)
