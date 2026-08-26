from __future__ import annotations

import asyncio
import logging
import struct
import time
from unittest.mock import AsyncMock, MagicMock, patch

import can
from aiohttp.test_utils import TestClient, TestServer

from lib.can_manager import CANManager
from lib.control.position_loop import M3508PositionLoop, make_position_pid
from lib.control.target_refresh import GenericTargetRefresher
from lib.drivers.base import ControlMode, MotorDriver, MotorState
from lib.drivers.edulite05 import Edulite05Driver
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import M3508Driver
from lib.health import MotorCheckResult
from lib.motor_check import MotorCheckRunner
from lib.sequence.engine import Sequence, step
from lib.sequence.motors import MotorHandle
from lib.server import RobotServer

# ---------------------------------------------------------------------- #
#  テスト用ダミー実装
# ---------------------------------------------------------------------- #


class _MockMotor(MotorDriver):
    """サーバ動作確認統合テスト用ドライバ。

    check_command / evaluate_check_result / reset_after_check は MotorCheckRunner
    の経路だけを通すための最小実装。判定結果は属性で差し替えられる。
    """

    def __init__(
        self,
        name: str,
        can_id: int = 1,
        *,
        evaluate_passed: bool = True,
        target_value: float = 1.0,
    ) -> None:
        super().__init__(name, can_id)
        self.evaluate_passed = evaluate_passed
        self.target_value = target_value
        self.last_magnitude: float | None = None
        self.reset_calls = 0

    def encode_target(self, mode, value):  # pragma: no cover - 本テストでは未使用
        return can.Message(arbitration_id=0x100 + self.can_id, data=bytes(8))

    def decode_feedback(self, msg: can.Message) -> MotorState:  # pragma: no cover
        return self._state

    def matches_feedback(self, msg: can.Message) -> bool:
        # check 用送信メッセージは self が送信した直後に echo されないので、
        # フィードバック受信ループで一致しないことを保証 (テスト分離のため)
        return False

    def check_command(self, *, magnitude: float) -> tuple[can.Message, dict]:
        self.last_magnitude = magnitude
        msg = can.Message(arbitration_id=0x100 + self.can_id, data=bytes(8))
        return msg, {"target": self.target_value, "mode": "current"}

    def evaluate_check_result(
        self,
        state: MotorState,
        context: dict,
        *,
        tolerance: float | None = None,
    ) -> tuple[bool, str | None]:
        return self.evaluate_passed, None if self.evaluate_passed else "差分過大"

    def reset_after_check(self) -> can.Message:
        self.reset_calls += 1
        return can.Message(arbitration_id=0x100 + self.can_id, data=bytes(8))


class _DummySequence(Sequence):
    """trigger 待ちなしの最小シーケンス。run() 完了後 _running は False に戻る。"""

    def __init__(self) -> None:
        super().__init__("test_seq")

    @step("ノーオペ")
    async def noop(self) -> None:
        return None


class _LongRunningSequence(Sequence):
    """run() を呼んだ後 _running=True のまま停止し続けるシーケンス。

    通常シーケンス実行中の拒否 (server._start_motor_check) を検証する目的で、
    実際にステップ内で長時間 await する代わりに run() 開始後に _running=True
    を直接観測できるよう trigger 待ちで止める。
    """

    def __init__(self) -> None:
        super().__init__("long_seq")

    @step("無限待機", require_trigger=True)
    async def wait_forever(self) -> None:
        return None


