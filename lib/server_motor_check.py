from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from lib.match_state import Court
from lib.sequence.engine import Sequence

logger = logging.getLogger(__name__)

__all__ = ["MotorCheckController", "Pausable"]


class Pausable(Protocol):
    async def pause(self, *, reason: str) -> None: ...

    def resume(self) -> None: ...


class MotorCheckController:
    def __init__(
        self,
        *,
        environment_deny: Callable[[], str | None],
        pausables: Callable[[], list[Pausable]],
        is_e_stop_active: Callable[[], bool],
        broadcast: Callable[[dict], Awaitable[None]],
    ) -> None:
        self._environment_deny = environment_deny
        self._pausables = pausables
        self._is_e_stop_active = is_e_stop_active
        self._broadcast = broadcast

        self._sequence: Sequence | None = None
        self._task: asyncio.Task[None] | None = None
        self._abort_requested: bool = False
        self._error: str | None = None
        self._last_payload: dict | None = None

    def set_sequence(self, sequence: Sequence, *, court: Court) -> None:
        self._sequence = sequence
        sequence.set_court(court)

    def set_court(self, court: Court) -> None:
        if self._sequence is not None:
            self._sequence.set_court(court)

    @property
    def sequence(self) -> Sequence | None:
        return self._sequence

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def deny_reason(self) -> str | None:
        if self._sequence is None:
            return "動作確認シーケンスが読み込まれていません"

        environment = self._environment_deny()
        if environment is not None:
            return environment

        if self.running:
            return "既に動作確認を実行中です"
        return None

    async def start(self) -> bool:
        deny = self.deny_reason()
        if deny is not None:
            await self.report_error(deny)
            return False

        sequence = self._sequence
        assert sequence is not None

        await sequence.reset()
        self._error = None
        self._abort_requested = False

        pausables = self._pausables()

        async def _run() -> None:
            try:
                for pausable in pausables:
                    await pausable.pause(reason="動作確認")
                if self._is_e_stop_active():
                    await self.report_error("緊急停止中のため動作確認を中止しました")
                    return
                if self._abort_requested:
                    await self.report_error("動作確認を中断しました")
                    return
                await sequence.run()
                failure = sequence.last_error
                if failure is not None:
                    await self.report_error(
                        f"ステップ '{failure.label}' で失敗しました: {failure.message}"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("動作確認エラー: %s", exc)
                await self.report_error(str(exc))
            finally:
                for pausable in pausables:
                    pausable.resume()

        self._task = asyncio.create_task(_run())
        return True

    def abort(self) -> None:
        self._abort_requested = True
        if self._sequence is not None:
            self._sequence.request_stop()

    def payload(self) -> dict:
        payload = {
            "type": "motor_check_state",
            "available": self._sequence is not None,
            "blocked_reason": self.deny_reason(),
            "running": False,
            "current_step": None,
            "step_index": 0,
            "total_steps": 0,
            "steps": [],
            "error": self._error,
            "last_error": None,
            "excluded_steps": [],
        }
        if self._sequence is None:
            return payload

        progress = self._sequence.progress
        payload["running"] = self.running
        payload["current_step"] = progress["current_step"]
        payload["step_index"] = progress["step_index"]
        payload["total_steps"] = progress["total_steps"]
        payload["steps"] = progress["steps"]
        payload["last_error"] = progress["last_error"]
        payload["excluded_steps"] = [
            excluded.to_dict() for excluded in self._sequence.excluded_steps
        ]
        return payload

    async def report_error(self, message: str) -> None:
        self._error = message
        logger.warning("動作確認: %s", message)
        await self.publish()

    async def publish(self) -> None:
        payload = self.payload()
        if payload == self._last_payload:
            return
        self._last_payload = payload
        await self._broadcast(payload)
