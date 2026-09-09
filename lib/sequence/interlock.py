"""軸どうしの干渉。「この指令が通ったあと、禁止した姿勢に入るか」だけを答える。

`MotionGuard` は軸 1 本ずつの条件しか表現できない (`docs/invariants.md` §2)。
ここは複数の軸の**指令後の状態**を組にして見る。判断はこの 1 箇所に閉じ、
手動操縦 (`ManualController._apply`) とシーケンス (`Sequence.move_to`) の両方が呼ぶ。

宣言は位置定数 yaml の `interlocks:` に位置名で書く (値は `positions` が持つ):

    interlocks:
      - when: { sub_pitch: close }       # この姿勢のあいだは
        require: { sub_offset: close }   # こちらもこの姿勢でなければならない

「その位置に居る」は軸の `tolerance` で判定し、中間の角は「居ない」と扱う (安全側)。
指令していない軸の現在値は**最後に指令した目標**を第一とし、無ければ (緊急停止で
消えた・起動直後) フィードバックを使う。どちらを使ったかは拒否文に出す。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from lib.drivers.base import ControlMode

if TYPE_CHECKING:
    from lib.match_state import Court
    from lib.sequence.motors import MotorGroup
    from lib.sequence.positions import PositionTable

__all__ = ["AxisInterlock", "AxisReading", "InterlockSpec", "InterlockViolation", "Source"]


class InterlockViolation(RuntimeError):
    """指令が通ると軸どうしの禁止姿勢に入るので拒否した。理由文は操縦者へそのまま返る。"""


@dataclass(frozen=True)
class InterlockSpec:
    """`when` の姿勢に全部揃っているあいだ、`require` の姿勢も全部揃っていなければならない。"""

    when: tuple[tuple[str, str], ...]
    require: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not self.when or not self.require:
            raise ValueError("interlocks の when と require はどちらも空にできません")
        overlap = sorted({axis for axis, _ in self.when} & {axis for axis, _ in self.require})
        if overlap:
            raise ValueError(
                f"interlocks の when と require に同じ軸 {', '.join(overlap)} は書けません"
            )

    @property
    def axes(self) -> tuple[str, ...]:
        return tuple(axis for axis, _ in (*self.when, *self.require))


class Source(StrEnum):
    COMMAND = "この指令"
    TARGET = "最後に指令した目標"
    FEEDBACK = "フィードバック"


@dataclass(frozen=True)
class AxisReading:
    value: float
    source: Source


class AxisInterlock:
    def __init__(self, table: PositionTable) -> None:
        self._table = table

    def check(
        self,
        commands: Mapping[str, float],
        *,
        court: Court,
        motors: MotorGroup,
    ) -> None:
        """`commands` (軸名 → 軸の単位の値) が全部通ったあとの姿勢を見て、禁止なら投げる。"""
        for spec in self._table.interlocks:
            if not any(axis in commands for axis in spec.axes):
                continue
            after = {
                axis: (
                    AxisReading(float(commands[axis]), Source.COMMAND)
                    if axis in commands
                    else self._read(axis, court, motors)
                )
                for axis in spec.axes
            }
            if not all(self._at(axis, name, after[axis].value, court) for axis, name in spec.when):
                continue
            broken = [
                (axis, name)
                for axis, name in spec.require
                if not self._at(axis, name, after[axis].value, court)
            ]
            if broken:
                raise InterlockViolation(self._explain(spec, broken, after, court))

    def _read(self, axis: str, court: Court, motors: MotorGroup) -> AxisReading:
        spec = self._table.axis(axis).for_court(court)
        missing = [name for name in spec.motor_names if name not in motors]
        if missing:
            # 読めない軸を「どこにも居ない」と扱うと when が成立せず素通りになる
            raise InterlockViolation(
                f"軸どうしの干渉を判定できません: 軸 '{axis}' のモータ "
                f"{', '.join(missing)} が構成にありません"
            )
        handles = [motors[name] for name in spec.motor_names]
        if all(h.has_target and h.mode is ControlMode.POSITION for h in handles):
            targets = {h.name: h.target for h in handles if h.target is not None}
            return AxisReading(spec.to_value(targets), Source.TARGET)
        feedback = {h.name: h.driver.feedback_position() for h in handles}
        return AxisReading(spec.to_value(feedback), Source.FEEDBACK)

    def _at(self, axis: str, name: str, value: float, court: Court) -> bool:
        spec = self._table.axis(axis)
        # tolerance の無い軸は読み込み時に拒否してあるので、ここで None にはならない
        assert spec.tolerance is not None
        return abs(value - self._table.raw(axis, name, court=court)) <= spec.tolerance

    def _explain(
        self,
        spec: InterlockSpec,
        broken: list[tuple[str, str]],
        after: Mapping[str, AxisReading],
        court: Court,
    ) -> str:
        rule = (
            " かつ ".join(f"{axis} が {name}" for axis, name in spec.when)
            + " のあいだ "
            + " かつ ".join(f"{axis} は {name}" for axis, name in spec.require)
            + " でなければなりません"
        )
        commanded = [
            f"{axis} が {self._label(axis, r.value, court)}"
            for axis, r in after.items()
            if r.source is Source.COMMAND
        ]
        remaining = [
            f"{axis} は {self._label(axis, r.value, court, note=str(r.source))} のまま"
            for axis, r in after.items()
            if r.source is not Source.COMMAND
        ]
        state = f"この指令で {' と '.join(commanded)} になり"
        state += f"、{'、'.join(remaining)}です" if remaining else "ます"
        # 次の手: require 側を動かして拒否されたなら when 側を外す、逆なら require 側を揃える
        commanded_axes = {axis for axis, r in after.items() if r.source is Source.COMMAND}
        if commanded_axes & {axis for axis, _ in broken}:
            hint = " と ".join(f"{axis} を {name} から外して" for axis, name in spec.when)
        else:
            hint = " と ".join(f"{axis} を {name} にして" for axis, name in broken)
        return f"軸どうしの干渉のため拒否: {rule}。{state}。先に {hint}ください"

    def _label(self, axis: str, value: float, court: Court, *, note: str = "") -> str:
        unit = self._table.axis(axis).unit
        at = [name for name in self._table.names(axis) if self._at(axis, name, value, court)]
        where = at[0] if at else "定義位置の外"
        return f"{where} ({value:g}{unit}{'、' + note if note else ''})"