def _build_server_with_motors(
    *,
    bus_channel: str,
    motors: dict[str, _MockMotor] | None = None,
    sequence: Sequence | None = None,
    feed_immediately: bool = True,
    motor_check_per_motor_timeout_ms: float = 200.0,
    motor_check_default_magnitude: dict[str, float] | None = None,
    motor_check_per_motor_overrides: dict[str, dict[str, float]] | None = None,
    position_loops: list[M3508PositionLoop] | None = None,
) -> tuple[RobotServer, CANManager, dict[str, _MockMotor], can.Bus]:
    """RobotServer + 実 CANManager + virtual バス + MockMotor の構成を組む。

    feed_immediately=True で send 直後に _last_rx_at を進めるパッチを当てる
    (フィードバック受信を即時模擬)。
    """
    if motors is None:
        motors = {"m1": _MockMotor("m1", 1)}
    if motor_check_default_magnitude is None:
        motor_check_default_magnitude = {"_MockMotor": 1.0}

    server = RobotServer(
        motor_check_per_motor_timeout_ms=motor_check_per_motor_timeout_ms,
        motor_check_default_magnitude=motor_check_default_magnitude,
        motor_check_per_motor_overrides=motor_check_per_motor_overrides,
    )
    mgr = CANManager()
    bus = can.Bus(interface="virtual", channel=bus_channel, receive_own_messages=False)
    mgr.add_bus("bus0", bus, channel=bus_channel)
    for motor in motors.values():
        mgr.add_motor("bus0", motor)
        # 全モータについて初期受信時刻を記録 (health の STALE 判定回避)
        mgr._last_rx_at[motor.name] = time.time()

    if feed_immediately:
        # CANManager.send を差し替え、送信直後に rx タイムスタンプを進める。
        # MotorCheckRunner._wait_for_feedback はこのタイムスタンプ更新で受信判定する。
        original_send = mgr.send

        async def _patched_send(motor_name: str, msg: can.Message) -> None:
            await original_send(motor_name, msg)
            mgr._last_rx_at[motor_name] = time.time() + 0.001

        mgr.send = _patched_send  # type: ignore[method-assign]

    seq = sequence if sequence is not None else _DummySequence()
    server.add_robot("main_hand", seq, mgr, position_loops=position_loops)
    return server, mgr, motors, bus


class _AutoClock:
    """呼ばれるたびに一定量進む単調クロック。実時間 sleep なしで dt を固定する。"""

    def __init__(self, step_s: float = 0.005) -> None:
        self.now = 1000.0
        self._step = step_s

    def __call__(self) -> float:
        self.now += self._step
        return self.now


class _LoopProbe:
    """位置制御ループ + そのバスへの送信フレーム記録。"""

    def __init__(
        self,
        mgr: CANManager,
        *,
        bus: str = "bus0",
        motor_name: str = "lift_m3508",
        can_id: int = 4,
        ki: float = 0.0,
    ) -> None:
        self.frames: list[can.Message] = []
        original_send_to_bus = mgr.send_to_bus

        async def _counting(bus_name: str, msg: can.Message) -> None:
            self.frames.append(msg)
            await original_send_to_bus(bus_name, msg)

        mgr.send_to_bus = _counting  # type: ignore[method-assign]

        self.mgr = mgr
        self.motor_name = motor_name
        self.driver = M3508Driver(motor_name, can_id=can_id)
        self.loop = M3508PositionLoop(
            mgr,
            bus,
            is_estop_active=lambda: False,
            time_source=_AutoClock(),
        )
        self.loop.add_motor(motor_name, self.driver, make_position_pid(kp=1.0, ki=ki))

    def feed(self, deg: float = 0.0) -> None:
        angle_raw = round(deg / 360.0 * 8192) % 8192
        data = struct.pack(">HhhBB", angle_raw, 0, 0, 25, 0)
        self.driver.update_state(can.Message(arbitration_id=0x200 + self.driver.can_id, data=data))
        self.mgr._last_rx_at[self.motor_name] = time.time()


async def _wait_for_running_runner(
    server: RobotServer, robot: str, *, timeout: float = 2.0
) -> MotorCheckRunner:
    deadline = time.time() + timeout
    while time.time() < deadline:
        runner = server._motor_check_runners.get(robot)
        if runner is not None and runner.is_running:
            return runner
        await asyncio.sleep(0.01)
    raise AssertionError("動作確認 runner が起動しなかった")


async def _drain(ws, *, timeout: float = 0.05, limit: int = 50) -> list[dict]:
    """WS から残メッセージを排出する。タイムアウトしたら戻る。"""
    drained: list[dict] = []
    for _ in range(limit):
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=timeout)
        except TimeoutError:
            break
        drained.append(msg)
    return drained


