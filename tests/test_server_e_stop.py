from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import TestClient, TestServer

from lib.can_manager import CANManager
from lib.drivers.base import MotorState
from lib.drivers.generic import GenericDriver
from lib.match_state import (
    ROLE_MAIN_HAND,
    ROLE_MONITOR,
    ROLE_SUB_HAND,
    ChecklistItem,
    Mode,
    Phase,
)
from lib.sequence.engine import Sequence, step
from lib.server import RobotServer

_ROBOT_NAMES = ("main_hand", "sub_hand")

_DEFS = {
    ROLE_MONITOR: [ChecklistItem(id="power", label="電源投入確認")],
    ROLE_MAIN_HAND: [ChecklistItem(id="home", label="メイン初期位置確認")],
    ROLE_SUB_HAND: [ChecklistItem(id="home", label="サブ初期位置確認")],
}


class GatedSequence(Sequence):
    """実行中のステップを外部から任意のタイミングで完了させられるシーケンス。

    緊急停止は「今動いているステップの後に次のステップが動き出さない」ことが
    要点なので、ステップ境界をテスト側が制御できる形にする。
    """

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


def _make_mock_can_manager() -> CANManager:
    mgr = MagicMock(spec=CANManager)
    motor = MagicMock()
    motor.state = MotorState(position=0.0, velocity=0.0, current=0.0, temperature=30.0)
    motor.name = "m1"
    mgr._motors = {"m1": motor}
    mgr.get_motor.return_value = motor
    mgr.send = AsyncMock()
    mgr.send_to_bus = AsyncMock()
    mgr.activate_motors = AsyncMock()
    mgr._buses = {"bus0": MagicMock()}
    return mgr


def _build_server() -> RobotServer:
    server = RobotServer(checklist_definitions=_DEFS)
    for name in _ROBOT_NAMES:
        server.add_robot(name, GatedSequence(name), _make_mock_can_manager())
    return server


def _sequences(server: RobotServer) -> list[GatedSequence]:
    return [server._robots[name].sequence for name in _ROBOT_NAMES]  # type: ignore[misc]


def _complete_all(server: RobotServer) -> None:
    for role in (ROLE_MAIN_HAND, ROLE_SUB_HAND):
        for item in server.match.checklists[role].items:
            server.match.set_checklist_item(role, item.id, True)


def _complete_required(server: RobotServer) -> None:
    """現在のモードで必須のロールだけをチェック完了させ READY へ進める。"""
    for role in server.match.required_roles:
        for item in server.match.checklists[role].items:
            server.match.set_checklist_item(role, item.id, True)


