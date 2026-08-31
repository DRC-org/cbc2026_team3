"""WebSocket クライアントの集合と、そこへの配信経路。

**テレメトリ配信は 1 クライアントの不調で止めてはならない。** 配信は全クライアントへ
直列に送るため、詰まった 1 台を無期限に待つと他の全員 (Monitor 含む) の値が凍る。
しかも WebSocket は開いたままなので UI は「接続中」を出し続け、操縦者は凍った値を
最新だと思って見続けることになる。操縦者のノート PC がスリープに入る・Wi-Fi が
切れるだけで起きうる。

これを防ぐ約束事は 4 つあり、**どれも「経路が 1 本であること」に依存している**:

1. 送信には必ず ``_WS_SEND_TIMEOUT_S`` を通す (``send_or_drop``)
2. 切り離しの ``close()`` は別タスクへ逃がす —— ``close()`` も相手のクローズ応答を
   待つので、配信ループ上で await すると送信上限を設けた意味がなくなり、同じ場所で
   詰まる
3. 反復は必ずクライアント集合の**スナップショット**に対して行う。送信は 1 通ごとに
   await するので、その隙に接続ハンドラが同じ集合へ add / discard を行いうる
   (操縦者のリロード 1 回で起きる)。集合をそのまま回すと
   ``RuntimeError: Set changed size during iteration`` になり、例外ガードの無い
   ``activate_e_stop`` の経路では E-STOP を押した本人の WS が切れる
4. 配信ループの例外ガードを外さない (呼び出し側が持つ)

サーバー本体に置いていた頃は、配信経路を 1 つ足すたびにこの 4 つを守り直す必要が
あった。集合と送信をここへ閉じ込めると、**経路を増やすには本クラスへメソッドを
足すしかなくなる**ので、守る場所が構造的に 1 つに保たれる。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from aiohttp import web

logger = logging.getLogger(__name__)

__all__ = ["WsHub"]

#: 1 クライアントへの送信・クローズに許す時間。これを超えた相手は生存とみなさない。
#: 20Hz の配信周期 (50ms) に対して十分長く、操縦者が「画面が固まった」と感じる前に
#: 切り離せる長さ
_WS_SEND_TIMEOUT_S = 1.0


class WsHub:
    """接続中の WebSocket クライアントと、そこへの唯一の配信経路。"""

    def __init__(self) -> None:
        self._clients: set[web.WebSocketResponse] = set()
        #: 切り離し中のクライアントを閉じるタスク。GC で消えないよう参照を保持する
        self._closing_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ #
    #  接続の出入り
    # ------------------------------------------------------------------ #

    def add(self, ws: web.WebSocketResponse) -> None:
        self._clients.add(ws)

    def discard(self, ws: web.WebSocketResponse) -> None:
        self._clients.discard(ws)

    @property
    def has_clients(self) -> bool:
        return bool(self._clients)

    # ------------------------------------------------------------------ #
    #  配信
    # ------------------------------------------------------------------ #

    async def broadcast_json(self, payload: dict) -> None:
        """1 メッセージを全クライアントへ配信する。"""
        await self.fanout([payload])

    async def fanout(self, payloads: list[dict]) -> None:
        """全クライアントへの配信経路はここ 1 本だけ。

        シリアライズはクライアント数によらず 1 回。20Hz の配信で全員ぶん JSON を
        作り直すのは無駄でしかない。

        送信はクライアント単位でまとめ、途中で失敗した相手には残りを送らない
        (送れないと分かった相手に投げ続けるぶん、他の配信が遅れる)。
        """
        if not payloads:
            return
        messages = [json.dumps(payload, ensure_ascii=False) for payload in payloads]

        dead: set[web.WebSocketResponse] = set()
        # モジュール docstring の 3.: 必ずスナップショットに対して回す
        for ws in list(self._clients):
            if ws.closed:
                dead.add(ws)
                continue
            for msg in messages:
                if not await self.send_or_drop(ws, msg):
                    dead.add(ws)
                    break

        await self.drop(dead)

    async def send_or_drop(self, ws: web.WebSocketResponse, msg: str) -> bool:
        """1 クライアントへ送信する。詰まった相手は生存とみなさず切り離す。

        aiohttp の ``send_str`` は相手が読まなくなると無期限に待つ。送信ごとに
        上限を設け、超えた相手は False を返して呼び出し側に切り離させる。
        """
        try:
            await asyncio.wait_for(ws.send_str(msg), timeout=_WS_SEND_TIMEOUT_S)
            return True
        except TimeoutError:
            logger.warning("WebSocket 送信がタイムアウトしたためクライアントを切り離します")
        except ConnectionResetError:
            logger.debug("WebSocket 送信先が既に切断されています")
        except Exception:
            logger.warning("WebSocket 送信に失敗したためクライアントを切り離します", exc_info=True)
        return False

    async def drop(self, dead: set[web.WebSocketResponse]) -> None:
        """切り離したクライアントの後始末。ソケットも閉じて再接続を促す。

        ``close()`` は相手からのクローズ応答を待つ。送信が詰まっている相手は
        まさにその応答を返さないので、ここで await すると送信タイムアウトを
        設けた意味がなくなり配信ループが同じ場所で止まる。
        後始末は別タスクへ切り離し、配信ループは決して待たない。
        """
        self._clients -= dead
        for ws in dead:
            task = asyncio.create_task(self._close_quietly(ws))
            self._closing_tasks.add(task)
            task.add_done_callback(self._closing_tasks.discard)

    # ------------------------------------------------------------------ #
    #  終了処理
    # ------------------------------------------------------------------ #

    def cancel_closing_tasks(self) -> None:
        """切り離し中のクローズを打ち切る (終了処理の一部)。"""
        for task in self._closing_tasks:
            task.cancel()
        self._closing_tasks.clear()

    async def close_all(self) -> None:
        """接続中のクライアントを全て閉じる (終了処理の共通経路)。

        ``close()`` は相手のクローズ応答を待つため、スリープしたノート PC が 1 台
        繋がっているだけで終了処理が返らなくなり、CAN を落とす後始末まで到達しない。
        送信と同じ上限を通し、複数台をまとめて待つことで最悪でも上限 1 回ぶんで抜ける。
        """
        clients = set(self._clients)
        self._clients.clear()
        if clients:
            await asyncio.gather(*(self._close_quietly(ws) for ws in clients))

    async def _close_quietly(self, ws: web.WebSocketResponse) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.close(), timeout=_WS_SEND_TIMEOUT_S)