async def _wait_until_idle(server: RobotServer, robot: str, *, timeout: float = 2.0) -> None:
    """指定ロボットの動作確認タスクが完了するまで待つ。"""
    task = server._motor_check_tasks.get(robot)
    if task is None:
        return
    await asyncio.wait_for(task, timeout=timeout)


# ---------------------------------------------------------------------- #
#  テストケース
# ---------------------------------------------------------------------- #


class TestMotorCheckStartKicksOffRunner:
    async def test_motor_check_start_kicks_off_runner(self) -> None:
        # WS で motor_check_start を送ると runner が起動し、progress イベントが配信される
        server, _, _, bus = _build_server_with_motors(bus_channel="vsrvchk_start")
        try:
            app = server.create_app()
            async with TestClient(TestServer(app)) as client:
                ws = await client.ws_connect("/ws")
                await _drain(ws)

                await ws.send_json({"type": "motor_check_start", "robot": "main_hand"})

                got_progress = False
                for _ in range(30):
                    try:
                        msg = await asyncio.wait_for(ws.receive_json(), timeout=0.1)
                    except TimeoutError:
                        break
                    if msg.get("type") == "motor_check_progress":
                        assert msg["robot"] == "main_hand"
                        assert msg["current"] == "m1"
                        assert msg["index"] == 0
                        assert msg["total"] == 1
                        got_progress = True
                        break

                assert got_progress, "motor_check_progress が配信されなかった"
                await _wait_until_idle(server, "main_hand")
                await ws.close()
        finally:
            bus.shutdown()


class TestMotorCheckDoneIncludesSnapshot:
    async def test_motor_check_done_includes_snapshot(self) -> None:
        # 全モータ完了後 motor_check_done が配信され overall=ok
        motors = {
            "m1": _MockMotor("m1", 1, evaluate_passed=True),
            "m2": _MockMotor("m2", 2, evaluate_passed=True),
        }
        server, _, _, bus = _build_server_with_motors(bus_channel="vsrvchk_done", motors=motors)
        try:
            app = server.create_app()
            async with TestClient(TestServer(app)) as client:
                ws = await client.ws_connect("/ws")
                await _drain(ws)

                await ws.send_json({"type": "motor_check_start", "robot": "main_hand"})

                got_done = False
                snapshot = None
                for _ in range(50):
                    try:
                        msg = await asyncio.wait_for(ws.receive_json(), timeout=0.2)
                    except TimeoutError:
                        break
                    if msg.get("type") == "motor_check_done":
                        got_done = True
                        snapshot = msg["snapshot"]
                        break

                assert got_done, "motor_check_done が配信されなかった"
                assert snapshot is not None
                assert snapshot["overall"] == "ok"
                assert snapshot["robot"] == "main_hand"
                assert len(snapshot["records"]) == 2
                await ws.close()
        finally:
            bus.shutdown()


class TestMotorCheckRecordPerMotor:
    async def test_motor_check_record_per_motor(self) -> None:
        # モータごとに record メッセージが 1 つずつ配信される
        motors = {
            "m1": _MockMotor("m1", 1, evaluate_passed=True),
            "m2": _MockMotor("m2", 2, evaluate_passed=False),
        }
        server, _, _, bus = _build_server_with_motors(bus_channel="vsrvchk_record", motors=motors)
        try:
            app = server.create_app()
            async with TestClient(TestServer(app)) as client:
                ws = await client.ws_connect("/ws")
                await _drain(ws)

                await ws.send_json({"type": "motor_check_start", "robot": "main_hand"})

                # 動作確認タスクの完了を待ってから WS バッファを順に読み出すことで、
                # done と record 配信の到着順レースを排除する。
                await _wait_until_idle(server, "main_hand", timeout=3.0)

                records_seen: list[dict] = []
                got_done = False
                for _ in range(60):
                    try:
                        msg = await asyncio.wait_for(ws.receive_json(), timeout=0.2)
                    except TimeoutError:
                        break
                    if msg.get("type") == "motor_check_record":
                        records_seen.append(msg["record"])
                    if msg.get("type") == "motor_check_done":
                        got_done = True

                assert got_done, "motor_check_done が配信されなかった"
                # 各モータで 1 つずつ record が配信される (順序は到着順)
                motor_names = sorted(r["motor"] for r in records_seen)
                assert motor_names == ["m1", "m2"]
                results_by_motor = {r["motor"]: r["result"] for r in records_seen}
                assert results_by_motor["m1"] == MotorCheckResult.PASSED.value
                assert results_by_motor["m2"] == MotorCheckResult.FAILED.value
                await ws.close()
        finally:
            bus.shutdown()


