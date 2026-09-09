from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

__all__ = ["SuctionPad", "SuctionSelection", "SuctionSelectionError", "suction_of"]


class SuctionSelectionError(RuntimeError):
    """吸着に使うパッドが 1 つも選ばれていないまま吸着しようとした。"""


@dataclass(frozen=True)
class SuctionPad:
    axis: str
    label: str


class SuctionSelection:
    """吸着に使うパッド (弁の軸) の集合。正はサーバーのロボットコンテキストが持つ。

    ワークの形で乗らないパッドの弁を開けると真空が抜けて吸着が外れるので、操縦者が
    次の吸着ステップで開ける弁を減らせる。既定は全部 ON。
    """

    def __init__(self, pads: Iterable[SuctionPad]) -> None:
        self._pads = tuple(pads)
        if not self._pads:
            raise ValueError("吸着パッドが 1 つも無い")
        axes = [pad.axis for pad in self._pads]
        if len(set(axes)) != len(axes):
            raise ValueError(f"吸着パッドの軸名が重複している: {', '.join(axes)}")
        self._enabled: frozenset[str] = frozenset(axes)

    @classmethod
    def numbered(cls, axes: Iterable[str]) -> SuctionSelection:
        return cls(SuctionPad(axis=axis, label=str(i)) for i, axis in enumerate(axes, start=1))

    @property
    def pads(self) -> tuple[SuctionPad, ...]:
        return self._pads

    @property
    def axes(self) -> tuple[str, ...]:
        return tuple(pad.axis for pad in self._pads)

    def enabled(self) -> tuple[str, ...]:
        return tuple(axis for axis in self.axes if axis in self._enabled)

    def is_enabled(self, axis: str) -> bool:
        return axis in self._enabled

    def select(self, axes: object) -> str | None:
        """有効なパッドを差し替える。**拒んだ理由を返す (通れば None)。**

        空の選択は通す。吸着ステップの側が拒むので、選び直しの途中で 0 個を経由できる。
        """
        if not isinstance(axes, list) or not all(isinstance(axis, str) for axis in axes):
            return "吸着パッドの指定が軸名の配列ではありません"
        known = self.axes
        unknown = [axis for axis in axes if axis not in known]
        if unknown:
            return (
                f"{', '.join(unknown)} は吸着パッドではありません"
                f" (指定できる軸: {', '.join(known)})"
            )
        self._enabled = frozenset(axes)
        return None

    def to_dict(self) -> dict:
        return {
            "pads": [
                {"axis": pad.axis, "label": pad.label, "enabled": pad.axis in self._enabled}
                for pad in self._pads
            ]
        }


def suction_of(sequence: object) -> SuctionSelection | None:
    """シーケンスが吸着パッドの選択を持つならそれを返す。サーバーの配線が唯一の呼び手。"""
    selection = getattr(sequence, "suction", None)
    return selection if isinstance(selection, SuctionSelection) else None
