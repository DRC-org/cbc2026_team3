from __future__ import annotations

import asyncio
import contextlib
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
from lib.drivers.base import ControlMode
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import M3508Driver
from lib.health import BusHealth, BusHealthInfo, HealthSnapshot, MotorHealth, MotorHealthInfo
from lib.match_state import (
    ROLE_PRE_MATCH,
    ChecklistItem,
    Phase,
)
from lib.sequence.engine import Sequence, step
from lib.sequence.motors import MotorHandle
from tests.fake_can import mock_can_manager, set_motors
from tests.feedback_frames import feed_generic
from tests.server_fixtures import ServerFixture, drain, recv_type, wait_until

_ROBOT_NAMES = ("main_hand", "sub_hand")

_DEFS = {
    ROLE_PRE_MATCH: [ChecklistItem(id="home", label="初期位置確認")],
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


def _build_fixture() -> ServerFixture:
    fx = ServerFixture.build(checklist_definitions=_DEFS)
    for name in _ROBOT_NAMES:
        fx.add_robot(name, GatedSequence(name))
    return fx


async def _start_both_sequences(fx: ServerFixture, ws) -> list[GatedSequence]:
    """試合を開始し、両ロボットのシーケンスをゲートステップまで進める。"""
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
    """シーケンスの常駐ループを短時間だけ回して止める。

    「未処理の開始要求が破棄されたか」は内部フラグを覗いても分かるが、それでは
    *フラグの名前* を固定するだけで、守りたい事実 —— 誰も押していないのに機体が
    動き出さないこと —— を確かめられない。常駐ループを実際に回し、要求が
    残っていれば必ず現れる「最初のステップの実行」を観測する。
    """
    task = asyncio.create_task(seq.run_forever())
    await asyncio.sleep(settle_s)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _expect_no_rejection(ws, command: str, *, tries: int = 40) -> None:
    """一定時間 command_rejected が流れてこないことを確認する。"""
    for _ in range(tries):
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=0.2)
        except (TimeoutError, TypeError):
            return
        if msg.get("type") == "command_rejected" and msg.get("command") == command:
            raise AssertionError(f"{command} が拒否された: {msg.get('reason')}")


async def _enter_e_stop(fx: ServerFixture, ws) -> list[GatedSequence]:
    """試合中にシーケンスを走らせたうえで緊急停止状態まで持っていく。"""
    seqs = await _start_both_sequences(fx, ws)
    await ws.send_json({"type": "e_stop"})
    await wait_until(lambda: fx.e_stop_active)
    await _release_gates_and_settle(seqs)
    return seqs


class TestEStopStopsSequences:
    async def test_e_stop_stops_all_running_sequences(self) -> None:
        """緊急停止後に次ステップが走ると、新しいモータ目標値が停止指令を上書きする。"""
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
        """CAN 送信が失敗しても停止は成立させる (送信不能な時ほど停止が要る)。"""
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
        """停止フレーム生成そのものが失敗しても、シーケンス停止まで到達すること。"""
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
        """開始要求が処理される前に緊急停止が入っても、その要求で走り出さないこと。"""
        fx = _build_fixture()
        seq = fx.sequence(_ROBOT_NAMES[0])
        seq.request_start()

        await fx.command({"type": "e_stop"})

        await _spin_resident_loop(seq)
        assert seq.executed == [], "破棄されたはずの開始要求でシーケンスが走り出した"


class TestEStopReleaseKeepsSequencesStopped:
    async def test_release_does_not_restart_sequence(self) -> None:
        """解除は再開合図ではない。操縦者の sequence_start を待つ設計を守る。"""
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
        """緊急停止中に START でロボットが動き出すと、操縦者が止める手段を失う。"""
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
            # 常駐ループは走ったままなので、要求が残っていれば先頭から走り直す
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
            # ジャンプが通っていれば after_step が走って executed に現れる
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
        """止める方向の操作は緊急停止中こそ通す必要がある。"""
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _enter_e_stop(fx, ws)

            stop_spy = MagicMock()
            seqs[0].request_stop = stop_spy  # type: ignore[method-assign]

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
        """解除後は従来どおり操縦者の START で再開できること。"""
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
    """match_start が通ると操縦者の sequence_start が解禁されるため、停止中は塞ぐ。"""

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
        """解除後は試合開始が通り、操縦者の sequence_start が受理されること。"""
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

            # 試合開始そのものは機体を動かさない
            seqs = fx.sequences()
            await asyncio.sleep(0.1)
            assert all(s.executed == [] for s in seqs)

            await ws.send_json({"type": "sequence_start", "robot": "main_hand"})
            started = await wait_until(lambda: seqs[0].executed == ["gate"])
            assert started, "解除後に sequence_start が通っていない"

            await _release_gates_and_settle(seqs)
            await ws.close()