async def _wait_until(predicate, *, timeout: float = 2.0) -> bool:
    """条件成立をポーリングで待つ (固定 sleep より取りこぼしに強い)。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def _start_both_sequences(server: RobotServer, ws) -> list[GatedSequence]:
    """試合を開始し、両ロボットのシーケンスをゲートステップまで進める。"""
    _complete_all(server)
    await ws.send_json({"type": "match_start"})
    for name in _ROBOT_NAMES:
        await ws.send_json({"type": "sequence_start", "robot": name})

    seqs = _sequences(server)
    started = await _wait_until(lambda: all(s.executed == ["gate"] for s in seqs))
    assert started, "シーケンスがゲートステップまで進まなかった"
    return seqs


async def _release_gates_and_settle(seqs: list[GatedSequence]) -> None:
    for s in seqs:
        s.gate.set()
    await _wait_until(lambda: all(not s._running for s in seqs))


async def _recv_type(ws, wanted: str, *, tries: int = 40) -> dict | None:
    """周期配信の state メッセージに紛れた特定 type のメッセージを拾う。"""
    for _ in range(tries):
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=0.2)
        except (TimeoutError, TypeError):
            return None
        if msg.get("type") == wanted:
            return msg
    return None


async def _expect_no_rejection(ws, command: str, *, tries: int = 40) -> None:
    """一定時間 command_rejected が流れてこないことを確認する。"""
    for _ in range(tries):
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=0.2)
        except (TimeoutError, TypeError):
            return
        if msg.get("type") == "command_rejected" and msg.get("command") == command:
            raise AssertionError(f"{command} が拒否された: {msg.get('reason')}")


async def _enter_e_stop(server: RobotServer, ws) -> list[GatedSequence]:
    """試合中にシーケンスを走らせたうえで緊急停止状態まで持っていく。"""
    seqs = await _start_both_sequences(server, ws)
    await ws.send_json({"type": "e_stop"})
    await _wait_until(lambda: server._e_stop_active)
    await _release_gates_and_settle(seqs)
    return seqs


class TestEStopStopsSequences:
    async def test_e_stop_stops_all_running_sequences(self) -> None:
        """緊急停止後に次ステップが走ると、新しいモータ目標値が停止指令を上書きする。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _start_both_sequences(server, ws)

            await ws.send_json({"type": "e_stop"})
            await _wait_until(lambda: server._e_stop_active)

            await _release_gates_and_settle(seqs)

            for s in seqs:
                assert s.executed == ["gate"], f"{s.name}: 緊急停止後に後続ステップが実行された"
                assert s._running is False, f"{s.name}: 緊急停止後もシーケンスが実行中"

            await ws.close()

    async def test_e_stop_stops_sequences_when_bus_send_fails(self) -> None:
        """CAN 送信が失敗しても停止は成立させる (送信不能な時ほど停止が要る)。"""
        server = _build_server()
        for name in _ROBOT_NAMES:
            server._robots[name].can_manager.send_to_bus = AsyncMock(
                side_effect=RuntimeError("バス送信失敗")
            )
            server._robots[name].can_manager.send = AsyncMock(
                side_effect=RuntimeError("モータ送信失敗")
            )
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _start_both_sequences(server, ws)

            await ws.send_json({"type": "e_stop"})
            await _wait_until(lambda: server._e_stop_active)

            await _release_gates_and_settle(seqs)

            for s in seqs:
                assert s.executed == ["gate"]
                assert s._running is False

            await ws.close()

    async def test_e_stop_stops_sequences_when_encode_raises(self) -> None:
        """停止フレーム生成そのものが失敗しても、シーケンス停止まで到達すること。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _start_both_sequences(server, ws)

            with patch.object(
                GenericDriver, "encode_e_stop", side_effect=RuntimeError("エンコード失敗")
            ):
                await ws.send_json({"type": "e_stop"})
                await _wait_until(lambda: server._e_stop_active)

            await _release_gates_and_settle(seqs)

            for s in seqs:
                assert s.executed == ["gate"]
                assert s._running is False

            assert server._e_stop_active is True
            await ws.close()

    async def test_e_stop_discards_pending_start_request(self) -> None:
        """開始要求が処理される前に緊急停止が入っても、その要求で走り出さないこと。"""
        server = _build_server()
        seq = _sequences(server)[0]
        seq.request_start()
        assert seq._resume_event.is_set()

        await server._handle_command({"type": "e_stop"})

        assert not seq._resume_event.is_set()


class TestEStopReleaseKeepsSequencesStopped:
    async def test_release_does_not_restart_sequence(self) -> None:
        """解除は再開合図ではない。操縦者の sequence_start を待つ設計を守る。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _start_both_sequences(server, ws)

            await ws.send_json({"type": "e_stop"})
            await _wait_until(lambda: server._e_stop_active)
            await _release_gates_and_settle(seqs)

            await ws.send_json({"type": "e_stop_release"})
            await _wait_until(lambda: not server._e_stop_active)
            await asyncio.sleep(0.1)

            for s in seqs:
                assert s.executed == ["gate"]
                assert s._running is False

            await ws.close()


