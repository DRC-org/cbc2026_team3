from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from lib.can_manager import CANManager
from lib.sequence.engine import Sequence, step
from tests.server_fixtures import ServerFixture


class _NoopSequence(Sequence):
    def __init__(self) -> None:
        super().__init__("noop_seq")

    @step("ノーオペ")
    async def noop(self) -> None:
        return None


def _bare_can_manager() -> CANManager:
    mgr = CANManager()
    mgr.add_bus("bus0", MagicMock(), channel="vbroadcast0")
    return mgr


class _StalledClient:
    def __init__(self) -> None:
        self.closed = False
        self.close_called = False

    async def send_str(self, msg: str) -> None:
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.close_called = True
        await asyncio.Event().wait()


class _HealthyClient:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[str] = []

    async def send_str(self, msg: str) -> None:
        self.sent.append(msg)

    async def close(self) -> None:
        self.closed = True


class _HandshakeClient:
    def __init__(self, *, stall_after: int | None = None) -> None:
        self.closed = False
        self.close_called = False
        self.sent: list[str] = []
        self._stall_after = stall_after

    async def prepare(self, request: object) -> None:
        return None

    async def send_str(self, msg: str) -> None:
        if self._stall_after is not None and len(self.sent) >= self._stall_after:
            await asyncio.Event().wait()
        self.sent.append(msg)

    async def close(self) -> None:
        self.close_called = True
        await asyncio.Event().wait()

    def exception(self) -> BaseException | None:
        return None

    def __aiter__(self) -> _HandshakeClient:
        return self

    async def __anext__(self) -> object:
        raise StopAsyncIteration


class _ExplodingClient:
    def __init__(self) -> None:
        self.closed = False
        self.attempts = 0

    async def send_str(self, msg: str) -> None:
        self.attempts += 1
        raise RuntimeError("transport is closing")

    async def close(self) -> None:
        self.closed = True


