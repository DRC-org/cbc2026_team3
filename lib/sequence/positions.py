from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from lib.axis_sync import MotorSpec, SyncGroup
from lib.drivers.base import ControlMode
from lib.match_state import Court

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "AxisSpec",
    "LimitSpec",
    "ManualSpec",
    "MotionSpec",
    "MotorSpec",
    "PositionLookupError",
    "PositionTable",
    "load_position_table",
]

DEFAULT_TIMEOUT_S = 5.0


class PositionLookupError(RuntimeError):
    """位置定数の参照に失敗したときに送出される。"""


@dataclass(frozen=True)
class ManualSpec:
    min_value: float
    max_value: float
    steps: tuple[float, ...]

    def clamp(self, value: float) -> float:
        if value < self.min_value:
            return self.min_value
        if value > self.max_value:
            return self.max_value
        return value

    def contains(self, value: float) -> bool:
        return self.min_value <= value <= self.max_value

    def to_dict(self) -> dict[str, object]:
        return {"min": self.min_value, "max": self.max_value, "steps": list(self.steps)}


@dataclass(frozen=True)
class HomingSpec:
    sensor: str | None
    direction: float
    search_distance: float
    step: float
    settle_s: float
    motor_sensors: tuple[tuple[str, str], ...] | None = None
    align_distance: float | None = None

    def __post_init__(self) -> None:
        if self.sensor is not None and self.motor_sensors is not None:
            raise ValueError(
                "homing.sensor と homing.sensors は併記できません "
                "(どちらのスイッチが原点を決めるか決まりません)。"
                "スイッチ 1 本なら sensor、モータごとに 1 本ずつなら sensors だけを書いてください"
            )
        if self.sensor is None and self.motor_sensors is None:
            raise ValueError(
                "homing には sensor (スイッチ 1 本) か "
                "sensors (モータ名 → センサ名) のどちらかが必要です"
            )
        if self.motor_sensors is not None and not self.motor_sensors:
            raise ValueError("homing.sensors が空です (見るセンサが 1 本もありません)")
        if self.direction not in (1.0, -1.0):
            raise ValueError(f"homing.direction は +1 か -1: {self.direction!r}")
        if self.search_distance <= 0.0:
            raise ValueError(f"homing.search_distance は正の値: {self.search_distance!r}")
        if self.step <= 0.0:
            raise ValueError(f"homing.step は正の値: {self.step!r}")
        if self.settle_s < 0.0:
            raise ValueError(f"homing.settle_s は 0 以上: {self.settle_s!r}")
        if self.step > self.search_distance:
            raise ValueError(
                f"homing.step ({self.step}) が "
                f"search_distance ({self.search_distance}) を超えています"
            )
        self._validate_align_distance()

    def _validate_align_distance(self) -> None:
        if self.motor_sensors is not None:
            if self.align_distance is None:
                raise ValueError(
                    "homing.sensors を書いた軸には align_distance が必要です "
                    "(片側だけを動かす整列段の唯一の無人の歯止めなので省略できません)"
                )
        elif self.align_distance is not None:
            raise ValueError(
                "homing.align_distance は sensors を書いた軸にのみ指定できます "
                "(整列段が無いので書いても効きません)"
            )

        if self.align_distance is None:
            return
        if self.align_distance <= 0.0:
            raise ValueError(f"homing.align_distance は正の値: {self.align_distance!r}")
        if self.align_distance < self.step:
            raise ValueError(
                f"homing.align_distance ({self.align_distance}) が "
                f"step ({self.step}) より小さいため 1 歩も進めません"
            )

    @property
    def sensors(self) -> Mapping[str, str] | None:
        if self.motor_sensors is None:
            return None
        return MappingProxyType(dict(self.motor_sensors))

    @property
    def sensor_names(self) -> tuple[str, ...]:
        if self.motor_sensors is not None:
            return tuple(sensor for _motor, sensor in self.motor_sensors)
        return (self.sensor,) if self.sensor is not None else ()