class TestMotorCheckRejectedDuringEStop:
    async def test_e_stop_continues_after_individual_send_failures_and_broadcasts(
        self, caplog
    ) -> None:
        server = RobotServer()
        manager = CANManager()
        bus0 = can.Bus(interface="virtual", channel="vsrvchk_estop_fail0")
        bus1 = can.Bus(interface="virtual", channel="vsrvchk_estop_fail1")
        manager.add_bus("bus0", bus0)
        manager.add_bus("bus1", bus1)
        manager.add_motor("bus0", Edulite05Driver("arm0", can_id=5))
        manager.add_motor("bus1", Edulite05Driver("arm1", can_id=6))
        server.add_robot("main_hand", _DummySequence(), manager)

        try:
            with (
                patch.object(
                    manager,
                    "send",
                    new_callable=AsyncMock,
                    side_effect=[can.CanError("driver failure"), None],
                ) as send,
                patch.object(
                    manager,
                    "send_to_bus",
                    new_callable=AsyncMock,
                    side_effect=[can.CanError("bus failure"), None],
                ) as send_to_bus,
                patch.object(
                    server, "_broadcast_e_stop_state", new_callable=AsyncMock
                ) as broadcast,
                caplog.at_level(logging.ERROR),
            ):
                await server._handle_command({"type": "e_stop"})

            assert send.await_count == 2
            assert send_to_bus.await_count == 2
            # 操縦者操作による緊急停止には停止理由が無い (内部検知のときだけ載る)
            broadcast.assert_awaited_once_with(reason=None)
            assert "driver固有送信失敗" in caplog.text
            assert "bus送信失敗" in caplog.text
        finally:
            bus0.shutdown()
            bus1.shutdown()

    async def test_e_stop_aborts_runner_and_sends_edulite_extended_disable(self) -> None:
        server = RobotServer()
        manager = CANManager()
        bus = can.Bus(interface="virtual", channel="vsrvchk_edulite_estop")
        manager.add_bus("bus0", bus)
        motor = Edulite05Driver("arm", can_id=5)
        manager.add_motor("bus0", motor)
        server.add_robot("main_hand", _DummySequence(), manager)
        runner = MagicMock(is_running=True)
        server._motor_check_runners["main_hand"] = runner

        try:
            with (
                patch.object(manager, "send", new_callable=AsyncMock) as send,
                patch.object(manager, "send_to_bus", new_callable=AsyncMock),
            ):
                await server._handle_command({"type": "e_stop"})

            runner.abort.assert_called_once_with()
            send.assert_awaited_once()
            motor_name, message = send.await_args.args
            assert motor_name == "arm"
            assert message.is_extended_id is True
            assert motor.parse_can_id(message.arbitration_id)[0] == motor.COMM_TYPE_DISABLE
            assert message.data == bytes(8)
        finally:
            bus.shutdown()

    async def test_motor_check_rejected_during_e_stop(self) -> None:
        # e_stop 状態で motor_check_start → motor_check_error が配信され runner 起動なし
        server, _, _, bus = _build_server_with_motors(bus_channel="vsrvchk_estop")
        try:
            server._e_stop_active = True

            app = server.create_app()
            async with TestClient(TestServer(app)) as client:
                ws = await client.ws_connect("/ws")
                await _drain(ws)

                await ws.send_json({"type": "motor_check_start", "robot": "main_hand"})

                got_error = False
                for _ in range(30):
                    try:
                        msg = await asyncio.wait_for(ws.receive_json(), timeout=0.1)
                    except TimeoutError:
                        break
                    if msg.get("type") == "motor_check_error":
                        assert msg["robot"] == "main_hand"
                        assert "緊急停止" in msg["message"]
                        got_error = True
                        break

                assert got_error, "motor_check_error が配信されなかった"
                # runner は起動していない
                assert "main_hand" not in server._motor_check_runners
                await ws.close()
        finally:
            bus.shutdown()


