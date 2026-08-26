from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from lib.can_manager import CANManager
from lib.drivers.base import MotorState
from lib.match_state import Phase
from lib.sequence.engine import Sequence, step
from lib.server import RobotServer
from tests.fake_health import ok_health_snapshot


async def _recv_type(ws, wanted: str, *, tries: int = 40) -> dict | None:
    """接続直後の match_state や周期配信に紛れた特定 type のメッセージを拾う。"""
    for _ in range(tries):
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=0.2)
        except (TimeoutError, TypeError):
            return None
        if msg.get("type") == wanted:
            return msg
    return None


def _enter_match(server: RobotServer) -> None:
    """シーケンス操作コマンドはフェーズゲートで試合中のみ許可される。"""
    for role, checklist in server.match.checklists.items():
        for item in checklist.items:
            server.match.set_checklist_item(role, item.id, True)
    server.match._phase = Phase.MATCH


class DummySequence(Sequence):
    """テスト用の最小シーケンス。"""

    def __init__(self) -> None:
        super().__init__("test_seq")
        self.triggered = False

    @step("待機ステップ", require_trigger=True)
    async def wait_step(self) -> None:
        self.triggered = True


def _make_mock_can_manager() -> CANManager:
    """モータ状態を返せる mock CANManager を生成する。"""
    mgr = MagicMock(spec=CANManager)
    motor = MagicMock()
    motor.state = MotorState(position=1500.0, velocity=0.0, current=0.2, temperature=35.0)
    motor.name = "m3508_1"
    mgr._motors = {"m3508_1": motor}
    mgr.get_motor.return_value = motor
    mgr.send = AsyncMock()
    mgr.send_to_bus = AsyncMock()
    mgr._buses = {"generic_bus": MagicMock()}
    # health() が HealthSnapshot を返さないと、サーバーの「判定できないものは DOWN」
    # 経路を常に踏み、本番とは別物の状態でテストすることになる
    mgr.health.side_effect = lambda **_kwargs: ok_health_snapshot(mgr)
    return mgr


def _build_server() -> RobotServer:
    server = RobotServer()
    seq = DummySequence()
    can_mgr = _make_mock_can_manager()
    server.add_robot("main_hand", seq, can_mgr)
    return server


class TestStateMessageFormat:
    async def test_state_message_format(self) -> None:
        """state メッセージの JSON 形式を検証する。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            # ブロードキャストを手動でトリガー
            await server._broadcast_state()

            msg = await _recv_type(ws, "state")
            assert msg is not None
            assert msg["type"] == "state"
            assert msg["robot"] == "main_hand"
            assert msg["sequence"] == "test_seq"
            assert "current_step" in msg
            assert "step_index" in msg
            assert "total_steps" in msg
            assert "waiting_trigger" in msg
            assert "motors" in msg
            assert "m3508_1" in msg["motors"]

            motor_data = msg["motors"]["m3508_1"]
            assert motor_data["pos"] == 1500.0
            assert motor_data["vel"] == 0.0
            assert motor_data["torque"] == 0.2
            assert motor_data["temp"] == 35.0

            await ws.close()


class TestTriggerCommand:
    async def test_trigger_command(self) -> None:
        """trigger コマンドでシーケンスの trigger() が呼ばれることを検証する。"""
        server = _build_server()
        _enter_match(server)
        ctx = server._robots["main_hand"]
        seq = ctx.sequence
        app = server.create_app()

        # シーケンスを実行して trigger 待ち状態にする
        task = asyncio.create_task(seq.run())
        await asyncio.sleep(0.05)
        assert seq.waiting_trigger is True

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "trigger", "robot": "main_hand"})
            await asyncio.sleep(0.05)

            assert seq.waiting_trigger is False
            await ws.close()

        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


class TestEStopCommand:
    async def test_e_stop_command(self) -> None:
        """e_stop コマンドが処理されることを検証する。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "e_stop"})
            await asyncio.sleep(0.05)
            await ws.close()

        # send_to_bus が呼ばれたことを確認
        can_mgr = server._robots["main_hand"].can_manager
        can_mgr.send_to_bus.assert_called()


class TestUnknownCommandIgnored:
    async def test_unknown_command_ignored(self) -> None:
        """不明なコマンドでエラーにならないことを検証する。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "totally_unknown_command"})
            await asyncio.sleep(0.05)

            # 接続が維持されていることを確認
            assert not ws.closed

            # 正常にブロードキャストを受信できることを確認
            await server._broadcast_state()
            msg = await _recv_type(ws, "state")
            assert msg is not None

            await ws.close()


class TestEStopSetsActiveState:
    async def test_e_stop_sets_active_state(self) -> None:
        """e_stop コマンドで e_stop_state メッセージが配信されることを検証する。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "e_stop"})
            await asyncio.sleep(0.05)

            assert server._e_stop_active is True

            msg = await _recv_type(ws, "e_stop_state")
            assert msg is not None
            assert msg["active"] is True

            await ws.close()


class TestEStopRelease:
    async def test_e_stop_release(self) -> None:
        """e_stop_release コマンドで e_stop_state active=false が配信されることを検証する。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            # まず緊急停止を有効化
            await ws.send_json({"type": "e_stop"})
            await asyncio.sleep(0.05)
            msg = await _recv_type(ws, "e_stop_state")
            assert msg is not None
            assert msg["active"] is True

            # 緊急停止を解除
            await ws.send_json({"type": "e_stop_release"})
            await asyncio.sleep(0.05)

            assert server._e_stop_active is False

            # ブロードキャストループの state メッセージが混在するため、
            # e_stop_state メッセージが見つかるまで読み進める
            found = False
            for _ in range(20):
                try:
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=0.1)
                except TimeoutError:
                    break
                if msg.get("type") == "e_stop_state" and msg.get("active") is False:
                    found = True
                    break
            assert found, "e_stop_state active=false メッセージが配信されなかった"

            await ws.close()


class TestStateIncludesEStopActive:
    async def test_state_includes_e_stop_active(self) -> None:
        """state メッセージに e_stop_active フィールドが含まれることを検証する。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            await server._broadcast_state()
            msg = await _recv_type(ws, "state")
            assert msg is not None
            assert "e_stop_active" in msg
            assert msg["e_stop_active"] is False

            await ws.close()


class TestSetParamCommand:
    async def test_set_param_command(self) -> None:
        """set_param コマンドの受付を検証する。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json(
                {
                    "type": "set_param",
                    "motor": "m3508_1",
                    "key": "kp",
                    "value": 1.5,
                }
            )
            await asyncio.sleep(0.05)

            # エラーなく接続が維持されていること
            assert not ws.closed
            await ws.close()
