from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from lib.drivers.base import ControlMode
from lib.match_state import Court

# 軸ごとの到達待ち上限 [s] の既定値。機構が引っかかったまま試合が止まるのを避けるため、
# yaml で timeout_s を書かなかった軸にも必ずタイムアウトを与える
DEFAULT_TIMEOUT_S = 5.0


class PositionLookupError(RuntimeError):
    """位置定数の参照に失敗したときに送出される。"""


@dataclass(frozen=True)
class MotorSpec:
    """論理軸を構成する 1 台のモータの単位換算。

    逆回転で同一動作をするペア (y_axis / rotate) は ``scale`` の符号で表す。
    専用の invert フラグを設けないのは、単位換算と回転方向が 2 箇所に分かれると
    片方だけ直したときに気付けないため。
    """

    name: str
    scale: float
    offset: float

    def to_command(self, value: float) -> float:
        return value * self.scale + self.offset

    def to_value(self, command: float) -> float:
        """指令値・フィードバックを人間の単位へ戻す (同期監視の偏差計算で使う)。"""
        return (command - self.offset) / self.scale


@dataclass(frozen=True)
class AxisSpec:
    """1 論理軸の単位換算とデフォルト待ち条件。

    ``command = value * scale + offset`` で人間の単位からモータ指令値へ換算する。
    3 種類のモータ (M3508=モータ軸 deg / EDULITE 05=rad / 自作=deg) で指令単位が
    異なるため、換算はここに一元化してシーケンス本体には持ち込まない。

    1 つの論理軸が複数モータで駆動される場合 (左右直結のラックアンドピニオン等) は
    ``motors`` に複数を並べる。この場合 ``scale`` / ``offset`` / ``to_command`` は
    先頭モータへの委譲であり、実際の指令は ``motors`` 全体で組み立てる必要がある。
    """

    name: str
    unit: str
    command_unit: str
    timeout_s: float
    # 人間の単位での到達許容差。None ならドライバ既定値を使う
    tolerance: float | None
    motors: tuple[MotorSpec, ...]
    # 人間の単位でのモータ間ずれ許容差。超過で停止する。単一モータ軸では None
    sync_tolerance: float | None = None
    command_mode: ControlMode = ControlMode.POSITION
    # 到達判定を持たない軸 (duty / velocity) の指令後固定待ち [s]
    settle_s: float = 0.0

    def __post_init__(self) -> None:
        if not self.motors:
            raise ValueError(f"axes.{self.name} にモータがありません")

    @property
    def scale(self) -> float:
        return self.motors[0].scale

    @property
    def offset(self) -> float:
        return self.motors[0].offset

    @property
    def motor_names(self) -> tuple[str, ...]:
        return tuple(motor.name for motor in self.motors)

    @property
    def is_paired(self) -> bool:
        return len(self.motors) > 1

    def to_command(self, value: float) -> float:
        return self.motors[0].to_command(value)

    def to_commands(self, value: float) -> dict[str, float]:
        return {motor.name: motor.to_command(value) for motor in self.motors}

    @property
    def command_tolerance(self) -> float | None:
        if self.tolerance is None:
            return None
        # 許容差は幅であって向きを持たないため、scale が負でも正の幅になるようにする
        return abs(self.tolerance * self.scale)


_AXIS_KEYS = frozenset(
    {
        "unit",
        "command_unit",
        "scale",
        "offset",
        "timeout_s",
        "tolerance",
        "motors",
        "sync_tolerance",
        "command_mode",
        "settle_s",
    }
)

_MOTOR_KEYS = frozenset({"scale", "offset"})

# 位置定数から出してよい指令モード。CURRENT を除くのは、位置名に紐付けて開ループの
# トルク指令を出す用途が無く、誤記のまま機構へ流れると破損に直結するため
_COMMAND_MODES = {
    mode.value: mode for mode in (ControlMode.POSITION, ControlMode.VELOCITY, ControlMode.DUTY)
}


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

    def sync_tolerance(self, axis: str) -> float | None:
        return self.axis(axis).sync_tolerance

    def command_mode(self, axis: str) -> ControlMode:
        return self.axis(axis).command_mode

    def settle_s(self, axis: str) -> float:
        return self.axis(axis).settle_s

    def paired_axes(self) -> tuple[str, ...]:
        """同期監視の対象となる軸 (sync_tolerance を持つ軸)。"""
        return tuple(name for name, spec in self._axes.items() if spec.sync_tolerance is not None)

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
        """モータへそのまま渡せる指令値を返す (複数モータ軸では先頭モータの値)。"""
        return self.axis(axis).to_command(self.raw(axis, name, court=court))

    def commands(self, axis: str, name: str, *, court: Court | None = None) -> dict[str, float]:
        """モータ名 → 指令値。逆回転ペアではモータごとに異なる scale が効く。"""
        return self.axis(axis).to_commands(self.raw(axis, name, court=court))