class TestMotorCheckRejectedDuringSequence:
    async def test_motor_check_rejected_during_sequence(self) -> None:
        # 通常シーケンス _running=True 中の start は拒否される
        seq = _LongRunningSequence()
        server, _, _, bus = _build_server_with_motors(bus_channel="vsrvchk_seq", sequence=seq)
        try:
            # シーケンスを起動して trigger 待ち (_running=True, waiting_trigger=True)
            seq_task = asyncio.create_task(seq.run())
            await asyncio.sleep(0.05)
            assert seq._running is True

            app = server.create_app()
            async with TestClient(TestServer(app)) as client:
                ws = await client.ws_connect("/ws")
                await _drain(ws)

                await ws.send_json({"type": "motor_check_start", "robot": "main_hand"})

                got_error = False
                for _ in range(30):
                    try:
                        msg = await asyncio.wait_for(ws.receive_json(), timeout=0.1)
                    except TimeoutError:
                        break
                    if msg.get("type") == "motor_check_error":
                        assert "シーケンス" in msg["message"]
                        got_error = True
                        break

                assert got_error
                assert "main_hand" not in server._motor_check_runners
                await ws.close()
        finally:
            seq_task.cancel()
            with __import__("contextlib").suppress(asyncio.CancelledError):
                await seq_task
            bus.shutdown()


class TestMotorCheckRejectedWhenAlreadyRunning:
    async def test_motor_check_rejected_when_already_running(self) -> None:
        # 1 回目 start 直後にもう一度 start → 2 回目は拒否
        # フィードバックを送らせず長く時間がかかるよう per_motor_timeout を長くしてある
        motors = {"m1": _MockMotor("m1", 1)}
        server, _, _, bus = _build_server_with_motors(
            bus_channel="vsrvchk_busy",
            motors=motors,
            feed_immediately=False,
            motor_check_per_motor_timeout_ms=2000.0,
        )
        try:
            app = server.create_app()
            async with TestClient(TestServer(app)) as client:
                ws = await client.ws_connect("/ws")
                await _drain(ws)

                # 1 回目 start (実行中のまま放置できるよう feed_immediately=False)
                await ws.send_json({"type": "motor_check_start", "robot": "main_hand"})

                # runner 起動を待つ
                for _ in range(50):
                    await asyncio.sleep(0.01)
                    runner = server._motor_check_runners.get("main_hand")
                    if runner is not None and runner.is_running:
                        break
                else:
                    raise AssertionError("1 回目 runner が起動しなかった")

                await _drain(ws)

                # 2 回目 start → 拒否される
                await ws.send_json({"type": "motor_check_start", "robot": "main_hand"})

                got_error = False
                for _ in range(30):
                    try:
                        msg = await asyncio.wait_for(ws.receive_json(), timeout=0.1)
                    except TimeoutError:
                        break
                    if msg.get("type") == "motor_check_error":
                        assert "既に" in msg["message"] or "実行中" in msg["message"]
                        got_error = True
                        break

                assert got_error

                # 中断して片付け
                runner = server._motor_check_runners["main_hand"]
                runner.abort()
                await _wait_until_idle(server, "main_hand", timeout=4.0)
                await ws.close()
        finally:
            bus.shutdown()


