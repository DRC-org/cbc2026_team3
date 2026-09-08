from __future__ import annotations

import asyncio
import logging

import pytest
from aiohttp.test_utils import TestClient, TestServer

from lib.config_schema import MatchSettings
from lib.control.position_loop import M3508PositionLoop
from lib.control.sync_monitor import SyncMonitor
from lib.control.target_refresh import GenericTargetRefresher
from lib.match_state import (
    ROLE_PRE_MATCH,
    ChecklistItem,
    Court,
    Phase,
)
from lib.sequence.engine import Sequence, step
from tests.fake_can import mock_can_manager
from tests.server_fixtures import ServerFixture, recv_type, seed_jitter_overrun

_DEFS = {
    ROLE_PRE_MATCH: [
        ChecklistItem(id="home", label="初期位置確認"),
        ChecklistItem(id="gripper", label="グリッパ開状態確認"),
    ],
}


async def _complete_checklist(ws) -> None:
    for item in _DEFS[ROLE_PRE_MATCH]:
        await ws.send_json(
            {"type": "checklist_set", "role": ROLE_PRE_MATCH, "item_id": item.id, "checked": True}
        )


_ROBOT_NAMES = ("main_hand", "sub_hand")


def _build_fixture_with_periodic_tasks() -> tuple[
    ServerFixture, M3508PositionLoop, SyncMonitor, GenericTargetRefresher
]:
    fx = ServerFixture.build(checklist_definitions=_DEFS)
    mgr = mock_can_manager(("y_axis_r",))

    position_loop = M3508PositionLoop(mgr, "bus0")
    sync_monitor = SyncMonitor([], {}, last_feedback_at=lambda _name: None)
    refresher = GenericTargetRefresher([])

    fx.add_robot(
        "main_hand",
        DummySequence("main_hand"),
        mgr,
        position_loops=[position_loop],
        sync_monitors=[sync_monitor],
        target_refreshers=[refresher],
    )
    for task in (position_loop, sync_monitor, refresher):
        seed_jitter_overrun(task, count=3, worst_s=0.04)
    return fx, position_loop, sync_monitor, refresher


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


class _GatedCheckSequence(Sequence):
    def __init__(self) -> None:
        super().__init__("motor_check")
        self.gate = asyncio.Event()

    @step("ゲート待ち")
    async def hold(self) -> None:
        await self.gate.wait()


def _build_fixture(**server_kwargs: object) -> ServerFixture:
    fx = ServerFixture.build(checklist_definitions=_DEFS, **server_kwargs)
    for name in _ROBOT_NAMES:
        fx.add_robot(name, DummySequence(name))
    return fx


