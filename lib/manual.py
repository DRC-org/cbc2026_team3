from __future__ import annotations

import logging
from collections.abc import Collection
from enum import StrEnum
from typing import TYPE_CHECKING

from lib.drivers.base import ControlMode
from lib.match_state import Court
from lib.sequence.motors import build_axis_handle
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
        sent = await self._send(spec, self._positions.raw(axis, name, court=self._court))
        self._targets[axis] = sent
        logger.info("manual move: axis=%s position=%s value=%s", axis, name, sent)
        return sent

    async def set_value(self, axis: str, value: float) -> float:
        spec = self._axis(axis)
        manual = self._require_manual(spec)
        sent = await self._send(spec, manual.clamp(float(value)))
        self._targets[axis] = sent
        return sent

    async def jog(self, axis: str, delta: float) -> float:
        spec = self._axis(axis)
        self._require_manual(spec)
        origin = self._targets.get(axis)
        if origin is None:
            origin = self.observed_value(axis)
        return await self.set_value(axis, origin + float(delta))

    def is_always_manual(self, axis: str) -> bool:
        # 未定義の軸は ManualControlError のまま返す。サーバーがモードの理由で覆い隠すと、
        # 軸名の打ち間違いが「切り替えても直らない拒否」に見える
        return self._axis(axis).manual_always

    def always_manual_axes(self) -> tuple[str, ...]:
        return self._positions.manual_always_axes()

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
                    "manual_always": spec.manual_always,
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

    async def _send(self, spec: AxisSpec, value: float) -> float:
        # 左右直結ペアを同一フレームで送る責務は AxisHandle.set_target_value が持つ。
        # 返ってきた指令をそのまま信じる —— リミット保護が端で頭打ちにした要求値を
        # ジョグ起点や画面へ返すと、起点は押すたびに禁止側へ伸び、退避しようとしても
        # 押した回数だけ戻らない。素通りした指令は要求値のまま返す (丸め誤差を足さない)
        commands = spec.to_commands(value)
        sent = await build_axis_handle(spec, self._motors).set_target_value(commands)
        if sent == commands:
            return value
        return spec.to_value(sent)

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
