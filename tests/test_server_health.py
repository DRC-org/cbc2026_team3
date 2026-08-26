from __future__ import annotations

import asyncio
import time

import can
from aiohttp.test_utils import TestClient, TestServer

from lib.can_manager import CANManager
from lib.drivers.base import MotorState
from lib.health import BusHealth
from lib.sequence.engine import Sequence, step
from lib.server import RobotServer
from tests.fake_drivers import CheckStubDriver


class _MockMotor(CheckStubDriver):
    """サーバーヘルス統合テスト用の最小モータドライバ。

    health() の判定経路だけを検証するために、フィードバックパース実装を持たず
    判定フラグを属性で制御する。tests/test_can_manager_health.py の _FakeMotor
    と同じ思想。
    """

    def __init__(self, name: str, can_id: int) -> None:
        super().__init__(name, can_id)
        self.thermal_warning = False
        self.thermal_fault = False
        self.overcurrent = False
        self.fault = False

    def encode_target(self, mode, value):  # pragma: no cover - 本テストでは未使用
        return can.Message(arbitration_id=0x100 + self.can_id, data=bytes(8))

    def decode_feedback(self, msg: can.Message) -> MotorState:
        return self._state

    def matches_feedback(self, msg: can.Message) -> bool:
        return msg.arbitration_id == 0x200 + self.can_id

    def has_thermal_warning(self, temp_warning_c: float) -> bool:
        return self.thermal_warning

    def has_thermal_fault(self, temp_critical_c: float) -> bool:
        return self.thermal_fault

    def has_overcurrent_warning(self) -> bool:
        return self.overcurrent

    def is_fault(self) -> bool:
        return self.fault


class _DummySequence(Sequence):
    def __init__(self) -> None:
        super().__init__("test_seq")

    @step("ノーオペ")
    async def noop(self) -> None:
        return None


def _build_server_with_motors(
    *,
    bus_channel: str = "vsrvhealth0",
    fresh_feedback: bool = True,
) -> tuple[RobotServer, CANManager, _MockMotor, can.Bus]:
    """RobotServer + 実 CANManager + virtual バス + MockMotor の構成を組む。

    fresh_feedback=True で _last_rx_at を現在時刻に設定し OK 判定にする。
    False のままなら STALE 判定 (受信ゼロ) になる。
    """
    server = RobotServer()
    mgr = CANManager()
    bus = can.Bus(interface="virtual", channel=bus_channel, receive_own_messages=False)
    motor = _MockMotor("m1", 1)
    mgr.add_bus("bus0", bus, channel=bus_channel)
    mgr.add_motor("bus0", motor)

    if fresh_feedback:
        mgr._last_rx_at[motor.name] = time.time()

    seq = _DummySequence()
    server.add_robot("main_hand", seq, mgr)
    return server, mgr, motor, bus


class TestHealthEndpointEmptyRobots:
    async def test_health_endpoint_empty_robots(self) -> None:
        # ロボット未登録時でも 200 OK を返し、overall=ok / robots は空辞書のはず
        server = RobotServer()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/health")
            assert resp.status == 200
            data = await resp.json()
            assert data["overall"] == "ok"
            assert data["robots"] == {}


class TestHealthEndpointReturns200WhenOk:
    async def test_health_endpoint_returns_200_when_ok(self) -> None:
        # 全モータ・全バスが OK 判定なら 200 / overall=ok / robots[name] に snapshot.to_dict()
        server, _, _, bus = _build_server_with_motors(bus_channel="vsrvhealth_ok")
        try:
            app = server.create_app()

            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/health")
                assert resp.status == 200
                data = await resp.json()
                assert data["overall"] == "ok"
                assert "main_hand" in data["robots"]
                snap = data["robots"]["main_hand"]
                # HealthSnapshot.to_dict() の構造を踏襲しているか
                assert snap["overall"] == "ok"
                assert isinstance(snap["buses"], list)
                assert isinstance(snap["motors"], list)
                assert snap["buses"][0]["name"] == "bus0"
                assert snap["motors"][0]["name"] == "m1"
                assert snap["motors"][0]["state"] == "ok"
        finally:
            bus.shutdown()


class TestHealthEndpointReturns503WhenDegraded:
    async def test_health_endpoint_returns_503_when_degraded(self) -> None:
        # モータ STALE → overall=degraded → 503
        server, _, _, bus = _build_server_with_motors(
            bus_channel="vsrvhealth_deg", fresh_feedback=False
        )
        try:
            # _last_rx_at 未設定で STALE になる
            app = server.create_app()

            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/health")
                assert resp.status == 503
                data = await resp.json()
                assert data["overall"] == "degraded"
                snap = data["robots"]["main_hand"]
                assert snap["motors"][0]["state"] == "stale"
        finally:
            bus.shutdown()


class TestHealthEndpointReturns503WhenDown:
    async def test_health_endpoint_returns_503_when_down(self) -> None:
        # bus_off → BusHealth.DOWN → overall=down → 503
        server, mgr, _, bus = _build_server_with_motors(bus_channel="vsrvhealth_down")
        try:
            mgr._bus_off["bus0"] = True
            app = server.create_app()

            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/health")
                assert resp.status == 503
                data = await resp.json()
                assert data["overall"] == "down"
                snap = data["robots"]["main_hand"]
                assert snap["buses"][0]["state"] == "down"
                assert snap["buses"][0]["bus_off"] is True
        finally:
            bus.shutdown()


