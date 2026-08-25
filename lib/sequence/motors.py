from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from typing import TYPE_CHECKING

from lib.drivers.base import ControlMode

if TYPE_CHECKING:
    from lib.can_manager import CANManager
    from lib.drivers.base import MotorDriver, MotorState

# 到達待ちのポーリング間隔。CAN フィードバックは 1kHz 前後で届くため
# 10ms 周期なら取りこぼしがなく、asyncio ループへの負荷も無視できる
_DEFAULT_POLL_INTERVAL_S = 0.01

TargetSink = Callable[[ControlMode, float], Awaitable[None]]
EStopChecker = Callable[[], bool]


class EStopActiveError(RuntimeError):
    """緊急停止中にモータ指令を出そうとしたときに送出される。"""


class MotorHandle:
    """1 モータへの目標値送信と到達待ちを担うハンドル。"""

    def __init__(
        self,
        name: str,
        driver: MotorDriver,
        can_manager: CANManager,
        *,
        is_estop_active: EStopChecker | None = None,
        target_sink: TargetSink | None = None,
        poll_interval: float = _DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._name = name
        self._driver = driver
        self._can_manager = can_manager
        self._is_estop_active = is_estop_active
        self._target_sink = target_sink
        self._poll_interval = poll_interval
        self._target: float | None = None
        self._mode: ControlMode | None = None

    # ---- 参照系 ----

    @property
    def name(self) -> str:
        return self._name

    @property
    def driver(self) -> MotorDriver:
        return self._driver

    @property
    def state(self) -> MotorState:
        return self._driver.state

    @property
    def target(self) -> float | None:
        return self._target

    @property
    def mode(self) -> ControlMode | None:
        return self._mode

    @property
    def has_target(self) -> bool:
        return self._target is not None and self._mode is not None

    def set_target_sink(self, sink: TargetSink | None) -> None:
        """目標値の送り先を差し替える (PC 側 PID ループの後付け用)。"""
        self._target_sink = sink

    # ---- 指令系 ----

    async def set_position(self, value: float) -> None:
        await self.set_target(ControlMode.POSITION, value)

    async def set_velocity(self, value: float) -> None:
        await self.set_target(ControlMode.VELOCITY, value)

    async def set_current(self, value: float) -> None:
        await self.set_target(ControlMode.CURRENT, value)

    async def set_duty(self, value: float) -> None:
        await self.set_target(ControlMode.DUTY, value)

    async def set_target(self, mode: ControlMode, value: float) -> None:
        """目標値を送信する。緊急停止中は送信せず EStopActiveError を送出する。"""
        # 緊急停止はサーバ層でも遮断しているが、ロボットが動く経路には多重に安全装置を置く
        if self._is_estop_active is not None and self._is_estop_active():
            raise EStopActiveError(f"緊急停止中のためモータ '{self._name}' に指令できません")

        value = float(value)
        if self._target_sink is not None:
            # M3508 のようにドライバ単体では目標モードを表現できないモータ向けの差し込み口。
            # PC 側の制御ループが目標値を受け取り、実際の CAN 送信を代行する
            await self._target_sink(mode, value)
        else:
            msg = self._driver.encode_target(mode, value)
            await self._can_manager.send(self._name, msg)

        self._mode = mode
        self._target = value

    def clear_target(self) -> None:
        """到達待ちの対象から外す。"""
        self._target = None
        self._mode = None

    # ---- 到達判定 ----

    def is_reached(self, *, tolerance: float | None = None) -> bool:
        if self._target is None or self._mode is None:
            return True
        return self._driver.is_target_reached(self._target, self._mode, tolerance=tolerance)

    async def wait_reached(
        self,
        *,
        tolerance: float | None = None,
        timeout: float | None = None,
    ) -> bool:
        """目標到達を待つ。到達すれば True、タイムアウトなら False。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.is_reached(tolerance=tolerance):
                return True
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                await asyncio.sleep(min(self._poll_interval, remaining))
            else:
                await asyncio.sleep(self._poll_interval)


class MotorGroup:
    """モータ名でハンドルを引くコンテナ。シーケンスからは属性アクセスで使う。"""

    def __init__(self, handles: Mapping[str, MotorHandle] | None = None) -> None:
        self._handles: dict[str, MotorHandle] = dict(handles) if handles else {}

    def add(self, handle: MotorHandle) -> None:
        self._handles[handle.name] = handle

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._handles)

    @property
    def handles(self) -> tuple[MotorHandle, ...]:
        return tuple(self._handles.values())

    def items(self) -> list[tuple[str, MotorHandle]]:
        return list(self._handles.items())

    def __getitem__(self, name: str) -> MotorHandle:
        return self._handles[name]

    def __contains__(self, name: object) -> bool:
        return name in self._handles

    def __iter__(self) -> Iterator[str]:
        return iter(self._handles)

    def __len__(self) -> int:
        return len(self._handles)

    def __getattr__(self, name: str) -> MotorHandle:
        # 内部属性の探索まで拾うと __init__ 前の参照や copy/pickle が壊れる
        if name.startswith("_"):
            raise AttributeError(name)
        handles = self.__dict__.get("_handles", {})
        if name in handles:
            return handles[name]
        # 試合中に原因不明で止まらないよう、利用可能なモータ名を必ず添える
        available = ", ".join(handles) or "(なし)"
        raise AttributeError(f"モータ '{name}' は存在しません。利用可能なモータ: {available}")

    async def wait_all_reached(
        self,
        *,
        tolerance: float | None = None,
        timeout: float | None = None,
    ) -> bool:
        """目標値が設定されている全モータの到達を待つ。1 つでも未到達なら False。"""
        pending = [handle for handle in self._handles.values() if handle.has_target]
        if not pending:
            return True
        results = await asyncio.gather(
            *(handle.wait_reached(tolerance=tolerance, timeout=timeout) for handle in pending)
        )
        return all(results)

    def clear_targets(self) -> None:
        for handle in self._handles.values():
            handle.clear_target()


def build_motor_group(
    can_manager: CANManager,
    motors: Mapping[str, MotorDriver],
    *,
    is_estop_active: EStopChecker | None = None,
    target_sinks: Mapping[str, TargetSink] | None = None,
) -> MotorGroup:
    """CANManager とモータ辞書から MotorGroup を組み立てる。"""
    group = MotorGroup()
    for name, driver in motors.items():
        group.add(
            MotorHandle(
                name,
                driver,
                can_manager,
                is_estop_active=is_estop_active,
                target_sink=(target_sinks or {}).get(name),
            )
        )
    return group