class TestEStopBlocksSequenceCommands:
    async def test_sequence_start_rejected_while_e_stop_active(self) -> None:
        """緊急停止中に START でロボットが動き出すと、操縦者が止める手段を失う。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _enter_e_stop(server, ws)

            await ws.send_json({"type": "sequence_start", "robot": "main_hand"})

            msg = await _recv_type(ws, "command_rejected")
            assert msg is not None
            assert msg["command"] == "sequence_start"
            assert msg["reason"]

            await asyncio.sleep(0.1)
            assert seqs[0].executed == ["gate"]
            assert seqs[0]._running is False
            assert not seqs[0]._resume_event.is_set()

            await ws.close()

    async def test_sequence_jump_rejected_while_e_stop_active(self) -> None:
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _enter_e_stop(server, ws)

            await ws.send_json({"type": "sequence_jump", "robot": "main_hand", "step_index": 1})

            msg = await _recv_type(ws, "command_rejected")
            assert msg is not None
            assert msg["command"] == "sequence_jump"
            assert msg["reason"]

            await asyncio.sleep(0.1)
            assert seqs[0]._jump_request is None
            assert not seqs[0]._resume_event.is_set()
            assert seqs[0].executed == ["gate"]

            await ws.close()

    async def test_trigger_rejected_while_e_stop_active(self) -> None:
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _enter_e_stop(server, ws)

            spy = MagicMock()
            seqs[0].trigger = spy  # type: ignore[method-assign]

            await ws.send_json({"type": "trigger", "robot": "main_hand"})

            msg = await _recv_type(ws, "command_rejected")
            assert msg is not None
            assert msg["command"] == "trigger"
            assert msg["reason"]

            spy.assert_not_called()
            await ws.close()

    async def test_stop_direction_commands_pass_during_e_stop(self) -> None:
        """止める方向の操作は緊急停止中こそ通す必要がある。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _enter_e_stop(server, ws)

            stop_spy = MagicMock()
            seqs[0].request_stop = stop_spy  # type: ignore[method-assign]

            await ws.send_json({"type": "sequence_stop", "robot": "main_hand"})
            await _expect_no_rejection(ws, "sequence_stop")
            stop_spy.assert_called()

            await ws.send_json({"type": "e_stop"})
            await _expect_no_rejection(ws, "e_stop")
            assert server._e_stop_active is True

            await ws.send_json({"type": "match_reset"})
            await _expect_no_rejection(ws, "match_reset")
            assert server.match.phase is Phase.SETUP

            await ws.close()

    async def test_sequence_start_allowed_after_release(self) -> None:
        """解除後は従来どおり操縦者の START で再開できること。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _enter_e_stop(server, ws)

            await ws.send_json({"type": "e_stop_release"})
            await _wait_until(lambda: not server._e_stop_active)

            for s in seqs:
                s.gate.clear()
                s.executed.clear()

            await ws.send_json({"type": "sequence_start", "robot": "main_hand"})
            restarted = await _wait_until(lambda: seqs[0].executed == ["gate"])
            assert restarted, "解除後に sequence_start が通っていない"

            seqs[0].gate.set()
            await _wait_until(lambda: not seqs[0]._running)
            await ws.close()


class TestEStopBlocksMatchStart:
    """全自動では match_start が両機の起動を兼ねるため、緊急停止中は必ず塞ぐ必要がある。"""

    async def test_match_start_rejected_while_e_stop_active_full_auto(self) -> None:
        """緊急停止中に試合開始を押しても両機が動き出さないこと。"""
        server = _build_server()
        server.match.set_mode(Mode.FULL_AUTO)
        server._apply_match_settings()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            _complete_required(server)
            assert server.match.phase is Phase.READY

            await ws.send_json({"type": "e_stop"})
            await _wait_until(lambda: server._e_stop_active)

            await ws.send_json({"type": "match_start"})

            msg = await _recv_type(ws, "command_rejected")
            assert msg is not None
            assert msg["command"] == "match_start"
            assert msg["reason"]

            await asyncio.sleep(0.1)
            assert server.match.phase is Phase.READY
            for s in _sequences(server):
                assert s.executed == [], f"{s.name}: 緊急停止中の試合開始でシーケンスが走った"
                assert s._running is False
                assert not s._resume_event.is_set()

            await ws.close()

    async def test_match_start_rejected_while_e_stop_active_semi_auto(self) -> None:
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            _complete_required(server)
            assert server.match.phase is Phase.READY

            await ws.send_json({"type": "e_stop"})
            await _wait_until(lambda: server._e_stop_active)

            await ws.send_json({"type": "match_start"})

            msg = await _recv_type(ws, "command_rejected")
            assert msg is not None
            assert msg["command"] == "match_start"

            await asyncio.sleep(0.1)
            assert server.match.phase is Phase.READY

            await ws.close()

    async def test_match_start_allowed_after_release_full_auto(self) -> None:
        """解除後は全自動の試合開始が従来どおり両機を起動すること。"""
        server = _build_server()
        server.match.set_mode(Mode.FULL_AUTO)
        server._apply_match_settings()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            _complete_required(server)

            await ws.send_json({"type": "e_stop"})
            await _wait_until(lambda: server._e_stop_active)

            await ws.send_json({"type": "e_stop_release"})
            await _wait_until(lambda: not server._e_stop_active)

            await ws.send_json({"type": "match_start"})

            seqs = _sequences(server)
            started = await _wait_until(lambda: all(s.executed == ["gate"] for s in seqs))
            assert started, "解除後に全自動の match_start が両機を起動していない"
            assert server.match.phase is Phase.MATCH

            await _release_gates_and_settle(seqs)
            await ws.close()

    async def test_match_start_starts_both_without_e_stop(self) -> None:
        """緊急停止していない通常時の全自動 match_start が従来どおり動くこと。"""
        server = _build_server()
        server.match.set_mode(Mode.FULL_AUTO)
        server._apply_match_settings()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            _complete_required(server)

            await ws.send_json({"type": "match_start"})

            seqs = _sequences(server)
            started = await _wait_until(lambda: all(s.executed == ["gate"] for s in seqs))
            assert started
            assert server.match.phase is Phase.MATCH

            await _release_gates_and_settle(seqs)
            await ws.close()


class TestEStopBlocksSetParam:
    async def test_set_param_rejected_while_e_stop_active(self) -> None:
        """緊急停止中のパラメータ書き換えは停止状態を崩しうるため拒否する。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _enter_e_stop(server, ws)

            await ws.send_json({"type": "set_param", "motor": "m1", "key": "kp", "value": 1.0})

            msg = await _recv_type(ws, "command_rejected")
            assert msg is not None
            assert msg["command"] == "set_param"
            assert msg["reason"]

            await ws.close()

    async def test_set_param_allowed_without_e_stop(self) -> None:
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "set_param", "motor": "m1", "key": "kp", "value": 1.0})
            await _expect_no_rejection(ws, "set_param", tries=5)
            await ws.close()


