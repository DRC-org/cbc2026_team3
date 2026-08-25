from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from lib.match_state import Court

# 軸ごとの到達待ち上限 [s] の既定値。機構が引っかかったまま試合が止まるのを避けるため、
# yaml で timeout_s を書かなかった軸にも必ずタイムアウトを与える
DEFAULT_TIMEOUT_S = 5.0


class PositionLookupError(RuntimeError):
    """位置定数の参照に失敗したときに送出される。"""


@dataclass(frozen=True)
class AxisSpec:
    """1 軸の単位換算とデフォルト待ち条件。

    ``command = value * scale + offset`` で人間の単位からモータ指令値へ換算する。
    3 種類のモータ (M3508=モータ軸 deg / EDULITE 05=rad / 自作=deg) で指令単位が
    異なるため、換算はここに一元化してシーケンス本体には持ち込まない。
    """

    name: str
    unit: str
    command_unit: str
    scale: float
    offset: float
    timeout_s: float
    # 人間の単位での到達許容差。None ならドライバ既定値を使う
    tolerance: float | None

    def to_command(self, value: float) -> float:
        return value * self.scale + self.offset

    @property
    def command_tolerance(self) -> float | None:
        if self.tolerance is None:
            return None
        # 許容差は幅であって向きを持たないため、scale が負でも正の幅になるようにする
        return abs(self.tolerance * self.scale)


_AXIS_KEYS = frozenset({"unit", "command_unit", "scale", "offset", "timeout_s", "tolerance"})


class PositionTable:
    """機構位置の定数表。軸ごとの単位換算とコート差異を吸収する。"""

    def __init__(
        self,
        axes: Mapping[str, AxisSpec],
        positions: Mapping[str, Mapping[str, float | dict[str, float]]],
        *,
        source: str = "<inline>",
    ) -> None:
        self._axes: dict[str, AxisSpec] = dict(axes)
        self._positions: dict[str, dict[str, float | dict[str, float]]] = {
            axis: dict(values) for axis, values in positions.items()
        }
        self._source = source

    @classmethod
    def empty(cls, *, source: str = "<inline>") -> PositionTable:
        return cls({}, {}, source=source)

    @property
    def source(self) -> str:
        return self._source

    @property
    def is_empty(self) -> bool:
        return not self._axes

    @property
    def axes(self) -> tuple[str, ...]:
        return tuple(self._axes)

    def names(self, axis: str) -> tuple[str, ...]:
        return tuple(self._positions.get(axis, {}))

    def axis(self, axis: str) -> AxisSpec:
        spec = self._axes.get(axis)
        if spec is None:
            available = ", ".join(self._axes) or "(なし)"
            raise PositionLookupError(
                f"軸 '{axis}' が {self._source} に定義されていません。定義済みの軸: {available}"
            )
        return spec

    def timeout(self, axis: str) -> float:
        return self.axis(axis).timeout_s

    def tolerance(self, axis: str) -> float | None:
        return self.axis(axis).command_tolerance

    def raw(self, axis: str, name: str, *, court: Court | None = None) -> float:
        """人間の単位のままの値を返す (ログ・検証用)。"""
        spec = self.axis(axis)
        values = self._positions.get(axis, {})
        if name not in values:
            available = ", ".join(values) or "(なし)"
            raise PositionLookupError(
                f"位置 '{spec.name}.{name}' が {self._source} に定義されていません。"
                f"定義済みの位置: {available}"
            )

        value = values[name]
        if isinstance(value, dict):
            if court is None:
                raise PositionLookupError(
                    f"位置 '{spec.name}.{name}' はコート別に定義されていますが "
                    "コートが指定されていません"
                )
            return float(value[str(court)])
        return float(value)

    def command(self, axis: str, name: str, *, court: Court | None = None) -> float:
        """モータへそのまま渡せる指令値を返す。"""
        return self.axis(axis).to_command(self.raw(axis, name, court=court))