class TestMotorCheckAbort:
    async def test_motor_check_abort(self) -> None:
        # m1 の wait 中に abort コマンドが届き、abort フラグが立った状態で
        # m1 が完走 → 残りは SKIPPED で抜ける。これにより通常運用に近い経路
        # (実行途中の中断) を検証する。
        motors = {
            "m1": _MockMotor("m1", 1),
            "m2": _MockMotor("m2", 2),
            "m3": _MockMotor("m3", 3),
        }
        server, mgr, _, bus = _build_server_with_motors(
            bus_channel="vsrvchk_abort",
            motors=motors,
            feed_immediately=False,
            motor_check_per_motor_timeout_ms=3000.0,
        )
        try:
            # 各モータの「1 回目の send (= check_command)」では即時フィードバックを返し、
            # 「2 回目の send (= reset_after_check)」では小さな遅延を入れる。
            # m1 の reset 中に WS 経由の abort が届く設計。
            original_send = mgr.send
            send_counts: dict[str, int] = {}

            async def _patched_send(motor_name: str, msg: can.Message) -> None:
                await original_send(motor_name, msg)
                send_counts[motor_name] = send_counts.get(motor_name, 0) + 1
                count = send_counts[motor_name]
                if count == 1:
                    # check_command 直後 → 即時フィードバック
                    mgr._last_rx_at[motor_name] = time.time() + 0.001
                elif motor_name == "m1" and count == 2:
                    # reset 中に abort が処理される時間を稼ぐ
                    await asyncio.sleep(0.05)

            mgr.send = _patched_send  # type: ignore[method-assign]

            app = server.create_app()
            async with TestClient(TestServer(app)) as client:
                ws = await client.ws_connect("/ws")
                await _drain(ws)

                await ws.send_json({"type": "motor_check_start", "robot": "main_hand"})

                # m1 の progress を観測したら abort を投げる (m1 の wait 中に確実に届く)
                seen_m1_progress = False
                for _ in range(50):
                    try:
                        msg = await asyncio.wait_for(ws.receive_json(), timeout=0.2)
                    except TimeoutError:
                        break
                    if msg.get("type") == "motor_check_progress" and msg["current"] == "m1":
                        seen_m1_progress = True
                        break

                assert seen_m1_progress, "m1 の progress を観測できなかった"

                await ws.send_json({"type": "motor_check_abort", "robot": "main_hand"})

                await _wait_until_idle(server, "main_hand", timeout=3.0)

                snapshot = server._motor_check_last.get("main_hand")
                assert snapshot is not None
                # m1 PASSED, m2 / m3 は SKIPPED で抜ける
                results = {r.motor: r.result.value for r in snapshot.records}
                assert results["m1"] == MotorCheckResult.PASSED.value
                assert results["m2"] == MotorCheckResult.SKIPPED.value
                assert results["m3"] == MotorCheckResult.SKIPPED.value
                await ws.close()
        finally:
            bus.shutdown()


class TestPostMotorCheckEndpoint:
    async def test_post_motor_check_endpoint(self) -> None:
        # POST /robots/main_hand/motor_check → 200 + {started: true}
        server, _, _, bus = _build_server_with_motors(bus_channel="vsrvchk_post_ok")
        try:
            app = server.create_app()
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/robots/main_hand/motor_check")
                assert resp.status == 200
                data = await resp.json()
                assert data["started"] is True

                await _wait_until_idle(server, "main_hand")

                # 知らないロボットは 404
                resp2 = await client.post("/robots/unknown_robot/motor_check")
                assert resp2.status == 404
        finally:
            bus.shutdown()


class TestPostMotorCheckEndpointReturns409WhenRejected:
    async def test_post_motor_check_endpoint_returns_409_when_rejected(self) -> None:
        # e_stop 中の POST は 409
        server, _, _, bus = _build_server_with_motors(bus_channel="vsrvchk_post_409")
        try:
            server._e_stop_active = True
            app = server.create_app()
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/robots/main_hand/motor_check")
                assert resp.status == 409
                data = await resp.json()
                assert data["started"] is False
        finally:
            bus.shutdown()