def _number(path: str, raw: dict, key: str, default: float | None) -> float | None:
    if key not in raw or raw[key] is None:
        return default
    try:
        return float(raw[key])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}.{key} が数値ではありません: {raw[key]!r}") from exc


def _parse_motors(axis_name: str, raw: dict) -> tuple[MotorSpec, ...]:
    """軸を構成するモータ群を組み立てる。

    ``motors:`` を書かない軸は「軸名 = モータ名」の単一モータ軸として扱う (既存 yaml と互換)。
    """
    if raw.get("motors") is None:
        if "motors" in raw:
            raise ValueError(f"axes.{axis_name}.motors が空です")
        # 軸直下のキー検証は _parse_axis が行うため、ここでは未知キーを見ない
        return (_parse_motor(f"axes.{axis_name}", axis_name, raw, strict_keys=False),)

    # どちらの scale が効くか曖昧なまま機構を動かすと破損に直結するため併記を拒否する
    conflicting = sorted(set(raw) & _MOTOR_KEYS)
    if conflicting:
        raise ValueError(
            f"axes.{axis_name} は motors と軸直下の {', '.join(conflicting)} を併記しています "
            "(どちらが効くか決まらないため起動を拒否します)"
        )

    motors_raw = raw["motors"]
    if not isinstance(motors_raw, dict):
        raise ValueError(f"axes.{axis_name}.motors は辞書である必要があります: {motors_raw!r}")
    if not motors_raw:
        raise ValueError(f"axes.{axis_name}.motors が空です")

    return tuple(
        _parse_motor(
            f"axes.{axis_name}.motors.{motor_name}", motor_name, motor_raw, strict_keys=True
        )
        for motor_name, motor_raw in motors_raw.items()
    )


def _parse_motor(path: str, motor_name: str, raw: object, *, strict_keys: bool) -> MotorSpec:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} は辞書である必要があります: {raw!r}")

    if strict_keys:
        unknown = set(raw) - _MOTOR_KEYS
        if unknown:
            raise ValueError(f"{path} に未知のキー: {', '.join(sorted(unknown))}")

    scale = _number(path, raw, "scale", 1.0)
    if scale is None or scale == 0.0:
        raise ValueError(f"{path}.scale が 0 です (どの値を書いても同じ位置になります)")

    return MotorSpec(
        name=motor_name,
        scale=float(scale),
        offset=float(_number(path, raw, "offset", 0.0) or 0.0),
    )


def _parse_axis(name: str, raw: object) -> AxisSpec:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"axes.{name} は辞書である必要があります: {raw!r}")

    unknown = set(raw) - _AXIS_KEYS
    if unknown:
        raise ValueError(f"axes.{name} に未知のキー: {', '.join(sorted(unknown))}")

    path = f"axes.{name}"
    motors = _parse_motors(name, raw)

    timeout_s = _number(path, raw, "timeout_s", DEFAULT_TIMEOUT_S)
    if timeout_s is None or timeout_s <= 0.0:
        raise ValueError(f"axes.{name}.timeout_s は正の秒数である必要があります: {timeout_s!r}")

    tolerance = _number(path, raw, "tolerance", None)
    if tolerance is not None and tolerance < 0.0:
        raise ValueError(f"axes.{name}.tolerance は 0 以上である必要があります: {tolerance!r}")

    sync_tolerance = _number(path, raw, "sync_tolerance", None)
    if sync_tolerance is not None:
        # 単一モータ軸では偏差の比較対象が無い。黙って無視すると
        # 「防護を書いたつもり」のまま運用に入ってしまうため読み込みを拒否する
        if len(motors) < 2:
            raise ValueError(
                f"axes.{name}.sync_tolerance はモータ 2 台以上の軸にのみ指定できます "
                f"(現在 {len(motors)} 台)"
            )
        if sync_tolerance < 0.0:
            raise ValueError(
                f"axes.{name}.sync_tolerance は 0 以上である必要があります: {sync_tolerance!r}"
            )

    command_mode = _parse_command_mode(name, raw.get("command_mode"))

    settle_s = _number(path, raw, "settle_s", 0.0)
    if settle_s is None or settle_s < 0.0:
        raise ValueError(f"axes.{name}.settle_s は 0 以上である必要があります: {settle_s!r}")

    return AxisSpec(
        name=name,
        unit=str(raw.get("unit", "")),
        command_unit=str(raw.get("command_unit", "")),
        timeout_s=float(timeout_s),
        tolerance=tolerance,
        motors=motors,
        sync_tolerance=sync_tolerance,
        command_mode=command_mode,
        settle_s=float(settle_s),
    )


def _parse_command_mode(axis_name: str, raw: object) -> ControlMode:
    if raw is None:
        return ControlMode.POSITION
    mode = _COMMAND_MODES.get(str(raw))
    if mode is None:
        allowed = ", ".join(_COMMAND_MODES)
        raise ValueError(
            f"axes.{axis_name}.command_mode に未対応の値: {raw!r} (指定できるのは {allowed})"
        )
    return mode


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