class TestBroadcastResilience:
    async def test_詰まったクライアントは切り離され配信は完了する(self, monkeypatch) -> None:
        ServerFixture.shrink_ws_send_timeout(monkeypatch)

        fx = ServerFixture.build()
        stalled = _StalledClient()
        healthy = _HealthyClient()
        fx.attach_clients(stalled, healthy)

        await asyncio.wait_for(fx.publish({"type": "ping"}), timeout=2.0)

        assert not fx.is_connected(stalled)
        assert fx.is_connected(healthy)
        assert healthy.sent == ['{"type": "ping"}']

        await asyncio.sleep(0.01)
        assert stalled.close_called
        assert fx.has_closing_tasks, "クローズタスクの参照を保持していないと GC で消える"

    async def test_例外を投げるクライアントも切り離される(self) -> None:
        fx = ServerFixture.build()
        exploding = _ExplodingClient()
        healthy = _HealthyClient()
        fx.attach_clients(exploding, healthy)

        await asyncio.wait_for(fx.publish({"type": "ping"}), timeout=2.0)

        assert not fx.is_connected(exploding)
        assert fx.is_connected(healthy)
        assert len(healthy.sent) == 1

    async def test_配信ループは例外が出ても次の周期へ進む(self) -> None:
        fx = ServerFixture.build()
        fx.set_broadcast_interval(0.001)
        calls = {"n": 0}

        async def flaky() -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("一時的な失敗")

        fx.patch_publish_state(flaky)

        task = asyncio.create_task(fx.broadcast_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        assert calls["n"] > 1


class TestFanout:
    async def test_複数メッセージは順序どおり届く(self) -> None:
        fx = ServerFixture.build()
        healthy = _HealthyClient()
        fx.attach_clients(healthy)

        await fx.fanout([{"type": "a"}, {"type": "b"}])

        assert healthy.sent == ['{"type": "a"}', '{"type": "b"}']

    async def test_1通目に失敗した相手へ2通目は送らない(self) -> None:
        fx = ServerFixture.build()
        exploding = _ExplodingClient()
        healthy = _HealthyClient()
        fx.attach_clients(exploding, healthy)

        await fx.fanout([{"type": "a"}, {"type": "b"}])

        assert exploding.attempts == 1
        assert not fx.is_connected(exploding)
        assert healthy.sent == ['{"type": "a"}', '{"type": "b"}']

    async def test_テレメトリ配信も詰まった相手を切り離す(self, monkeypatch) -> None:
        ServerFixture.shrink_ws_send_timeout(monkeypatch)

        fx = ServerFixture.build()
        fx.add_robot("main_hand", _NoopSequence(), _bare_can_manager())
        stalled = _StalledClient()
        healthy = _HealthyClient()
        fx.attach_clients(stalled, healthy)

        await asyncio.wait_for(fx.publish_state(), timeout=2.0)

        assert not fx.is_connected(stalled)
        assert fx.is_connected(healthy)
        assert json.loads(healthy.sent[0])["type"] == "state"


class TestConnectHandshakeIsNotUnbounded:
    @pytest.mark.parametrize("stall_after", [0, 1, 2])
    async def test_詰まった相手でも接続ハンドラは返る(self, monkeypatch, stall_after: int) -> None:
        ServerFixture.shrink_ws_send_timeout(monkeypatch)

        fx = ServerFixture.build()
        client = _HandshakeClient(stall_after=stall_after)

        await asyncio.wait_for(fx.run_ws_handler(client), timeout=2.0)

        assert not fx.is_connected(client)
        assert len(client.sent) == stall_after
        await asyncio.sleep(0.01)
        assert client.close_called
        assert fx.has_closing_tasks

    async def test_正常な相手にはスナップショット3通が届く(self) -> None:
        fx = ServerFixture.build()
        client = _HandshakeClient()

        await asyncio.wait_for(fx.run_ws_handler(client), timeout=2.0)

        assert [json.loads(m)["type"] for m in client.sent] == [
            "server_info",
            "match_state",
            "motor_check_state",
        ]


class TestShutdownDoesNotHang:
    async def test_on_shutdown_は詰まったクライアントを待たない(self, monkeypatch) -> None:
        ServerFixture.shrink_ws_send_timeout(monkeypatch)

        fx = ServerFixture.build()
        app = fx.create_app()
        stalled = _StalledClient()
        fx.attach_clients(stalled)

        await asyncio.wait_for(fx.shutdown(app), timeout=2.0)

        assert fx.client_count == 0
        assert stalled.close_called

    async def test_cleanup_は詰まったクライアントを待たない(self, monkeypatch) -> None:
        ServerFixture.shrink_ws_send_timeout(monkeypatch)

        fx = ServerFixture.build()
        stalled = _StalledClient()
        fx.attach_clients(stalled)

        await asyncio.wait_for(fx.server.cleanup(), timeout=2.0)

        assert fx.client_count == 0
        assert stalled.close_called


class _JoiningClient:
    def __init__(self, fx: ServerFixture, newcomer: object) -> None:
        self.closed = False
        self.sent: list[str] = []
        self._fx = fx
        self._newcomer = newcomer
        self._joined = False

    async def send_str(self, msg: str) -> None:
        if not self._joined:
            self._joined = True
            self._fx.connect_client(self._newcomer)
        self.sent.append(msg)

    async def close(self) -> None:
        self.closed = True


class TestFanoutToleratesConcurrentConnect:
    async def test_配信中に1台繋がっても配信は完了する(self) -> None:
        fx = ServerFixture.build()
        newcomer = _HealthyClient()
        joiner = _JoiningClient(fx, newcomer)
        fx.attach_clients(joiner)

        await asyncio.wait_for(fx.publish({"type": "ping"}), timeout=2.0)

        assert joiner.sent == ['{"type": "ping"}']
        assert fx.is_connected(newcomer)

    async def test_緊急停止の配信は途中接続で操縦者のWSを切らない(self) -> None:
        fx = ServerFixture.build()
        fx.add_robot("main_hand", _NoopSequence(), _bare_can_manager())
        newcomer = _HealthyClient()
        joiner = _JoiningClient(fx, newcomer)
        fx.attach_clients(joiner)

        await asyncio.wait_for(fx.command({"type": "e_stop"}), timeout=2.0)

        assert fx.e_stop_active is True
        assert fx.is_connected(joiner), "緊急停止を押した操縦者が切り離された"

    async def test_試合状態の配信も途中接続で落ちない(self) -> None:
        fx = ServerFixture.build()
        newcomer = _HealthyClient()
        joiner = _JoiningClient(fx, newcomer)
        fx.attach_clients(joiner)

        await asyncio.wait_for(fx.command({"type": "match_reset"}), timeout=2.0)

        assert fx.is_connected(joiner)
