from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from aiohttp.test_utils import TestClient, TestServer

from lib.can_manager import CANManager
from lib.drivers.base import MotorState
from lib.match_state import (
    ROLE_MAIN_HAND,
    ROLE_SUB_HAND,
    ChecklistItem,
    Court,
    Phase,
)
from lib.sequence.engine import Sequence, step
from lib.server import RobotServer
from tests.fake_health import ok_health_snapshot

_DEFS = {
    ROLE_MAIN_HAND: [ChecklistItem(id="home", label="メイン初期位置確認")],
    ROLE_SUB_HAND: [ChecklistItem(id="home", label="サブ初期位置確認")],
}


class DummySequence(Sequence):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.executed: list[str] = []

    @step("最初のステップ")
    async def first(self) -> None:
        self.executed.append("first")
        await asyncio.sleep(0.01)

    @step("待機ステップ", require_trigger=True)
    async def wait_step(self) -> None:
        self.executed.append("wait_step")


def _make_mock_can_manager() -> CANManager:
    mgr = MagicMock(spec=CANManager)
    motor = MagicMock()
    motor.state = MotorState(position=0.0, velocity=0.0, current=0.0, temperature=30.0)
    motor.name = "m1"
    mgr._motors = {"m1": motor}
    mgr.get_motor.return_value = motor
    mgr.send = AsyncMock()
    mgr.send_to_bus = AsyncMock()
    mgr._buses = {"bus0": MagicMock()}
    # health() が HealthSnapshot を返さないと、サーバーの「判定できないものは DOWN」
    # 経路を常に踏み、本番とは別物の状態でテストすることになる
    mgr.health.side_effect = lambda **_kwargs: ok_health_snapshot(mgr)
    return mgr


def _build_server() -> RobotServer:
    server = RobotServer(checklist_definitions=_DEFS)
    for name in ("main_hand", "sub_hand"):
        server.add_robot(name, DummySequence(name), _make_mock_can_manager())
    return server


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


def _complete(server: RobotServer, role: str) -> None:
    for item in server.match.checklists[role].items:
        server.match.set_checklist_item(role, item.id, True)


class TestMatchStateSnapshotOnConnect:
    async def test_snapshot_sent_immediately(self) -> None:
        """接続直後に match_state が届かないと、リロードした操縦者が現在の状況を知れない。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            msg = await _recv_type(ws, "match_state")
            assert msg is not None
            assert msg["phase"] == "setup"
            assert msg["court"] == "red"
            assert set(msg["checklists"]) == {ROLE_MAIN_HAND, ROLE_SUB_HAND}
            await ws.close()


class TestSequenceDoesNotAutoStart:
    async def test_sequence_idle_after_startup(self) -> None:
        """明示的な開始合図があるまでシーケンスを走らせない。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)):
            await asyncio.sleep(0.2)
            for name in ("main_hand", "sub_hand"):
                seq = server._robots[name].sequence
                assert seq._running is False
                assert seq.executed == []


class TestCourtCommand:
    async def test_set_court_propagates_to_sequences(self) -> None:
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "set_court", "court": "blue"})
            await asyncio.sleep(0.05)

            assert server.match.court is Court.BLUE
            for name in ("main_hand", "sub_hand"):
                assert server._robots[name].sequence.court is Court.BLUE
            await ws.close()

    async def test_invalid_value_ignored(self) -> None:
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "set_court", "court": "green"})
            await asyncio.sleep(0.05)

            assert server.match.court is Court.RED
            assert not ws.closed
            await ws.close()


class TestChecklistCommands:
    async def test_checklist_set_broadcasts_match_state(self) -> None:
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _recv_type(ws, "match_state")

            await ws.send_json(
                {
                    "type": "checklist_set",
                    "role": ROLE_MAIN_HAND,
                    "item_id": "home",
                    "checked": True,
                }
            )
            msg = await _recv_type(ws, "match_state")
            assert msg is not None
            assert msg["checklists"][ROLE_MAIN_HAND]["completed"] is True
            # 片方だけでは試合に入れない
            assert msg["can_start_match"] is False
            await ws.close()

    async def test_both_operators_unlock_ready(self) -> None:
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            for role in (ROLE_MAIN_HAND, ROLE_SUB_HAND):
                await ws.send_json(
                    {"type": "checklist_set", "role": role, "item_id": "home", "checked": True}
                )
            await asyncio.sleep(0.05)

            assert server.match.phase is Phase.READY
            assert server.match.can_start_match is True
            await ws.close()


