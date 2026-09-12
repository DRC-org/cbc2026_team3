from __future__ import annotations

import logging
import math
from collections.abc import Collection
from enum import StrEnum
from typing import TYPE_CHECKING

from lib.drivers.base import ControlMode
from lib.match_state import Court
from lib.sequence.interlock import AxisInterlock, InterlockViolation
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
        court: Court | None = None,
    ) -> None:
        self._motors = motors
        self._positions = positions
        self._court: Court | None = court
        self._interlock = AxisInterlock(positions)
        # 微調整の基準: 軸ごとに (epoch, 最初のジョグの直前の目標)。epoch は呼ぶ側が決める
        # 「同じステップのあいだ」の印で、変わったら取り直す
        self._baselines: dict[str, tuple[object, float]] = {}
        # 目標を起点にしてよいか分からない軸。緊急停止・再励磁のあとはモータが目標を
        # 持ったまま無励磁で動いているので、次の 1 回だけ実測から起点を取る
        self._stale: set[str] = set()

    def set_court(self, court: Court | None) -> None:
        self._court = court

    async def move_to_position(self, axis: str, name: str) -> float:
        spec = self._axis(axis)
        value = self._positions.raw(axis, name, court=self._court)
        await self._apply(spec, value)
        logger.info("manual move: axis=%s position=%s value=%s", axis, name, value)
        return value

    async def set_value(self, axis: str, value: float) -> float:
        spec = self._axis(axis)
        manual = self._require_manual(spec)
        return await self._apply(spec, manual.clamp(float(value)))

    async def jog(self, axis: str, delta: float, *, epoch: object = None) -> float:
        """今の目標から相対移動する。``manual:`` を持つ軸のみ。

        起点は**モータが今持っている目標** (無ければ実測)。手動で送った値を別に控えて
        起点にすると、シーケンスが位置名で動かした後もその古い値から動き出す
        (箱 2 で詰めた値を箱 3 で起点にして 160mm 飛ぶ)。実測を起点にしないのは、
        追従中の連打が吸われるため。
        丸めが `clamp` ではなく `clamp_from` なのは、起点が範囲の外に居るとき
        `clamp` が 1 歩目だけを境界まで飛ばすため (零点確定前は範囲の外が普通)。
        """
        spec = self._axis(axis)
        manual = self._require_manual(spec)
        target = None if spec.name in self._stale else self._axis_target(spec)
        origin = float(target if target is not None else self.observed_value(axis))
        held = self._baselines.get(spec.name)
        if held is None or held[0] != epoch:
            self._baselines[spec.name] = (epoch, origin)
        value = manual.clamp_from(origin, origin + float(delta))
        logger.info(
            "manual jog: axis=%s delta=%+g origin=%.3f -> %.3f (%s)",
            axis,
            float(delta),
            origin,
            value,
            "目標" if target is not None else "実測",
        )
        return await self._apply(spec, value)

    def baseline(self, axis: str, epoch: object) -> float | None:
        """その epoch で最初にジョグしたときの起点。別の epoch のものは返さない。"""
        held = self._baselines.get(axis)
        return None if held is None or held[0] != epoch else held[1]

    async def _apply(self, spec: AxisSpec, value: float) -> float:
        """丸め終わった値を干渉条件にかけてから送る (`set_value` / `jog` の共通の出口)。"""
        try:
            self._interlock.check({spec.name: value}, court=self._court, motors=self._motors)
        except InterlockViolation as exc:
            raise ManualControlError(str(exc)) from exc
        await self._send(spec, spec.to_commands(value))
        self._stale.discard(spec.name)
        return value

    def axis_members(self, axis: str) -> tuple[str, ...]:
        """その軸を成すモータ名。未定義の軸は ManualControlError (指令口ではなく読む口)。"""
        return self._axis(axis).motor_names

    def is_always_manual(self, axis: str) -> bool:
        # 未定義の軸は ManualControlError のまま返す。サーバーがモードの理由で覆い隠すと、
        # 軸名の打ち間違いが「切り替えても直らない拒否」に見える
        return self._axis(axis).manual_always

    def always_manual_axes(self) -> tuple[str, ...]:
        return self._positions.manual_always_axes()

    def has_range(self, axis: str) -> bool:
        """`manual:` を持つ軸か (連続値を送ってよい軸か)。未定義の軸は送出する。"""
        return self._axis(axis).manual is not None

    def observed_value(self, axis: str) -> float:
        spec = self._axis(axis)
        return spec.to_value(self._feedback_positions(spec))

    def axes_info(self, *, epoch: object = None) -> list[dict]:
        info: list[dict] = []
        for name in self._positions.axes:
            spec = self._axis(name)
            info.append(
                {
                    "name": name,
                    "unit": spec.unit,
                    "command_mode": spec.command_mode.value,
                    "value": self._safe_observed_value(spec),
                    "target": self._axis_target(spec),
                    "baseline": self.baseline(name, epoch),
                    "manual": spec.manual.to_dict() if spec.manual is not None else None,
                    "manual_always": spec.manual_always,
                    "deviation": self._safe_deviation(spec),
                    "sync_tolerance": spec.sync_tolerance,
                    "positions": self._position_entries(name),
                    "motors": list(spec.motor_names),
                    "linkage": self._linkage_info(spec),
                }
            )
        return info

    def _linkage_info(self, spec: AxisSpec) -> dict | None:
        """リンク機構の軸だけ持つ、中心のずれとその上限。UI はこれで ± の口を出す。"""
        linkage = spec.linkage
        if linkage is None:
            return None
        gap = self._axis_target(spec)
        return {
            "center": linkage.center.value,
            "max": linkage.geometry.crank,
            "limit": None if gap is None else linkage.geometry.center_limit(gap),
        }

    def set_linkage_center(self, axis: str, center_mm: float) -> float:
        """左右の中心のずれ [mm] を控える。指令のたびにその隙間でずらせる量へ丸められる。"""
        spec = self._axis(axis)
        if spec.linkage is None:
            raise ManualControlError(f"軸 '{axis}' はリンク機構ではないので中心をずらせません")
        limit = spec.linkage.geometry.crank
        if not math.isfinite(center_mm) or abs(center_mm) > limit:
            raise ManualControlError(
                f"中心のずれ {center_mm:.3g}mm は ±{limit:.3g}mm を超えています"
            )
        spec.linkage.center.value = float(center_mm)
        return spec.linkage.center.value

    async def shift_linkage_center(self, axis: str, center_mm: float) -> float | None:
        """中心をずらし、今の隙間で**片側ずつ**送り直す。目標が無ければ控えるだけ (None)。

        両側を同時に動かすと途中で押し合って動かない (2026-09-12 実機)。縮む側をまず
        縮めきり (open) へ逃がし、伸びる側を目的へ、最後に逃がした側を目的へ戻す。
        どちらも縮まない (中心が変わらない) ときはまとめて送る。
        """
        spec = self._axis(axis)
        linkage = spec.linkage
        if linkage is None:
            raise ManualControlError(f"軸 '{axis}' はリンク機構ではないので中心をずらせません")
        gap = self._axis_target(spec)
        self.set_linkage_center(axis, center_mm)
        if gap is None:
            return None
        before = {name: getattr(self._motors, name).target for name in spec.motor_names}
        after = spec.to_commands(gap)
        opened = {side.motor: side.servo(180.0) for side in (linkage.left, linkage.right)}
        retracting = [
            name
            for name in spec.motor_names
            if before[name] is not None
            and abs(after[name] - opened[name]) < abs(before[name] - opened[name]) - 1e-9
        ]
        if len(retracting) != 1:
            await self._send(spec, after)
            return gap
        retract = retracting[0]
        extend = next(name for name in spec.motor_names if name != retract)
        await self._move_motor(spec, retract, opened[retract])
        await self._move_motor(spec, extend, after[extend])
        await self._move_motor(spec, retract, after[retract])
        return gap

    async def _move_motor(self, spec: AxisSpec, name: str, value: float) -> None:
        """リンク軸の片側だけを動かして待つ (中心をずらすときの段取り専用)。"""
        handle = getattr(self._motors, name)
        motor = next(m for m in spec.motors if m.name == name)
        tolerance = None if spec.tolerance is None else motor.to_tolerance(spec.tolerance)
        await handle.set_target(spec.command_mode, float(value))
        if not await handle.wait_reached(
            tolerance=tolerance, timeout=spec.timeout_s, expect_target=True
        ):
            raise ManualControlError(
                f"{name} を {value:.1f}{spec.command_unit} へ"
                f" {spec.timeout_s} 秒以内に動かせませんでした"
            )

    async def resend_target(self, axis: str) -> float | None:
        """今の目標をもう 1 度送る (中心を変えた直後にその場で効かせる)。目標が無ければ None。"""
        spec = self._axis(axis)
        value = self._axis_target(spec)
        if value is None:
            return None
        await self._send(spec, spec.to_commands(value))
        return value

    def reset(self) -> None:
        self._baselines.clear()
        self._stale.update(self._positions.axes)

    def reset_axes_for_motors(self, motor_names: Collection[str]) -> None:
        dropped = set(motor_names)
        for axis in [
            name
            for name in self._positions.axes
            if dropped.intersection(self._positions.axis(name).motor_names)
        ]:
            self._baselines.pop(axis, None)
            self._stale.add(axis)

    def on_e_stop(self) -> None:
        self.reset()

    def _axis(self, axis: str) -> AxisSpec:
        try:
            return self._positions.axis(axis).for_court(self._court)
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
        handle = AxisHandle(
            spec,
            [getattr(self._motors, name) for name in spec.motor_names],
            sensor_active=self._motors.sensor_active,
            axis_state=self._motors.axis_state,
            pressed_toward=self._motors.pressed_toward,
        )
        await handle.set_target_value(commands)

    def _axis_target(self, spec: AxisSpec) -> float | None:
        """**実際にモータへ送った目標**を軸の値へ戻す。手動のぶんだけ配ってはならない。

        手動目標 (`_targets`) だけを配ると、シーケンス・零点確定・吸着ステップで
        動かした結果が画面から消える —— 弁を開いて吸っているのに UI では閉じたまま、
        サーボを動かしたのに無かったことになる (2026-09-11 実機)。基板は指令を保持
        しているので、画面だけが実態とずれる。

        ジョグの起点もこれ (フィードバックから取ると追従中の連打が吸われる。
        `docs/invariants.md` §4)。
        """
        commands: dict[str, float] = {}
        try:
            for name in spec.motor_names:
                target = getattr(self._motors, name).target
                if target is None:
                    return None
                commands[name] = target
            return spec.to_value(commands)
        except (AttributeError, KeyError, ValueError):
            # 登録されていないモータの軸 (机上の構成)。測れないものは null で配る
            return None

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
