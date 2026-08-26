"""command_rejected の宛先を検証する。

拒否通知は「今その操作をした人」への返答であって、全員への通知ではない。
全配信していると、Monitor が試合中に set_court を弾かれただけで両操縦者の画面にも
赤トーストが出る。自分が押していない操作の拒否は操縦者にとってノイズでしかなく、
本当に自分の操作が弾かれたときの通知と区別が付かなくなる。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from aiohttp.test_utils import TestClient, TestServer

from lib.can_manager import CANManager
from lib.drivers.base import MotorState
from lib.match_state import Court, Phase
from lib.sequence.engine import Sequence, step
from lib.server import RobotServer
from tests.fake_health import ok_health_snapshot


class _DummySequence(Sequence):
    def __init__(self) -> None:
        super().__init__("test_seq")

    @step("ノーオペ")
    async def noop(self) -> None:
        return None


def _make_mock_can_manager() -> CANManager:
    mgr = MagicMock(spec=CANManager)
    motor = MagicMock()
    motor.state = MotorState(position=0.0, velocity=0.0, current=0.0, temperature=30.0)
    motor.name = "m1"
    mgr._motors = {"m1": motor}
    mgr.send = AsyncMock()
    mgr.send_to_bus = AsyncMock()
    mgr._buses = {"bus0": MagicMock()}
    mgr.health.side_effect = lambda **_kwargs: ok_health_snapshot(mgr)
    return mgr


def _build_server() -> RobotServer:
    server = RobotServer()
    server.add_robot("main_hand", _DummySequence(), _make_mock_can_manager())
    return server


def _enter_match(server: RobotServer) -> None:
    for role, checklist in server.match.checklists.items():
        for item in checklist.items:
            server.match.set_checklist_item(role, item.id, True)
    server.match._phase = Phase.MATCH


async def _recv_type(ws, wanted: str, *, tries: int = 40) -> dict | None:
    for _ in range(tries):
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=0.2)
        except (TimeoutError, TypeError):
            return None
        if msg.get("type") == wanted:
            return msg
    return None


async def _expect_no_type(ws, unwanted: str, *, tries: int = 8) -> None:
    for _ in range(tries):
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=0.1)
        except (TimeoutError, TypeError):
            return
        assert msg.get("type") != unwanted, f"{unwanted} が要求元以外へ配信された: {msg}"


class TestRejectionGoesToRequesterOnly:
    async def test_phase_denied_command_notifies_requester_only(self) -> None:
        """フェーズゲートの拒否は要求元 1 台だけに返ること。"""
        server = _build_server()
        _enter_match(server)
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            requester = await client.ws_connect("/ws")
            bystander = await client.ws_connect("/ws")

            # 試合中の set_court はフェーズゲートで拒否される
            await requester.send_json({"type": "set_court", "court": Court.BLUE.value})

            msg = await _recv_type(requester, "command_rejected")
            assert msg is not None
            assert msg["command"] == "set_court"
            assert msg["reason"]

            await _expect_no_type(bystander, "command_rejected")

            await requester.close()
            await bystander.close()

    async def test_e_stop_denied_command_notifies_requester_only(self) -> None:
        """緊急停止ゲートの拒否も要求元 1 台だけに返ること。"""
        server = _build_server()
        _enter_match(server)
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            requester = await client.ws_connect("/ws")
            bystander = await client.ws_connect("/ws")

            await server.activate_e_stop()
            await requester.send_json({"type": "sequence_start", "robot": "main_hand"})

            msg = await _recv_type(requester, "command_rejected")
            assert msg is not None
            assert msg["command"] == "sequence_start"

            await _expect_no_type(bystander, "command_rejected")

            await requester.close()
            await bystander.close()

    async def test_match_start_rejection_uses_requester_ws(self) -> None:
        """match_start の拒否 (_handle_match_start 経路) が要求元へ返ること。

        この経路は _handle_command のフェーズゲートを通り抜けた後の防御的な
        二重判定なので、ゲートを迂回して直接呼び出さないと踏めない。
        """
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            requester = await client.ws_connect("/ws")
            await asyncio.sleep(0.05)
            assert len(server._ws_clients) == 1
            server_ws = next(iter(server._ws_clients))

            server.match._phase = Phase.SETUP
            await server._handle_match_start(server_ws)

            msg = await _recv_type(requester, "command_rejected")
            assert msg is not None
            assert msg["command"] == "match_start"

            await requester.close()

    async def test_internal_command_without_requester_is_not_broadcast(self) -> None:
        """要求元 ws を持たない経路 (内部呼び出し) では誰にも送らないこと。

        HTTP POST や内部の安全機構から来たコマンドには返す相手がいない。
        代わりに全配信すると、誰も押していない拒否通知が全画面に出る。
        """
        server = _build_server()
        _enter_match(server)
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            watcher = await client.ws_connect("/ws")

            await server._handle_command({"type": "set_court", "court": Court.BLUE.value})

            await _expect_no_type(watcher, "command_rejected")

            await watcher.close()
