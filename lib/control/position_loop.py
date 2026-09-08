from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from lib.axis_sync import SyncGroup
from lib.config_schema import DEFAULT_HEALTH
from lib.control.feedback import FeedbackFreshness
from lib.control.periodic import PausablePeriodicTask
from lib.control.pid import PIDController
from lib.control.sync_guard import SyncGuard
from lib.control.trajectory import TrapezoidalProfile
from lib.drivers.base import ControlMode
from lib.drivers.m3508 import CURRENT_MAX, CURRENT_MIN, M3508Driver

if TYPE_CHECKING:
    from lib.can_manager import CANManager

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_INTERVAL_S",
    "M3508PositionLoop",
    "make_position_pid",
]


DEFAULT_INTERVAL_S = 0.005

DEFAULT_MAX_DT_S = 0.05

TargetSink = Callable[[ControlMode, float], Awaitable[None]]
EStopChecker = Callable[[], bool]
SleepFunc = Callable[[float], Awaitable[None]]


def make_position_pid(
    kp: float,
    ki: float = 0.0,
    kd: float = 0.0,
    *,
    integral_limit: float | None = None,
    dead_band: float = 0.0,
) -> PIDController:
    return PIDController(
        kp,
        ki,
        kd,
        output_min=float(CURRENT_MIN),
        output_max=float(CURRENT_MAX),
        integral_limit=integral_limit,
        dead_band=dead_band,
    )


@dataclass
class _Axis:
    driver: M3508Driver
    pid: PIDController
    mode: ControlMode | None = None
    target: float | None = None
    stale: bool = field(default=False)
    last_output: float = field(default=0.0)
    saturated: bool = field(default=False)
    profile: TrapezoidalProfile | None = field(default=None)
    velocity_ff: float = field(default=0.0)
    profile_anchored: bool = field(default=False)