class TestGetMotorCheckLastEndpoint:
    async def test_get_motor_check_last_endpoint(self) -> None:
        # 実行前は {snapshot: null}、実行後はスナップショット dict
        server, _, _, bus = _build_server_with_motors(bus_channel="vsrvchk_last")
        try:
            app = server.create_app()
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/robots/main_hand/motor_check/last")
                assert resp.status == 200
                data = await resp.json()
                assert data["snapshot"] is None

                # 実行
                resp = await client.post("/robots/main_hand/motor_check")
                assert resp.status == 200
                await _wait_until_idle(server, "main_hand", timeout=3.0)

                resp = await client.get("/robots/main_hand/motor_check/last")
                assert resp.status == 200
                data = await resp.json()
                assert data["snapshot"] is not None
                assert data["snapshot"]["robot"] == "main_hand"
                assert data["snapshot"]["overall"] == "ok"

                # 知らないロボットは 404
                resp = await client.get("/robots/unknown/motor_check/last")
                assert resp.status == 404
        finally:
            bus.shutdown()


class TestMotorCheckUnknownRobotSilentIgnoreOnWs:
    async def test_motor_check_unknown_robot_silent_ignore_on_ws(self) -> None:
        # 知らないロボット名の motor_check_start は無視 (例外で接続が切れない)
        server, _, _, bus = _build_server_with_motors(bus_channel="vsrvchk_unk")
        try:
            app = server.create_app()
            async with TestClient(TestServer(app)) as client:
                ws = await client.ws_connect("/ws")
                await _drain(ws)

                await ws.send_json({"type": "motor_check_start", "robot": "ghost"})
                await asyncio.sleep(0.1)

                # 接続維持 + runner 未起動
                assert not ws.closed
                assert "ghost" not in server._motor_check_runners
                assert "main_hand" not in server._motor_check_runners
                await ws.close()
        finally:
            bus.shutdown()


class TestPositionLoopExclusion:
    """動作確認と M3508 位置制御ループ (0x200) の排他。"""

    async def test_position_loop_sends_no_frame_during_motor_check(self) -> None:
        server, mgr, _, bus = _build_server_with_motors(
            bus_channel="vsrvchk_loop_busy",
            feed_immediately=False,
            motor_check_per_motor_timeout_ms=2000.0,
        )
        probe = _LoopProbe(mgr)
        server._robots["main_hand"].position_loops = [probe.loop]
        try:
            assert await server._start_motor_check("main_hand") is True
            runner = await _wait_for_running_runner(server, "main_hand")

            assert probe.loop.is_paused is True
            before = len(probe.frames)
            await probe.loop.step()
            # 動作確認中にループが 0x200 を送ると、指令が 0 電流で上書きされる
            assert len(probe.frames) == before

            runner.abort()
            await _wait_until_idle(server, "main_hand", timeout=4.0)
        finally:
            bus.shutdown()

    async def test_position_loop_resumed_after_motor_check(self) -> None:
        server, mgr, _, bus = _build_server_with_motors(bus_channel="vsrvchk_loop_resume")
        probe = _LoopProbe(mgr)
        server._robots["main_hand"].position_loops = [probe.loop]
        try:
            assert await server._start_motor_check("main_hand") is True
            await _wait_until_idle(server, "main_hand", timeout=4.0)

            assert probe.loop.is_paused is False
            before = len(probe.frames)
            await probe.loop.step()
            assert len(probe.frames) == before + 1
        finally:
            bus.shutdown()

    async def test_position_loop_resumed_after_abort(self) -> None:
        server, mgr, _, bus = _build_server_with_motors(
            bus_channel="vsrvchk_loop_abort",
            feed_immediately=False,
            motor_check_per_motor_timeout_ms=2000.0,
        )
        probe = _LoopProbe(mgr)
        server._robots["main_hand"].position_loops = [probe.loop]
        try:
            assert await server._start_motor_check("main_hand") is True
            runner = await _wait_for_running_runner(server, "main_hand")
            runner.abort()
            await _wait_until_idle(server, "main_hand", timeout=4.0)

            assert probe.loop.is_paused is False
        finally:
            bus.shutdown()

    async def test_position_loop_resumed_when_runner_raises(self) -> None:
        server, mgr, _, bus = _build_server_with_motors(bus_channel="vsrvchk_loop_raise")
        probe = _LoopProbe(mgr)
        server._robots["main_hand"].position_loops = [probe.loop]
        try:
            with patch.object(
                MotorCheckRunner,
                "run",
                new_callable=AsyncMock,
                side_effect=RuntimeError("テスト用例外"),
            ):
                assert await server._start_motor_check("main_hand") is True
                await _wait_until_idle(server, "main_hand", timeout=4.0)

            # 復帰しないとリフトが保持電流を失ったままになる
            assert probe.loop.is_paused is False
        finally:
            bus.shutdown()

    async def test_pid_integral_not_carried_over_after_motor_check(self) -> None:
        server, mgr, _, bus = _build_server_with_motors(bus_channel="vsrvchk_loop_integral")
        probe = _LoopProbe(mgr, ki=10.0)
        server._robots["main_hand"].position_loops = [probe.loop]
        try:
            probe.feed(0.0)
            await probe.loop.set_target(probe.motor_name, ControlMode.POSITION, 10.0)
            await probe.loop.step()
            await probe.loop.step()
            assert probe.loop.pid(probe.motor_name).integral != 0.0

            assert await server._start_motor_check("main_hand") is True
            await _wait_until_idle(server, "main_hand", timeout=4.0)

            # 動作確認でモータが動かされた後に古い積分が残ると復帰時に暴れる
            assert probe.loop.pid(probe.motor_name).integral == 0.0
            assert probe.loop.target(probe.motor_name) == 10.0
        finally:
            bus.shutdown()

    async def test_motor_check_works_without_position_loops(self) -> None:
        # sub_hand 等 M3508 が居ない構成でも従来どおり動く
        server, _, _, bus = _build_server_with_motors(bus_channel="vsrvchk_loop_absent")
        try:
            assert server._robots["main_hand"].position_loops == []

            assert await server._start_motor_check("main_hand") is True
            await _wait_until_idle(server, "main_hand", timeout=4.0)

            snapshot = server._motor_check_last.get("main_hand")
            assert snapshot is not None
            assert snapshot.overall == "ok"
        finally:
            bus.shutdown()

    async def test_add_robot_defaults_to_no_position_loops(self) -> None:
        server = RobotServer()
        mgr = CANManager()
        server.add_robot("sub_hand", _DummySequence(), mgr)

        assert server._robots["sub_hand"].position_loops == []
        assert server._robots["sub_hand"].target_refreshers == []


