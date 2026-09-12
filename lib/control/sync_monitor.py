from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING

from lib.axis_sync import SyncGroup
from lib.config_schema import DEFAULT_HEALTH
from lib.control.feedback import FeedbackFreshness
from lib.control.periodic import PeriodicTask

if TYPE_CHECKING:
    from lib.drivers.base import MotorDriver

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_INTERVAL_S", "SyncMonitor"]

DEFAULT_INTERVAL_S = 0.02

DEFAULT_VIOLATION_SAMPLES = 2

ViolationHandler = Callable[[str, float, int], None]
FeedbackClock = Callable[[], float]
SleepFunc = Callable[[float], Awaitable[None]]


class SyncMonitor(PeriodicTask):
    def __init__(
        self,
        groups: Sequence[SyncGroup],
        drivers: Mapping[str, MotorDriver],
        *,
        last_feedback_at: Callable[[str], float | None],
        feedback_timeout_ms: float = DEFAULT_HEALTH.feedback_timeout_ms,
        interval_s: float = DEFAULT_INTERVAL_S,
        violation_samples: int = DEFAULT_VIOLATION_SAMPLES,
        on_violation: ViolationHandler | None = None,
        feedback_clock: FeedbackClock = time.time,
        time_source: Callable[[], float] = time.monotonic,
        sleep: SleepFunc = asyncio.sleep,
    ) -> None:
        super().__init__(interval_s=interval_s, time_source=time_source, sleep=sleep, logger=logger)
        self._groups = tuple(groups)
        self._drivers = drivers
        self._freshness = FeedbackFreshness(
            last_feedback_at, timeout_ms=feedback_timeout_ms, clock=feedback_clock
        )
        self._violation_samples = max(1, violation_samples)
        self._on_violation = on_violation

        self._counts: dict[str, int] = {}
        self._violated: set[str] = set()
        self._suspended: dict[str, int] = {}
        self._unreferenced_logged: set[str] = set()
        # 緊急停止の解除で `reset()` が走ってもここだけは残す。解除するたび即再発する
        # 状態を「何回目か」として理由文へ載せられるのは、ラッチをまたぐこの数だけ。
        self._retrips: dict[str, int] = {}

    @property
    def group_names(self) -> tuple[str, ...]:
        return tuple(group.name for group in self._groups)

    @property
    def groups(self) -> tuple[SyncGroup, ...]:
        return self._groups

    @property
    def violated(self) -> frozenset[str]:
        return frozenset(self._violated)

    def reset(self) -> None:
        self._counts.clear()
        self._violated.clear()

    @contextlib.contextmanager
    def suspend_group(self, name: str, *, reason: str = "原点の付け替え") -> Iterator[None]:
        if name not in self.group_names:
            raise KeyError(f"同期監視は軸 '{name}' を持っていません")

        self._suspended[name] = self._suspended.get(name, 0) + 1
        logger.info("同期監視を一時停止 (axis=%s, 理由=%s)", name, reason)
        try:
            yield
        finally:
            depth = self._suspended.get(name, 1) - 1
            if depth > 0:
                self._suspended[name] = depth
            else:
                self._suspended.pop(name, None)
                self._counts[name] = 0
                logger.info("同期監視を再開 (axis=%s)", name)

    @contextlib.contextmanager
    def suspend_all(self, *, reason: str) -> Iterator[None]:
        with contextlib.ExitStack() as stack:
            for name in self.group_names:
                stack.enter_context(self.suspend_group(name, reason=reason))
            yield

    def is_suspended(self, name: str) -> bool:
        return self._suspended.get(name, 0) > 0

    def _label(self) -> str:
        return f"同期監視 ({', '.join(self.group_names) or '対象なし'})"

    def step(self) -> None:
        now = self._freshness.now()
        for group in self._groups:
            self._check_group(group, now)

    async def _tick(self) -> None:
        self.step()

    def _check_group(self, group: SyncGroup, now: float) -> None:
        if self.is_suspended(group.name):
            return

        positions = self._fresh_positions(group, now)
        deviation = group.violation(positions)
        if deviation is None:
            self._counts[group.name] = 0
            return

        count = self._counts.get(group.name, 0) + 1
        self._counts[group.name] = count
        if count < self._violation_samples:
            return

        if group.name in self._violated:
            return

        self._violated.add(group.name)
        retrips = self._retrips.get(group.name, 0) + 1
        self._retrips[group.name] = retrips
        logger.error(
            "同期ずれを検出 (axis=%s, deviation=%.3f, tolerance=%.3f, 通算=%d 回目)",
            group.name,
            deviation,
            group.tolerance,
            retrips,
        )
        self._notify(group.name, deviation, retrips)

    def _fresh_positions(self, group: SyncGroup, now: float) -> dict[str, float]:
        positions: dict[str, float] = {}
        for member in group.members:
            driver = self._drivers.get(member.name)
            if driver is None:
                continue
            if self._freshness.is_stale(member.name, now):
                continue
            # 原点が未確立のうちに報告される論理値は物理姿勢を指さないので、偏差判定の
            # 材料にしない (メンバーが 2 未満になり deviation は None になる)
            if not driver.position_reference_established():
                self._note_unreferenced(member.name)
                continue
            positions[member.name] = driver.feedback_position()
        return positions

    def _note_unreferenced(self, motor_name: str) -> None:
        # 20ms 周期なので、除外が始まった 1 回だけ残す
        if motor_name in self._unreferenced_logged:
            return
        self._unreferenced_logged.add(motor_name)
        logger.info("原点が未確立のため同期判定から除外 (motor=%s)", motor_name)

    def _notify(self, group_name: str, deviation: float, retrips: int) -> None:
        if self._on_violation is None:
            return
        try:
            self._on_violation(group_name, deviation, retrips)
        except Exception:
            logger.exception("同期ずれハンドラで例外 (axis=%s)", group_name)
