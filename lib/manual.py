from __future__ import annotations

import logging
from collections.abc import Collection
from enum import StrEnum
from typing import TYPE_CHECKING

from lib.drivers.base import ControlMode
from lib.match_state import Court
from lib.sequence.motors import AxisHandle
from lib.sequence.positions import PositionLookupError

if TYPE_CHECKING:
    from lib.sequence.motors import MotorGroup
    from lib.sequence.positions import AxisSpec, ManualSpec, PositionTable

logger = logging.getLogger(__name__)

__all__ = ["ManualControlError", "ManualController", "OperationMode"]


class OperationMode(StrEnum):
    SEQUENCE = "sequence"
    MANUAL = "manual"


class ManualControlError(RuntimeError):
    """手動指令を受理できないときに送出される (理由は操縦者へそのまま返す)。"""


class ManualController:
    def __init__(
        self,
        motors: MotorGroup,
        positions: PositionTable,
        *,
        court: Court = Court.RED,
    ) -> None:
        self._motors = motors
        self._positions = positions
        self._court = court
        self._targets: dict[str, float] = {}

    def set_court(self, court: Court) -> None:
        self._court = court

    async def move_to_position(self, axis: str, name: str) -> float:
        spec = self._axis(axis)
        value = self._positions.raw(axis, name, court=self._court)
        await self._send(spec, spec.to_commands(value))
        self._targets[axis] = value
        logger.info("manual move: axis=%s position=%s value=%s", axis, name, value)
        return value

    async def set_value(self, axis: str, value: float) -> float:
        spec = self._axis(axis)
        manual = self._require_manual(spec)
        clamped = manual.clamp(float(value))
        await self._send(spec, spec.to_commands(clamped))
        self._targets[axis] = clamped
        return clamped

    async def jog(self, axis: str, delta: float) -> float:
        spec = self._axis(axis)
        self._require_manual(spec)
        origin = self._targets.get(axis)
        if origin is None:
            origin = self.observed_value(axis)
        return await self.set_value(axis, origin + float(delta))

    def observed_value(self, axis: str) -> float:
        spec = self._axis(axis)
        return spec.to_value(self._feedback_positions(spec))

    def axes_info(self) -> list[dict]:
        info: list[dict] = []
        for name in self._positions.axes:
            spec = self._positions.axis(name)
            info.append(
                {
                    "name": name,
                    "unit": spec.unit,
                    "command_mode": spec.command_mode.value,
                    "value": self._safe_observed_value(spec),
                    "target": self._targets.get(name),
                    "manual": spec.manual.to_dict() if spec.manual is not None else None,
                    "deviation": self._safe_deviation(spec),
                    "sync_tolerance": spec.sync_tolerance,
                    "positions": self._position_entries(name),
                    "motors": list(spec.motor_names),
                }
            )
        return info

    def reset(self) -> None:
        self._targets.clear()

    def reset_axes_for_motors(self, motor_names: Collection[str]) -> None:
        dropped = set(motor_names)
        for axis in [
            name
            for name in self._targets
            if dropped.intersection(self._positions.axis(name).motor_names)
        ]:
            del self._targets[axis]

    def on_e_stop(self) -> None:
        self.reset()

    def _axis(self, axis: str) -> AxisSpec:
        try:
            return self._positions.axis(axis)
        except PositionLookupError as exc:
            raise ManualControlError(str(exc)) from exc

    def _require_manual(self, spec: AxisSpec) -> ManualSpec:
        if spec.manual is None:
            allowed = ", ".join(self._positions.manual_axes()) or "(なし)"
            raise ManualControlError(
                f"軸 '{spec.name}' は連続操作の対象外です "
                f"(位置名の指定のみ受け付けます。連続操作できる軸: {allowed})"
            )
        return spec.manual

    async def _send(self, spec: AxisSpec, commands: dict[str, float]) -> None:
        handle = AxisHandle(spec, [getattr(self._motors, name) for name in spec.motor_names])
        await handle.set_target_value(commands)

    def _feedback_positions(self, spec: AxisSpec) -> dict[str, float]:
        return {
            name: getattr(self._motors, name).driver.feedback_position()
            for name in spec.motor_names
            if name in self._motors
        }

    def _position_entries(self, axis: str) -> list[dict]:
        entries: list[dict] = []
        for name in self._positions.names(axis):
            try:
                value: float | None = self._positions.raw(axis, name, court=self._court)
            except Exception:
                logger.debug("位置 '%s.%s' の値を引けません", axis, name, exc_info=True)
                value = None
            entries.append({"name": name, "value": value})
        return entries

    def _safe_deviation(self, spec: AxisSpec) -> float | None:
        group = spec.sync_group
        if group is None or spec.command_mode is not ControlMode.POSITION:
            return None
        try:
            return group.deviation(self._feedback_positions(spec))
        except Exception:
            logger.debug("軸 '%s' の左右偏差を算出できません", spec.name, exc_info=True)
            return None

    def _safe_observed_value(self, spec: AxisSpec) -> float | None:
        if spec.command_mode is not ControlMode.POSITION:
            return None
        try:
            return self.observed_value(spec.name)
        except Exception:
            logger.debug("軸 '%s' の現在値を算出できません", spec.name, exc_info=True)
            return None