@dataclass(frozen=True)
class LimitSpec:
    """リミットスイッチによる機構破壊防止。**軸の機構的性質**なのでここに置く。

    「そのスイッチに触れたら、そのスイッチのある側へ進む指令を止める」ことだけを
    宣言する。**方向を持つのが要点で、逆方向は必ず通す** —— 無条件に止めると、
    端に触れた軸を戻す操作ごと塞がれ、二度と動かせない軸ができる。

    零点確定 (`HomingSpec`) と同じスイッチを見るが役割は別物である。あちらは
    「当たるまで動かして原点を決める」手順で、こちらは「当たったらそれ以上進めない」
    常駐保護。だから ``homing`` を持たない軸 (`sub_y_axis` / `sub_lift`) にも書ける。

    ``direction`` の意味は ``HomingSpec.direction`` と揃える (人間の単位での増減方向)。
    両者で符号の意味が食い違うと、同じスイッチを指す 2 つの宣言が逆向きを表すことに
    なり、config を読んでもどちらが正なのか決められない。
    """

    #: 監視するセンサ名 (config の `sensors:` に登録された名前)
    sensor: str
    #: **この向きへ進む指令を止める端**。+1 か -1 のみ
    direction: float

    def __post_init__(self) -> None:
        if not self.sensor:
            raise ValueError("limits.sensor はセンサ名の文字列が必要です")
        if self.direction not in (1.0, -1.0):
            # 0 は「どちらも止めない」= 書いたのに効かない保護、±1 以外の値は
            # 「どちら側の端か」を表せない
            raise ValueError(f"limits.direction は +1 か -1: {self.direction!r}")


@dataclass(frozen=True)
class MotionSpec:
    max_velocity: float
    max_acceleration: float
    velocity_ff: float = 0.0

    def __post_init__(self) -> None:
        if self.max_velocity <= 0.0:
            raise ValueError(f"motion.max_velocity は正の値: {self.max_velocity!r}")
        if self.max_acceleration <= 0.0:
            raise ValueError(f"motion.max_acceleration は正の値: {self.max_acceleration!r}")
        if self.velocity_ff < 0.0:
            raise ValueError(f"motion.velocity_ff は 0 以上: {self.velocity_ff!r}")

    def duration_for(self, distance: float) -> float:
        travel = abs(distance)
        if travel * self.max_acceleration <= self.max_velocity**2:
            return 2.0 * math.sqrt(travel / self.max_acceleration)
        return travel / self.max_velocity + self.max_velocity / self.max_acceleration