class TestTargetRefreshExclusion:
    """動作確認と自作モタドラ向け目標値再送の排他。

    再送は「最後に送った目標値」を 20Hz で送り続ける。動作確認中に被せると
    確認用の指令が打ち消され、健全なモータが FAILED と判定される。
    """

    def _refresher(self, mgr: CANManager) -> GenericTargetRefresher:
        driver = GenericDriver("conveyor", can_id=9, control_type=ControlMode.DUTY)
        handle = MotorHandle("conveyor", driver, mgr)
        return GenericTargetRefresher([handle])

    async def test_refresher_paused_during_motor_check(self) -> None:
        server, mgr, _, bus = _build_server_with_motors(
            bus_channel="vsrvchk_refresh_busy",
            feed_immediately=False,
            motor_check_per_motor_timeout_ms=2000.0,
        )
        refresher = self._refresher(mgr)
        server._robots["main_hand"].target_refreshers = [refresher]
        try:
            assert await server._start_motor_check("main_hand") is True
            runner = await _wait_for_running_runner(server, "main_hand")

            assert refresher.is_paused is True

            runner.abort()
            await _wait_until_idle(server, "main_hand", timeout=4.0)
        finally:
            bus.shutdown()

    async def test_refresher_resumed_after_motor_check(self) -> None:
        """再開に失敗すると、以後コンベアが 500ms で止まる機体になる。"""
        server, mgr, _, bus = _build_server_with_motors(bus_channel="vsrvchk_refresh_resume")
        refresher = self._refresher(mgr)
        server._robots["main_hand"].target_refreshers = [refresher]
        try:
            assert await server._start_motor_check("main_hand") is True
            await _wait_until_idle(server, "main_hand", timeout=4.0)

            assert refresher.is_paused is False
        finally:
            bus.shutdown()