class TestEStopBlocksSetParam:
    async def test_set_param_rejected_while_e_stop_active(self) -> None:
        """緊急停止中のパラメータ書き換えは停止状態を崩しうるため拒否する。"""
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _enter_e_stop(fx, ws)

            await ws.send_json({"type": "set_param", "motor": "m1", "key": "kp", "value": 1.0})

            msg = await recv_type(ws, "command_rejected")
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
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "set_param", "motor": "m1", "key": "kp", "value": 1.0})
            msg = await recv_type(ws, "command_rejected", tries=5)
            assert msg is None or "緊急停止" not in msg["reason"]
            await ws.close()


class TestEStopKeepsRecoveryCommands:
    async def test_match_finish_and_release_pass_during_e_stop(self) -> None:
        """試合終了・緊急停止解除は復帰経路なので緊急停止中も通す。"""
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _enter_e_stop(fx, ws)

            await ws.send_json({"type": "match_finish"})
            await _expect_no_rejection(ws, "match_finish", tries=5)
            assert fx.match.phase is Phase.FINISHED

            await ws.send_json({"type": "e_stop_release"})
            await _expect_no_rejection(ws, "e_stop_release", tries=5)
            assert fx.e_stop_active is False

            await ws.close()


class TestEStopReleaseRequiresActiveEStop:
    """「解除」は解除すべき状態があるときだけ通す。

    停止していない試合中に 1 通届くだけで同期ずれラッチが全解除され、全モータへ
    再励磁が飛ぶ。ずれが残っていれば再ラッチされるとはいえ、監視を無効化する
    操作が誰の意図でもなく走る経路を残す理由は無い (リロード直後の UI や
    リトライで実際に届きうる)。
    """

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
    """EDULITE 05 は緊急停止で無励磁になるため、解除で再励磁しないと以後動かない。"""

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
        """再有効化中にもう一度緊急停止が入ったら enable を送ってはならない。"""
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

            # 再有効化の最中にもう一度緊急停止が入った状況を、操縦者の e_stop と
            # 同じ経路で作る (フラグを直接立てると本番に無い状態を作りかねない)
            await fx.activate_e_stop()
            assert should_abort() is True

            await ws.close()