@dataclass(frozen=True)
class AxisSpec:
    name: str
    unit: str
    command_unit: str
    timeout_s: float
    tolerance: float | None
    motors: tuple[MotorSpec, ...]
    sync_tolerance: float | None = None
    sync_kp: float = 0.0
    sync_limit: float | None = None
    command_mode: ControlMode = ControlMode.POSITION
    settle_s: float = 0.0
    manual: ManualSpec | None = None
    manual_always: bool = False
    homing: HomingSpec | None = None
    motion: MotionSpec | None = None
    # リミットスイッチによる機構破壊防止。空ならこの軸に保護は無い
    limits: tuple[LimitSpec, ...] = ()

    def __post_init__(self) -> None:
        if not self.motors:
            raise ValueError(f"axes.{self.name} にモータがありません")
        if self.homing is not None and self.command_mode is not ControlMode.POSITION:
            raise ValueError(
                f"axes.{self.name}: homing は位置指令の軸にのみ書けます "
                f"(command_mode={self.command_mode.value})"
            )
        if self.motion is not None and self.command_mode is not ControlMode.POSITION:
            raise ValueError(
                f"axes.{self.name}: motion は位置指令の軸にのみ書けます "
                f"(command_mode={self.command_mode.value})"
            )
        # duty / on_off は現在位置も到達も観測できないので、「これ以上進めない」という
        # 判定そのものが成立しない (止める向きを決める材料が 1 つも無い)
        if self.limits and self.command_mode is not ControlMode.POSITION:
            raise ValueError(
                f"axes.{self.name}: limits は位置指令の軸にのみ書けます "
                f"(command_mode={self.command_mode.value})"
            )
        self._check_manual_always()
        self._check_homing_sensor_map()
        self._check_align_distance()
        self._check_limit_sensors()
        self._check_limit_homing_directions()

    def _check_manual_always(self) -> None:
        # 到達判定を持つ軸で許すと、シーケンスが move_to で書いた目標を手動が上書きし、
        # wait_reached が動かない位置を見続けてタイムアウトする。duty / on_off には
        # 到達判定が無く settle_s の固定待ちへ落ちるので、割り込んでも手順は壊れない
        if not self.manual_always:
            return
        if self.command_mode in (ControlMode.DUTY, ControlMode.ON_OFF):
            return
        raise ValueError(
            f"axes.{self.name}: manual_always は duty / on_off の軸にのみ書けます "
            f"(command_mode={self.command_mode.value})。到達判定を持つ軸で許すと、"
            "シーケンスが書いた目標を手動が上書きして到達待ちが必ずタイムアウトします"
        )

    def _check_homing_sensor_map(self) -> None:
        if self.homing is None or self.homing.sensors is None:
            return

        declared = set(self.homing.sensors)
        actual = set(self.motor_names)
        missing = sorted(actual - declared)
        extra = sorted(declared - actual)
        if not missing and not extra:
            return
        raise ValueError(
            f"axes.{self.name}.homing.sensors のキーが motors と一致しません "
            f"(不足: {', '.join(missing) or 'なし'} / 余分: {', '.join(extra) or 'なし'})"
        )

    def _check_align_distance(self) -> None:
        if self.homing is None or self.homing.align_distance is None:
            return
        if self.sync_tolerance is None:
            return
        if self.homing.align_distance < self.sync_tolerance:
            return
        raise ValueError(
            f"axes.{self.name}.homing.align_distance ({self.homing.align_distance}) は "
            f"sync_tolerance ({self.sync_tolerance}) より小さい必要があります "
            "(整列段のずれが偏差許容差に届くと零点確定の最中に緊急停止します)"
        )

    def _check_limit_sensors(self) -> None:
        """同じセンサを同じ軸へ 2 度書かせない。

        2 本の宣言が同じスイッチを指すと、向きが同じなら片方は何もせず、向きが
        違えばその軸は両方向とも塞がれる。どちらにしても「片方を直したのに
        もう片方が残る」形にしかならず、config を読んでも効いているほうが決まらない。
        """
        seen: set[str] = set()
        duplicated: set[str] = set()
        for limit in self.limits:
            if limit.sensor in seen:
                duplicated.add(limit.sensor)
            seen.add(limit.sensor)
        if duplicated:
            raise ValueError(
                f"axes.{self.name}.limits に同じセンサが複数あります: "
                f"{', '.join(sorted(duplicated))}"
            )

    def _check_limit_homing_directions(self) -> None:
        """同じスイッチを指す ``homing`` と ``limits`` が同じ向きを表しているか見る。

        食い違うと「零点確定に向かう向きの指令が保護で止まる」形で必ず失敗する。
        しかも零点確定の最中はその保護を外している (``LimitGuard.suspend_sensors``)
        ので、**症状が出るのは零点確定が終わった後の通常の指令**である ——
        「原点合わせは通るのに、その向きへ動かすと途中で止まる」としか見えず、
        config を読んでもどちらの符号が正なのか決められない。

        ここで見るのは**両方が指しているセンサだけ**。``limits`` にしか無い
        スイッチ (反対端のリミット) は零点確定と無関係なので、向きが逆で正しい。

        **この ``ValueError`` は起動を止めない。** ``main._load_position_table_file``
        は位置定数の読み込み失敗を「定数なしで起動」へ倒すので、実際に起きるのは
        **そのロボットの位置定数がまるごと消え、軸 0 本で立ち上がる**ことである
        (保護もシーケンスも零点確定も同時に無くなる)。起動ログの ERROR 1 行が
        唯一の手掛かりなので、この検査を足す側は「拒否されるから安全」と読んでは
        ならない。
        """
        if self.homing is None or not self.limits:
            return

        homing_sensors = set(self.homing.sensor_names)
        mismatched = [
            f"{limit.sensor} (limits {limit.direction:+g} / homing {self.homing.direction:+g})"
            for limit in self.limits
            if limit.sensor in homing_sensors and limit.direction != self.homing.direction
        ]
        if mismatched:
            raise ValueError(
                f"axes.{self.name}: 同じセンサを指す homing と limits で direction が"
                f"食い違っています: {', '.join(mismatched)}"
            )

    @property
    def motor_names(self) -> tuple[str, ...]:
        return tuple(motor.name for motor in self.motors)

    def to_commands(self, value: float) -> dict[str, float]:
        return {motor.name: motor.to_command(value) for motor in self.motors}

    def to_commands_each(self, values: Mapping[str, float]) -> dict[str, float]:
        missing = sorted(name for name in self.motor_names if name not in values)
        extra = sorted(set(values) - set(self.motor_names))
        if missing or extra:
            raise KeyError(
                f"軸 '{self.name}' のモータと一致しません "
                f"(不足: {', '.join(missing) or 'なし'} / 余分: {', '.join(extra) or 'なし'})"
            )
        return {motor.name: motor.to_command(values[motor.name]) for motor in self.motors}

    def to_value(self, commands: Mapping[str, float]) -> float:
        values = [
            motor.to_value(commands[motor.name]) for motor in self.motors if motor.name in commands
        ]
        if not values:
            raise PositionLookupError(f"軸 '{self.name}' の位置を算出できる値がありません")
        return sum(values) / len(values)

    @property
    def sync_group(self) -> SyncGroup | None:
        if self.sync_tolerance is None:
            return None
        return SyncGroup(
            name=self.name,
            members=self.motors,
            tolerance=self.sync_tolerance,
            sync_kp=self.sync_kp,
            sync_limit=self.sync_limit,
        )


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
        "sync_kp",
        "sync_limit",
        "command_mode",
        "settle_s",
        "manual",
        "manual_always",
        "homing",
        "motion",
        "limits",
    }
)

