from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Sequence
from typing import TYPE_CHECKING

from lib.control.periodic import PausablePeriodicTask
from lib.drivers.dm3520 import Dm3520Driver
from lib.drivers.edulite05 import Edulite05Driver
from lib.sequence.motors import MotorHandle

if TYPE_CHECKING:
    from lib.can_manager import CANManager

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_INTERVAL_S",
    "FIRMWARE_COMMAND_TIMEOUT_S",
    "GenericTargetRefresher",
    "QueryDrivenTargetRefresher",
    "TargetRefresher",
    "hold_target_refresh",
]

FIRMWARE_COMMAND_TIMEOUT_S = 0.5

DEFAULT_INTERVAL_S = 0.05

EStopChecker = Callable[[], bool]
SleepFunc = Callable[[float], Awaitable[None]]

_QUERY_DRIVEN_DRIVERS = (Dm3520Driver, Edulite05Driver)


class _TargetRefresherBase(PausablePeriodicTask):
    def __init__(
        self,
        handles: Sequence[MotorHandle],
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        is_estop_active: EStopChecker | None = None,
        time_source: Callable[[], float] = time.monotonic,
        sleep: SleepFunc = asyncio.sleep,
    ) -> None:
        super().__init__(interval_s=interval_s, time_source=time_source, sleep=sleep, logger=logger)
        self._handles = tuple(handles)
        self._is_estop_active = is_estop_active

    @property
    def motor_names(self) -> tuple[str, ...]:
        return tuple(handle.name for handle in self._handles)

    def clear_targets(self) -> None:
        for handle in self._handles:
            handle.clear_target()

    def clear_target(self, name: str) -> None:
        for handle in self._handles:
            if handle.name == name:
                handle.clear_target()
                return

    async def _on_run_exit(self) -> None:
        """降り際に停止指令も無励磁化も送らない。"""


class GenericTargetRefresher(_TargetRefresherBase):
    def _label(self) -> str:
        return f"目標値再送 ({', '.join(self.motor_names) or '対象なし'})"

    async def _step_locked(self) -> None:
        if self._paused:
            return
        if self._is_estop_active is not None and self._is_estop_active():
            return

        for handle in self._handles:
            try:
                await handle.resend_target()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log.exception(
                    f"send:{handle.name}",
                    "目標値の再送に失敗 (motor=%s)",
                    handle.name,
                )


class QueryDrivenTargetRefresher(_TargetRefresherBase):
    def __init__(
        self,
        handles: Sequence[MotorHandle],
        can_manager: CANManager,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        is_estop_active: EStopChecker | None = None,
        time_source: Callable[[], float] = time.monotonic,
        sleep: SleepFunc = asyncio.sleep,
    ) -> None:
        super().__init__(
            handles,
            interval_s=interval_s,
            is_estop_active=is_estop_active,
            time_source=time_source,
            sleep=sleep,
        )
        self._can_manager = can_manager
        self._idle_targets: dict[str, float] = {}

    def _label(self) -> str:
        return f"問い合わせ駆動 目標値再送 ({', '.join(self.motor_names) or '対象なし'})"

    async def _step_locked(self) -> None:
        if self._paused:
            return

        for handle in self._handles:
            try:
                if await handle.resend_target():
                    self._idle_targets.pop(handle.name, None)
                    continue
                await self._send_idle_target(handle)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log.exception(
                    f"send:{handle.name}",
                    "DM3520 への目標値送信に失敗 (motor=%s)",
                    handle.name,
                )

    async def _send_idle_target(self, handle: MotorHandle) -> None:
        driver = handle.driver
        if not isinstance(driver, _QUERY_DRIVEN_DRIVERS):
            return

        # レンジが未確認のあいだの実測位置は比例倍で読めている可能性があり、
        # 「今の姿勢を保て」に使うと励磁した瞬間にその値へ走る。
        if driver.activation_block_reason() is not None:
            self._idle_targets.pop(handle.name, None)
            return

        if self._is_estop_active is not None and self._is_estop_active():
            value = driver.idle_target_value()
            self._idle_targets.pop(handle.name, None)
        else:
            value = self._idle_targets.setdefault(handle.name, driver.idle_target_value())

        await self._can_manager.send(handle.name, driver.encode_target(driver.mode, value))

    def clear_targets(self) -> None:
        super().clear_targets()
        self._idle_targets.clear()

    def clear_target(self, name: str) -> None:
        super().clear_target(name)
        self._idle_targets.pop(name, None)


TargetRefresher = GenericTargetRefresher | QueryDrivenTargetRefresher


@contextlib.asynccontextmanager
async def hold_target_refresh(
    refreshers: Sequence[TargetRefresher], names: Collection[str], *, reason: str
) -> AsyncIterator[None]:
    """モータの意味が変わる操作のあいだ再送を黙らせ、抜けるときに目標とラッチを捨てる。

    原点の付け替えと緊急停止解除の再励磁が使う。どちらも操作の前に取った目標・ラッチが
    操作の後には信用できない (付け替えは論理 0 の指す物理位置が動く / 解除は停止中の
    実測位置が信用できない) ので、操作中は取らせず、抜けた後に取り直させる。

    捨てるだけでは足りない —— 再送は目標が無ければ「今の姿勢を保て」のラッチを取り直す
    ので、黙らせておかないと操作の直前に取ったラッチが操作の直後に別の位置を指す。
    捨てるのは対象モータだけ。全台へ広げると無関係な軸の `wait_reached` まで中断させる。
    """
    wanted = set(names)
    targeted = [r for r in refreshers if wanted & set(r.motor_names)]
    for refresher in targeted:
        await refresher.pause(reason=reason)
    try:
        yield
    finally:
        # 捨ててから再開する。逆にすると、捨てるまでの 1 周期で古い目標が送られる
        for refresher in targeted:
            for name in wanted.intersection(refresher.motor_names):
                refresher.clear_target(name)
            refresher.resume()
