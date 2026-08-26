from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from lib.match_state import (
    ROLE_MAIN_HAND,
    ROLE_SUB_HAND,
    ChecklistItem,
    Court,
    Phase,
)
from lib.sequence.engine import Sequence, step
from tests.server_fixtures import ServerFixture, recv_type

_DEFS = {
    ROLE_MAIN_HAND: [ChecklistItem(id="home", label="メイン初期位置確認")],
    ROLE_SUB_HAND: [ChecklistItem(id="home", label="サブ初期位置確認")],
}

_ROBOT_NAMES = ("main_hand", "sub_hand")


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


def _build_fixture() -> ServerFixture:
    fx = ServerFixture.build(checklist_definitions=_DEFS)
    for name in _ROBOT_NAMES:
        fx.add_robot(name, DummySequence(name))
    return fx


class TestMatchStateSnapshotOnConnect:
    async def test_snapshot_sent_immediately(self) -> None:
        """接続直後に match_state が届かないと、リロードした操縦者が現在の状況を知れない。"""
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            msg = await recv_type(ws, "match_state")
            assert msg is not None
            assert msg["phase"] == "setup"
            assert msg["court"] == "red"
            assert set(msg["checklists"]) == {ROLE_MAIN_HAND, ROLE_SUB_HAND}
            await ws.close()


class TestSequenceDoesNotAutoStart:
    async def test_sequence_idle_after_startup(self) -> None:
        """明示的な開始合図があるまでシーケンスを走らせない。"""
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            await asyncio.sleep(0.2)
            for seq in fx.sequences():
                assert seq.is_running is False
                assert seq.executed == []


class TestCourtCommand:
    async def test_set_court_propagates_to_sequences(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "set_court", "court": "blue"})
            await asyncio.sleep(0.05)

            assert fx.match.court is Court.BLUE
            for seq in fx.sequences():
                assert seq.court is Court.BLUE
            await ws.close()

    async def test_invalid_value_ignored(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "set_court", "court": "green"})
            await asyncio.sleep(0.05)

            assert fx.match.court is Court.RED
            assert not ws.closed
            await ws.close()


class TestChecklistCommands:
    async def test_checklist_set_broadcasts_match_state(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await recv_type(ws, "match_state")

            await ws.send_json(
                {
                    "type": "checklist_set",
                    "role": ROLE_MAIN_HAND,
                    "item_id": "home",
                    "checked": True,
                }
            )
            msg = await recv_type(ws, "match_state")
            assert msg is not None
            assert msg["checklists"][ROLE_MAIN_HAND]["completed"] is True
            # 片方だけでは試合に入れない
            assert msg["can_start_match"] is False
            await ws.close()

    async def test_both_operators_unlock_ready(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            for role in (ROLE_MAIN_HAND, ROLE_SUB_HAND):
                await ws.send_json(
                    {"type": "checklist_set", "role": role, "item_id": "home", "checked": True}
                )
            await asyncio.sleep(0.05)

            assert fx.match.phase is Phase.READY
            assert fx.match.can_start_match is True
            await ws.close()


class TestPhaseGate:
    async def test_sequence_start_rejected_before_match(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "sequence_start", "robot": "main_hand"})

            msg = await recv_type(ws, "command_rejected")
            assert msg is not None
            assert msg["command"] == "sequence_start"
            assert msg["reason"]

            await asyncio.sleep(0.1)
            assert fx.sequence("main_hand").executed == []
            await ws.close()

    async def test_sequence_start_allowed_in_match(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            fx.complete_all_checklists()

            await ws.send_json({"type": "match_start"})
            await asyncio.sleep(0.05)
            assert fx.match.phase is Phase.MATCH

            await ws.send_json({"type": "sequence_start", "robot": "main_hand"})
            await asyncio.sleep(0.15)
            assert fx.sequence("main_hand").executed == ["first"]
            # 操縦者が押した側だけが動く (試合開始は両機を起動しない)
            assert fx.sequence("sub_hand").executed == []
            await ws.close()

    async def test_motor_check_rejected_during_match(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()
        fx.enter_match()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "motor_check_start", "robot": "main_hand"})
            msg = await recv_type(ws, "motor_check_error")
            assert msg is not None
            await ws.close()

    async def test_motor_check_http_rejected_during_match(self) -> None:
        """HTTP 経路は handle_command を通らないため個別にゲートが要る。"""
        fx = _build_fixture()
        app = fx.create_app()
        fx.enter_match()

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/robots/main_hand/motor_check")
            assert resp.status == 409


class TestMatchStartDoesNotMoveRobots:
    async def test_match_start_leaves_sequences_idle(self) -> None:
        """試合開始はフェーズを進めるだけ。動き出すのは操縦者が START を押してから。"""
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            fx.complete_all_checklists()

            await ws.send_json({"type": "match_start"})
            await asyncio.sleep(0.15)

            assert fx.match.phase is Phase.MATCH
            for seq in fx.sequences():
                assert seq.executed == []
                assert seq.is_running is False
            await ws.close()


class TestMatchFinishAndReset:
    async def test_finish_stops_sequences(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            fx.complete_all_checklists()
            await ws.send_json({"type": "match_start"})
            await asyncio.sleep(0.05)
            await ws.send_json({"type": "sequence_start", "robot": "main_hand"})
            await asyncio.sleep(0.05)

            await ws.send_json({"type": "match_finish"})
            await asyncio.sleep(0.15)

            assert fx.match.phase is Phase.FINISHED
            assert fx.sequence("main_hand").is_running is False
            await ws.close()

    async def test_reset_returns_to_setup(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            fx.complete_all_checklists()
            await ws.send_json({"type": "match_start"})
            await asyncio.sleep(0.05)

            await ws.send_json({"type": "match_reset"})
            await asyncio.sleep(0.05)

            assert fx.match.phase is Phase.SETUP
            assert fx.match.checklists[ROLE_MAIN_HAND].completed is False
            await ws.close()
