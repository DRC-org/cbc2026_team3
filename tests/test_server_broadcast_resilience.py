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
from tests.server_fixtures import ServerFixture


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
        ServerFixture.shrink_ws_send_timeout(monkeypatch)

        fx = ServerFixture.build()
        stalled = _StalledClient()
        healthy = _HealthyClient()
        fx.attach_clients(stalled, healthy)

        await asyncio.wait_for(fx.publish({"type": "ping"}), timeout=2.0)

        # 詰まった側だけが切り離され、正常な側には届いている
        assert not fx.is_connected(stalled)
        assert fx.is_connected(healthy)
        assert healthy.sent == ['{"type": "ping"}']

        # 後始末は別タスクへ切り離されている (配信ループはこれを待たない)
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
        # 1 回目の例外でループが終わっていたら 2 回目以降は呼ばれない
        assert calls["n"] > 1


class TestFanout:
    """全配信経路が同じファンアウトを通ること。

    経路が分かれていると「送信タイムアウトを通す」「切り離しは別タスクへ逃がす」という
    不変条件を経路の数だけ守り続ける必要があり、増えた経路が 1 つ抜けただけで
    テレメトリ全体が 1 クライアントに引きずられて凍る。
    """

    async def test_複数メッセージは順序どおり届く(self) -> None:
        fx = ServerFixture.build()
        healthy = _HealthyClient()
        fx.attach_clients(healthy)

        await fx.fanout([{"type": "a"}, {"type": "b"}])

        assert healthy.sent == ['{"type": "a"}', '{"type": "b"}']

    async def test_1通目に失敗した相手へ2通目は送らない(self) -> None:
        # 送れないと分かった相手に残りを投げ続けるぶんだけ、他クライアントの配信が遅れる
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


class TestShutdownDoesNotHang:
    """終了処理も送信と同じ上限を通す。

    `close()` は相手のクローズ応答を待つ。スリープしたノート PC が 1 台繋がって
    いるだけでシャットダウンが返らなくなり、CAN を落とす後始末まで到達しない。
    """

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
    """送信の待ちの隙に別のクライアントを接続させるクライアント。

    操縦者がタブをリロードするだけで起きる。配信は 1 通ごとに await を挟むため、
    その隙に `_ws_handler` が同じ集合へ追加・削除を行う。配信側が集合をそのまま
    反復していると `RuntimeError: Set changed size during iteration` になり、
    `activate_e_stop` の配信からは例外ガード無しで呼び出し元まで抜けて
    E-STOP を押した操縦者の WebSocket がその場で切れる。
    """

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
    """配信の最中に接続が 1 本増減しても配信経路ごと落ちないこと。"""

    async def test_配信中に1台繋がっても配信は完了する(self) -> None:
        fx = ServerFixture.build()
        newcomer = _HealthyClient()
        joiner = _JoiningClient(fx, newcomer)
        fx.attach_clients(joiner)

        await asyncio.wait_for(fx.publish({"type": "ping"}), timeout=2.0)

        assert joiner.sent == ['{"type": "ping"}']
        assert fx.is_connected(newcomer)

    async def test_緊急停止の配信は途中接続で操縦者のWSを切らない(self) -> None:
        # activate_e_stop の finally には例外ガードが無い。ここで例外が抜けると
        # handle_command → _ws_handler まで伝播し、E-STOP を押した本人の接続が切れる
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