_MOTOR_KEYS = frozenset({"scale", "offset"})

_MANUAL_KEYS = frozenset({"min", "max", "steps"})

_HOMING_KEYS = frozenset(
    {"sensor", "sensors", "direction", "search_distance", "step", "settle_s", "align_distance"}
)
_HOMING_REQUIRED = frozenset({"direction", "search_distance", "step"})

_LIMIT_KEYS = frozenset({"sensor", "direction"})
#: **どちらも省略できない。** センサを省けば「どのスイッチも見ない保護」、方向を
#: 既定値で埋めれば「どちら側の端かを config が決めていない保護」になり、いずれも
#: 書いたのに効かない (`sync_kp` / `sync_limit` や `homing.search_distance` と同じ方針)
_LIMIT_REQUIRED = frozenset({"sensor", "direction"})

_MOTION_KEYS = frozenset({"max_velocity", "max_acceleration", "velocity_ff"})
_MOTION_REQUIRED = frozenset({"max_velocity", "max_acceleration"})

_DEFAULT_MANUAL_STEPS: tuple[float, ...] = (1.0,)

_COMMAND_MODES = {
    mode.value: mode
    for mode in (
        ControlMode.POSITION,
        ControlMode.VELOCITY,
        ControlMode.DUTY,
        ControlMode.ON_OFF,
    )
}


class PositionTable:
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

    @classmethod
    def merged(cls, tables: Sequence[PositionTable]) -> PositionTable:
        axes: dict[str, AxisSpec] = {}
        positions: dict[str, dict[str, float | dict[str, float]]] = {}
        owner: dict[str, str] = {}

        for table in tables:
            for name, spec in table._axes.items():
                if name in axes:
                    raise ValueError(
                        f"軸 '{name}' が複数の位置定数に定義されています "
                        f"({owner[name]} と {table.source})。"
                        "軸名はロボット横断に一意でなければなりません"
                    )
                axes[name] = spec
                owner[name] = table.source
            for name, values in table._positions.items():
                positions[name] = dict(values)

        source = " + ".join(table.source for table in tables) or "<merged>"
        return cls(axes, positions, source=source)

    @property
    def source(self) -> str:
        return self._source

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

    def sync_tolerance(self, axis: str) -> float | None:
        return self.axis(axis).sync_tolerance

    def manual_axes(self) -> tuple[str, ...]:
        return tuple(name for name, spec in self._axes.items() if spec.manual is not None)

    def manual_always_axes(self) -> tuple[str, ...]:
        return tuple(name for name, spec in self._axes.items() if spec.manual_always)

    def paired_axes(self) -> tuple[str, ...]:
        return tuple(name for name, spec in self._axes.items() if spec.sync_tolerance is not None)

    def limit_axes(self) -> tuple[str, ...]:
        """リミットスイッチ保護の対象となる軸 (``limits:`` を持つ軸)。"""
        return tuple(name for name, spec in self._axes.items() if spec.limits)

    def limits(self, axis: str) -> tuple[LimitSpec, ...]:
        """その軸の保護宣言。書いていない軸は空。"""
        return self.axis(axis).limits

    def raw(self, axis: str, name: str, *, court: Court | None = None) -> float:
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

    def commands(self, axis: str, name: str, *, court: Court | None = None) -> dict[str, float]:
        return self.axis(axis).to_commands(self.raw(axis, name, court=court))


