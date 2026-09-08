from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING

from lib.drivers.base import ControlMode
from lib.sequence.positions import PositionLookupError

if TYPE_CHECKING:
    from lib.can_manager import CANManager
    from lib.drivers.base import MotorDriver, MotorState
    from lib.sequence.positions import AxisSpec

_DEFAULT_POLL_INTERVAL_S = 0.01

TargetSink = Callable[[ControlMode, float], Awaitable[None]]
EStopChecker = Callable[[], bool]


class EStopActiveError(RuntimeError):
    """緊急停止中にモータ指令を出そうとしたときに送出される。"""


class WaitInterruptedError(RuntimeError):
    """到達待ちの最中に目標が消えた。"""


class MotorHandle:
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

    async def set_target(self, mode: ControlMode, value: float) -> None:
        if self._is_estop_active is not None and self._is_estop_active():
            raise EStopActiveError(f"緊急停止中のためモータ '{self._name}' に指令できません")

        value = float(value)
        await self._dispatch(mode, value)

        self._mode = mode
        self._target = value

    async def resend_target(self) -> bool:
        if self._target is None or self._mode is None:
            return False
        if self._is_estop_active is not None and self._is_estop_active():
            return False

        await self._dispatch(self._mode, self._target)
        return True

    async def _dispatch(self, mode: ControlMode, value: float) -> None:
        if self._target_sink is not None:
            await self._target_sink(mode, value)
        else:
            await self._can_manager.send(self._name, self._driver.encode_target(mode, value))

    def clear_target(self) -> None:
        self._target = None
        self._mode = None

    def is_reached(self, *, tolerance: float | None = None) -> bool:
        if self._target is None or self._mode is None:
            return True
        return self._driver.is_target_reached(self._target, self._mode, tolerance=tolerance)

    async def wait_reached(
        self,
        *,
        tolerance: float | None = None,
        timeout: float | None = None,
        expect_target: bool = False,
    ) -> bool:
        had_target = self.has_target or expect_target
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if had_target and not self.has_target:
                raise WaitInterruptedError(
                    f"モータ '{self._name}' の到達待ちが中断されました"
                    " (緊急停止などで目標値がクリアされました)"
                )
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

    def __getitem__(self, name: str) -> MotorHandle:
        return self._handles[name]

    def __contains__(self, name: object) -> bool:
        return name in self._handles

    def __iter__(self) -> Iterator[str]:
        return iter(self._handles)

    def __len__(self) -> int:
        return len(self._handles)

    def __getattr__(self, name: str) -> MotorHandle:
        if name.startswith("_"):
            raise AttributeError(name)
        handles = self.__dict__.get("_handles", {})
        if name in handles:
            return handles[name]
        available = ", ".join(handles) or "(なし)"
        raise AttributeError(f"モータ '{name}' は存在しません。利用可能なモータ: {available}")


class AxisHandle:
    def __init__(self, spec: AxisSpec, handles: Sequence[MotorHandle]) -> None:
        self._spec = spec
        self._handles = tuple(handles)
        self._motors = {motor.name: motor for motor in spec.motors}

    @property
    def name(self) -> str:
        return self._spec.name

    async def set_target_value(self, commands: Mapping[str, float]) -> None:
        try:
            values = [(handle, commands[handle.name]) for handle in self._handles]
        except KeyError as exc:
            raise KeyError(
                f"軸 '{self.name}' のモータ {exc.args[0]!r} に対する指令値がありません"
            ) from exc

        results = await asyncio.gather(
            *(handle.set_target(self._spec.command_mode, value) for handle, value in values),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            for (handle, _), result in zip(values, results, strict=True):
                if not isinstance(result, BaseException):
                    handle.clear_target()
            raise failures[0]

    async def wait_reached(
        self, *, timeout: float | None = None, expect_target: bool = False
    ) -> bool:
        if self._spec.command_mode is not ControlMode.POSITION:
            if self._spec.settle_s > 0.0:
                await asyncio.sleep(self._spec.settle_s)
            return True

        results = await asyncio.gather(
            *(
                handle.wait_reached(
                    tolerance=self._tolerance_for(handle.name),
                    timeout=timeout,
                    expect_target=expect_target,
                )
                for handle in self._handles
            )
        )
        return all(results)

    def observed_value(self) -> float:
        if self._spec.command_mode is not ControlMode.POSITION:
            raise PositionLookupError(
                f"軸 '{self.name}' は位置フィードバックを持ちません"
                f" (command_mode={self._spec.command_mode.value})"
            )
        return self._spec.to_value(
            {handle.name: handle.driver.feedback_position() for handle in self._handles}
        )

    def sync_violation(self) -> float | None:
        group = self._spec.sync_group
        if group is None:
            return None
        return group.violation(
            {handle.name: handle.driver.feedback_position() for handle in self._handles}
        )

    def _tolerance_for(self, motor_name: str) -> float | None:
        if self._spec.tolerance is None:
            return None
        return self._motors[motor_name].to_tolerance(self._spec.tolerance)


def build_motor_group(
    can_manager: CANManager,
    motors: Mapping[str, MotorDriver],
    *,
    is_estop_active: EStopChecker | None = None,
    target_sinks: Mapping[str, TargetSink] | None = None,
) -> MotorGroup:
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
