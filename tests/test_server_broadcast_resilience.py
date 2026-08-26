"""テレメトリ配信が 1 クライアントの不調に巻き込まれないことを検証する。

配信は全クライアントへ直列に送るため、詰まった 1 台をそのまま待つと他の全員 —
Monitor を含む — のテレメトリが止まる。しかも WebSocket 自体は開いたままなので
UI は「接続中」を出し続け、操縦者は凍った値を最新だと思って見続けることになる。
操縦者のノート PC がスリープに入る・Wi-Fi が切れるだけで起きうる。
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

from lib.can_manager import CANManager
from lib.sequence.engine import Sequence, step
from lib.server import RobotServer


class _NoopSequence(Sequence):
    """state メッセージを 1 通生成させるための最小シーケンス。"""

    def __init__(self) -> None:
        super().__init__("noop_seq")

    @step("ノーオペ")
    async def noop(self) -> None:
        return None


def _bare_can_manager() -> CANManager:
    """モータ 0 台・バス 1 本の実 CANManager (配信経路だけを見るため)。"""
    mgr = CANManager()
    mgr.add_bus("bus0", MagicMock(), channel="vbroadcast0")
    return mgr


class _StalledClient:
    """送信もクローズも永久に返さないクライアント。スリープしたノート PC を模す。

    `close()` まで詰まらせているのは意図的。WebSocket のクローズは相手の応答を
    待つので、送信が詰まっている相手は当然クローズにも応じない。後始末を
    配信ループ上で await すると送信タイムアウトを設けた意味がなくなる。
    """

    def __init__(self) -> None:
        self.closed = False
        self.close_called = False

    async def send_str(self, msg: str) -> None:
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.close_called = True
        await asyncio.Event().wait()


class _HealthyClient:
    """正常に受け取るクライアント。"""

    def __init__(self) -> None:
        self.closed = False
        self.sent: list[str] = []

    async def send_str(self, msg: str) -> None:
        self.sent.append(msg)

    async def close(self) -> None:
        self.closed = True


class _ExplodingClient:
    """送信で例外を投げるクライアント。"""

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
        # 実時間で 1 秒待たされるとテストが遅くなるだけなので上限を縮めて等価に検証する
        monkeypatch.setattr("lib.server._WS_SEND_TIMEOUT_S", 0.05)

        server = RobotServer()
        stalled = _StalledClient()
        healthy = _HealthyClient()
        server._ws_clients = {stalled, healthy}  # type: ignore[assignment]

        await asyncio.wait_for(server._broadcast_json({"type": "ping"}), timeout=2.0)

        # 詰まった側だけが切り離され、正常な側には届いている
        assert stalled not in server._ws_clients
        assert healthy in server._ws_clients
        assert healthy.sent == ['{"type": "ping"}']

        # 後始末は別タスクへ切り離されている (配信ループはこれを待たない)
        await asyncio.sleep(0.01)
        assert stalled.close_called
        assert server._closing_tasks, "クローズタスクの参照を保持していないと GC で消える"

    async def test_例外を投げるクライアントも切り離される(self) -> None:
        server = RobotServer()
        exploding = _ExplodingClient()
        healthy = _HealthyClient()
        server._ws_clients = {exploding, healthy}  # type: ignore[assignment]

        await asyncio.wait_for(server._broadcast_json({"type": "ping"}), timeout=2.0)

        assert exploding not in server._ws_clients
        assert healthy in server._ws_clients
        assert len(healthy.sent) == 1

    async def test_配信ループは例外が出ても次の周期へ進む(self, monkeypatch) -> None:
        server = RobotServer()
        server._broadcast_interval = 0.001
        calls = {"n": 0}

        async def flaky() -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("一時的な失敗")

        monkeypatch.setattr(server, "_broadcast_state", flaky)

        task = asyncio.create_task(server._broadcast_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        # 1 回目の例外でループが終わっていたら 2 回目以降は呼ばれない
        assert calls["n"] > 1


class TestFanout:
    """全配信経路が同じファンアウトを通ること。

    経路が分かれていると「送信タイムアウトを通す」「切り離しは別タスクへ逃がす」という
    不変条件を経路の数だけ守り続ける必要があり、増えた経路が 1 つ抜けただけで
    テレメトリ全体が 1 クライアントに引きずられて凍る。
    """

    async def test_複数メッセージは順序どおり届く(self) -> None:
        server = RobotServer()
        healthy = _HealthyClient()
        server._ws_clients = {healthy}  # type: ignore[assignment]

        await server._fanout([{"type": "a"}, {"type": "b"}])

        assert healthy.sent == ['{"type": "a"}', '{"type": "b"}']

    async def test_1通目に失敗した相手へ2通目は送らない(self) -> None:
        # 送れないと分かった相手に残りを投げ続けるぶんだけ、他クライアントの配信が遅れる
        server = RobotServer()
        exploding = _ExplodingClient()
        healthy = _HealthyClient()
        server._ws_clients = {exploding, healthy}  # type: ignore[assignment]

        await server._fanout([{"type": "a"}, {"type": "b"}])

        assert exploding.attempts == 1
        assert exploding not in server._ws_clients
        assert healthy.sent == ['{"type": "a"}', '{"type": "b"}']

    async def test_テレメトリ配信も詰まった相手を切り離す(self, monkeypatch) -> None:
        monkeypatch.setattr("lib.server._WS_SEND_TIMEOUT_S", 0.05)

        server = RobotServer()
        server.add_robot("main_hand", _NoopSequence(), _bare_can_manager())
        stalled = _StalledClient()
        healthy = _HealthyClient()
        server._ws_clients = {stalled, healthy}  # type: ignore[assignment]

        await asyncio.wait_for(server._broadcast_state(), timeout=2.0)

        assert stalled not in server._ws_clients
        assert healthy in server._ws_clients
        assert json.loads(healthy.sent[0])["type"] == "state"


class TestShutdownDoesNotHang:
    """終了処理も送信と同じ上限を通す。

    `close()` は相手のクローズ応答を待つ。スリープしたノート PC が 1 台繋がって
    いるだけでシャットダウンが返らなくなり、CAN を落とす後始末まで到達しない。
    """

    async def test_on_shutdown_は詰まったクライアントを待たない(self, monkeypatch) -> None:
        monkeypatch.setattr("lib.server._WS_SEND_TIMEOUT_S", 0.05)

        server = RobotServer()
        app = server.create_app()
        stalled = _StalledClient()
        server._ws_clients = {stalled}  # type: ignore[assignment]

        await asyncio.wait_for(server._on_shutdown(app), timeout=2.0)

        assert not server._ws_clients
        assert stalled.close_called

    async def test_cleanup_は詰まったクライアントを待たない(self, monkeypatch) -> None:
        monkeypatch.setattr("lib.server._WS_SEND_TIMEOUT_S", 0.05)

        server = RobotServer()
        stalled = _StalledClient()
        server._ws_clients = {stalled}  # type: ignore[assignment]

        await asyncio.wait_for(server.cleanup(), timeout=2.0)

        assert not server._ws_clients
        assert stalled.close_called