def _number(path: str, raw: dict, key: str, default: float | None) -> float | None:
    if key not in raw or raw[key] is None:
        return default
    try:
        return float(raw[key])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}.{key} が数値ではありません: {raw[key]!r}") from exc


def _parse_motors(axis_name: str, raw: dict) -> tuple[MotorSpec, ...]:
    if raw.get("motors") is None:
        if "motors" in raw:
            raise ValueError(f"axes.{axis_name}.motors が空です")
        return (_parse_motor(f"axes.{axis_name}", axis_name, raw, strict_keys=False),)

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
        if len(motors) < 2:
            raise ValueError(
                f"axes.{name}.sync_tolerance はモータ 2 台以上の軸にのみ指定できます "
                f"(現在 {len(motors)} 台)"
            )
        if sync_tolerance < 0.0:
            raise ValueError(
                f"axes.{name}.sync_tolerance は 0 以上である必要があります: {sync_tolerance!r}"
            )

    sync_kp, sync_limit = _parse_sync_gain(name, path, raw, motors, sync_tolerance)

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
        sync_kp=sync_kp,
        sync_limit=sync_limit,
        command_mode=command_mode,
        settle_s=float(settle_s),
        manual=_parse_manual(name, raw.get("manual"), command_mode),
        manual_always=_parse_manual_always(name, raw.get("manual_always")),
        homing=_parse_homing(name, raw.get("homing")),
        motion=_parse_motion(name, raw.get("motion")),
        limits=_parse_limits(name, raw.get("limits")),
    )


def _parse_sync_gain(
    name: str,
    path: str,
    raw: Mapping[str, object],
    motors: tuple[MotorSpec, ...],
    sync_tolerance: float | None,
) -> tuple[float, float | None]:
    sync_kp = _number(path, raw, "sync_kp", 0.0)
    sync_limit = _number(path, raw, "sync_limit", None)

    if sync_kp is None:
        raise ValueError(f"axes.{name}.sync_kp は数値である必要があります")
    if sync_kp < 0.0:
        raise ValueError(
            f"axes.{name}.sync_kp は 0 以上である必要があります (負値は正帰還に"
            f"なりずれが発散します): {sync_kp!r}"
        )
    if sync_limit is not None and sync_limit < 0.0:
        raise ValueError(f"axes.{name}.sync_limit は 0 以上である必要があります: {sync_limit!r}")

    if sync_kp == 0.0:
        if "sync_limit" in raw and "sync_kp" not in raw:
            raise ValueError(
                f"axes.{name}.sync_limit だけが指定されています "
                "(sync_kp が無いので同期補正は一切出ません)"
            )
        return 0.0, sync_limit

    if len(motors) < 2:
        raise ValueError(
            f"axes.{name}.sync_kp はモータ 2 台以上の軸にのみ指定できます (現在 {len(motors)} 台)"
        )
    if sync_tolerance is None:
        raise ValueError(
            f"axes.{name}.sync_kp には sync_tolerance が必要です "
            "(同期グループが作られないため補正も監視も効きません)"
        )
    if sync_limit is None:
        raise ValueError(
            f"axes.{name}.sync_kp を指定するなら sync_limit も必要です "
            "(押し合いの歯止めが無くなります)"
        )
    return float(sync_kp), float(sync_limit)