class TestStateMessageIncludesHealth:
    async def test_state_message_includes_health(self) -> None:
        # _build_state_message の戻り値に health キー (HealthSnapshot.to_dict()) が含まれる
        server, _, _, bus = _build_server_with_motors(bus_channel="vsrvhealth_state")
        try:
            msg = server._build_state_message("main_hand")
            assert "health" in msg
            health = msg["health"]
            assert health["overall"] == "ok"
            assert isinstance(health["buses"], list)
            assert isinstance(health["motors"], list)
            assert health["buses"][0]["name"] == "bus0"
            assert health["motors"][0]["name"] == "m1"
            # 既存フィールドが温存されているか (リグレッション防止)
            assert msg["type"] == "state"
            assert msg["robot"] == "main_hand"
            assert "motors" in msg
            assert "e_stop_active" in msg
        finally:
            bus.shutdown()


class TestHealthChangeEventPushedOnStateTransition:
    async def test_health_change_event_pushed_on_state_transition(self) -> None:
        # 初回 broadcast (差分なし) → モータを FAULT 化 → 次の broadcast で health_change 受信
        server, _, motor, bus = _build_server_with_motors(bus_channel="vsrvhealth_change")
        try:
            app = server.create_app()

            async with TestClient(TestServer(app)) as client:
                ws = await client.ws_connect("/ws")

                # 1 回目: 初回スナップショットを記録 (前回 None なので health_change なし)
                await server._broadcast_state()

                # 2 回目までに受信した state メッセージを排出する。差分検出後に
                # _last_health に書き込まれているはず。
                drained: list[dict] = []
                for _ in range(5):
                    try:
                        msg = await asyncio.wait_for(ws.receive_json(), timeout=0.05)
                        drained.append(msg)
                    except TimeoutError:
                        break

                # 全モータを FAULT 状態にする (state 遷移を起こすため)
                motor.fault = True

                # 2 回目: health_change が push されるはず
                await server._broadcast_state()

                found_change = False
                for _ in range(20):
                    try:
                        msg = await asyncio.wait_for(ws.receive_json(), timeout=0.1)
                    except TimeoutError:
                        break
                    # モータ m1 が ok → fault に遷移したイベントを検出
                    if (
                        msg.get("type") == "health_change"
                        and msg.get("target") == "motor:m1"
                        and msg.get("to") == "fault"
                    ):
                        found_change = True
                        break

                assert found_change, "motor m1 ok→fault の health_change が配信されなかった"

                await ws.close()
        finally:
            bus.shutdown()


class TestHealthCheckCommandTriggersBroadcast:
    async def test_health_check_command_triggers_broadcast(self) -> None:
        # {"type":"health_check"} 受信で即時 state 配信が走る
        server, _, _, bus = _build_server_with_motors(bus_channel="vsrvhealth_cmd")
        try:
            app = server.create_app()

            async with TestClient(TestServer(app)) as client:
                ws = await client.ws_connect("/ws")

                # 既存の broadcast loop からのメッセージを一旦排出
                for _ in range(5):
                    try:
                        await asyncio.wait_for(ws.receive_json(), timeout=0.05)
                    except TimeoutError:
                        break

                await ws.send_json({"type": "health_check"})

                # 即時 state 配信が来るはず (health フィールド付き)
                got_state_with_health = False
                for _ in range(20):
                    try:
                        msg = await asyncio.wait_for(ws.receive_json(), timeout=0.1)
                    except TimeoutError:
                        break
                    if msg.get("type") == "state" and "health" in msg:
                        got_state_with_health = True
                        break

                assert got_state_with_health, "health_check に対する state 配信が来なかった"

                await ws.close()
        finally:
            bus.shutdown()


class TestHealthComputationFailure:
    """健全性計算が壊れたときに「正常」を出してはならない。

    ここで OK に倒すと、監視系も操縦者も異常を検出する手段を丸ごと失う。
    """

    async def test_exception_is_reported_as_down(self) -> None:
        server, mgr, _motor, bus = _build_server_with_motors()
        try:

            def _boom(**_kwargs):
                raise RuntimeError("health 計算が壊れた")

            mgr.health = _boom  # type: ignore[method-assign]

            snap = server._compute_health("main_hand")

            assert snap.overall is BusHealth.DOWN
            assert snap.detail is not None
        finally:
            bus.shutdown()

    async def test_exception_makes_endpoint_return_503(self) -> None:
        server, mgr, _motor, bus = _build_server_with_motors()
        try:

            def _boom(**_kwargs):
                raise RuntimeError("health 計算が壊れた")

            mgr.health = _boom  # type: ignore[method-assign]
            app = server.create_app()

            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/health")
                assert resp.status == 503
                data = await resp.json()
                assert data["overall"] == "down"
        finally:
            bus.shutdown()

    async def test_non_snapshot_return_is_reported_as_down(self) -> None:
        """health() が HealthSnapshot 以外を返す構成は異常。黙って OK にしない。"""
        server, mgr, _motor, bus = _build_server_with_motors()
        try:
            mgr.health = lambda **_kwargs: None  # type: ignore[method-assign, assignment]

            snap = server._compute_health("main_hand")

            assert snap.overall is BusHealth.DOWN
            assert snap.detail is not None
        finally:
            bus.shutdown()

    async def test_failure_detail_reaches_state_message(self) -> None:
        """理由が残らないと、画面に出た DOWN の原因を操縦者が切り分けられない。"""
        server, mgr, _motor, bus = _build_server_with_motors()
        try:

            def _boom(**_kwargs):
                raise RuntimeError("health 計算が壊れた")

            mgr.health = _boom  # type: ignore[method-assign]

            state = server._build_state_message("main_hand")

            assert state["health"]["overall"] == "down"
            assert state["health"]["detail"]
        finally:
            bus.shutdown()