def _parse_axis(name: str, raw: object) -> AxisSpec:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"axes.{name} は辞書である必要があります: {raw!r}")

    unknown = set(raw) - _AXIS_KEYS
    if unknown:
        raise ValueError(f"axes.{name} に未知のキー: {', '.join(sorted(unknown))}")

    def _number(key: str, default: float | None) -> float | None:
        if key not in raw or raw[key] is None:
            return default
        try:
            return float(raw[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"axes.{name}.{key} が数値ではありません: {raw[key]!r}") from exc

    scale = _number("scale", 1.0)
    if scale == 0.0:
        raise ValueError(f"axes.{name}.scale が 0 です (どの値を書いても同じ位置になります)")

    timeout_s = _number("timeout_s", DEFAULT_TIMEOUT_S)
    if timeout_s is None or timeout_s <= 0.0:
        raise ValueError(f"axes.{name}.timeout_s は正の秒数である必要があります: {timeout_s!r}")

    tolerance = _number("tolerance", None)
    if tolerance is not None and tolerance < 0.0:
        raise ValueError(f"axes.{name}.tolerance は 0 以上である必要があります: {tolerance!r}")

    return AxisSpec(
        name=name,
        unit=str(raw.get("unit", "")),
        command_unit=str(raw.get("command_unit", "")),
        scale=float(scale),
        offset=float(_number("offset", 0.0) or 0.0),
        timeout_s=float(timeout_s),
        tolerance=tolerance,
    )


def _parse_value(axis: str, name: str, raw: object) -> float | dict[str, float]:
    if isinstance(raw, dict):
        # コート別定義。片方だけ書くと反対コートで無言のまま別の値になるため両方を必須にする
        missing = [court.value for court in Court if court.value not in raw]
        if missing:
            raise ValueError(
                f"positions.{axis}.{name} のコート別定義に {', '.join(missing)} がありません"
            )
        unknown = set(raw) - {court.value for court in Court}
        if unknown:
            raise ValueError(
                f"positions.{axis}.{name} に未知のコート: {', '.join(sorted(unknown))}"
            )
        return {court.value: _to_float(axis, name, raw[court.value]) for court in Court}
    return _to_float(axis, name, raw)


def _to_float(axis: str, name: str, raw: object) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        raise ValueError(f"positions.{axis}.{name} が数値ではありません: {raw!r}")
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"positions.{axis}.{name} が数値ではありません: {raw!r}") from exc


def load_position_table(config: dict | None, *, source: str = "<inline>") -> PositionTable:
    """位置定数 yaml 相当の dict から PositionTable を組み立てる。

    健全性チェックで例外を投げるのは、換算係数の欠落や誤記をそのまま通すと
    人間の単位の値が生の指令値として送られ、機構を破壊しかねないため。
    checklist.yaml のような「壊れていても起動する」方針は取らない。
    """
    config = config or {}

    axes_raw = config.get("axes") or {}
    if not isinstance(axes_raw, dict):
        raise ValueError(f"{source}: axes は辞書である必要があります")

    positions_raw = config.get("positions") or {}
    if not isinstance(positions_raw, dict):
        raise ValueError(f"{source}: positions は辞書である必要があります")

    axes = {name: _parse_axis(name, raw) for name, raw in axes_raw.items()}

    positions: dict[str, dict[str, float | dict[str, float]]] = {}
    for axis, values in positions_raw.items():
        if axis not in axes:
            raise ValueError(
                f"{source}: positions.{axis} に対応する axes.{axis} がありません "
                "(単位換算が決まらないため起動を拒否します)"
            )
        if not isinstance(values, dict):
            raise ValueError(f"{source}: positions.{axis} は辞書である必要があります")
        positions[axis] = {
            name: _parse_value(axis, name, raw_value) for name, raw_value in values.items()
        }

    return PositionTable(axes, positions, source=source)
