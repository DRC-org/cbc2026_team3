from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from aiohttp import web

logger = logging.getLogger(__name__)

__all__ = ["WsHub"]

_WS_SEND_TIMEOUT_S = 1.0


class WsHub:
    def __init__(self) -> None:
        self._clients: set[web.WebSocketResponse] = set()
        self._closing_tasks: set[asyncio.Task[None]] = set()

    def add(self, ws: web.WebSocketResponse) -> None:
        self._clients.add(ws)

    def discard(self, ws: web.WebSocketResponse) -> None:
        self._clients.discard(ws)

    @property
    def has_clients(self) -> bool:
        return bool(self._clients)

    async def broadcast_json(self, payload: dict) -> None:
        await self.fanout([payload])

    async def fanout(self, payloads: list[dict]) -> None:
        if not payloads:
            return
        messages = [json.dumps(payload, ensure_ascii=False) for payload in payloads]

        dead: set[web.WebSocketResponse] = set()
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
        self._clients -= dead
        for ws in dead:
            task = asyncio.create_task(self._close_quietly(ws))
            self._closing_tasks.add(task)
            task.add_done_callback(self._closing_tasks.discard)

    def cancel_closing_tasks(self) -> None:
        for task in self._closing_tasks:
            task.cancel()
        self._closing_tasks.clear()

    async def close_all(self) -> None:
        clients = set(self._clients)
        self._clients.clear()
        if clients:
            await asyncio.gather(*(self._close_quietly(ws) for ws in clients))

    async def _close_quietly(self, ws: web.WebSocketResponse) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.close(), timeout=_WS_SEND_TIMEOUT_S)
