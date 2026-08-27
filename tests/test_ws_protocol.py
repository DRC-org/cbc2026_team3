from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from lib.can_manager import CANManager
from lib.drivers.base import MotorState
from lib.health import BusHealth, MotorHealth
from lib.sequence.engine import Sequence, step
from tests.fake_can import mock_can_manager
from tests.fake_health import ok_health_snapshot
from tests.server_fixtures import ServerFixture, recv_type


class DummySequence(Sequence):
    """テスト用の最小シーケンス。"""

    def __init__(self) -> None:
        super().__init__("test_seq")
        self.triggered = False

    @step("待機ステップ", require_trigger=True)
    async def wait_step(self) -> None:
        self.triggered = True


def _build_fixture() -> ServerFixture:
    fx = ServerFixture.build()
    fx.add_robot(
        "main_hand",
        DummySequence(),
        mock_can_manager(
            {"m3508_1": MotorState(position=1500.0, velocity=0.0, current=0.2, temperature=35.0)},
            bus_name="generic_bus",
        ),
    )
    return fx


class TestStateMessageFormat:
    async def test_state_message_format(self) -> None:
        """state メッセージの JSON 形式を検証する。"""
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            # ブロードキャストを手動でトリガー
            await fx.publish_state()

            msg = await recv_type(ws, "state")
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
        fx = _build_fixture()
        fx.enter_match()
        seq = fx.sequence("main_hand")
        app = fx.create_app()

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
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "e_stop"})
            await asyncio.sleep(0.05)
            await ws.close()

        # send_to_bus が呼ばれたことを確認
        fx.can_manager("main_hand").send_to_bus.assert_called()


class TestUnknownCommandIgnored:
    async def test_unknown_command_ignored(self) -> None:
        """不明なコマンドでエラーにならないことを検証する。"""
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "totally_unknown_command"})
            await asyncio.sleep(0.05)

            # 接続が維持されていることを確認
            assert not ws.closed

            # 正常にブロードキャストを受信できることを確認
            await fx.publish_state()
            msg = await recv_type(ws, "state")
            assert msg is not None

            await ws.close()


class TestEStopSetsActiveState:
    async def test_e_stop_sets_active_state(self) -> None:
        """e_stop コマンドで e_stop_state メッセージが配信されることを検証する。"""
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "e_stop"})
            await asyncio.sleep(0.05)

            assert fx.e_stop_active is True

            msg = await recv_type(ws, "e_stop_state")
            assert msg is not None
            assert msg["active"] is True

            await ws.close()


class TestEStopRelease:
    async def test_e_stop_release(self) -> None:
        """e_stop_release コマンドで e_stop_state active=false が配信されることを検証する。"""
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            # まず緊急停止を有効化
            await ws.send_json({"type": "e_stop"})
            await asyncio.sleep(0.05)
            msg = await recv_type(ws, "e_stop_state")
            assert msg is not None
            assert msg["active"] is True

            # 緊急停止を解除
            await ws.send_json({"type": "e_stop_release"})
            await asyncio.sleep(0.05)

            assert fx.e_stop_active is False

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
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            await fx.publish_state()
            msg = await recv_type(ws, "state")
            assert msg is not None
            assert "e_stop_active" in msg
            assert msg["e_stop_active"] is False

            await ws.close()


class TestSetParamCommand:
    async def test_set_param_command(self) -> None:
        """set_param コマンドの受付を検証する。"""
        fx = _build_fixture()
        app = fx.create_app()

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


def _fault_health_snapshot(mgr: CANManager):
    """全モータ FAULT のスナップショット (health_change 差分を作るため)。"""
    snap = ok_health_snapshot(mgr)
    for motor in snap.motors:
        motor.state = MotorHealth.FAULT
    snap.overall = BusHealth.DOWN
    return snap


class TestStateIncludesRunning:
    async def test_state_includes_running(self) -> None:
        """state に running が含まれること。

        欠けていると UI が step_index/total_steps から実行中かを推測することになる。
        """
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            await fx.publish_state()
            msg = await recv_type(ws, "state")
            assert msg is not None
            assert msg["running"] is False

            await ws.close()

    async def test_state_running_true_while_sequence_runs(self) -> None:
        """シーケンス実行中は running=true になること。"""
        fx = _build_fixture()
        fx.enter_match()
        seq = fx.sequence("main_hand")
        app = fx.create_app()

        task = asyncio.create_task(seq.run())
        await asyncio.sleep(0.05)

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await fx.publish_state()
            msg = await recv_type(ws, "state")
            assert msg is not None
            assert msg["running"] is True
            await ws.close()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestHealthChangeIncludesRobot:
    async def test_health_change_includes_robot(self) -> None:
        """health_change にどの機体のイベントかが載ること。

        Monitor は 2 機分のイベントを 1 本のリストに並べるため、robot が無いと
        どちらの異常か判別できない。
        """
        fx = _build_fixture()
        can_mgr = fx.can_manager("main_hand")
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            # 1 回目で前回スナップショットを作り、2 回目で FAULT への遷移を出す
            await fx.publish_state()
            can_mgr.health.side_effect = lambda **_kwargs: _fault_health_snapshot(can_mgr)
            await fx.publish_state()

            msg = await recv_type(ws, "health_change")
            assert msg is not None
            assert msg["robot"] == "main_hand"
            assert msg["target"] == "motor:m3508_1"

            await ws.close()
