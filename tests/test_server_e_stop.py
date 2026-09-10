from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
import time
from unittest.mock import AsyncMock, MagicMock, patch

import can
import pytest
from aiohttp.test_utils import TestClient, TestServer

from lib.axis_sync import MotorSpec, SyncGroup
from lib.can_manager import CANManager
from lib.control.limit_monitor import LimitMonitor
from lib.control.position_loop import M3508PositionLoop, make_position_pid
from lib.control.sync_monitor import SyncMonitor
from lib.control.target_refresh import GenericTargetRefresher
from lib.drivers.base import ControlMode
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import M3508Driver
from lib.health import BusHealth, BusHealthInfo, HealthSnapshot, MotorHealth, MotorHealthInfo
from lib.match_state import Court, Phase
from lib.sequence.engine import Sequence, step
from lib.sequence.motors import MotorGroup, MotorHandle
from lib.sequence.positions import load_position_table
from tests.fake_can import (
    direct_runner,
    keep_feedback_fresh,
    mark_feedback_at,
    mock_bus,
    mock_can_manager,
    mock_driver,
    set_motors,
)
from tests.fake_drivers import StubFeedbackDriver
from tests.feedback_frames import feed_generic, feed_m3508
from tests.server_fixtures import ServerFixture, drain, recv_type, wait_until

_ROBOT_NAMES = ("main_hand", "sub_hand")


class GatedSequence(Sequence):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.executed: list[str] = []
        self.gate = asyncio.Event()

    @step("ゲートステップ")
    async def gate_step(self) -> None:
        self.executed.append("gate")
        await self.gate.wait()

    @step("後続ステップ")
    async def after_step(self) -> None:
        self.executed.append("after")


def _build_fixture() -> ServerFixture:
    fx = ServerFixture.build()
    for name in _ROBOT_NAMES:
        fx.add_robot(name, GatedSequence(name))
    return fx


async def _start_both_sequences(fx: ServerFixture, ws) -> list[GatedSequence]:
    fx.complete_all_checklists()
    await ws.send_json({"type": "match_start"})
    for name in _ROBOT_NAMES:
        await ws.send_json({"type": "sequence_start", "robot": name})

    seqs = fx.sequences()
    started = await wait_until(lambda: all(s.executed == ["gate"] for s in seqs))
    assert started, "シーケンスがゲートステップまで進まなかった"
    return seqs


async def _release_gates_and_settle(seqs: list[GatedSequence]) -> None:
    for s in seqs:
        s.gate.set()
    await wait_until(lambda: all(not s.is_running for s in seqs))


async def _spin_resident_loop(seq: GatedSequence, *, settle_s: float = 0.05) -> None:
    task = asyncio.create_task(seq.run_forever())
    await asyncio.sleep(settle_s)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _expect_no_rejection(ws, command: str, *, tries: int = 40) -> None:
    for _ in range(tries):
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=0.2)
        except (TimeoutError, TypeError):
            return
        if msg.get("type") == "command_rejected" and msg.get("command") == command:
            raise AssertionError(f"{command} が拒否された: {msg.get('reason')}")


async def _enter_e_stop(fx: ServerFixture, ws) -> list[GatedSequence]:
    seqs = await _start_both_sequences(fx, ws)
    await ws.send_json({"type": "e_stop"})
    await wait_until(lambda: fx.e_stop_active)
    await _release_gates_and_settle(seqs)
    return seqs


