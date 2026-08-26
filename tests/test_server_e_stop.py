from __future__ import annotations

import asyncio
import struct
import time
from unittest.mock import AsyncMock, MagicMock, patch

import can
from aiohttp.test_utils import TestClient, TestServer

from lib.axis_sync import MotorSpec, SyncGroup
from lib.can_manager import CANManager
from lib.control.position_loop import M3508PositionLoop, make_position_pid
from lib.control.sync_monitor import SyncMonitor
from lib.control.target_refresh import GenericTargetRefresher
from lib.drivers.base import ControlMode, MotorState
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import M3508Driver
from lib.match_state import (
    ROLE_MAIN_HAND,
    ROLE_SUB_HAND,
    ChecklistItem,
    Phase,
)
from lib.sequence.engine import Sequence, step
from lib.sequence.motors import MotorHandle
from lib.server import RobotServer
from tests.fake_health import ok_health_snapshot

_ROBOT_NAMES = ("main_hand", "sub_hand")

_DEFS = {
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
    # health() が HealthSnapshot を返さないと、サーバーの「判定できないものは DOWN」
    # 経路を常に踏み、本番とは別物の状態でテストすることになる
    mgr.health.side_effect = lambda **_kwargs: ok_health_snapshot(mgr)
    return mgr


def _build_server() -> RobotServer:
    server = RobotServer(checklist_definitions=_DEFS)
    for name in _ROBOT_NAMES:
        server.add_robot(name, GatedSequence(name), _make_mock_can_manager())
    return server


def _sequences(server: RobotServer) -> list[GatedSequence]:
    return [server._robots[name].sequence for name in _ROBOT_NAMES]  # type: ignore[misc]


def _complete_all(server: RobotServer) -> None:
    """全ロールのチェックを完了させ READY へ進める。"""
    for role in (ROLE_MAIN_HAND, ROLE_SUB_HAND):
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
    """match_start が通ると操縦者の sequence_start が解禁されるため、停止中は塞ぐ。"""

    async def test_match_start_rejected_while_e_stop_active(self) -> None:
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            _complete_all(server)
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

    async def test_match_start_allowed_after_release(self) -> None:
        """解除後は試合開始が通り、操縦者の sequence_start が受理されること。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            _complete_all(server)

            await ws.send_json({"type": "e_stop"})
            await _wait_until(lambda: server._e_stop_active)

            await ws.send_json({"type": "e_stop_release"})
            await _wait_until(lambda: not server._e_stop_active)

            await ws.send_json({"type": "match_start"})
            entered = await _wait_until(lambda: server.match.phase is Phase.MATCH)
            assert entered, "解除後の match_start が通っていない"

            # 試合開始そのものは機体を動かさない
            seqs = _sequences(server)
            await asyncio.sleep(0.1)
            assert all(s.executed == [] for s in seqs)

            await ws.send_json({"type": "sequence_start", "robot": "main_hand"})
            started = await _wait_until(lambda: seqs[0].executed == ["gate"])
            assert started, "解除後に sequence_start が通っていない"

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

    async def test_set_param_not_blocked_by_e_stop_gate_when_inactive(self) -> None:
        """緊急停止していなければ緊急停止ゲートでは弾かれないこと。

        このフィクスチャの m1 は PC 側 PID を持たない (M3508 位置制御ループが無い) ため
        set_param 自体は別の理由で拒否される。ここで見たいのは緊急停止ゲートの挙動だけ
        なので、拒否理由が緊急停止由来でないことを確認する。
        """
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "set_param", "motor": "m1", "key": "kp", "value": 1.0})
            msg = await _recv_type(ws, "command_rejected", tries=5)
            assert msg is None or "緊急停止" not in msg["reason"]
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


class TestActivateEStopFromInside:
    """同期監視など内部の異常検知から、操縦者の e_stop と同じ経路で止められること。"""

    async def test_same_side_effects_as_e_stop_command(self) -> None:
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _start_both_sequences(server, ws)

            await server.activate_e_stop(reason="y_axis の左右ずれ")

            assert server.e_stop_active is True
            for name in _ROBOT_NAMES:
                # 停止フレームはモータ個別・バス全体の両方へ出す
                server._robots[name].can_manager.send_to_bus.assert_awaited()

            await _release_gates_and_settle(seqs)
            for s in seqs:
                assert s.executed == ["gate"], f"{s.name}: 内部緊急停止後に後続ステップが実行された"
                assert s._running is False

            await ws.close()

    async def test_discards_pending_start_request(self) -> None:
        server = _build_server()
        seq = _sequences(server)[0]
        seq.request_start()
        assert seq._resume_event.is_set()

        await server.activate_e_stop(reason="rotate の左右ずれ")

        assert not seq._resume_event.is_set()

    async def test_reason_is_broadcast(self) -> None:
        """試合中に「なぜ止まったか」が操縦者に届かないと復旧できない。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            await server.activate_e_stop(reason="y_axis の左右ずれ 3.400mm が許容 2.000mm 超過")

            msg = await _recv_type(ws, "e_stop_state")
            assert msg is not None
            assert msg["active"] is True
            assert "y_axis" in msg["reason"]

            await ws.close()

    async def test_command_e_stop_keeps_broadcast_shape(self) -> None:
        """操縦者操作による緊急停止の配信内容は従来どおり (理由なしでも壊れない)。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            await ws.send_json({"type": "e_stop"})

            msg = await _recv_type(ws, "e_stop_state")
            assert msg is not None
            assert msg["active"] is True
            assert msg.get("reason") is None

            await ws.close()

    async def test_repeated_activation_is_safe(self) -> None:
        """同期監視は軸ごとに発報しうる。多重発報で状態が壊れないこと。"""
        server = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _start_both_sequences(server, ws)

            await server.activate_e_stop(reason="y_axis の左右ずれ")
            await server.activate_e_stop(reason="rotate の左右ずれ")
            await server.activate_e_stop()

            assert server.e_stop_active is True

            await _release_gates_and_settle(seqs)
            for s in seqs:
                assert s.executed == ["gate"]
                assert s._running is False

            await ws.close()


# ---------------------------------------------------------------------- #
#  同期ずれラッチの解除経路
# ---------------------------------------------------------------------- #


def _feed(driver: M3508Driver, deg: float) -> None:
    """M3508 のフィードバックフレームを 1 通流し込む。"""
    raw = round(deg / 360.0 * 8192) % 8192
    driver.update_state(
        can.Message(
            arbitration_id=0x200 + driver.can_id,
            data=struct.pack(">HhhBB", raw, 0, 0, 25, 0),
        )
    )


class _SyncFixture:
    """位置制御ループと同期監視を実物のまま RobotServer へ配線した一式。

    左右ペアのドライバをループと監視で共有する。実機と同じく「同じずれを
    双方が見る」構成にしないと、解除経路の穴が見えない。
    """

    def __init__(self, *, tolerance: float = 0.0) -> None:
        self.mgr = _make_mock_can_manager()
        self.right = M3508Driver("y_r", can_id=1)
        self.left = M3508Driver("y_l", can_id=2)
        self.mgr._motors = {"y_r": self.right, "y_l": self.left}
        self.mgr.last_feedback_at.side_effect = lambda _name: time.time()

        group = SyncGroup(
            name="y_axis",
            members=(
                MotorSpec(name="y_r", scale=1.0, offset=0.0),
                MotorSpec(name="y_l", scale=-1.0, offset=0.0),
            ),
            tolerance=tolerance,
        )

        self.server = RobotServer(checklist_definitions=_DEFS)
        self.loop = M3508PositionLoop(
            self.mgr,
            "can_m3508",
            is_estop_active=lambda: self.server.e_stop_active,
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
        self.server.add_robot(
            "main_hand",
            GatedSequence("main_hand"),
            self.mgr,
            position_loops=[self.loop],
            sync_monitors=[self.monitor],
        )
        # 累積角の原点は初回フィードバックで確定する。先に 0deg を流しておかないと
        # 「ずれた姿勢」がそのまま原点になり、偏差 0 と判定されてしまう
        _feed(self.right, 0.0)
        _feed(self.left, 0.0)

    def _on_violation(self, axis: str, deviation: float) -> None:
        self.violations.append((axis, deviation))
        task = asyncio.create_task(self.server.activate_e_stop(reason=f"{axis} の左右ずれ"))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def deviate(self) -> None:
        """左右が逆向きに 10deg ずれた状態にする (人間の単位で 20 のずれ)。"""
        _feed(self.right, 10.0)
        _feed(self.left, 10.0)

    def aligned(self) -> None:
        """逆回転ペアが正しく揃っている状態にする。"""
        _feed(self.right, 10.0)
        _feed(self.left, -10.0)

    async def settle(self) -> None:
        for _ in range(5):
            await asyncio.sleep(0)


class TestSyncLatchRelease:
    async def test_release_clears_position_loop_latch(self) -> None:
        """ラッチしたままだと y_axis はプロセス再起動まで電流 0 で復帰できない。"""
        fx = _SyncFixture()
        fx.deviate()
        await fx.loop.step()
        assert fx.loop.sync_violations == frozenset({"y_axis"})

        await fx.server._handle_command({"type": "e_stop"})
        await fx.server._handle_command({"type": "e_stop_release"})

        assert fx.loop.sync_violations == frozenset()

    async def test_release_clears_sync_monitor_latch(self) -> None:
        """SyncMonitor がラッチしたままだと、以後どれだけずれても二度と発報しない。"""
        fx = _SyncFixture()
        fx.deviate()
        fx.monitor.step()
        await fx.settle()
        assert fx.monitor.violated == frozenset({"y_axis"})
        assert fx.server.e_stop_active is True

        await fx.server._handle_command({"type": "e_stop_release"})

        assert fx.monitor.violated == frozenset()

    async def test_release_does_not_disable_monitoring(self) -> None:
        """解除は「再び監視を有効にする」であって「ずれを無かったことにする」ではない。

        解除後もずれが残っていれば、監視は同じ軸で再び発報して緊急停止へ戻す。
        ここが効かないと、操縦者は復帰したつもりで無監視の機体を動かすことになる。
        """
        fx = _SyncFixture()
        fx.deviate()
        fx.monitor.step()
        await fx.settle()
        assert len(fx.violations) == 1

        await fx.server._handle_command({"type": "e_stop_release"})
        assert fx.server.e_stop_active is False

        # 機構は直っていない (ずれたまま)
        fx.monitor.step()
        await fx.settle()

        assert len(fx.violations) == 2
        assert fx.server.e_stop_active is True

    async def test_release_does_not_disable_position_loop_detection(self) -> None:
        """位置制御ループ側も同じ。解除後にずれが残っていれば再びラッチする。"""
        fx = _SyncFixture()
        fx.deviate()
        await fx.loop.step()
        await fx.server._handle_command({"type": "e_stop"})
        await fx.server._handle_command({"type": "e_stop_release"})
        assert fx.loop.sync_violations == frozenset()

        await fx.loop.step()

        assert fx.loop.sync_violations == frozenset({"y_axis"})

    async def test_release_after_repair_keeps_axis_available(self) -> None:
        """人間がずれを直してから解除すれば、再ラッチせずに軸が使える。"""
        fx = _SyncFixture()
        fx.deviate()
        await fx.loop.step()
        await fx.server._handle_command({"type": "e_stop"})

        fx.aligned()
        await fx.server._handle_command({"type": "e_stop_release"})
        await fx.loop.step()

        assert fx.loop.sync_violations == frozenset()
        assert fx.monitor.violated == frozenset()


def _frames_to(mgr: CANManager, bus_name: str) -> list[can.Message]:
    """指定バスへ送信されたフレームを送信順に取り出す。"""
    return [call.args[1] for call in mgr.send_to_bus.await_args_list if call.args[0] == bus_name]


class TestEStopStopsM3508:
    """左右直結で最も危険な Y 軸 (M3508) へ、緊急停止で能動的に停止指令を出すこと。

    M3508 は ``emergency_stop_message()`` を持たず、自作モタドラ向けの 0x7FF も
    解釈しない。位置制御ループが電流 0 を送り続けることに頼ると、そのタスクが
    死んだ瞬間に「止める手段が 1 つも無い」状態になる。
    """

    async def test_zero_current_frame_is_sent(self) -> None:
        fx = _SyncFixture()
        await fx.loop.set_target("y_r", ControlMode.CURRENT, 3000.0)

        await fx.server.activate_e_stop()

        frames = _frames_to(fx.mgr, "can_m3508")
        assert frames, "M3508 のバスへ 1 通も送られていない"
        assert frames[-1].arbitration_id == 0x200
        assert struct.unpack(">hhhh", frames[-1].data) == (0, 0, 0, 0)

    async def test_sent_even_when_loop_is_not_running(self) -> None:
        """停止がループの生存に依存してはならない。"""
        fx = _SyncFixture()
        assert fx.loop.is_running is False

        await fx.server.activate_e_stop()

        assert _frames_to(fx.mgr, "can_m3508")

    async def test_targets_are_cleared(self) -> None:
        """目標が残っていると、ループが動き出した瞬間に再び電流が出る。"""
        fx = _SyncFixture()
        await fx.loop.set_target("y_r", ControlMode.POSITION, 30.0)

        await fx.server.activate_e_stop()

        assert fx.loop.target("y_r") is None

    async def test_bus_failure_does_not_block_other_frames(self) -> None:
        """1 バスの送信失敗で他への停止指令を諦めない (既存方針の維持)。"""
        fx = _SyncFixture()

        async def _fail_m3508(bus_name: str, msg: can.Message) -> None:
            if bus_name == "can_m3508":
                raise can.CanError("送信失敗 (テスト)")

        fx.mgr.send_to_bus = AsyncMock(side_effect=_fail_m3508)

        await fx.server.activate_e_stop()

        # 自作モタドラ向けの 0x7FF ブロードキャストは届いている
        assert [call.args[0] for call in fx.mgr.send_to_bus.await_args_list].count("bus0") == 1
        assert fx.server.e_stop_active is True


class TestEStopDropsRefreshTargets:
    """緊急停止の解除だけでコンベアが回り出さないこと。

    自作モタドラの目標値は 20Hz で再送し続けているため、停止時に目標を残すと
    解除した瞬間に再送が走り、操縦者が何も操作していないのに機体が動き出す。
    """

    async def test_targets_are_dropped_on_e_stop(self) -> None:
        server = _build_server()
        mgr = server._robots["main_hand"].can_manager
        driver = GenericDriver("conveyor", can_id=9, control_type=ControlMode.DUTY)
        handle = MotorHandle("conveyor", driver, mgr)
        refresher = GenericTargetRefresher([handle])
        server._robots["main_hand"].target_refreshers = [refresher]
        await handle.set_target(ControlMode.DUTY, 0.3)

        await server.activate_e_stop()

        assert handle.has_target is False


class TestSafetyStateBroadcast:
    async def test_latched_axes_are_broadcast(self) -> None:
        """どの軸がラッチされているかを UI が知れないと復旧操作を選べない。"""
        fx = _SyncFixture()
        fx.deviate()
        await fx.loop.step()
        fx.monitor.step()
        await fx.settle()

        state = fx.server._build_state_message("main_hand")

        assert state["safety"]["sync_violations"] == ["y_axis"]

    async def test_dead_safety_loops_are_visible(self) -> None:
        """200Hz の位置制御と 50Hz の監視が死んでも、現在は誰も気付けない。"""
        fx = _SyncFixture()

        state = fx.server._build_state_message("main_hand")
        assert state["safety"]["loops_running"] is False
        assert state["safety"]["monitors_running"] is False

        fx.loop.start()
        fx.monitor.start()
        await fx.settle()
        try:
            state = fx.server._build_state_message("main_hand")
            assert state["safety"]["loops_running"] is True
            assert state["safety"]["monitors_running"] is True
        finally:
            await fx.loop.stop()
            await fx.monitor.stop()
