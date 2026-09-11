from __future__ import annotations

import abc
import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import can

__all__ = [
    "JITTER_OVERRUN_MARGIN",
    "LOG_THROTTLE_S",
    "LogThrottle",
    "PausablePeriodicTask",
    "PeriodicTask",
    "is_bus_send_failure",
]

LOG_THROTTLE_S = 1.0


def is_bus_send_failure(exc: BaseException) -> bool:
    """CAN が送れない状態か (CANManager がバス単位で 1 行に畳んで報告済み)。

    非常停止でモータ電源が落ちている間は全バスの送信が毎周期失敗する。周期タスクごとに
    スタックトレースを出すと他のログが埋まるので、ここで判定して黙る。
    """
    return isinstance(exc, can.CanError)


JITTER_OVERRUN_MARGIN = 0.5

SleepFunc = Callable[[float], Awaitable[None]]
TimeSource = Callable[[], float]


class LogThrottle:
    def __init__(
        self,
        logger: logging.Logger,
        *,
        time_source: TimeSource = time.monotonic,
        interval_s: float = LOG_THROTTLE_S,
    ) -> None:
        self._logger = logger
        self._time_source = time_source
        self._interval_s = interval_s
        self._last_at: dict[str, float] = {}

    def exception(self, key: str, message: str, *args: object) -> None:
        now = self._time_source()
        last = self._last_at.get(key)
        if last is not None and now - last < self._interval_s:
            return
        self._last_at[key] = now
        self._logger.error(message, *args, exc_info=True)

    def warning(self, key: str, message: str, *args: object) -> None:
        now = self._time_source()
        last = self._last_at.get(key)
        if last is not None and now - last < self._interval_s:
            return
        self._last_at[key] = now
        self._logger.warning(message, *args)


class PeriodicTask(abc.ABC):
    def __init__(
        self,
        *,
        interval_s: float,
        time_source: TimeSource = time.monotonic,
        sleep: SleepFunc = asyncio.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self._interval_s = interval_s
        self._time_source = time_source
        self._sleep = sleep
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._logger = logger if logger is not None else logging.getLogger(type(self).__module__)
        self._log = LogThrottle(self._logger, time_source=time_source)

        self._last_tick_at: float | None = None
        self._jitter_overrun_count = 0
        self._worst_jitter_s = 0.0
        self._jitter_threshold_s = interval_s * JITTER_OVERRUN_MARGIN

    @abc.abstractmethod
    async def _tick(self) -> None:
        """1 周期分の処理。"""

    @abc.abstractmethod
    def _label(self) -> str:
        """ログに出すこのタスクの識別子 (例: ``位置制御ループ (bus=can_m3508)``)。"""

    async def _on_run_start(self) -> None:  # noqa: B027  (任意フック。既定は何もしない)
        """ループに入る直前のフック (時刻基準の取り直しなど)。"""

    async def _on_tick_error(self, exc: BaseException) -> None:
        if is_bus_send_failure(exc):
            return
        self._log.exception("tick", "%s の周期処理で例外", self._label())

    async def _on_run_exit(self) -> None:  # noqa: B027  (任意フック。既定は何もしない)
        """ループを降りるときのフック。異常終了・キャンセルでも必ず通る。"""

    def _observe_tick_start(self, now: float) -> None:
        last = self._last_tick_at
        self._last_tick_at = now
        if last is None:
            return

        # 実機の asyncio.sleep は必ず数百 us オーバーシュートするので、公称どおりに
        # 回っている機体でも worst_jitter_s には小さな正の値が入る。
        jitter = (now - last) - self._interval_s
        if jitter > self._worst_jitter_s:
            self._worst_jitter_s = jitter
        if jitter > self._jitter_threshold_s:
            self._jitter_overrun_count += 1
            self._log.warning(
                "jitter",
                "%s の実周期が乱れています (実測 %.1fms / 公称 %.1fms)",
                self._label(),
                (now - last) * 1000.0,
                self._interval_s * 1000.0,
            )

    @property
    def jitter_overrun_count(self) -> int:
        return self._jitter_overrun_count

    @property
    def worst_jitter_s(self) -> float:
        return self._worst_jitter_s

    def log_jitter_summary(self) -> None:
        if self._jitter_overrun_count == 0:
            self._logger.info(
                "%s の実周期: しきい値超過なし (最悪の遅れ %.1fms / 公称周期 %.1fms)",
                self._label(),
                self._worst_jitter_s * 1000.0,
                self._interval_s * 1000.0,
            )
        else:
            self._logger.info(
                "%s の実周期: しきい値超過 %d 回 (最悪の遅れ %.1fms / 公称周期 %.1fms)",
                self._label(),
                self._jitter_overrun_count,
                self._worst_jitter_s * 1000.0,
                self._interval_s * 1000.0,
            )

    def reset_jitter_stats(self) -> None:
        self._jitter_overrun_count = 0
        self._worst_jitter_s = 0.0

    async def run(self) -> None:
        await self._on_run_start()
        next_at = self._time_source() + self._interval_s
        try:
            while not self._stop_event.is_set():
                self._observe_tick_start(self._time_source())
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._on_tick_error(exc)

                delay = next_at - self._time_source()
                if delay > 0.0:
                    next_at += self._interval_s
                else:
                    next_at = self._time_source() + self._interval_s
                    delay = 0.0
                await self._sleep(delay)
        finally:
            await self._on_run_exit()

    def start(self) -> None:
        if self.is_running:
            raise RuntimeError(f"{self._label()} は既に実行中です")
        self._stop_event.clear()
        self._last_tick_at = None
        self._task = asyncio.create_task(self.run())

    def request_stop(self) -> None:
        self._stop_event.set()

    async def stop(self) -> None:
        self.request_stop()
        task = self._task
        self._task = None
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            self._logger.exception("%s は既に異常終了していました", self._label())

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def set_sleep(self, sleep: SleepFunc) -> None:
        self._sleep = sleep


class PausablePeriodicTask(PeriodicTask):
    def __init__(
        self,
        *,
        interval_s: float,
        time_source: TimeSource = time.monotonic,
        sleep: SleepFunc = asyncio.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(interval_s=interval_s, time_source=time_source, sleep=sleep, logger=logger)
        self._paused = False
        self._step_lock = asyncio.Lock()

    @abc.abstractmethod
    async def _step_locked(self) -> None:
        """``_step_lock`` を保持した状態で行う 1 周期分の処理。"""

    def _on_resume(self) -> None:
        """一時停止解除時のフック (積分のリセットなど)。"""

    async def step(self) -> None:
        async with self._step_lock:
            await self._step_locked()

    async def _tick(self) -> None:
        await self.step()

    @property
    def is_paused(self) -> bool:
        return self._paused

    async def pause(self, *, reason: str = "") -> None:
        async with self._step_lock:
            if self._paused:
                return
            self._paused = True
            self._logger.info(
                "%s を一時停止%s",
                self._label(),
                f" (理由={reason})" if reason else "",
            )

    def resume(self) -> None:
        if not self._paused:
            return
        self._on_resume()
        self._paused = False
        self._logger.info("%s を再開", self._label())