class TestEStopKeepsRecoveryCommands:
    async def test_match_finish_and_release_pass_during_e_stop(self) -> None:
        """試合終了・緊急停止解除は復帰経路なので緊急停止中も通す。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _enter_e_stop(server, ws)

            await ws.send_json({"type": "match_finish"})
            await _expect_no_rejection(ws, "match_finish", tries=5)
            assert server.match.phase is Phase.FINISHED

            await ws.send_json({"type": "e_stop_release"})
            await _expect_no_rejection(ws, "e_stop_release", tries=5)
            assert server._e_stop_active is False

            await ws.close()


class TestEStopReleaseReactivatesMotors:
    """EDULITE 05 は緊急停止で無励磁になるため、解除で再励磁しないと以後動かない。"""

    async def test_release_reactivates_motors_on_every_robot(self) -> None:
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _enter_e_stop(server, ws)

            for name in _ROBOT_NAMES:
                server._robots[name].can_manager.activate_motors.assert_not_awaited()

            await ws.send_json({"type": "e_stop_release"})
            await _wait_until(lambda: not server._e_stop_active)
            awaited = await _wait_until(
                lambda: all(
                    server._robots[name].can_manager.activate_motors.await_count == 1
                    for name in _ROBOT_NAMES
                )
            )

            assert awaited, "緊急停止解除でモータの再有効化が呼ばれていない"
            await ws.close()

    async def test_reactivation_is_abortable_by_a_new_e_stop(self) -> None:
        """再有効化中にもう一度緊急停止が入ったら enable を送ってはならない。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _enter_e_stop(server, ws)

            await ws.send_json({"type": "e_stop_release"})
            await _wait_until(
                lambda: server._robots["main_hand"].can_manager.activate_motors.await_count == 1
            )

            call = server._robots["main_hand"].can_manager.activate_motors.await_args
            should_abort = call.kwargs["should_abort"]
            assert should_abort() is False

            server._e_stop_active = True
            assert should_abort() is True

            await ws.close()
