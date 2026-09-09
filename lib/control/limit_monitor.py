"""移動中の可動端インターロック。**指令を出す瞬間の歯止めだけでは届かない。**

`MotionGuard` は `AxisHandle.set_target_value` から呼ばれる。つまり **1 通の指令を
書くその瞬間しか見ていない**。小刻みなジョグは 1 歩ごとに判定を通るので端で止まるが、
遠い目標を 1 回書くと、その後スイッチが立っても誰も見ていない —— ドライバは書かれた
目標へサーボし続け、機構はスイッチを踏み越える (2026-09-09 実機: `sub_y_axis` へ
`-450` を 1 回指令し、後端の作動点 `-447.0` を 3mm 越えた)。

そこでこのタスクが周期的に「**今の実測位置から、今書かれている目標へ向かう向き**」の
端センサを見て、押されている / 読めていないなら**その場の実測位置を目標へ書き直す**。
目標そのものを書き換えないと、問い合わせ駆動の再送 (`TargetRefresher`) が古い目標を
送り直して押し込み続ける。

**見るのは可動端だけ。** `max_step` は「1 指令で実測位置から離れてよい量」の判定なので、
移動中の実測位置に当てると長距離移動の途中で必ず誤発火する。トルクは誤発火が怖いので
ここでは見ない。**判定そのものは `MotionGuard.check_limit` が持ち、ここには書き写さない。**
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from lib.control.periodic import PeriodicTask
from lib.drivers.base import ControlMode
from lib.motion_guard import GuardViolation, MotionGuard
from lib.sequence.motors import AxisHandle

if TYPE_CHECKING:
    from lib.match_state import Court
    from lib.sequence.motors import MotorGroup, MotorHandle, SensorReader
    from lib.sequence.positions import AxisSpec, PositionTable

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_INTERVAL_S", "LimitMonitor"]

DEFAULT_INTERVAL_S = 0.02

CourtSource = Callable[[], "Court"]
SleepFunc = Callable[[float], Awaitable[None]]


class LimitMonitor(PeriodicTask):
    def __init__(
        self,
        positions: PositionTable,
        motors: MotorGroup,
        *,
        sensor_active: SensorReader,
        court: CourtSource,
        interval_s: float = DEFAULT_INTERVAL_S,
        time_source: Callable[[], float] = time.monotonic,
        sleep: SleepFunc = asyncio.sleep,
    ) -> None:
        super().__init__(interval_s=interval_s, time_source=time_source, sleep=sleep, logger=logger)
        self._positions = positions
        self._motors = motors
        self._sensor_active = sensor_active
        self._court = court
        self._guards = _build_guards(positions, motors)
        # 発火したときに書いた目標。**同じ目標のあいだは撃ち直さない** ——
        # 20〜50Hz で `set_target_value` を撃ち続けるとバスが埋まる
        self._stopped: dict[str, float] = {}

    @property
    def axis_names(self) -> tuple[str, ...]:
        return tuple(self._guards)

    @property
    def stopped_axes(self) -> frozenset[str]:
        return frozenset(self._stopped)

    def _label(self) -> str:
        return f"可動端監視 ({', '.join(self.axis_names) or '対象なし'})"

    async def _tick(self) -> None:
        await self.step()

    async def step(self) -> None:
        for axis in self._guards:
            try:
                await self._check_axis(axis)
            except Exception:
                self._log.exception(f"axis:{axis}", "可動端監視で例外 (axis=%s)", axis)

    async def _check_axis(self, axis: str) -> None:
        spec = self._positions.axis(axis).for_court(self._court())
        handles = [self._motors[name] for name in spec.motor_names]

        target = self._commanded_value(spec, handles)
        if target is None:
            self._stopped.pop(axis, None)
            return
        if self._stopped.get(axis) == target:
            return

        observed = spec.to_value({h.name: h.driver.feedback_position() for h in handles})
        delta = target - observed
        try:
            self._guards[axis].check_limit(
                axis=axis, delta=delta, sensor_active=self._sensor_active
            )
        except GuardViolation as exc:
            await self._stop_here(axis, spec, handles, observed, exc)
            return
        self._stopped.pop(axis, None)

    def _commanded_value(self, spec: AxisSpec, handles: list[MotorHandle]) -> float | None:
        commands: dict[str, float] = {}
        for handle in handles:
            if handle.target is None or handle.mode is not ControlMode.POSITION:
                return None
            commands[handle.name] = handle.target
        return spec.to_value(commands)

    async def _stop_here(
        self,
        axis: str,
        spec: AxisSpec,
        handles: list[MotorHandle],
        observed: float,
        exc: GuardViolation,
    ) -> None:
        await AxisHandle(spec, handles, sensor_active=self._sensor_active).set_target_value(
            spec.to_commands(observed)
        )
        # 書いた結果から取り直す。`to_commands` の往復で桁の下の方がずれると、
        # 次の周期に計算する目標と一致せず撃ち直しになる
        written = self._commanded_value(spec, handles)
        if written is not None:
            self._stopped[axis] = written
        self._logger.warning(
            "移動中に可動端で停止: axis=%s, 実測=%.3f%s, 目標を実測へ書き直しました (%s)",
            axis,
            observed,
            spec.unit,
            exc,
        )


def _build_guards(positions: PositionTable, motors: MotorGroup) -> dict[str, MotionGuard]:
    guards: dict[str, MotionGuard] = {}
    for name in positions.axes:
        spec = positions.axis(name)
        # `guard.limits` を書いていない軸は監視しない (既定値で埋めない)
        if spec.guard is None or spec.guard.limits is None:
            continue
        missing = [motor for motor in spec.motor_names if motor not in motors]
        if missing:
            logger.warning(
                "可動端監視をスキップ: 軸 %s のモータ %s がこのロボットに存在しません",
                name,
                ", ".join(missing),
            )
            continue
        guards[name] = MotionGuard(spec.guard)
    return guards