def _parse_homing(axis_name: str, raw: object) -> HomingSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"axes.{axis_name}.homing は辞書である必要があります: {raw!r}")

    unknown = sorted(set(raw) - _HOMING_KEYS)
    if unknown:
        raise ValueError(
            f"axes.{axis_name}.homing に未知のキー: {', '.join(unknown)} "
            f"(指定できるのは {', '.join(sorted(_HOMING_KEYS))})"
        )

    missing = sorted(_HOMING_REQUIRED - set(raw))
    if missing:
        raise ValueError(f"axes.{axis_name}.homing に必須キーがありません: {', '.join(missing)}")

    path = f"axes.{axis_name}.homing"
    sensor = raw.get("sensor")
    if sensor is not None and (not isinstance(sensor, str) or not sensor):
        raise ValueError(f"{path}.sensor はセンサ名の文字列: {sensor!r}")

    motor_sensors = _parse_homing_sensor_map(path, raw.get("sensors"))
    align_distance = _number(path, raw, "align_distance", None)

    try:
        return HomingSpec(
            sensor=sensor,
            motor_sensors=motor_sensors,
            direction=float(raw["direction"]),
            search_distance=float(raw["search_distance"]),
            step=float(raw["step"]),
            settle_s=float(raw.get("settle_s", 0.05)),
            align_distance=align_distance,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}: {exc}") from exc