class TestEStopStopsSequences:
    async def test_e_stop_stops_all_running_sequences(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _start_both_sequences(fx, ws)

            await ws.send_json({"type": "e_stop"})
            await wait_until(lambda: fx.e_stop_active)

            await _release_gates_and_settle(seqs)

            for s in seqs:
                assert s.executed == ["gate"], f"{s.name}: 緊急停止後に後続ステップが実行された"
                assert s.is_running is False, f"{s.name}: 緊急停止後もシーケンスが実行中"

            await ws.close()

    async def test_e_stop_stops_sequences_when_bus_send_fails(self) -> None:
        fx = _build_fixture()
        for mgr in fx.can_managers():
            mgr.send_to_bus = AsyncMock(side_effect=RuntimeError("バス送信失敗"))
            mgr.send = AsyncMock(side_effect=RuntimeError("モータ送信失敗"))
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _start_both_sequences(fx, ws)

            await ws.send_json({"type": "e_stop"})
            await wait_until(lambda: fx.e_stop_active)

            await _release_gates_and_settle(seqs)

            for s in seqs:
                assert s.executed == ["gate"]
                assert s.is_running is False

            await ws.close()

    async def test_e_stop_stops_sequences_when_encode_raises(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _start_both_sequences(fx, ws)

            with patch.object(
                GenericDriver, "encode_e_stop", side_effect=RuntimeError("エンコード失敗")
            ):
                await ws.send_json({"type": "e_stop"})
                await wait_until(lambda: fx.e_stop_active)

            await _release_gates_and_settle(seqs)

            for s in seqs:
                assert s.executed == ["gate"]
                assert s.is_running is False

            assert fx.e_stop_active is True
            await ws.close()

    async def test_e_stop_discards_pending_start_request(self) -> None:
        fx = _build_fixture()
        seq = fx.sequence(_ROBOT_NAMES[0])
        seq.request_start()

        await fx.command({"type": "e_stop"})

        await _spin_resident_loop(seq)
        assert seq.executed == [], "破棄されたはずの開始要求でシーケンスが走り出した"


class TestEStopReleaseKeepsSequencesStopped:
    async def test_release_does_not_restart_sequence(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _start_both_sequences(fx, ws)

            await ws.send_json({"type": "e_stop"})
            await wait_until(lambda: fx.e_stop_active)
            await _release_gates_and_settle(seqs)

            await ws.send_json({"type": "e_stop_release"})
            await wait_until(lambda: not fx.e_stop_active)
            await asyncio.sleep(0.1)

            for s in seqs:
                assert s.executed == ["gate"]
                assert s.is_running is False

            await ws.close()


class TestEStopBlocksSequenceCommands:
    async def test_sequence_start_rejected_while_e_stop_active(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _enter_e_stop(fx, ws)

            await ws.send_json({"type": "sequence_start", "robot": "main_hand"})

            msg = await recv_type(ws, "command_rejected")
            assert msg is not None
            assert msg["command"] == "sequence_start"
            assert msg["reason"]

            await asyncio.sleep(0.1)
            assert seqs[0].executed == ["gate"]
            assert seqs[0].is_running is False

            await ws.close()

    async def test_sequence_jump_rejected_while_e_stop_active(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _enter_e_stop(fx, ws)

            await ws.send_json({"type": "sequence_jump", "robot": "main_hand", "step_index": 1})

            msg = await recv_type(ws, "command_rejected")
            assert msg is not None
            assert msg["command"] == "sequence_jump"
            assert msg["reason"]

            await asyncio.sleep(0.1)
            assert seqs[0].executed == ["gate"]

            await ws.close()

    async def test_trigger_rejected_while_e_stop_active(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _enter_e_stop(fx, ws)

            spy = MagicMock()
            seqs[0].trigger = spy  # type: ignore[method-assign]

            await ws.send_json({"type": "trigger", "robot": "main_hand"})

            msg = await recv_type(ws, "command_rejected")
            assert msg is not None
            assert msg["command"] == "trigger"
            assert msg["reason"]

            spy.assert_not_called()
            await ws.close()

    async def test_stop_direction_commands_pass_during_e_stop(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _enter_e_stop(fx, ws)

            stop_spy = MagicMock()
            seqs[0].discard_pending_start = stop_spy  # type: ignore[method-assign]

            await ws.send_json({"type": "sequence_stop", "robot": "main_hand"})
            await _expect_no_rejection(ws, "sequence_stop")
            stop_spy.assert_called()

            await ws.send_json({"type": "e_stop"})
            await _expect_no_rejection(ws, "e_stop")
            assert fx.e_stop_active is True

            await ws.send_json({"type": "match_reset"})
            await _expect_no_rejection(ws, "match_reset")
            assert fx.match.phase is Phase.SETUP

            await ws.close()

    async def test_sequence_start_allowed_after_release(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _enter_e_stop(fx, ws)

            await ws.send_json({"type": "e_stop_release"})
            await wait_until(lambda: not fx.e_stop_active)

            for s in seqs:
                s.gate.clear()
                s.executed.clear()

            await ws.send_json({"type": "sequence_start", "robot": "main_hand"})
            restarted = await wait_until(lambda: seqs[0].executed == ["gate"])
            assert restarted, "解除後に sequence_start が通っていない"

            seqs[0].gate.set()
            await wait_until(lambda: not seqs[0].is_running)
            await ws.close()


class TestEStopBlocksMatchStart:
    async def test_match_start_rejected_while_e_stop_active(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            fx.complete_all_checklists()
            assert fx.match.phase is Phase.READY

            await ws.send_json({"type": "e_stop"})
            await wait_until(lambda: fx.e_stop_active)

            await ws.send_json({"type": "match_start"})

            msg = await recv_type(ws, "command_rejected")
            assert msg is not None
            assert msg["command"] == "match_start"
            assert msg["reason"]

            await asyncio.sleep(0.1)
            assert fx.match.phase is Phase.READY
            for s in fx.sequences():
                assert s.executed == [], f"{s.name}: 緊急停止中の試合開始でシーケンスが走った"
                assert s.is_running is False

            await ws.close()

    async def test_match_start_allowed_after_release(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            fx.complete_all_checklists()

            await ws.send_json({"type": "e_stop"})
            await wait_until(lambda: fx.e_stop_active)

            await ws.send_json({"type": "e_stop_release"})
            await wait_until(lambda: not fx.e_stop_active)

            await ws.send_json({"type": "match_start"})
            entered = await wait_until(lambda: fx.match.phase is Phase.MATCH)
            assert entered, "解除後の match_start が通っていない"

            seqs = fx.sequences()
            await asyncio.sleep(0.1)
            assert all(s.executed == [] for s in seqs)

            await ws.send_json({"type": "sequence_start", "robot": "main_hand"})
            started = await wait_until(lambda: seqs[0].executed == ["gate"])
            assert started, "解除後に sequence_start が通っていない"

            await _release_gates_and_settle(seqs)
            await ws.close()


class TestEStopKeepsRecoveryCommands:
    async def test_match_finish_and_release_pass_during_e_stop(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _enter_e_stop(fx, ws)

            await ws.send_json({"type": "match_finish"})
            await _expect_no_rejection(ws, "match_finish", tries=5)
            assert await wait_until(lambda: fx.match.phase is Phase.FINISHED)

            await ws.send_json({"type": "e_stop_release"})
            await _expect_no_rejection(ws, "e_stop_release", tries=5)
            assert await wait_until(lambda: not fx.e_stop_active)

            await ws.close()


class TestEStopReleaseRequiresActiveEStop:
    async def test_停止中でない解除は理由付きで拒否される(self) -> None:
        fx = _build_fixture()
        for name in _ROBOT_NAMES:
            fx.can_manager(name).activate_motors.reset_mock()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            await ws.send_json({"type": "e_stop_release"})
            msg = await recv_type(ws, "command_rejected")

            assert msg is not None
            assert msg["command"] == "e_stop_release"
            for name in _ROBOT_NAMES:
                fx.can_manager(name).activate_motors.assert_not_awaited()

            await ws.close()


class TestEStopReleaseReactivatesMotors:
    async def test_release_reactivates_motors_on_every_robot(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _enter_e_stop(fx, ws)

            for mgr in fx.can_managers():
                mgr.activate_motors.assert_not_awaited()

            await ws.send_json({"type": "e_stop_release"})
            await wait_until(lambda: not fx.e_stop_active)
            awaited = await wait_until(
                lambda: all(mgr.activate_motors.await_count == 1 for mgr in fx.can_managers())
            )

            assert awaited, "緊急停止解除でモータの再有効化が呼ばれていない"
            await ws.close()

    async def test_reactivation_is_abortable_by_a_new_e_stop(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _enter_e_stop(fx, ws)

            await ws.send_json({"type": "e_stop_release"})
            main_can = fx.can_manager("main_hand")
            await wait_until(lambda: main_can.activate_motors.await_count == 1)

            should_abort = main_can.activate_motors.await_args.kwargs["should_abort"]
            assert should_abort() is False

            await fx.activate_e_stop()
            assert should_abort() is True

            await ws.close()


class TestReleaseDoesNotBlockTheCommandLoop:
    async def test_再励磁の完了を待たずに次のコマンドを処理する(self) -> None:
        fx = _build_fixture()
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _enter_e_stop(fx, ws)

            try:
                await ws.send_json({"type": "e_stop_release"})
                assert await wait_until(lambda: not fx.e_stop_active)

                await ws.send_json({"type": "e_stop"})
                assert await wait_until(lambda: fx.e_stop_active), (
                    "再励磁の完了を待つあいだ E-STOP の押し直しが処理されていない"
                )
            finally:
                gate.set()
            await fx.wait_reactivation()
            await ws.close()


class TestUnenergizedMotorsAreVisible:
    async def test_release_reports_motors_that_failed_to_energize(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=["m1"])
        keep_feedback_fresh(fx.can_manager("main_hand"))

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _enter_e_stop(fx, ws)

            await ws.send_json({"type": "e_stop_release"})
            await wait_until(lambda: not fx.e_stop_active)
            reported = await wait_until(
                lambda: fx.state_message("main_hand")["safety"]["unenergized_motors"] == ["m1"]
            )

            assert reported, "励磁に失敗したモータが safety に載っていない"
            assert fx.state_message("sub_hand")["safety"]["unenergized_motors"] == []
            await ws.close()

    async def test_nothing_is_reported_while_e_stopped(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=["m1"])
        keep_feedback_fresh(fx.can_manager("main_hand"))

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _enter_e_stop(fx, ws)

            await ws.send_json({"type": "e_stop_release"})
            assert await wait_until(
                lambda: fx.state_message("main_hand")["safety"]["unenergized_motors"] == ["m1"]
            ), "停止で消えることを見る前に、まず載っていることを確かめる"
            await fx.activate_e_stop(reason="再度停止")

            assert fx.state_message("main_hand")["safety"]["unenergized_motors"] == []
            await ws.close()

    async def test_driver_reported_disable_is_surfaced(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()
        energized = MagicMock()
        energized.name = "energized"
        energized.is_energized.return_value = True
        dropped = MagicMock()
        dropped.name = "dropped"
        dropped.is_energized.return_value = False
        unknown = MagicMock()
        unknown.name = "unknown"
        unknown.is_energized.return_value = None
        set_motors(
            fx.can_manager("main_hand"),
            {"energized": energized, "dropped": dropped, "unknown": unknown},
        )
        keep_feedback_fresh(fx.can_manager("main_hand"))

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _enter_e_stop(fx, ws)

            await ws.send_json({"type": "e_stop_release"})
            reported = await wait_until(
                lambda: fx.state_message("main_hand")["safety"]["unenergized_motors"] == ["dropped"]
            )

            assert reported, "ドライバが報告した無励磁が safety に載っていない"
            await ws.close()


class TestActivateEStopFromInside:
    async def test_same_side_effects_as_e_stop_command(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _start_both_sequences(fx, ws)

            await fx.activate_e_stop(reason="y_axis の左右ずれ")

            assert fx.e_stop_active is True
            for mgr in fx.can_managers():
                mgr.send_to_bus.assert_awaited()

            await _release_gates_and_settle(seqs)
            for s in seqs:
                assert s.executed == ["gate"], f"{s.name}: 内部緊急停止後に後続ステップが実行された"
                assert s.is_running is False

            await ws.close()

    async def test_discards_pending_start_request(self) -> None:
        fx = _build_fixture()
        seq = fx.sequence(_ROBOT_NAMES[0])
        seq.request_start()

        await fx.activate_e_stop(reason="rotate の左右ずれ")

        await _spin_resident_loop(seq)
        assert seq.executed == [], "破棄されたはずの開始要求でシーケンスが走り出した"

    async def test_reason_is_broadcast(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            await fx.activate_e_stop(reason="y_axis の左右ずれ 3.400mm が許容 2.000mm 超過")

            msg = await recv_type(ws, "e_stop_state")
            assert msg is not None
            assert msg["active"] is True
            assert "y_axis" in msg["reason"]

            await ws.close()

    async def test_command_e_stop_keeps_broadcast_shape(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            await ws.send_json({"type": "e_stop"})

            msg = await recv_type(ws, "e_stop_state")
            assert msg is not None
            assert msg["active"] is True
            assert msg.get("reason") is None

            await ws.close()

    async def test_repeated_activation_is_safe(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _start_both_sequences(fx, ws)

            await fx.activate_e_stop(reason="y_axis の左右ずれ")
            await fx.activate_e_stop(reason="rotate の左右ずれ")
            await fx.activate_e_stop()

            assert fx.e_stop_active is True

            await _release_gates_and_settle(seqs)
            for s in seqs:
                assert s.executed == ["gate"]
                assert s.is_running is False

            await ws.close()


class _SyncFixture:
    def __init__(self, *, tolerance: float = 0.0) -> None:
        self.mgr = mock_can_manager()
        self.right = M3508Driver("y_r", can_id=1)
        self.left = M3508Driver("y_l", can_id=2)
        set_motors(self.mgr, {"y_r": self.right, "y_l": self.left})
        self.mgr.last_feedback_at.side_effect = lambda _name: time.time()

        group = SyncGroup(
            name="y_axis",
            members=(
                MotorSpec(name="y_r", scale=1.0, offset=0.0),
                MotorSpec(name="y_l", scale=-1.0, offset=0.0),
            ),
            tolerance=tolerance,
        )

        self._server_fx = ServerFixture.build()
        self.loop = M3508PositionLoop(
            self.mgr,
            "can_m3508",
            is_estop_active=lambda: self._server_fx.e_stop_active,
        )
        self.loop.add_motor("y_r", self.right, make_position_pid(kp=1.0))
        self.loop.add_motor("y_l", self.left, make_position_pid(kp=1.0))
        self.loop.add_sync_group(group)

        self.violations: list[tuple[str, float]] = []
        self.tasks: set[asyncio.Task[None]] = set()
        self.monitor = SyncMonitor(
            [group],
            {"y_r": self.right, "y_l": self.left},  # type: ignore[arg-type]
            last_feedback_at=lambda _name: time.time(),
            violation_samples=1,
            on_violation=self._on_violation,
        )
        self._server_fx.add_robot(
            "main_hand",
            GatedSequence("main_hand"),
            self.mgr,
            position_loops=[self.loop],
            sync_monitors=[self.monitor],
        )
        feed_m3508(self.right, deg=0.0)
        feed_m3508(self.left, deg=0.0)

    def _on_violation(self, axis: str, deviation: float, retrips: int = 1) -> None:
        self.violations.append((axis, deviation))
        task = asyncio.create_task(self._server_fx.activate_e_stop(reason=f"{axis} の左右ずれ"))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def command(self, payload: dict) -> None:
        await self._server_fx.command(payload)

    async def activate_e_stop(self, *, reason: str | None = None) -> None:
        await self._server_fx.activate_e_stop(reason=reason)

    @property
    def e_stop_active(self) -> bool:
        return self._server_fx.e_stop_active

    def state_message(self, robot: str = "main_hand") -> dict:
        return self._server_fx.state_message(robot)

    def deviate(self) -> None:
        feed_m3508(self.right, deg=10.0)
        feed_m3508(self.left, deg=10.0)

    def aligned(self) -> None:
        feed_m3508(self.right, deg=10.0)
        feed_m3508(self.left, deg=-10.0)

    async def settle(self) -> None:
        for _ in range(5):
            await asyncio.sleep(0)


class TestSyncLatchRelease:
    async def test_release_clears_position_loop_latch(self) -> None:
        fx = _SyncFixture()
        fx.deviate()
        await fx.loop.step()
        assert fx.loop.sync_violations == frozenset({"y_axis"})

        await fx.command({"type": "e_stop"})
        await fx.command({"type": "e_stop_release"})

        assert fx.loop.sync_violations == frozenset()

    async def test_release_clears_sync_monitor_latch(self) -> None:
        fx = _SyncFixture()
        fx.deviate()
        fx.monitor.step()
        await fx.settle()
        assert fx.monitor.violated == frozenset({"y_axis"})
        assert fx.e_stop_active is True

        await fx.command({"type": "e_stop_release"})

        assert fx.monitor.violated == frozenset()

    async def test_release_does_not_disable_monitoring(self) -> None:
        fx = _SyncFixture()
        fx.deviate()
        fx.monitor.step()
        await fx.settle()
        assert len(fx.violations) == 1

        await fx.command({"type": "e_stop_release"})
        assert fx.e_stop_active is False

        fx.monitor.step()
        await fx.settle()

        assert len(fx.violations) == 2
        assert fx.e_stop_active is True

    async def test_release_does_not_disable_position_loop_detection(self) -> None:
        fx = _SyncFixture()
        fx.deviate()
        await fx.loop.step()
        await fx.command({"type": "e_stop"})
        await fx.command({"type": "e_stop_release"})
        assert fx.loop.sync_violations == frozenset()

        await fx.loop.step()

        assert fx.loop.sync_violations == frozenset({"y_axis"})

    async def test_release_after_repair_keeps_axis_available(self) -> None:
        fx = _SyncFixture()
        fx.deviate()
        await fx.loop.step()
        await fx.command({"type": "e_stop"})

        fx.aligned()
        await fx.command({"type": "e_stop_release"})
        await fx.loop.step()

        assert fx.loop.sync_violations == frozenset()
        assert fx.monitor.violated == frozenset()


def _frames_to(mgr: CANManager, bus_name: str) -> list[can.Message]:
    return [call.args[1] for call in mgr.send_to_bus.await_args_list if call.args[0] == bus_name]


class TestEStopStopsM3508:
    async def test_zero_current_frame_is_sent(self) -> None:
        fx = _SyncFixture()
        await fx.loop.set_target("y_r", ControlMode.CURRENT, 3000.0)

        await fx.activate_e_stop()

        frames = _frames_to(fx.mgr, "can_m3508")
        assert frames, "M3508 のバスへ 1 通も送られていない"
        assert frames[-1].arbitration_id == 0x200
        assert struct.unpack(">hhhh", frames[-1].data) == (0, 0, 0, 0)

    async def test_sent_even_when_loop_is_not_running(self) -> None:
        fx = _SyncFixture()
        assert fx.loop.is_running is False

        await fx.activate_e_stop()

        assert _frames_to(fx.mgr, "can_m3508")

    async def test_targets_are_cleared(self) -> None:
        fx = _SyncFixture()
        await fx.loop.set_target("y_r", ControlMode.POSITION, 30.0)

        await fx.activate_e_stop()

        assert fx.loop.target("y_r") is None

    async def test_bus_failure_does_not_block_other_frames(self) -> None:
        fx = _SyncFixture()

        async def _fail_m3508(bus_name: str, msg: can.Message) -> None:
            if bus_name == "can_m3508":
                raise can.CanError("送信失敗 (テスト)")

        fx.mgr.send_to_bus = AsyncMock(side_effect=_fail_m3508)

        await fx.activate_e_stop()

        assert [call.args[0] for call in fx.mgr.send_to_bus.await_args_list].count("bus0") == 1
        assert fx.e_stop_active is True


class TestEStopDropsRefreshTargets:
    async def test_targets_are_dropped_on_e_stop(self) -> None:
        fx = _build_fixture()
        mgr = fx.can_manager("main_hand")
        driver = GenericDriver("conveyor", can_id=9, control_type=ControlMode.DUTY)
        handle = MotorHandle("conveyor", driver, mgr)
        refresher = GenericTargetRefresher([handle])
        fx.set_target_refreshers("main_hand", [refresher])
        await handle.set_target(ControlMode.DUTY, 0.3)

        await fx.activate_e_stop()

        assert handle.has_target is False


class TestEStopCompletionLogIsOneLine:
    async def test_2台でも締めの行は1行で全機の名前を載せる(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        fx = _build_fixture()

        with caplog.at_level(logging.INFO, logger="lib.server"):
            await fx.activate_e_stop()

        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("E-STOP 送信")]
        assert len(lines) == 1
        assert lines[0].startswith("E-STOP 送信試行完了: ")
        assert "main_hand" in lines[0]
        assert "sub_hand" in lines[0]


class TestSafetyStateBroadcast:
    async def test_latched_axes_are_broadcast(self) -> None:
        fx = _SyncFixture()
        fx.deviate()
        await fx.loop.step()
        fx.monitor.step()
        await fx.settle()

        state = fx.state_message()

        assert state["safety"]["sync_violations"] == ["y_axis"]

    async def test_dead_safety_loops_are_visible(self) -> None:
        fx = _SyncFixture()

        state = fx.state_message()
        assert state["safety"]["loops_running"] is False
        assert state["safety"]["monitors_running"] is False

        fx.loop.start()
        fx.monitor.start()
        await fx.settle()
        try:
            state = fx.state_message()
            assert state["safety"]["loops_running"] is True
            assert state["safety"]["monitors_running"] is True
        finally:
            await fx.loop.stop()
            await fx.monitor.stop()


class _LimitFixture:
    """可動端監視 1 本を載せたサーバー。後端スイッチが押された状態で組む。"""

    def __init__(self) -> None:
        table = load_position_table(
            {
                "axes": {
                    "sub_y_axis": {
                        "unit": "mm",
                        "command_unit": "rad",
                        "scale": 2.0,
                        "tolerance": 0.1,
                        "guard": {"limits": {"plus": "front", "minus": "rear"}, "max_step": 500.0},
                    }
                },
                "positions": {"sub_y_axis": {"home": 0.0}},
            },
            source="<test>",
        )
        self.driver = StubFeedbackDriver("sub_y_axis", 1)
        self.handle = MotorHandle("sub_y_axis", self.driver, mock_can_manager())
        group = MotorGroup()
        group.add(self.handle)
        self.monitor = LimitMonitor(
            table,
            group,
            sensor_active=lambda name: name == "rear",
            sensor_contact_count=lambda _name: 0,
            court=lambda: Court.RED,
        )
        self.fx = ServerFixture.build()
        self.fx.add_robot("sub_hand", GatedSequence("sub_hand"), limit_monitors=[self.monitor])

    def safety(self) -> dict:
        return self.fx.state_message("sub_hand")["safety"]


class TestLimitMonitorLivenessBroadcast:
    async def test_dead_limit_monitor_is_visible(self) -> None:
        fx = _LimitFixture()

        safety = fx.safety()
        assert safety["limit_monitors_running"] is False
        assert safety["limit_monitors"] == [
            {"axes": ["sub_y_axis"], "running": False, "stopped": []}
        ]
        # 同期監視の欄は同期監視だけを見る (可動端監視は自分の欄で報告する)
        assert safety["monitors_running"] is True

        fx.monitor.start()
        try:
            assert await wait_until(lambda: fx.safety()["limit_monitors_running"] is True)
            assert fx.safety()["limit_monitors"][0]["running"] is True
        finally:
            await fx.monitor.stop()

    async def test_axis_held_at_limit_is_broadcast(self) -> None:
        fx = _LimitFixture()
        fx.driver.set_observed(position=-447.5 * 2.0)
        await fx.handle.set_target(ControlMode.POSITION, -450.0 * 2.0)

        await fx.monitor.step()

        assert fx.safety()["limit_monitors"][0]["stopped"] == ["sub_y_axis"]


class TestTargetRefresherLivenessBroadcast:
    def _fixture(self) -> tuple[ServerFixture, GenericTargetRefresher]:
        fx = ServerFixture.build()
        mgr = mock_can_manager(("conveyor",))
        driver = GenericDriver("conveyor", can_id=9, control_type=ControlMode.DUTY)
        refresher = GenericTargetRefresher([MotorHandle("conveyor", driver, mgr)])
        fx.add_robot("main_hand", GatedSequence("main_hand"), mgr, target_refreshers=[refresher])
        return fx, refresher

    async def test_再送タスクの生死が配信される(self) -> None:
        fx, refresher = self._fixture()

        state = fx.state_message("main_hand")
        assert state["safety"]["refreshers_running"] is False

        refresher.start()
        try:
            state = fx.state_message("main_hand")
            assert state["safety"]["refreshers_running"] is True
            assert state["safety"]["target_refreshers"] == [
                {"motors": ["conveyor"], "running": True, "paused": False}
            ]
        finally:
            await refresher.stop()

    async def test_一時停止中も配信に現れる(self) -> None:
        fx, refresher = self._fixture()
        refresher.start()
        try:
            await refresher.pause(reason="動作確認")
            state = fx.state_message("main_hand")
            assert state["safety"]["target_refreshers"][0]["paused"] is True
        finally:
            await refresher.stop()


async def _latest_e_stop_state(ws) -> dict:
    latest: dict | None = None
    for msg in await drain(ws, timeout=0.1, limit=80):
        if msg.get("type") == "e_stop_state":
            latest = msg
    assert latest is not None, "e_stop_state が配信されなかった"
    return latest


class TestEStopReasonIsRetained:
    async def test_理由は定期再配信でも保たれる(self) -> None:
        fx = _build_fixture()
        fx.freeze_broadcast()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            await fx.activate_e_stop(reason="y_axis の左右ずれ 3.400mm が許容 2.000mm 超過")
            first = await recv_type(ws, "e_stop_state")
            assert first is not None
            assert "y_axis" in first["reason"]

            await fx.publish_state()
            again = await _latest_e_stop_state(ws)

            assert again["active"] is True
            assert again.get("reason") == first["reason"], "再配信で停止理由が消えた"

            await ws.close()

    async def test_操縦者の停止操作は判明済みの原因を塗り潰さない(self) -> None:
        fx = _build_fixture()
        fx.freeze_broadcast()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            await fx.activate_e_stop(reason="rotate の左右ずれを検知しました")
            await ws.send_json({"type": "e_stop"})
            await wait_until(lambda: fx.e_stop_active)
            await fx.publish_state()

            latest = await _latest_e_stop_state(ws)
            assert latest["active"] is True
            assert "rotate" in (latest.get("reason") or "")

            await ws.close()

    async def test_解除すると次の停止に前回の理由は残らない(self) -> None:
        fx = _build_fixture()
        fx.freeze_broadcast()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            await fx.activate_e_stop(reason="y_axis の左右ずれを検知しました")
            await ws.send_json({"type": "e_stop_release"})
            await wait_until(lambda: not fx.e_stop_active)

            await ws.send_json({"type": "e_stop"})
            await wait_until(lambda: fx.e_stop_active)
            await fx.publish_state()

            latest = await _latest_e_stop_state(ws)
            assert latest["active"] is True
            assert latest.get("reason") is None, "解除したはずの前回の停止理由が残っている"

            await ws.close()


def _health_with_feedback_at(mgr, at: float) -> HealthSnapshot:
    return HealthSnapshot(
        timestamp=time.time(),
        overall=BusHealth.OK,
        buses=[
            BusHealthInfo(
                name=name,
                channel=name,
                state=BusHealth.OK,
                last_tx_at=at,
                last_rx_at=at,
                tx_error_count=0,
                rx_error_count=0,
                bus_off=False,
            )
            for name in mgr.bus_names
        ],
        motors=[
            MotorHealthInfo(
                name=name,
                bus=mgr.bus_names[0],
                state=MotorHealth.OK,
                last_feedback_at=at,
                feedback_age_ms=0.0,
                temperature=30.0,
                detail=None,
            )
            for name in mgr.motors
        ],
    )


class TestBoardReportedEStop:
    def _fixture_with_generic(self, *, e_stop: bool) -> tuple[ServerFixture, GenericDriver]:
        fx = _build_fixture()
        drv = GenericDriver("conveyor", 0x11, control_type=ControlMode.DUTY)
        feed_generic(drv, e_stop=e_stop)
        set_motors(fx.can_manager("main_hand"), {"conveyor": drv})
        return fx, drv

    async def test_board_flag_activates_server_e_stop(self) -> None:
        fx, _ = self._fixture_with_generic(e_stop=True)

        await fx.publish_state()

        assert fx.e_stop_active is True

    async def test_reason_names_the_motor(self) -> None:
        fx, _ = self._fixture_with_generic(e_stop=True)

        await fx.publish_state()

        payload = fx.server._e_stop_reason
        assert payload is not None
        assert "main_hand" in payload
        assert "conveyor" in payload

    async def test_no_flag_keeps_running(self) -> None:
        fx, _ = self._fixture_with_generic(e_stop=False)

        await fx.publish_state()

        assert fx.e_stop_active is False

    async def test_mock_motors_without_the_flag_are_ignored(self) -> None:
        fx = _build_fixture()

        await fx.publish_state()

        assert fx.e_stop_active is False

    async def test_release_is_not_undone_by_feedback_from_before_the_clear(self) -> None:
        fx, _ = self._fixture_with_generic(e_stop=True)
        await fx.publish_state()
        assert fx.e_stop_active is True

        mgr = fx.can_manager("main_hand")
        stale_at = time.time()
        await fx.command({"type": "e_stop_release"})
        assert fx.e_stop_active is False
        await fx.wait_reactivation()

        mgr.health.side_effect = lambda **_kwargs: _health_with_feedback_at(mgr, stale_at)
        await fx.publish_state()

        assert fx.e_stop_active is False

    async def test_再励磁の最中は基板の報告で止め直さない(self) -> None:
        fx, _ = self._fixture_with_generic(e_stop=True)
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        await fx.publish_state()
        assert fx.e_stop_active is True

        try:
            await fx.command({"type": "e_stop_release"})
            assert fx.e_stop_active is False

            await fx.publish_state()
            assert fx.e_stop_active is False, "再励磁の最中に基板の報告で止め直した"
        finally:
            gate.set()
        await fx.wait_reactivation()

    async def test_still_pressed_after_release_stops_again(self) -> None:
        fx, _ = self._fixture_with_generic(e_stop=True)
        await fx.publish_state()

        await fx.command({"type": "e_stop_release"})
        assert fx.e_stop_active is False
        await fx.wait_reactivation()

        await fx.publish_state()

        assert fx.e_stop_active is True


_BOARD_CAN_ID = 0x11
_ENERGIZE_FRAME_ID = 0x123


def _is_latch_clear(msg: object, device_id: int = _BOARD_CAN_ID) -> bool:
    if not isinstance(msg, can.Message):
        return False
    expected = GenericDriver.encode_e_stop_clear(device_id)
    return msg.arbitration_id == expected.arbitration_id and bytes(msg.data) == bytes(expected.data)


def _is_energize(msg: object) -> bool:
    return isinstance(msg, can.Message) and msg.arbitration_id == _ENERGIZE_FRAME_ID


def _is_broadcast_clear(msg: object) -> bool:
    return (
        isinstance(msg, can.Message)
        and msg.arbitration_id == 0x0FF
        and bytes(msg.data) == bytes((0x01, 0x5A, 0xA5))
    )


_Sent = list[tuple[str, object, float]]


def _fixture_with_boards(*, with_energized_motor: bool = True) -> tuple[ServerFixture, _Sent, dict]:
    fx = ServerFixture.build()
    sent: _Sent = []
    boards: dict[str, GenericDriver] = {}
    for index, name in enumerate(_ROBOT_NAMES):
        mgr = CANManager(run_blocking=direct_runner())
        for bus_name in (f"can{index}", f"can{index}_spare"):
            bus = mock_bus()
            bus.send.side_effect = lambda msg, _bus=bus_name: sent.append((_bus, msg, time.time()))
            mgr.add_bus(bus_name, bus)
        board = GenericDriver(f"{name}_board", _BOARD_CAN_ID, control_type=ControlMode.DUTY)
        mgr.add_motor(f"can{index}", board)
        boards[name] = board
        if with_energized_motor:
            energized = mock_driver(f"{name}_arm", 0x21)
            energized.emergency_stop_message.return_value = None
            energized.activation_steps.return_value = [
                (
                    can.Message(
                        arbitration_id=_ENERGIZE_FRAME_ID, data=bytes(8), is_extended_id=False
                    ),
                    0.0,
                )
            ]
            mgr.add_motor(f"can{index}", energized)
        fx.add_robot(name, GatedSequence(name), mgr)
    return fx, sent, boards


class TestLatchClearIsNeverAborted:
    async def test_ラッチ解除は全ロボットの励磁より先に出る(self) -> None:
        fx, sent, _boards = _fixture_with_boards()
        await fx.activate_e_stop(reason="停止")
        sent.clear()

        await fx.command({"type": "e_stop_release"})
        await fx.wait_reactivation()

        first_energize = next(
            (index for index, (_bus, msg, _at) in enumerate(sent) if _is_energize(msg)), None
        )
        assert first_energize is not None, "励磁フレームが 1 通も飛んでいない"
        for index, name in enumerate(_ROBOT_NAMES):
            bus_name = f"can{index}"
            first_clear = next(
                (
                    i
                    for i, (bus, msg, _at) in enumerate(sent)
                    if bus == bus_name and _is_latch_clear(msg)
                ),
                None,
            )
            assert first_clear is not None, f"{name} へラッチ解除フレームが飛んでいない"
            assert first_clear < first_energize, (
                f"{name} のラッチ解除が、どこかのロボットの励磁より後になっている"
            )

    async def test_解除直後に停止が再発動しても2台目へ届く(self) -> None:
        fx, sent, _boards = _fixture_with_boards()
        await fx.activate_e_stop(reason="停止")

        main = fx.can_manager("main_hand")
        clear_main_hand = main.clear_e_stop_latches

        async def _clear_then_e_stop(**kwargs: object) -> list[str]:
            uncleared = await clear_main_hand(**kwargs)
            await fx.activate_e_stop(reason="解除直後に再発動")
            return uncleared

        main.clear_e_stop_latches = _clear_then_e_stop
        sent.clear()

        await fx.command({"type": "e_stop_release"})
        await fx.wait_reactivation()

        assert any(bus == "can1" and _is_latch_clear(msg) for bus, msg, _at in sent), (
            "2 台目のロボットへラッチ解除フレームが 1 通も飛んでいない"
        )
        assert fx.e_stop_active is True
        assert not any(_is_energize(msg) for _bus, msg, _at in sent), (
            "緊急停止が再発動しているのに励磁フレームが飛んでいる"
        )


class TestEStopClearIsBroadcast:
    async def test_全バスへブロードキャスト解除が飛ぶ(self) -> None:
        fx, sent, _boards = _fixture_with_boards()
        await fx.activate_e_stop(reason="停止")
        sent.clear()

        await fx.command({"type": "e_stop_release"})
        await fx.wait_reactivation()

        reached = {bus for bus, msg, _at in sent if _is_broadcast_clear(msg)}
        assert reached == {"can0", "can0_spare", "can1", "can1_spare"}, (
            f"ブロードキャスト解除が届いていないバスがある: {reached}"
        )

    async def test_yamlに無いdevice_idもブロードキャストで救われる(self) -> None:
        fx, sent, _boards = _fixture_with_boards(with_energized_motor=False)
        await fx.activate_e_stop(reason="停止")
        sent.clear()

        await fx.command({"type": "e_stop_release"})
        await fx.wait_reactivation()

        unregistered = 0x43
        assert not any(_is_latch_clear(msg, unregistered) for _bus, msg, _at in sent)
        for bus_name in ("can0", "can0_spare", "can1", "can1_spare"):
            assert any(bus == bus_name and _is_broadcast_clear(msg) for bus, msg, _at in sent), (
                f"{bus_name} に登録済みの自作モタドラが居なくても解除は出さねばならない"
            )

    async def test_ブロードキャスト解除は基板報告の起点より前に送る(self) -> None:
        fx, sent, boards = _fixture_with_boards(with_energized_motor=False)
        board = boards["main_hand"]
        mgr = fx.can_manager("main_hand")
        feed_generic(board, e_stop=True)
        mark_feedback_at(mgr, board.name, time.time())

        await fx.publish_state()
        assert fx.e_stop_active is True, "基板の報告でサーバーが停止していない"

        sent.clear()
        await fx.command({"type": "e_stop_release"})
        await fx.wait_reactivation()

        broadcast_at = next(
            (at for bus, msg, at in sent if bus == "can0" and _is_broadcast_clear(msg)), None
        )
        assert broadcast_at is not None, "ブロードキャスト解除が送られていない"
        mark_feedback_at(mgr, board.name, broadcast_at)
        await fx.publish_state()

        assert fx.e_stop_active is False, (
            "解除フレームより前の起点で基板の報告を信じ、自分で止め直している"
        )
