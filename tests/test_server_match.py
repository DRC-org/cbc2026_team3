from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from lib.config_schema import MatchSettings
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


def _build_fixture(**server_kwargs: object) -> ServerFixture:
    fx = ServerFixture.build(checklist_definitions=_DEFS, **server_kwargs)
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


async def _match_state_with_phase(ws: object, phase: str) -> dict:
    """指定フェーズの match_state を拾う。

    チェックリスト操作でも match_state が飛ぶため、接続直後のスナップショットを
    そのまま見ると必ず setup を掴む。
    """
    for _ in range(10):
        msg = await recv_type(ws, "match_state")
        if msg is None:
            break
        if msg["phase"] == phase:
            return msg
    raise AssertionError(f"phase={phase} の match_state が配信されなかった")


class TestMatchTimerBroadcast:
    """タイマーは match_state に相乗りして全クライアントへ届く。

    専用の配信経路を作らないのは、`_fanout` の約束事 (送信ごとの上限・切り離しの
    後始末) を守る経路をこれ以上増やさないため。接続直後のスナップショットが
    そのままアンカーになるので、途中接続の同期にも追加の仕組みが要らない。
    """

    async def test_snapshot_carries_configured_duration(self) -> None:
        """config/system.yaml の試合時間が実際に配信へ載ること。

        既定値と同じ値で試すと、設定を配線し忘れた実装でもテストが通ってしまう。
        当日ルールが変われば yaml を書き換えるのに画面が 3 分のまま、という
        壊れ方はログにも UI にも現れない。
        """
        fx = _build_fixture(match_settings=MatchSettings(duration_s=90.0))
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            msg = await recv_type(ws, "match_state")
            assert msg is not None
            assert msg["timer"] == {"running": False, "elapsed_ms": 0, "duration_ms": 90000}
            await ws.close()

    async def test_timer_starts_running_when_the_match_starts(self) -> None:
        """試合開始の配信でタイマーが走り出すこと。ここが false のままだと
        全デバイスが 3:00 を表示したまま止まる。"""
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            for role in (ROLE_MAIN_HAND, ROLE_SUB_HAND):
                await ws.send_json(
                    {"type": "checklist_set", "role": role, "item_id": "home", "checked": True}
                )
            await ws.send_json({"type": "match_start"})
            await asyncio.sleep(0.05)

            msg = await _match_state_with_phase(ws, "match")
            assert msg["timer"]["running"] is True
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


_MULTI_DEFS = {
    ROLE_MAIN_HAND: [
        ChecklistItem(id="home", label="メイン初期位置確認"),
        ChecklistItem(id="gripper", label="グリッパ開状態確認"),
    ],
    ROLE_SUB_HAND: [ChecklistItem(id="home", label="サブ初期位置確認")],
}


class TestServerInfoOnConnect:
    """開発用ボタンの表示可否はサーバーが配る (クライアントのビルド時定数にしない)。"""

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
    """開発用の一括チェック。**本番起動では効かない**ことまでが仕様。"""

    async def test_rejected_without_dev_tools(self) -> None:
        fx = ServerFixture.build(checklist_definitions=_MULTI_DEFS)
        for name in _ROBOT_NAMES:
            fx.add_robot(name, DummySequence(name))
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "checklist_check_all"})
            msg = await recv_type(ws, "command_rejected")
            assert msg is not None
            assert msg["command"] == "checklist_check_all"
            # 拒否されただけでなく、チェックが 1 つも付いていないこと
            assert fx.match.can_start_match is False
            assert fx.match.phase is Phase.SETUP
            await ws.close()

    async def test_checks_every_role_with_dev_tools(self) -> None:
        fx = ServerFixture.build(checklist_definitions=_MULTI_DEFS, dev_tools=True)
        for name in _ROBOT_NAMES:
            fx.add_robot(name, DummySequence(name))
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await recv_type(ws, "match_state")
            await ws.send_json({"type": "checklist_check_all"})
            msg = await recv_type(ws, "match_state")
            assert msg is not None
            for role in (ROLE_MAIN_HAND, ROLE_SUB_HAND):
                assert all(item["checked"] for item in msg["checklists"][role]["items"])
            assert msg["can_start_match"] is True
            assert fx.match.phase is Phase.READY
            await ws.close()

    async def test_role_argument_limits_the_effect(self) -> None:
        fx = ServerFixture.build(checklist_definitions=_MULTI_DEFS, dev_tools=True)
        for name in _ROBOT_NAMES:
            fx.add_robot(name, DummySequence(name))
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await recv_type(ws, "match_state")
            await ws.send_json({"type": "checklist_check_all", "role": ROLE_MAIN_HAND})
            msg = await recv_type(ws, "match_state")
            assert msg is not None
            assert msg["checklists"][ROLE_MAIN_HAND]["completed"] is True
            assert msg["checklists"][ROLE_SUB_HAND]["completed"] is False
            assert msg["can_start_match"] is False
            await ws.close()

    async def test_rejected_during_match(self) -> None:
        """開発用でもフェーズゲートは外れない (試合中に指差喚呼を触らせない)。"""
        fx = ServerFixture.build(checklist_definitions=_MULTI_DEFS, dev_tools=True)
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