class TestPhaseGate:
    async def test_sequence_start_rejected_before_match(self) -> None:
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "sequence_start", "robot": "main_hand"})

            msg = await _recv_type(ws, "command_rejected")
            assert msg is not None
            assert msg["command"] == "sequence_start"
            assert msg["reason"]

            await asyncio.sleep(0.1)
            assert server._robots["main_hand"].sequence.executed == []
            await ws.close()

    async def test_sequence_start_allowed_in_match(self) -> None:
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            _complete(server, ROLE_MAIN_HAND)
            _complete(server, ROLE_SUB_HAND)

            await ws.send_json({"type": "match_start"})
            await asyncio.sleep(0.05)
            assert server.match.phase is Phase.MATCH

            await ws.send_json({"type": "sequence_start", "robot": "main_hand"})
            await asyncio.sleep(0.15)
            assert server._robots["main_hand"].sequence.executed == ["first"]
            # 操縦者が押した側だけが動く (試合開始は両機を起動しない)
            assert server._robots["sub_hand"].sequence.executed == []
            await ws.close()

    async def test_motor_check_rejected_during_match(self) -> None:
        server = _build_server()
        app = server.create_app()
        _complete(server, ROLE_MAIN_HAND)
        _complete(server, ROLE_SUB_HAND)
        server.match.match_start()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "motor_check_start", "robot": "main_hand"})
            msg = await _recv_type(ws, "motor_check_error")
            assert msg is not None
            await ws.close()

    async def test_motor_check_http_rejected_during_match(self) -> None:
        """HTTP 経路は _handle_command を通らないため個別にゲートが要る。"""
        server = _build_server()
        app = server.create_app()
        _complete(server, ROLE_MAIN_HAND)
        _complete(server, ROLE_SUB_HAND)
        server.match.match_start()

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/robots/main_hand/motor_check")
            assert resp.status == 409


class TestMatchStartDoesNotMoveRobots:
    async def test_match_start_leaves_sequences_idle(self) -> None:
        """試合開始はフェーズを進めるだけ。動き出すのは操縦者が START を押してから。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            _complete(server, ROLE_MAIN_HAND)
            _complete(server, ROLE_SUB_HAND)

            await ws.send_json({"type": "match_start"})
            await asyncio.sleep(0.15)

            assert server.match.phase is Phase.MATCH
            for name in ("main_hand", "sub_hand"):
                seq = server._robots[name].sequence
                assert seq.executed == []
                assert seq._running is False
            await ws.close()


class TestMatchFinishAndReset:
    async def test_finish_stops_sequences(self) -> None:
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            _complete(server, ROLE_MAIN_HAND)
            _complete(server, ROLE_SUB_HAND)
            await ws.send_json({"type": "match_start"})
            await asyncio.sleep(0.05)
            await ws.send_json({"type": "sequence_start", "robot": "main_hand"})
            await asyncio.sleep(0.05)

            await ws.send_json({"type": "match_finish"})
            await asyncio.sleep(0.15)

            assert server.match.phase is Phase.FINISHED
            assert server._robots["main_hand"].sequence._running is False
            await ws.close()

    async def test_reset_returns_to_setup(self) -> None:
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            _complete(server, ROLE_MAIN_HAND)
            _complete(server, ROLE_SUB_HAND)
            await ws.send_json({"type": "match_start"})
            await asyncio.sleep(0.05)

            await ws.send_json({"type": "match_reset"})
            await asyncio.sleep(0.05)

            assert server.match.phase is Phase.SETUP
            assert server.match.checklists[ROLE_MAIN_HAND].completed is False
            await ws.close()