class TestActivateEStopFromInside:
    """同期監視など内部の異常検知から、操縦者の e_stop と同じ経路で止められること。"""

    async def test_same_side_effects_as_e_stop_command(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            seqs = await _start_both_sequences(fx, ws)

            await fx.activate_e_stop(reason="y_axis の左右ずれ")

            assert fx.e_stop_active is True
            for mgr in fx.can_managers():
                # 停止フレームはモータ個別・バス全体の両方へ出す
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
        """試合中に「なぜ止まったか」が操縦者に届かないと復旧できない。"""
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
        """操縦者操作による緊急停止の配信内容は従来どおり (理由なしでも壊れない)。"""
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
        """同期監視は軸ごとに発報しうる。多重発報で状態が壊れないこと。"""
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

        self._server_fx = ServerFixture.build(checklist_definitions=_DEFS)
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
        # 累積角の原点は初回フィードバックで確定する。先に 0deg を流しておかないと
        # 「ずれた姿勢」がそのまま原点になり、偏差 0 と判定されてしまう
        _feed(self.right, 0.0)
        _feed(self.left, 0.0)

    def _on_violation(self, axis: str, deviation: float) -> None:
        self.violations.append((axis, deviation))
        task = asyncio.create_task(self._server_fx.activate_e_stop(reason=f"{axis} の左右ずれ"))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # 「サーバーへの操作」はこのクラス自身の顔として出す。テスト側が
    # 内側のフィクスチャを辿ると、配線の持ち方を変えるたびに全テストが壊れる
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

        await fx.command({"type": "e_stop"})
        await fx.command({"type": "e_stop_release"})

        assert fx.loop.sync_violations == frozenset()

    async def test_release_clears_sync_monitor_latch(self) -> None:
        """SyncMonitor がラッチしたままだと、以後どれだけずれても二度と発報しない。"""
        fx = _SyncFixture()
        fx.deviate()
        fx.monitor.step()
        await fx.settle()
        assert fx.monitor.violated == frozenset({"y_axis"})
        assert fx.e_stop_active is True

        await fx.command({"type": "e_stop_release"})

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

        await fx.command({"type": "e_stop_release"})
        assert fx.e_stop_active is False

        # 機構は直っていない (ずれたまま)
        fx.monitor.step()
        await fx.settle()

        assert len(fx.violations) == 2
        assert fx.e_stop_active is True

    async def test_release_does_not_disable_position_loop_detection(self) -> None:
        """位置制御ループ側も同じ。解除後にずれが残っていれば再びラッチする。"""
        fx = _SyncFixture()
        fx.deviate()
        await fx.loop.step()
        await fx.command({"type": "e_stop"})
        await fx.command({"type": "e_stop_release"})
        assert fx.loop.sync_violations == frozenset()

        await fx.loop.step()

        assert fx.loop.sync_violations == frozenset({"y_axis"})

    async def test_release_after_repair_keeps_axis_available(self) -> None:
        """人間がずれを直してから解除すれば、再ラッチせずに軸が使える。"""
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

        await fx.activate_e_stop()

        frames = _frames_to(fx.mgr, "can_m3508")
        assert frames, "M3508 のバスへ 1 通も送られていない"
        assert frames[-1].arbitration_id == 0x200
        assert struct.unpack(">hhhh", frames[-1].data) == (0, 0, 0, 0)

    async def test_sent_even_when_loop_is_not_running(self) -> None:
        """停止がループの生存に依存してはならない。"""
        fx = _SyncFixture()
        assert fx.loop.is_running is False

        await fx.activate_e_stop()

        assert _frames_to(fx.mgr, "can_m3508")

    async def test_targets_are_cleared(self) -> None:
        """目標が残っていると、ループが動き出した瞬間に再び電流が出る。"""
        fx = _SyncFixture()
        await fx.loop.set_target("y_r", ControlMode.POSITION, 30.0)

        await fx.activate_e_stop()

        assert fx.loop.target("y_r") is None

    async def test_bus_failure_does_not_block_other_frames(self) -> None:
        """1 バスの送信失敗で他への停止指令を諦めない (既存方針の維持)。"""
        fx = _SyncFixture()

        async def _fail_m3508(bus_name: str, msg: can.Message) -> None:
            if bus_name == "can_m3508":
                raise can.CanError("送信失敗 (テスト)")

        fx.mgr.send_to_bus = AsyncMock(side_effect=_fail_m3508)

        await fx.activate_e_stop()

        # 自作モタドラ向けの 0x7FF ブロードキャストは届いている
        assert [call.args[0] for call in fx.mgr.send_to_bus.await_args_list].count("bus0") == 1
        assert fx.e_stop_active is True


class TestEStopDropsRefreshTargets:
    """緊急停止の解除だけでコンベアが回り出さないこと。

    自作モタドラの目標値は 20Hz で再送し続けているため、停止時に目標を残すと
    解除した瞬間に再送が走り、操縦者が何も操作していないのに機体が動き出す。
    """

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


class TestSafetyStateBroadcast:
    async def test_latched_axes_are_broadcast(self) -> None:
        """どの軸がラッチされているかを UI が知れないと復旧操作を選べない。"""
        fx = _SyncFixture()
        fx.deviate()
        await fx.loop.step()
        fx.monitor.step()
        await fx.settle()

        state = fx.state_message()

        assert state["safety"]["sync_violations"] == ["y_axis"]

    async def test_dead_safety_loops_are_visible(self) -> None:
        """200Hz の位置制御と 50Hz の監視が死んでも、現在は誰も気付けない。"""
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


class TestTargetRefresherLivenessBroadcast:
    """20Hz の目標値再送が死んだことも配信しないと誰にも気付けない。

    再送が止まるとファームのウォッチドッグが 500ms で全 generic アクチュエータの
    出力を落とす (試合中にコンベアとグリッパが無反応になる)。WS は繋がったままで
    モータ状態も届き続けるため、画面は正常に見えたままになる —— 位置制御ループと
    同期監視について `_safety_state` の docstring が言っているのと同じ状況。
    """

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


# ---------------------------------------------------------------------- #
#  停止理由の保持
# ---------------------------------------------------------------------- #


async def _latest_e_stop_state(ws) -> dict:
    """流れてきた e_stop_state のうち最後の 1 通を返す。

    再配信のたびに理由が載り直しているかは「最初の 1 通」では見えない。
    停止中に UI が見続けるのは最後に届いた 1 通なので、そこを見る。
    """
    latest: dict | None = None
    for msg in await drain(ws, timeout=0.1, limit=80):
        if msg.get("type") == "e_stop_state":
            latest = msg
    assert latest is not None, "e_stop_state が配信されなかった"
    return latest


class TestEStopReasonIsRetained:
    """停止理由はサーバーが保持し、再配信のたびに載せ直す。

    `_broadcast_state` は停止中に毎ティック e_stop_state を送り直す。理由を
    サーバー側が持っていないと、自動検知で止まった直後の 1 通だけが本当の原因を
    載せ、以降の再配信が UI の表示を「操縦者の停止操作」へ塗り替えてしまう。
    原因を説明できる唯一の情報が、それ自身の正反対で上書きされる形になる。
    """

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
        # 機体側の自動検知で止まった後に操縦者が E-STOP を押すのは普通の流れ。
        # そこで理由が消えると、画面は「操縦者が押した」という正反対の説明に変わる
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
    """フィードバック受信時刻だけを指定した OK スナップショット。

    「解除フレームより前に届いたフィードバック」を作るには時刻そのものを
    置く必要があり、実フレームを流しても時計を狙った位置には置けない。
    """
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
    """基板が FEEDBACK の緊急停止ビットで報告した停止を、サーバー全体へ伝播すること。

    自作 DC モタドラは物理停止スイッチの押下と CAN 初期化失敗をラッチへ落とす。
    サーバーが拾わないと **機体は止まっているのに UI は平常のまま** になり、
    操縦者はシーケンスが進まない理由を画面から知る手段が無い。
    """

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
        """止まった理由が「どのロボットのどのモータか」まで分かること。"""
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
        """自作モタドラ以外 (M3508 / EDULITE) を巻き込まないこと。"""
        fx = _build_fixture()

        await fx.publish_state()

        assert fx.e_stop_active is False

    async def test_release_is_not_undone_by_feedback_from_before_the_clear(self) -> None:
        """解除フレーム送信より前に届いたフィードバックで停止をかけ直さないこと。

        解除は「解除フレーム送信 → 基板がラッチを外す → 次の FEEDBACK」の順に
        伝わる。送信前のフィードバックに残った緊急停止ビットを信じると、解除した瞬間に
        サーバーが自分で止め直し、**二度と解除できない機体** になる。
        """
        fx, _ = self._fixture_with_generic(e_stop=True)
        await fx.publish_state()
        assert fx.e_stop_active is True

        mgr = fx.can_manager("main_hand")
        stale_at = time.time()
        await fx.command({"type": "e_stop_release"})
        assert fx.e_stop_active is False

        # 解除フレームより前に届いていたフィードバック (緊急停止ビットは立ったまま)
        mgr.health.side_effect = lambda **_kwargs: _health_with_feedback_at(mgr, stale_at)
        await fx.publish_state()

        assert fx.e_stop_active is False

    async def test_still_pressed_after_release_stops_again(self) -> None:
        """解除しても基板がまだ止まっているなら、改めて停止させること。

        物理スイッチが押されたままなら、ファームは解除フレームを受けても
        次のループで再ラッチする。そこで動けるようにしてしまうと、
        「押しているのに機体が動く」状態を UI が作り出すことになる。
        """
        fx, _ = self._fixture_with_generic(e_stop=True)
        await fx.publish_state()

        await fx.command({"type": "e_stop_release"})
        assert fx.e_stop_active is False

        # 解除後に届いたフィードバックでも緊急停止ビットが立っている
        await fx.publish_state()

        assert fx.e_stop_active is True