class M3508PositionLoop(PausablePeriodicTask):
    def __init__(
        self,
        can_manager: CANManager,
        bus_name: str,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        max_dt_s: float = DEFAULT_MAX_DT_S,
        feedback_timeout_ms: float = DEFAULT_HEALTH.feedback_timeout_ms,
        is_estop_active: EStopChecker | None = None,
        time_source: Callable[[], float] = time.monotonic,
        feedback_clock: Callable[[], float] = time.time,
        sleep: SleepFunc = asyncio.sleep,
    ) -> None:
        super().__init__(interval_s=interval_s, time_source=time_source, sleep=sleep, logger=logger)
        self._can_manager = can_manager
        self._bus_name = bus_name
        self._max_dt_s = max_dt_s
        self._is_estop_active = is_estop_active
        self._freshness = FeedbackFreshness(
            can_manager.last_feedback_at,
            timeout_ms=feedback_timeout_ms,
            clock=feedback_clock,
        )
        self._sync = SyncGuard(context=f"bus={bus_name}", logger=logger)

        self._axes: dict[str, _Axis] = {}
        self._last_tick: float = time_source()

    def add_motor(self, name: str, driver: M3508Driver, pid: PIDController) -> None:
        if name in self._axes:
            raise ValueError(f"モータ '{name}' は既に登録済み")
        for existing_name, axis in self._axes.items():
            if axis.driver.can_id == driver.can_id:
                raise ValueError(f"can_id {driver.can_id} が重複 ('{name}' と '{existing_name}')")
        self._axes[name] = _Axis(driver=driver, pid=pid)

    def add_sync_group(self, group: SyncGroup) -> None:
        for member in group.members:
            if member.name not in self._axes:
                raise ValueError(
                    f"同期グループ '{group.name}' のモータ '{member.name}' が"
                    f"このループ (bus={self._bus_name}) に未登録"
                )
        self._sync.add(group)

    def set_motion_profile(
        self, name: str, profile: TrapezoidalProfile, *, velocity_ff: float = 0.0
    ) -> None:
        if name not in self._axes:
            raise KeyError(name)
        if velocity_ff < 0.0:
            raise ValueError(f"velocity_ff は 0 以上: {velocity_ff}")
        axis = self._axes[name]
        axis.profile = profile
        axis.velocity_ff = float(velocity_ff)
        axis.profile_anchored = False

    @property
    def bus_name(self) -> str:
        return self._bus_name

    @property
    def motor_names(self) -> tuple[str, ...]:
        return tuple(self._axes)

    @property
    def sync_group_names(self) -> tuple[str, ...]:
        return self._sync.group_names

    @property
    def sync_violations(self) -> frozenset[str]:
        return self._sync.violations

    def _label(self) -> str:
        return f"位置制御ループ (bus={self._bus_name})"

    def pid(self, name: str) -> PIDController:
        return self._axes[name].pid

    def _paired_with(self, name: str) -> tuple[str, ...]:
        group_name = self._sync.group_of(name)
        if group_name is None:
            return (name,)
        return self._sync.members_of(group_name)

    def target(self, name: str) -> float | None:
        return self._axes[name].target

    def is_saturated(self, name: str) -> bool:
        return self._axes[name].saturated

    async def set_target(self, name: str, mode: ControlMode, value: float) -> None:
        axis = self._axes[name]

        if mode is ControlMode.POSITION:
            if axis.mode is not ControlMode.POSITION:
                axis.pid.reset()
            axis.mode = ControlMode.POSITION
            axis.target = float(value)
            if axis.profile is not None and axis.profile_anchored:
                axis.profile.retarget(axis.target)
            return

        if mode is ControlMode.CURRENT:
            axis.pid.reset()
            axis.profile_anchored = False
            axis.mode = ControlMode.CURRENT
            axis.target = float(value)
            return

        raise ValueError(
            f"M3508 位置制御ループは POSITION / CURRENT のみ対応 (受け取った: {mode.name})"
        )

    def clear_target(self, name: str) -> None:
        self._reset_axis(self._axes[name])

    @staticmethod
    def _reset_axis(axis: _Axis) -> None:
        axis.mode = None
        axis.target = None
        axis.pid.reset()
        axis.last_output = 0.0
        axis.saturated = False
        axis.profile_anchored = False

    def set_origin_here(self, name: str) -> None:
        self._capture_origin(self._paired_with(name))

    def set_group_origin_here(self, name: str) -> None:
        self._capture_origin(self._sync.members_of(name))

    def _capture_origin(self, names: tuple[str, ...]) -> None:
        for motor in names:
            self._axes[motor].driver.reset_multi_turn_origin()
        for motor in names:
            self.clear_target(motor)

    def reset_sync_violation(self, name: str | None = None) -> None:
        self._sync.reset(name)

    def target_sink(self, name: str) -> TargetSink:
        if name not in self._axes:
            raise KeyError(name)

        async def sink(mode: ControlMode, value: float) -> None:
            await self.set_target(name, mode, value)

        return sink

    def target_sinks(self) -> dict[str, TargetSink]:
        return {name: self.target_sink(name) for name in self._axes}

    async def _step_locked(self) -> None:
        dt = self._elapsed()

        estop = self._is_estop_active is not None and self._is_estop_active()
        if estop:
            self._disable_all()

        if self._paused:
            return

        if estop:
            await self._send([0, 0, 0, 0])
            return

        wall_now = self._freshness.now()
        stale = {name: self._freshness.is_stale(name, wall_now) for name in self._axes}
        blocked = self._sync.blocked(stale=stale, position_of=self._feedback_position)
        corrections = self._sync.corrections(
            position_of=self._feedback_position,
            skip_groups=(
                blocked | self._open_loop_groups() | self._sync.skewed_groups(target_of=self.target)
            ),
        )

        currents = [0, 0, 0, 0]
        for name, axis in self._axes.items():
            currents[axis.driver.can_id - 1] = self._compute_current(
                name,
                axis,
                dt,
                stale=stale[name],
                blocked=self._sync.group_of(name) in blocked,
                correction=corrections.get(name, 0.0),
            )

        await self._send(currents)

    async def send_stop_frame(self) -> None:
        self._disable_all()
        await self._send([0, 0, 0, 0])

    def _on_resume(self) -> None:
        for axis in self._axes.values():
            axis.pid.reset()
            axis.profile_anchored = False
        self._last_tick = self._time_source()

    async def _on_run_start(self) -> None:
        self._last_tick = self._time_source()

    async def _on_tick_error(self) -> None:
        self._log.exception("tick", "位置制御ループの周期処理で例外 (bus=%s)", self._bus_name)
        self._discard_profile_anchors()
        await self._send_zero_safely()

    async def _on_run_exit(self) -> None:
        self._discard_profile_anchors()
        await self._send_zero_safely()

    def _discard_profile_anchors(self) -> None:
        for axis in self._axes.values():
            axis.pid.reset()
            axis.profile_anchored = False

    def _elapsed(self) -> float:
        now = self._time_source()
        dt = now - self._last_tick
        self._last_tick = now
        if dt < 0.0:
            return 0.0
        return min(dt, self._max_dt_s)

    def _feedback_position(self, name: str) -> float:
        return self._axes[name].driver.feedback_position()

    def _open_loop_groups(self) -> frozenset[str]:
        return frozenset(
            group
            for group in self._sync.group_names
            if not all(
                self._axes[member].mode is ControlMode.POSITION
                and self._axes[member].target is not None
                for member in self._sync.members_of(group)
            )
        )

    def _compute_current(
        self,
        name: str,
        axis: _Axis,
        dt: float,
        *,
        stale: bool,
        blocked: bool,
        correction: float,
    ) -> int:
        output, closed_loop = self._control_output(
            name, axis, dt, stale=stale, blocked=blocked, correction=correction
        )

        axis.last_output = output
        axis.saturated = closed_loop and self._is_saturated(axis, output)
        return round(output)

    @staticmethod
    def _is_saturated(axis: _Axis, output: float) -> bool:
        return output >= axis.pid.output_max - 1.0 or output <= axis.pid.output_min + 1.0

    def _control_output(
        self, name: str, axis: _Axis, dt: float, *, stale: bool, blocked: bool, correction: float
    ) -> tuple[float, bool]:
        if axis.target is None or axis.mode is None:
            return 0.0, False

        if stale:
            if not axis.stale:
                axis.stale = True
                logger.warning(
                    "フィードバック途絶のため電流 0 に落とす (motor=%s, bus=%s)",
                    name,
                    self._bus_name,
                )
            axis.pid.reset()
            axis.profile_anchored = False
            return 0.0, False

        if axis.stale:
            axis.stale = False
            logger.info("フィードバック復帰 (motor=%s, bus=%s)", name, self._bus_name)

        if blocked:
            axis.pid.reset()
            axis.profile_anchored = False
            return 0.0, False

        if axis.mode is ControlMode.CURRENT:
            return float(axis.target), False

        setpoint = axis.target
        feedforward = correction
        if axis.profile is not None:
            if not axis.profile_anchored:
                self._anchor_profile(axis)
            setpoint, reference_velocity = axis.profile.advance(dt)
            feedforward += axis.velocity_ff * reference_velocity

        return (
            axis.pid.update(
                setpoint,
                axis.driver.multi_turn_position,
                dt,
                feedforward=feedforward,
            ),
            True,
        )

    @staticmethod
    def _anchor_profile(axis: _Axis) -> None:
        profile = axis.profile
        if profile is None:
            return
        profile.reset(axis.driver.multi_turn_position)
        if axis.target is not None:
            profile.retarget(axis.target)
        axis.profile_anchored = True

    def _disable_all(self) -> None:
        for axis in self._axes.values():
            self._reset_axis(axis)

    async def _send(self, currents: list[int]) -> None:
        await self._can_manager.send_to_bus(
            self._bus_name, M3508Driver.encode_current_frame(currents)
        )

    async def _send_zero_safely(self) -> None:
        try:
            await self._send([0, 0, 0, 0])
        except asyncio.CancelledError:
            raise
        except Exception:
            self._log.exception("zero", "0 電流フレームの送信に失敗 (bus=%s)", self._bus_name)