class TestMatchStateSnapshotOnConnect:
    async def test_snapshot_sent_immediately(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            msg = await recv_type(ws, "match_state")
            assert msg is not None
            assert msg["phase"] == "setup"
            assert msg["court"] == "red"
            assert set(msg["checklists"]) == {ROLE_PRE_MATCH}
            await ws.close()


async def _match_state_with_phase(ws: object, phase: str) -> dict:
    for _ in range(10):
        msg = await recv_type(ws, "match_state")
        if msg is None:
            break
        if msg["phase"] == phase:
            return msg
    raise AssertionError(f"phase={phase} の match_state が配信されなかった")


class TestMatchTimerBroadcast:
    async def test_snapshot_carries_configured_duration(self) -> None:
        fx = _build_fixture(match_settings=MatchSettings(duration_s=90.0))
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            msg = await recv_type(ws, "match_state")
            assert msg is not None
            assert msg["timer"] == {"running": False, "elapsed_ms": 0, "duration_ms": 90000}
            await ws.close()

    async def test_timer_starts_running_when_the_match_starts(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _complete_checklist(ws)
            await ws.send_json({"type": "match_start"})
            await asyncio.sleep(0.05)

            msg = await _match_state_with_phase(ws, "match")
            assert msg["timer"]["running"] is True
            await ws.close()


class TestSequenceDoesNotAutoStart:
    async def test_sequence_idle_after_startup(self) -> None:
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
                    "role": ROLE_PRE_MATCH,
                    "item_id": "home",
                    "checked": True,
                }
            )
            msg = await recv_type(ws, "match_state")
            assert msg is not None
            assert msg["checklists"][ROLE_PRE_MATCH]["completed"] is False
            assert msg["can_start_match"] is False
            await ws.close()

    async def test_every_item_unlocks_ready(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _complete_checklist(ws)
            await asyncio.sleep(0.05)

            assert fx.match.phase is Phase.READY
            assert fx.match.can_start_match is True
            await ws.close()


class TestServerInfoOnConnect:
    async def test_flags_are_sent_on_connect(self) -> None:
        fx = _build_fixture(dev_tools=True, dry_run=True)
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            msg = await recv_type(ws, "server_info")
            assert msg is not None
            assert msg["dev_tools"] is True
            assert msg["dry_run"] is True
            await ws.close()

    async def test_dev_tools_defaults_to_disabled(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            msg = await recv_type(ws, "server_info")
            assert msg is not None
            assert msg["dev_tools"] is False
            await ws.close()


class TestChecklistCheckAll:
    async def test_rejected_without_dev_tools(self) -> None:
        fx = ServerFixture.build(checklist_definitions=_DEFS)
        for name in _ROBOT_NAMES:
            fx.add_robot(name, DummySequence(name))
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "checklist_check_all"})
            msg = await recv_type(ws, "command_rejected")
            assert msg is not None
            assert msg["command"] == "checklist_check_all"
            assert fx.match.can_start_match is False
            assert fx.match.phase is Phase.SETUP
            await ws.close()

    async def test_checks_every_role_with_dev_tools(self) -> None:
        fx = ServerFixture.build(checklist_definitions=_DEFS, dev_tools=True)
        for name in _ROBOT_NAMES:
            fx.add_robot(name, DummySequence(name))
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await recv_type(ws, "match_state")
            await ws.send_json({"type": "checklist_check_all"})
            msg = await recv_type(ws, "match_state")
            assert msg is not None
            assert all(item["checked"] for item in msg["checklists"][ROLE_PRE_MATCH]["items"])
            assert msg["can_start_match"] is True
            assert fx.match.phase is Phase.READY
            await ws.close()

    async def test_unknown_role_checks_nothing(self) -> None:
        fx = ServerFixture.build(checklist_definitions=_DEFS, dev_tools=True)
        for name in _ROBOT_NAMES:
            fx.add_robot(name, DummySequence(name))
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await recv_type(ws, "match_state")
            await ws.send_json({"type": "checklist_check_all", "role": "nobody"})
            await asyncio.sleep(0.05)

            assert fx.match.checklists[ROLE_PRE_MATCH].completed is False
            assert fx.match.can_start_match is False
            assert fx.match.phase is Phase.SETUP
            await ws.close()

    async def test_rejected_during_match(self) -> None:
        fx = ServerFixture.build(checklist_definitions=_DEFS, dev_tools=True)
        for name in _ROBOT_NAMES:
            fx.add_robot(name, DummySequence(name))
        fx.enter_match()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "checklist_check_all"})
            msg = await recv_type(ws, "command_rejected")
            assert msg is not None
            assert msg["command"] == "checklist_check_all"
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
            assert fx.sequence("sub_hand").executed == []
            await ws.close()

    async def test_motor_check_rejected_during_match(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()
        fx.enter_match()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await recv_type(ws, "motor_check_state")

            await ws.send_json({"type": "motor_check_start"})
            msg = await recv_type(ws, "motor_check_state")
            assert msg is not None
            assert "試合中" in (msg["error"] or "")
            await ws.close()

    async def test_motor_check_http_rejected_during_match(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()
        fx.enter_match()

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/motor_check")
            assert resp.status == 409


class TestMatchStartDuringMotorCheck:
    async def test_動作確認中の試合開始は理由付きで拒否される(self) -> None:
        fx = _build_fixture()
        check = _GatedCheckSequence()
        fx.set_motor_check_sequence(check)
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            fx.complete_all_checklists()
            assert await fx.start_motor_check() is True
            await fx.wait_motor_check_running()

            await ws.send_json({"type": "match_start"})
            msg = await recv_type(ws, "command_rejected")

            assert msg is not None
            assert msg["command"] == "match_start"
            assert "動作確認" in msg["reason"]
            assert fx.match.phase is not Phase.MATCH

            check.gate.set()
            await fx.wait_motor_check_idle()
            await ws.close()

    async def test_動作確認が終われば開始できる(self) -> None:
        fx = _build_fixture()
        check = _GatedCheckSequence()
        fx.set_motor_check_sequence(check)
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            fx.complete_all_checklists()
            check.gate.set()
            assert await fx.start_motor_check() is True
            await fx.wait_motor_check_idle()

            await ws.send_json({"type": "match_start"})
            await asyncio.sleep(0.05)

            assert fx.match.phase is Phase.MATCH
            await ws.close()


class TestMatchStartDoesNotMoveRobots:
    async def test_match_start_leaves_sequences_idle(self) -> None:
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
            assert fx.match.checklists[ROLE_PRE_MATCH].completed is False
            await ws.close()


class TestMatchStartResetsRxDownEpisodes:
    async def test_試合開始で全ロボットのエピソード数をリセットする(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _complete_checklist(ws)
            await ws.send_json({"type": "match_start"})
            await asyncio.sleep(0.05)

            for name in _ROBOT_NAMES:
                fx.can_manager(name).reset_rx_down_episodes.assert_called_once()
            await ws.close()

    async def test_ゲートで拒否された試みではリセットしない(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "match_start"})
            await asyncio.sleep(0.05)

            assert fx.match.phase is Phase.SETUP, "拒否されず試合が始まってしまっている"
            for name in _ROBOT_NAMES:
                fx.can_manager(name).reset_rx_down_episodes.assert_not_called()
            await ws.close()


class TestJitterResetOnMatchStart:
    async def test_match_start_が成立すると全タスクの乱れをリセットする(self) -> None:
        fx, position_loop, sync_monitor, refresher = _build_fixture_with_periodic_tasks()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            fx.complete_all_checklists()

            await ws.send_json({"type": "match_start"})
            await asyncio.sleep(0.05)

            assert fx.match.phase is Phase.MATCH
            for task in (position_loop, sync_monitor, refresher):
                assert task.jitter_overrun_count == 0
                assert task.worst_jitter_s == pytest.approx(0.0)
            await ws.close()

    async def test_フェーズゲートで拒否されたときはリセットしない(self) -> None:
        fx, position_loop, sync_monitor, refresher = _build_fixture_with_periodic_tasks()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            await ws.send_json({"type": "match_start"})
            msg = await recv_type(ws, "command_rejected")

            assert msg is not None
            assert msg["command"] == "match_start"
            assert fx.match.phase is not Phase.MATCH
            for task in (position_loop, sync_monitor, refresher):
                assert task.jitter_overrun_count == 3
                assert task.worst_jitter_s == pytest.approx(0.04)
            await ws.close()

    async def test_動作確認中の拒否ではリセットしない(self) -> None:
        fx, position_loop, sync_monitor, refresher = _build_fixture_with_periodic_tasks()
        check = _GatedCheckSequence()
        fx.set_motor_check_sequence(check)
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            fx.complete_all_checklists()
            assert await fx.start_motor_check() is True
            await fx.wait_motor_check_running()

            await ws.send_json({"type": "match_start"})
            msg = await recv_type(ws, "command_rejected")

            assert msg is not None
            assert msg["command"] == "match_start"
            assert "動作確認" in msg["reason"]
            assert fx.match.phase is not Phase.MATCH
            for task in (position_loop, sync_monitor, refresher):
                assert task.jitter_overrun_count == 3
                assert task.worst_jitter_s == pytest.approx(0.04)

            check.gate.set()
            await fx.wait_motor_check_idle()
            await ws.close()


class TestJitterSummaryOnMatchFinish:
    @staticmethod
    def _summary_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
        return [r.getMessage() for r in caplog.records if " の実周期: " in r.getMessage()]

    async def test_match_finish_が集計を残してから0に戻す(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        fx, position_loop, sync_monitor, refresher = _build_fixture_with_periodic_tasks()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            fx.complete_all_checklists()
            await ws.send_json({"type": "match_start"})
            await asyncio.sleep(0.05)
            assert fx.match.phase is Phase.MATCH

            for task in (position_loop, sync_monitor, refresher):
                seed_jitter_overrun(task, count=5, worst_s=0.06)

            with caplog.at_level(logging.INFO):
                await ws.send_json({"type": "match_finish"})
                await asyncio.sleep(0.05)

            assert fx.match.phase is Phase.FINISHED
            assert len(self._summary_lines(caplog)) == 3
            for task in (position_loop, sync_monitor, refresher):
                assert task.jitter_overrun_count == 0
                assert task.worst_jitter_s == pytest.approx(0.0)
            await ws.close()

    async def test_試合中でなければ集計を残さない(self, caplog: pytest.LogCaptureFixture) -> None:
        fx, position_loop, sync_monitor, refresher = _build_fixture_with_periodic_tasks()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            with caplog.at_level(logging.INFO):
                await ws.send_json({"type": "match_finish"})
                msg = await recv_type(ws, "command_rejected")

            assert msg is not None
            assert msg["command"] == "match_finish"
            assert fx.match.phase is not Phase.FINISHED
            assert self._summary_lines(caplog) == []
            for task in (position_loop, sync_monitor, refresher):
                assert task.jitter_overrun_count == 3
                assert task.worst_jitter_s == pytest.approx(0.04)
            await ws.close()