def _parse_homing_sensor_map(path: str, raw: object) -> tuple[tuple[str, str], ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{path}.sensors はモータ名 → センサ名の辞書: {raw!r}")

    pairs: list[tuple[str, str]] = []
    for motor_name, sensor_name in raw.items():
        if not isinstance(motor_name, str) or not motor_name:
            raise ValueError(f"{path}.sensors のキーはモータ名の文字列: {motor_name!r}")
        if not isinstance(sensor_name, str) or not sensor_name:
            raise ValueError(f"{path}.sensors.{motor_name} はセンサ名の文字列: {sensor_name!r}")
        pairs.append((motor_name, sensor_name))
    return tuple(pairs)


def _parse_limits(axis_name: str, raw: object) -> tuple[LimitSpec, ...]:
    """リミットスイッチ保護の宣言を読む。書かない軸は空 (保護なし)。

    値の妥当性 (方向が ±1 か) は ``LimitSpec.__post_init__`` が見る。ここで見るのは
    キーの綴りと型、そして必須キーが揃っていることだけ (``_parse_homing`` と同じ分担)。

    **空リストは拒否する。** ``limits: []`` は「保護を書いた」ようにしか読めないのに
    1 本も監視しないので、書き忘れと区別が付かない。保護なしを表す書き方は
    「キーごと書かない」ただ 1 つに保つ。
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"axes.{axis_name}.limits はリストである必要があります: {raw!r}")
    if not raw:
        raise ValueError(
            f"axes.{axis_name}.limits が空です (保護しないなら limits を書かないでください)"
        )

    specs: list[LimitSpec] = []
    for index, entry in enumerate(raw):
        path = f"axes.{axis_name}.limits[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{path} は辞書である必要があります: {entry!r}")

        unknown = sorted(set(entry) - _LIMIT_KEYS)
        if unknown:
            raise ValueError(
                f"{path} に未知のキー: {', '.join(unknown)} "
                f"(指定できるのは {', '.join(sorted(_LIMIT_KEYS))})"
            )

        missing = sorted(_LIMIT_REQUIRED - set(entry))
        if missing:
            raise ValueError(f"{path} に必須キーがありません: {', '.join(missing)}")

        sensor = entry["sensor"]
        if not isinstance(sensor, str) or not sensor:
            raise ValueError(f"{path}.sensor はセンサ名の文字列: {sensor!r}")

        try:
            specs.append(LimitSpec(sensor=sensor, direction=float(entry["direction"])))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path}: {exc}") from exc

    return tuple(specs)


def _parse_motion(axis_name: str, raw: object) -> MotionSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"axes.{axis_name}.motion は辞書である必要があります: {raw!r}")

    unknown = sorted(set(raw) - _MOTION_KEYS)
    if unknown:
        raise ValueError(
            f"axes.{axis_name}.motion に未知のキー: {', '.join(unknown)} "
            f"(指定できるのは {', '.join(sorted(_MOTION_KEYS))})"
        )

    missing = sorted(key for key in _MOTION_REQUIRED if raw.get(key) is None)
    if missing:
        raise ValueError(
            f"axes.{axis_name}.motion に必須キーがありません: {', '.join(missing)} "
            "(速度と加速度は対で初めて軌道が決まります)"
        )

    path = f"axes.{axis_name}.motion"
    try:
        return MotionSpec(
            max_velocity=float(raw["max_velocity"]),
            max_acceleration=float(raw["max_acceleration"]),
            velocity_ff=float(raw.get("velocity_ff", 0.0)),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}: {exc}") from exc


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


def _parse_manual(axis_name: str, raw: object, command_mode: ControlMode) -> ManualSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"axes.{axis_name}.manual は辞書である必要があります: {raw!r}")

    unknown = set(raw) - _MANUAL_KEYS
    if unknown:
        raise ValueError(f"axes.{axis_name}.manual に未知のキー: {', '.join(sorted(unknown))}")

    if command_mode is not ControlMode.POSITION:
        raise ValueError(
            f"axes.{axis_name}.manual は command_mode: position の軸にのみ指定できます "
            f"(現在 {command_mode.value})"
        )

    path = f"axes.{axis_name}.manual"
    min_value = _number(path, raw, "min", None)
    max_value = _number(path, raw, "max", None)
    if min_value is None or max_value is None:
        missing = ", ".join(key for key in ("min", "max") if raw.get(key) is None)
        raise ValueError(f"{path} に {missing} がありません (可動範囲が決まりません)")
    if min_value >= max_value:
        raise ValueError(f"{path}.min は max より小さい必要があります: {min_value} >= {max_value}")

    steps = _parse_manual_steps(path, raw.get("steps"))
    return ManualSpec(min_value=float(min_value), max_value=float(max_value), steps=steps)


def _parse_manual_always(axis_name: str, raw: object) -> bool:
    # 型だけを見る (command_mode との整合は AxisSpec.__post_init__)。"false" のような
    # 文字列を真と読むと、書いたつもりの無い軸がシーケンス中に手動で動かせてしまう
    if raw is None:
        return False
    if not isinstance(raw, bool):
        raise ValueError(f"axes.{axis_name}.manual_always は真偽値である必要があります: {raw!r}")
    return raw


def _parse_manual_steps(path: str, raw: object) -> tuple[float, ...]:
    if raw is None:
        return _DEFAULT_MANUAL_STEPS
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path}.steps は 1 つ以上の数値のリストである必要があります: {raw!r}")

    steps: list[float] = []
    for entry in raw:
        if isinstance(entry, bool) or not isinstance(entry, int | float):
            raise ValueError(f"{path}.steps に数値でない要素: {entry!r}")
        if entry <= 0.0:
            raise ValueError(f"{path}.steps は正の値である必要があります: {entry!r}")
        steps.append(float(entry))
    return tuple(steps)


def _parse_value(axis: str, name: str, raw: object) -> float | dict[str, float]:
    if isinstance(raw, dict):
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
        _check_manual_range(source, axes[axis], positions[axis])
        _check_motion_timeout(source, axes[axis], positions[axis])

    return PositionTable(axes, positions, source=source)


def _check_motion_timeout(
    source: str,
    spec: AxisSpec,
    values: Mapping[str, float | dict[str, float]],
) -> None:
    motion = spec.motion
    if motion is None:
        return

    candidates = [
        float(candidate)
        for value in values.values()
        for candidate in (value.values() if isinstance(value, dict) else (value,))
    ]
    if len(candidates) < 2:
        return

    span = max(candidates) - min(candidates)
    required = motion.duration_for(span)
    if required <= spec.timeout_s:
        return

    suggested = math.ceil(required * 100.0) / 100.0
    raise ValueError(
        f"{source}: axes.{spec.name} は motion の制限では最大移動 {span} {spec.unit} に "
        f"{required:.3f} 秒かかり、timeout_s ({spec.timeout_s}) を必ず超えます "
        f"(timeout_s を {suggested} 以上にするか、max_velocity / max_acceleration を"
        "上げてください)"
    )


def _check_manual_range(
    source: str,
    spec: AxisSpec,
    values: Mapping[str, float | dict[str, float]],
) -> None:
    manual = spec.manual
    if manual is None:
        return

    outside: list[str] = []
    for name, value in values.items():
        candidates = value.values() if isinstance(value, dict) else (value,)
        for candidate in candidates:
            if not manual.contains(float(candidate)):
                outside.append(f"{name}={candidate}")
                break

    if outside:
        raise ValueError(
            f"{source}: positions.{spec.name} の {', '.join(outside)} が "
            f"axes.{spec.name}.manual の範囲 [{manual.min_value}, {manual.max_value}] の外です "
            "(シーケンスで行ける位置へ手動で行けない軸になるため起動を拒否します)"
        )
