from __future__ import annotations

import time
from collections.abc import Callable, Collection
from dataclasses import dataclass

from lib.drivers.base import ControlMode
from lib.match_state import Court
from lib.sequence.motors import MotorGroup, origin_confirmed
from lib.sequence.positions import (
    AxisSpec,
    CourtUnresolvedError,
    PositionLookupError,
    PositionTable,
)

__all__ = ["PositionCapture", "PositionCaptureStore"]

#: 貼り先に合わせた桁。`positions:` 配下は軸が 2、位置名が 4
_AXIS_INDENT = "  "
_NAME_INDENT = "    "
#: 既存ファイルの行末コメントが揃っている桁
_COMMENT_COLUMN = 25


def _format_value(value: float) -> str:
    """yaml へ書く体裁。**丸めるのは表示の桁だけで、控えた値そのものは丸めない。**"""
    text = f"{value:.3f}".rstrip("0")
    return f"{text}0" if text.endswith(".") else text


@dataclass(frozen=True)
class PositionCapture:
    axis: str
    name: str
    value: float
    unit: str
    captured_at: float

    def to_dict(self) -> dict:
        return {
            "axis": self.axis,
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "captured_at": self.captured_at,
        }


class PositionCaptureStore:
    """「今いる位置を、その位置名として控える」控え帳。**config は書き換えない。**

    サーバーが yaml を書き換えると、再起動したときファイルとメモリのどちらが正か
    分からなくなる。控えるのはメモリだけで、出すのは貼るだけの yaml 断片。書くのは人。

    受ける位置名を `PositionTable` にある名前へ限るのは、自由入力だとタイポで位置名が
    増殖し、`sequences/*.py` から参照されない死んだ名前ができるため。
    """

    def __init__(
        self,
        positions: PositionTable,
        motors: MotorGroup,
        *,
        court: Callable[[], Court | None],
        stale_motors: Callable[[Collection[str]], tuple[str, ...]],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._positions = positions
        self._motors = motors
        self._court = court
        self._stale_motors = stale_motors
        self._clock = clock
        self._entries: dict[tuple[str, str], PositionCapture] = {}

    def targets(self) -> dict[str, tuple[str, ...]]:
        """控えられる軸と、その軸が受ける位置名。**UI はここに並んだものだけを出す。**"""
        found: dict[str, tuple[str, ...]] = {}
        for axis in self._positions.axes:
            if self._positions.axis(axis).command_mode is not ControlMode.POSITION:
                continue
            names = self._positions.names(axis)
            if names:
                found[axis] = names
        return found

    def capture(self, axis: object, name: object) -> str | None:
        """今の実測をその位置名として控える。**拒んだ理由を返す (通れば None)。**"""
        spec, reason = self._resolve_axis(axis)
        if spec is None:
            return reason
        assert isinstance(axis, str)

        if not isinstance(name, str) or not name:
            return "位置名が指定されていません"
        known = self._positions.names(axis)
        if name not in known:
            return (
                f"位置 '{axis}.{name}' は定義されていません"
                f" (控えられる位置名: {', '.join(known) or '(なし)'})。"
                "名前を増やすときは先に位置定数 yaml へ書いてください"
            )

        if not origin_confirmed(spec, self._motors):
            return (
                f"軸 '{axis}' の零点が未確定です。先にその軸の零点合わせを行ってください"
                " (確定前の実測は機械原点からの値ではありません)"
            )

        stale = self._stale_motors(spec.motor_names)
        if stale:
            return f"軸 '{axis}' の実測が古いため控えられません (応答なし: {', '.join(stale)})"

        value, reason = self._observed_value(spec)
        if value is None:
            return reason

        self._entries[(axis, name)] = PositionCapture(
            axis=axis,
            name=name,
            value=value,
            unit=spec.unit,
            captured_at=self._clock(),
        )
        return None

    def entries(self) -> tuple[PositionCapture, ...]:
        """位置定数 yaml と同じ並び。貼る人が上から順に突き合わせられる。"""
        order = {
            (axis, name): (axis_index, name_index)
            for axis_index, axis in enumerate(self._positions.axes)
            for name_index, name in enumerate(self._positions.names(axis))
        }
        return tuple(
            sorted(self._entries.values(), key=lambda e: order.get((e.axis, e.name), (-1, -1)))
        )

    def yaml_fragment(self) -> str | None:
        """そのまま位置定数 yaml へ貼れる断片。**控えが無ければ `None`。**"""
        captured = self.entries()
        if not captured:
            return None

        lines = [f"# {self._positions.source} の positions: 配下へ貼る"]
        current: str | None = None
        for entry in captured:
            if entry.axis != current:
                lines.append(f"{_AXIS_INDENT}{entry.axis}:")
                current = entry.axis
            body = f"{_NAME_INDENT}{entry.name}: {_format_value(entry.value)}"
            lines.append(f"{body.ljust(_COMMENT_COLUMN)} # [{entry.unit}]")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "targets": {axis: list(names) for axis, names in self.targets().items()},
            "entries": [entry.to_dict() for entry in self.entries()],
            "yaml": self.yaml_fragment(),
        }

    def _resolve_axis(self, axis: object) -> tuple[AxisSpec | None, str | None]:
        if not isinstance(axis, str) or not axis:
            return None, "軸が指定されていません"
        try:
            spec = self._positions.axis(axis)
        except PositionLookupError as exc:
            return None, str(exc)
        if spec.command_mode is not ControlMode.POSITION:
            return None, (
                f"軸 '{axis}' は位置を控えられません"
                f" (command_mode={spec.command_mode.value}: 位置の実測を持たない)"
            )
        return spec, None

    def _observed_value(self, spec: AxisSpec) -> tuple[float | None, str | None]:
        """全モータの実測から軸の値を出す。**欠けた 1 台を黙って外した平均は作らない**
        (`origin_confirmed` が全員そろっていることを先に確かめている)。"""
        try:
            # 換算 (scale) だけがコート別で、mm の座標系は両コート共通。コートが
            # 決まるまでは生角を値へ直せないので、控える値も出せない
            resolved = spec.for_court(self._court())
            observed = {
                motor: self._motors[motor].driver.feedback_position() for motor in spec.motor_names
            }
            return resolved.to_value(observed), None
        except (CourtUnresolvedError, PositionLookupError) as exc:
            return None, str(exc)
