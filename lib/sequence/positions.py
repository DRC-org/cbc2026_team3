from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType

from lib.axis_sync import MotorSpec, SyncGroup
from lib.drivers.base import ControlMode
from lib.match_state import Court

# 可動端インターロック・跳躍量・トルクの判断は最下位層に閉じてある。ここは
# 「宣言を yaml から読む」だけで、判断そのものは持たない
from lib.motion_guard import LimitSpec, MotionGuardSpec, RequiredRange
from lib.sequence.interlock import InterlockSpec

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "AxisSpec",
    "CourtMotorSpec",
    "CourtUnresolvedError",
    "InterlockSpec",
    "ManualSpec",
    "MotionGuardSpec",
    "MotionSpec",
    "MotorSpec",
    "PositionLookupError",
    "PositionTable",
    "TravelSpec",
    "load_position_table",
]

DEFAULT_TIMEOUT_S = 5.0


class PositionLookupError(RuntimeError):
    """位置定数の参照に失敗したときに送出される。"""


class CourtUnresolvedError(RuntimeError):
    """コート別の scale を持つ軸を、コートを解決せずに換算しようとした。"""


@dataclass(frozen=True)
class CourtMotorSpec(MotorSpec):
    """コートで回転の向きが鏡になるモータ。`scale` は大きさだけで、向きは `for_court` が決める。"""

    court_scales: tuple[tuple[str, float], ...] = ()

    def for_court(self, court: Court) -> MotorSpec:
        return MotorSpec(
            name=self.name, scale=dict(self.court_scales)[court.value], offset=self.offset
        )

    def to_command(self, value: float) -> float:
        raise self._unresolved()

    def to_value(self, command: float) -> float:
        raise self._unresolved()

    def to_tolerance(self, tolerance: float) -> float:
        raise self._unresolved()

    def _unresolved(self) -> CourtUnresolvedError:
        return CourtUnresolvedError(
            f"モータ '{self.name}' の scale はコート別なのにコートが解決されていません "
            "(AxisSpec.for_court を通してください)"
        )


@dataclass(frozen=True)
class TravelSpec:
    """軸が**機械的に到達しうる**範囲。`ManualSpec` とは別物である。

    あちらは「手動操縦で動かしてよい範囲」で、意図的に狭められる。こちらは
    機構が届いてしまう範囲なので、1 つの値に載せると片方を狭めた瞬間に
    もう片方が黙って壊れる。
    """

    min_value: float
    max_value: float

    @property
    def span(self) -> float:
        return self.max_value - self.min_value

    def to_dict(self) -> dict[str, object]:
        return {"min": self.min_value, "max": self.max_value}


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

    def clamp_from(self, origin: float, value: float) -> float:
        """起点から動かす指令を丸める。**範囲を起点まで広げてから丸める。**

        ``clamp`` をそのまま使うと、起点が範囲の外に居るときだけ 1 歩が刻み幅を
        無視して境界まで飛ぶ (実機で実測 +9.96mm・``max`` 2.0mm の軸へ -1.0 の
        ジョグを送り、約 8mm 動いた)。**零点確定がまだの軸は原点が電源投入位置
        なので、範囲の外に居るのは異常ではなく普通である。**

        外へ広げる向きは従来どおり境界で止まるので、範囲の内側から呼ぶ限り
        ``clamp`` と一致する。範囲の外からは「寄る向きにだけ動ける」ことになり、
        丸めた結果が起点から離れる量は必ず ``|value - origin|`` 以下になる。
        """
        low = min(self.min_value, origin)
        high = max(self.max_value, origin)
        if value < low:
            return low
        if value > high:
            return high
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
    #: 触れた状態から離れるのに許す距離 [軸の unit]。None なら step の既定倍数。
    #: ON 区間の広さで決まる値なので、step の倍数に頼ると精度を上げるほど離れられなくなる
    release_distance: float | None = None
    #: 粗探索の刻み [軸の unit]。書くと二段探索になる。時間の問題を解く値で、精度は step が決める
    coarse_step: float | None = None
    motor_sensors: tuple[tuple[str, str], ...] | None = None
    align_distance: float | None = None
    #: 整列段 1 歩の刻み [軸の unit]。None なら step と同じ。
    #: 探索段は左右 2 台で押すのに整列段は 1 台なので、同じ刻みでは同じ押しが出ない
    align_step: float | None = None
    #: 零点確定の直後に退避する位置名 (`positions.<軸>` のキー)。原点姿勢が他の軸と
    #: 干渉する軸で、次の軸を寄せる前に干渉域を抜けるために要る。距離ではなく位置名で
    #: 持つのは、0.0 そのものがスイッチの動作点だから (`sub_y_axis` と同じ流儀)
    retreat_position: str | None = None

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
        if self.release_distance is not None and self.release_distance <= 0.0:
            raise ValueError(f"homing.release_distance は正の値: {self.release_distance!r}")
        if self.step > self.search_distance:
            raise ValueError(
                f"homing.step ({self.step}) が "
                f"search_distance ({self.search_distance}) を超えています"
            )
        self._validate_coarse_step()
        self._validate_align_distance()
        self._validate_align_step()

    def _validate_coarse_step(self) -> None:
        if self.coarse_step is None:
            return
        if self.coarse_step <= 0.0:
            raise ValueError(f"homing.coarse_step は正の値: {self.coarse_step!r}")
        if self.coarse_step <= self.step:
            # 粗くない粗探索は所要時間を倍にするだけ
            raise ValueError(
                f"homing.coarse_step ({self.coarse_step}) は "
                f"step ({self.step}) より大きい必要があります"
            )
        if self.coarse_step > self.search_distance:
            raise ValueError(
                f"homing.coarse_step ({self.coarse_step}) が "
                f"search_distance ({self.search_distance}) を超えています"
            )
        if self.release_distance is None:
            # 離脱は粗い刻みで動くのに、既定の許容は step から作られるので桁が合わない
            raise ValueError("homing.coarse_step を指定するなら release_distance も必要です")

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

    def _validate_align_step(self) -> None:
        if self.align_step is None:
            return
        if self.motor_sensors is None:
            raise ValueError(
                "homing.align_step は sensors を書いた軸にのみ指定できます "
                "(整列段が無いので書いても効きません)"
            )
        if self.align_step <= 0.0:
            raise ValueError(f"homing.align_step は正の値: {self.align_step!r}")
        if self.align_step < self.step:
            # 整列段は 1 台で押すので、探索段より細かい刻みでは静止摩擦を越えられない
            raise ValueError(
                f"homing.align_step ({self.align_step}) は "
                f"step ({self.step}) 以上である必要があります"
            )
        if self.align_distance is not None and self.align_step >= self.align_distance:
            raise ValueError(
                f"homing.align_step ({self.align_step}) が "
                f"align_distance ({self.align_distance}) 以上のため 1 歩で上限を越えます"
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
    # 軸が機械的に到達しうる範囲。**`manual` の流用ではない** (あちらは手動操縦で
    # 動かしてよい範囲で、意図的に狭められる)。1 回転未満なら電文値から論理角への
    # 等価表現が 1 つに決まるので、ドライバが回転数を一意化できる。None なら
    # 一意化しない —— 「可動域 0」ではないので既定値では埋めない
    travel: TravelSpec | None = None
    homing: HomingSpec | None = None
    motion: MotionSpec | None = None
    # ドライバ内蔵の位置ループが速度を決める軸が、実測で下回らないと分かっている速さ
    # [unit/s]。timeout_s の検算にだけ使い、指令には影響しない。None なら検算しない
    # (書かない = 「速度が分からない」であって「速い」ではない)
    min_speed: float | None = None
    # 指令を出す直前の歯止め (可動端インターロック・跳躍量・トルク)。
    # None ならこの軸は今までどおり素通り。既定値で埋めないのは、埋めた値が
    # 効いているのか書き忘れなのかがコードから読めなくなるため
    guard: MotionGuardSpec | None = None

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
        if self.min_speed is not None:
            if self.command_mode is not ControlMode.POSITION:
                raise ValueError(
                    f"axes.{self.name}: min_speed は位置指令の軸にのみ書けます "
                    f"(command_mode={self.command_mode.value})"
                )
            if self.min_speed <= 0.0:
                raise ValueError(f"axes.{self.name}.min_speed は正の値: {self.min_speed!r}")
            # 速度を PC 側で決める軸の所要時間は motion が厳密に持つ。両方あると
            # どちらで検算したかが読めなくなる
            if self.motion is not None:
                raise ValueError(
                    f"axes.{self.name}: min_speed と motion は併記できません "
                    "(motion を書いた軸の所要時間は motion から決まります)"
                )
        # duty / on_off は現在位置が常に 0 として読めるので、書けても 1 度も判定しない
        if self.guard is not None and self.command_mode is not ControlMode.POSITION:
            raise ValueError(
                f"axes.{self.name}: guard は位置指令の軸にのみ書けます "
                f"(command_mode={self.command_mode.value})"
            )
        if self.travel is not None and self.command_mode is not ControlMode.POSITION:
            raise ValueError(
                f"axes.{self.name}: travel は位置指令の軸にのみ書けます "
                f"(command_mode={self.command_mode.value})"
            )
        self._check_manual_always()
        self._check_homing_sensor_map()
        self._check_align_distance()
        self._check_guard_limits_match_homing()
        self._check_court_scale_sync()

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

    def _check_guard_limits_match_homing(self) -> None:
        """零点確定で当てに行くスイッチは、**同じ向きの可動端**として宣言されていること。

        両者はセンサ名の文字列でしか繋がっていないので、取り違えは黙って通る。
        現れ方は 2 つとも「機構を壊すまで出ない」:

        - **載せ忘れ** (左右 2 本のうち 1 本だけ書いた等) —— そのスイッチは探索で
          当てた後、誰も守らない端として残る
        - **逆側へ載せた** —— 守りが反転し、押されている端へ進む指令だけが通る。
          しかも零点確定は 1 歩目から拒否されるので、症状は「その軸だけ
          いつも零点確定で失敗する」になり、原因が config から読めない

        `guard.limits` を書かない軸 (歯止めそのものが無い) は対象外。
        """
        if self.homing is None or self.guard is None or self.guard.limits is None:
            return
        limits = self.guard.limits
        toward, away = (
            (limits.minus, limits.plus)
            if self.homing.direction < 0
            else (limits.plus, limits.minus)
        )
        side = "minus" if self.homing.direction < 0 else "plus"
        opposite = "plus" if self.homing.direction < 0 else "minus"

        wrong = sorted(name for name in self.homing.sensor_names if name in away)
        if wrong:
            raise ValueError(
                f"axes.{self.name}.guard.limits.{opposite} に原点センサ "
                f"{', '.join(wrong)} が書かれています "
                f"(homing.direction={self.homing.direction:+g} なので {side} 側です)。"
                "逆に書くと守りが反転し、押されている端へ進む指令だけが通ります"
            )

        missing = sorted(name for name in self.homing.sensor_names if name not in toward)
        if missing:
            raise ValueError(
                f"axes.{self.name}.guard.limits.{side} に原点センサ "
                f"{', '.join(missing)} がありません "
                "(探索で当てに行く端は必ず歯止めにも載せること。"
                "載せ忘れた 1 本は「守られていない端」として残ります)"
            )

    def _check_court_scale_sync(self) -> None:
        # 同期監視は起動時に 1 度だけ組まれてコートを知らないので、走行中に換算で落ちる
        if self.sync_tolerance is not None and self.court_dependent:
            raise ValueError(
                f"axes.{self.name}: sync_tolerance とコート別の scale は併用できません "
                "(同期監視はコートを知りません)"
            )

    @property
    def motor_names(self) -> tuple[str, ...]:
        return tuple(motor.name for motor in self.motors)

    @property
    def court_dependent(self) -> bool:
        return any(isinstance(motor, CourtMotorSpec) for motor in self.motors)

    def for_court(self, court: Court | None) -> AxisSpec:
        """コートを解決した spec。**未確定 (`None`) なら解決せずに返す。**

        未確定を黙って片方のコートへ倒さないことで、コート依存軸だけが
        `require_resolved` で落ち、非依存軸は今までどおり通る。
        """
        if court is None or not self.court_dependent:
            return self
        return replace(
            self,
            motors=tuple(
                motor.for_court(court) if isinstance(motor, CourtMotorSpec) else motor
                for motor in self.motors
            ),
        )

    def require_resolved(self) -> None:
        # 黙って片方のコートを採ると、忘れた経路だけが赤コートで鏡になり config からも読めない
        if self.court_dependent:
            raise CourtUnresolvedError(
                f"軸 '{self.name}' の scale はコート別なのにコートが解決されていません "
                "(AxisSpec.for_court を通してください)"
            )

    def to_commands(self, value: float) -> dict[str, float]:
        self.require_resolved()
        return {motor.name: motor.to_command(value) for motor in self.motors}

    def to_commands_each(self, values: Mapping[str, float]) -> dict[str, float]:
        self.require_resolved()
        missing = sorted(name for name in self.motor_names if name not in values)
        extra = sorted(set(values) - set(self.motor_names))
        if missing or extra:
            raise KeyError(
                f"軸 '{self.name}' のモータと一致しません "
                f"(不足: {', '.join(missing) or 'なし'} / 余分: {', '.join(extra) or 'なし'})"
            )
        return {motor.name: motor.to_command(values[motor.name]) for motor in self.motors}

    def to_value(self, commands: Mapping[str, float]) -> float:
        self.require_resolved()
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
        "min_speed",
        "guard",
        "travel",
    }
)

_MOTOR_KEYS = frozenset({"scale", "offset"})

_MANUAL_KEYS = frozenset({"min", "max", "steps"})

_TRAVEL_KEYS = frozenset({"min", "max"})

_HOMING_KEYS = frozenset(
    {
        "sensor",
        "sensors",
        "direction",
        "search_distance",
        "step",
        "settle_s",
        "align_distance",
        "align_step",
        "release_distance",
        "coarse_step",
        "retreat_position",
    }
)
#: 探索距離を既定値で埋めると、配線が抜けた状態で機構端まで押し込む経路ができる
_HOMING_REQUIRED = frozenset({"direction", "search_distance", "step"})

_GUARD_KEYS = frozenset({"limits", "max_step", "stall_torque", "requires", "not_with"})

_GUARD_REQUIRES_KEYS = frozenset({"axis", "at", "between"})

_GUARD_LIMIT_KEYS = frozenset({"plus", "minus"})

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
        interlocks: Sequence[InterlockSpec] = (),
        source: str = "<inline>",
    ) -> None:
        self._axes: dict[str, AxisSpec] = dict(axes)
        self._positions: dict[str, dict[str, float | dict[str, float]]] = {
            axis: dict(values) for axis, values in positions.items()
        }
        self._interlocks = tuple(interlocks)
        self._source = source

    @classmethod
    def empty(cls, *, source: str = "<inline>") -> PositionTable:
        return cls({}, {}, source=source)

    @classmethod
    def merged(cls, tables: Sequence[PositionTable]) -> PositionTable:
        axes: dict[str, AxisSpec] = {}
        positions: dict[str, dict[str, float | dict[str, float]]] = {}
        interlocks: list[InterlockSpec] = []
        owner: dict[str, str] = {}

        for table in tables:
            interlocks.extend(table._interlocks)
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
        return cls(axes, positions, interlocks=interlocks, source=source)

    @property
    def source(self) -> str:
        return self._source

    @property
    def axes(self) -> tuple[str, ...]:
        return tuple(self._axes)

    @property
    def interlocks(self) -> tuple[InterlockSpec, ...]:
        return self._interlocks

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

    def court_dependent_axes(self) -> tuple[str, ...]:
        """**指令の換算がコートで鏡になる軸。**コートが決まるまで 1 通も出せない。

        コート未確定のゲートはこれを単一情報源にする。UI にもサーバーにも軸名を
        書き写さないため。コート別の**位置の値**を持つだけの軸はここに入らない
        (それは `court_dependent_position_axes`)。
        """
        return tuple(name for name, spec in self._axes.items() if spec.court_dependent)

    def court_dependent_position_axes(self) -> tuple[str, ...]:
        """コート別の値を持つ位置を抱える軸。**その位置名を引くときだけコートが要る。**

        換算そのものは両コートで同じなので、他の位置や連続値の指令は未確定でも通る。
        """
        return tuple(
            axis
            for axis, values in self._positions.items()
            if any(isinstance(value, dict) for value in values.values())
        )

    def homing_prerequisites(self, axes: Collection[str] | None = None) -> dict[str, str]:
        """`axes` を動かす前に寄せておく軸と、その位置名。**`guard.requires` から導く。**

        入るのは `at:` (1 点) で書かれた条件だけで、`between:` は寄せ先が一意に
        決まらないので入らない (順序を決めるのには効かせる)。**動作確認の手順が
        軸名を書き写さずに済む唯一の口**で、宣言を変えれば手順も一緒に変わる。

        同じ軸へ別々の位置を要求する宣言は読み込み時に弾いてあるので、ここで
        後勝ちに潰れることはない。
        """
        targets = self.axes if axes is None else tuple(axes)
        prerequisites: dict[str, str] = {}
        for name in targets:
            guard = self.axis(name).guard
            for required in () if guard is None else guard.requires:
                position = required.single_position
                if position is not None:
                    prerequisites[required.axis] = position
        return prerequisites

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
        spec = self.axis(axis)
        if court is not None:
            spec = spec.for_court(court)
        return spec.to_commands(self.raw(axis, name, court=court))


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

    offset = float(_number(path, raw, "offset", 0.0) or 0.0)
    if isinstance(raw.get("scale"), dict):
        return _parse_court_motor(path, motor_name, raw["scale"], offset)

    scale = _number(path, raw, "scale", 1.0)
    if scale is None or scale == 0.0:
        raise ValueError(f"{path}.scale が 0 です (どの値を書いても同じ位置になります)")

    return MotorSpec(name=motor_name, scale=float(scale), offset=offset)


def _parse_court_motor(path: str, motor_name: str, raw: dict, offset: float) -> CourtMotorSpec:
    scales = _parse_court_values(f"{path}.scale", raw)
    magnitudes = {abs(value) for value in scales.values()}
    if 0.0 in magnitudes:
        raise ValueError(f"{path}.scale が 0 です (どの値を書いても同じ位置になります)")
    # 大きさまで変わると motion / tolerance の換算もコート別になり、起動時の検証が知らずに通る
    if len(magnitudes) != 1:
        raise ValueError(
            f"{path}.scale はコートで符号だけが変わる値です (大きさが違います: {raw!r})"
        )
    return CourtMotorSpec(
        name=motor_name,
        scale=magnitudes.pop(),
        offset=offset,
        court_scales=tuple(sorted(scales.items())),
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
        min_speed=_number(path, raw, "min_speed", None),
        guard=_parse_guard(name, raw.get("guard")),
        travel=_parse_travel(name, raw.get("travel")),
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

    retreat_position = raw.get("retreat_position")
    if retreat_position is not None and (
        not isinstance(retreat_position, str) or not retreat_position
    ):
        raise ValueError(f"{path}.retreat_position は位置名の文字列: {retreat_position!r}")

    try:
        return HomingSpec(
            sensor=sensor,
            motor_sensors=motor_sensors,
            direction=float(raw["direction"]),
            search_distance=float(raw["search_distance"]),
            step=float(raw["step"]),
            settle_s=float(raw.get("settle_s", 0.05)),
            align_distance=align_distance,
            align_step=_number(path, raw, "align_step", None),
            retreat_position=retreat_position,
            release_distance=(
                float(raw["release_distance"]) if raw.get("release_distance") is not None else None
            ),
            coarse_step=(float(raw["coarse_step"]) if raw.get("coarse_step") is not None else None),
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


def _parse_guard(axis_name: str, raw: object) -> MotionGuardSpec | None:
    """指令を出す直前の歯止めを読む。**書かない軸は None (今までどおり素通り)。**

    値の妥当性 (正の値か) は ``MotionGuardSpec.__post_init__`` が見る。ここで
    見るのはキーの綴りと型だけ —— ``homing`` / ``motion`` と同じ作法。

    **省略した項目は「その守りが無い」ことを意味する**ので既定値では埋めない。
    埋めると、効いている値なのか書き忘れなのかが config から読めなくなる。

    ``requires`` / ``not_with`` はここでは読まない。**他の軸と、その軸の位置名を
    参照するので、表が全部揃うまで数値へ解決できない。** 読むのは
    ``_resolve_guard_interference`` (``load_position_table`` の最後)。
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"axes.{axis_name}.guard は辞書である必要があります: {raw!r}")

    unknown = sorted(set(raw) - _GUARD_KEYS)
    if unknown:
        raise ValueError(
            f"axes.{axis_name}.guard に未知のキー: {', '.join(unknown)} "
            f"(指定できるのは {', '.join(sorted(_GUARD_KEYS))})"
        )

    path = f"axes.{axis_name}.guard"
    try:
        return MotionGuardSpec(
            limits=_parse_guard_limits(axis_name, raw.get("limits")),
            max_step=(float(raw["max_step"]) if raw.get("max_step") is not None else None),
            stall_torque=(
                float(raw["stall_torque"]) if raw.get("stall_torque") is not None else None
            ),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}: {exc}") from exc


def _parse_guard_limits(axis_name: str, raw: object) -> LimitSpec | None:
    """可動端のセンサ名を読む。**どちらの端も省略でき、1 つの端に何本でも書ける。**

    片端にしかスイッチが無い機構は普通にあるので、書かなかった側はインターロックが
    掛からない (= 守られていない端として残る)。**どちらが + でどちらが - かを
    取り違えると守りが反転する**ので、名前は必ず実機で当てて確かめること。

    複数本は左右直結ペアのため (`y_axis` は同じ端に左右 1 本ずつ持つ)。1 本しか
    無い端は文字列のまま書ける —— 書式を 1 つに強制すると、既にある宣言を
    書き換える理由の無い変更が入り、その差分に紛れて向きを取り違えられる。
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"axes.{axis_name}.guard.limits は辞書である必要があります: {raw!r}")

    unknown = sorted(set(raw) - _GUARD_LIMIT_KEYS)
    if unknown:
        raise ValueError(
            f"axes.{axis_name}.guard.limits に未知のキー: {', '.join(unknown)} "
            f"(指定できるのは {', '.join(sorted(_GUARD_LIMIT_KEYS))})"
        )

    return LimitSpec(
        plus=_parse_guard_limit_sensors(axis_name, "plus", raw.get("plus")),
        minus=_parse_guard_limit_sensors(axis_name, "minus", raw.get("minus")),
    )


def _parse_guard_limit_sensors(axis_name: str, key: str, raw: object) -> tuple[str, ...]:
    where = f"axes.{axis_name}.guard.limits.{key}"
    if raw is None:
        return ()
    values = [raw] if isinstance(raw, str) else raw
    if not isinstance(values, list) or not values:
        raise ValueError(f"{where} はセンサ名の文字列か、その空でない並び: {raw!r}")

    names: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{where} はセンサ名の文字列か、その空でない並び: {raw!r}")
        # 重複を黙って畳むと、2 本書いたつもりの片方が誤って同じ名前でも気付けない
        if value in names:
            raise ValueError(f"{where} にセンサ '{value}' が 2 回書かれています")
        names.append(value)
    return tuple(names)


@dataclass(frozen=True)
class _RequiresDecl:
    """yaml に書かれたままの干渉条件。**位置名のままで、数値をまだ持たない。**"""

    axis: str
    #: 参照する位置名。`at` なら 1 つ、`between` なら 2 つ
    names: tuple[str, ...]
    #: エラー文用の yaml パス (`axes.sub_rotate.guard.requires[0]`)
    path: str


def _guard_declarations(
    axes_raw: Mapping[str, object],
) -> tuple[dict[str, tuple[_RequiresDecl, ...]], dict[str, tuple[str, ...]]]:
    """各軸の `guard.requires` / `guard.not_with` を、位置名のまま読む。

    キーの綴りと型は `_parse_guard` が既に弾いている (`_GUARD_KEYS`)。
    """
    requires: dict[str, tuple[_RequiresDecl, ...]] = {}
    not_with: dict[str, tuple[str, ...]] = {}

    for name, raw in axes_raw.items():
        guard_raw = raw.get("guard") if isinstance(raw, dict) else None
        if not isinstance(guard_raw, dict):
            continue
        declared = _parse_guard_requires(name, guard_raw.get("requires"))
        if declared:
            requires[name] = declared
        partners = _parse_guard_not_with(name, guard_raw.get("not_with"))
        if partners:
            not_with[name] = partners

    return requires, not_with


def _parse_guard_requires(axis_name: str, raw: object) -> tuple[_RequiresDecl, ...]:
    """`requires` を位置名のまま読む。**生の数値は受け付けない。**

    書式は `at:` (1 点) と `between: [a, b]` (2 点で挟む) の 2 つだけ。数値を許すと
    同じ座標が位置定数と歯止めの 2 箇所に書かれ、位置を動かしたときに片方だけが
    古くなる (CLAUDE.md「同じ判定を 2 箇所に書かない」)。
    """
    if raw is None:
        return ()
    where = f"axes.{axis_name}.guard.requires"
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{where} は空でない並びである必要があります: {raw!r}")

    declarations: list[_RequiresDecl] = []
    for index, entry in enumerate(raw):
        path = f"{where}[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{path} は辞書である必要があります: {entry!r}")

        unknown = sorted(set(entry) - _GUARD_REQUIRES_KEYS)
        if unknown:
            raise ValueError(
                f"{path} に未知のキー: {', '.join(unknown)} "
                f"(指定できるのは {', '.join(sorted(_GUARD_REQUIRES_KEYS))})"
            )

        target = entry.get("axis")
        if not isinstance(target, str) or not target:
            raise ValueError(f"{path}.axis は軸名の文字列である必要があります: {target!r}")

        has_at = entry.get("at") is not None
        has_between = entry.get("between") is not None
        if has_at == has_between:
            raise ValueError(
                f"{path} には at (1 点) か between (2 点) のどちらか一方が必要です: {entry!r}"
            )

        if has_at:
            names: tuple[str, ...] = (_guard_position_name(f"{path}.at", entry["at"]),)
        else:
            names = _parse_guard_between(f"{path}.between", entry["between"])
        declarations.append(_RequiresDecl(axis=target, names=names, path=path))

    return tuple(declarations)


def _parse_guard_between(where: str, raw: object) -> tuple[str, str]:
    if not isinstance(raw, list) or len(raw) != 2:
        raise ValueError(f"{where} は位置名 2 つの並びである必要があります: {raw!r}")
    low, high = (_guard_position_name(where, value) for value in raw)
    # 同じ名前で挟むのは 1 点を指すのと同じで、それは at で書く道がある
    if low == high:
        raise ValueError(f"{where} に位置 '{low}' が 2 回書かれています (1 点なら at で書きます)")
    return low, high


def _guard_position_name(where: str, raw: object) -> str:
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{where} は位置名で書きます (生の数値は受け付けません): {raw!r}")
    return raw


def _parse_guard_not_with(axis_name: str, raw: object) -> tuple[str, ...]:
    where = f"axes.{axis_name}.guard.not_with"
    if raw is None:
        return ()
    values = [raw] if isinstance(raw, str) else raw
    if not isinstance(values, list) or not values:
        raise ValueError(f"{where} は軸名の文字列か、その空でない並び: {raw!r}")

    names: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{where} は軸名の文字列か、その空でない並び: {raw!r}")
        if value not in names:
            names.append(value)
    return tuple(names)


def _resolve_guard_interference(
    source: str,
    axes: dict[str, AxisSpec],
    positions: Mapping[str, Mapping[str, float | dict[str, float]]],
    axes_raw: Mapping[str, object],
) -> dict[str, AxisSpec]:
    """位置名で書かれた干渉条件を、**読み込み時に**数値の区間へ解決する。

    層の線引きは「yaml が名前を宣言 → ここが解決 → `MotionGuardSpec` は解決済みの
    数値だけ運ぶ」。`MotionGuard` が位置表もコートも見ないという性質を崩さないため。

    誤記は警告ではなく**起動拒否**にする。干渉の歯止めは間違っていても普段は
    「たまたま通る」だけで、機構が当たるまで誰も気付けない。
    """
    declared_requires, declared_not_with = _guard_declarations(axes_raw)
    resolved_requires = {
        name: tuple(_resolve_required_range(source, axes, positions, decl) for decl in declarations)
        for name, declarations in declared_requires.items()
    }
    _check_requires_acyclic(source, resolved_requires)
    _check_prerequisites_agree(source, resolved_requires)
    _check_prerequisites_homeable(source, axes, resolved_requires)
    resolved_not_with = _symmetrize_not_with(source, axes, declared_not_with)

    updated = dict(axes)
    for name in set(resolved_requires) | set(resolved_not_with):
        spec = updated[name]
        # 対称化で初めて歯止めが付く軸がある (相手側にだけ not_with を書いた軸)。
        # 「書かない = その守りが無い」を破ってはいない —— 制約は宣言されていて、
        # 書いてある場所が反対側なだけ
        guard = spec.guard if spec.guard is not None else MotionGuardSpec()
        updated[name] = replace(
            spec,
            guard=replace(
                guard,
                requires=resolved_requires.get(name, ()),
                not_with=resolved_not_with.get(name, ()),
            ),
        )
    return updated


def _resolve_required_range(
    source: str,
    axes: Mapping[str, AxisSpec],
    positions: Mapping[str, Mapping[str, float | dict[str, float]]],
    decl: _RequiresDecl,
) -> RequiredRange:
    target = axes.get(decl.axis)
    if target is None:
        available = ", ".join(axes) or "(なし)"
        raise ValueError(
            f"{source}: {decl.path} が参照する軸 '{decl.axis}' がこの位置定数にありません "
            f"(定義済みの軸: {available})"
        )
    if target.command_mode is not ControlMode.POSITION:
        raise ValueError(
            f"{source}: {decl.path} が参照する軸 '{decl.axis}' は "
            f"command_mode={target.command_mode.value} です "
            "(位置を持たない軸は「今どこに居るか」を答えられないので条件になりません)"
        )
    if target.tolerance is None:
        raise ValueError(
            f"{source}: {decl.path} が参照する軸 '{decl.axis}' に tolerance がありません "
            "(区間を到達許容差ぶん広げられないと、到達した実測がそのまま区間の外になります)"
        )

    values = positions.get(decl.axis, {})
    resolved: list[float] = []
    for name in decl.names:
        if name not in values:
            available = ", ".join(values) or "(なし)"
            raise ValueError(
                f"{source}: {decl.path} が参照する位置 '{decl.axis}.{name}' が"
                f"定義されていません (定義済みの位置: {available})"
            )
        value = values[name]
        if isinstance(value, dict):
            raise ValueError(
                f"{source}: {decl.path} が参照する位置 '{decl.axis}.{name}' は"
                "コート別に分岐しています "
                "(干渉の区間は両コート共通の座標系でしか書けません。"
                "コートで変わってよいのは軸の scale の符号だけです)"
            )
        resolved.append(float(value))

    # **参照先の tolerance ぶん広げる。** 広げないと試合シーケンスが自分で壊れる ——
    # 端の位置へ到達許容差の内側で止まった実測は、広げていない区間からはみ出し、
    # 次の段がその実測を見て拒否する。会場でしか出ない壊れ方になる。
    # 参照先の scale がコート別 (sub_lift) でも tolerance は人間の単位なので、
    # ここでは換算を 1 度も通さない —— 通すとコート未解決で読み込みごと落ちる
    tolerance = target.tolerance
    return RequiredRange(
        axis=decl.axis,
        low=min(resolved) - tolerance,
        high=max(resolved) + tolerance,
        names=decl.names,
        unit=target.unit,
    )


def _check_requires_acyclic(source: str, requires: Mapping[str, tuple[RequiredRange, ...]]) -> None:
    """依存は一方向であること。

    循環すると「どちらを先に動かしても相手に拒否される」姿勢が作れ、そこから抜ける
    手が無くなる。一方向なら**詰んでも下流の軸を動かせば必ず解ける**。
    """
    visiting: list[str] = []
    done: set[str] = set()

    def walk(axis: str) -> None:
        if axis in done:
            return
        if axis in visiting:
            cycle = [*visiting[visiting.index(axis) :], axis]
            raise ValueError(
                f"{source}: guard.requires の参照が循環しています: {' → '.join(cycle)} "
                "(依存は一方向でなければ、どちらを先に動かしても拒否される姿勢が作れます)"
            )
        visiting.append(axis)
        for required in requires.get(axis, ()):
            walk(required.axis)
        visiting.pop()
        done.add(axis)

    for axis in requires:
        walk(axis)


def _check_prerequisites_agree(
    source: str, requires: Mapping[str, tuple[RequiredRange, ...]]
) -> None:
    """同じ軸を `at:` で参照する宣言は、同じ位置名でなければならない。

    別々の位置を要求されると「どちらへ寄せれば両方が通るのか」が決まらず、
    寄せ先を 1 つ選ぶ側 (`PositionTable.homing_prerequisites`) が黙って後勝ちで
    片方を落とす。落ちたことは画面にもログにも出ないので、起動時に拒否する。
    """
    chosen: dict[str, tuple[str, str]] = {}
    for axis, declarations in requires.items():
        for required in declarations:
            position = required.single_position
            if position is None:
                continue
            previous = chosen.get(required.axis)
            if previous is None:
                chosen[required.axis] = (position, axis)
                continue
            if previous[0] != position:
                raise ValueError(
                    f"{source}: 軸 '{required.axis}' へ別々の位置が要求されています "
                    f"(axes.{previous[1]} は '{previous[0]}'、axes.{axis} は '{position}')。"
                    "零点確定の前に寄せる先が 1 つに決まりません"
                )


def _check_prerequisites_homeable(
    source: str,
    axes: Mapping[str, AxisSpec],
    requires: Mapping[str, tuple[RequiredRange, ...]],
) -> None:
    """零点確定する軸が `at:` で参照する軸も、零点確定できなければならない。

    参照先は零点確定の前に**位置名で寄せる**先になる (`homing_prerequisites`)。
    位置名は原点からの相対値なので、原点が確定していない軸へ書くと**どこへ動くか
    分からない指令**になる。`requires` が原点の確定した軸についてしか意味を持たない
    という穴 (`docs/todo.md`) を、寄せる経路が自分で踏むことになる。

    参照する側が `homing:` を持たない軸なら、この経路を通らないので対象外。
    """
    for axis, declarations in requires.items():
        if axes[axis].homing is None:
            continue
        for required in declarations:
            if required.single_position is None:
                continue
            if axes[required.axis].homing is None:
                raise ValueError(
                    f"{source}: axes.{axis} は零点確定の前に軸 '{required.axis}' を "
                    f"'{required.single_position}' へ寄せますが、'{required.axis}' に "
                    "homing がありません (原点が確定していない軸へ位置名で指令すると、"
                    "どこへ動くか分かりません)"
                )


def _symmetrize_not_with(
    source: str, axes: Mapping[str, AxisSpec], declared: Mapping[str, tuple[str, ...]]
) -> dict[str, tuple[str, ...]]:
    """`not_with` を**片側の宣言から両側へ**広げる。

    両側に書かせると、片方を消したときに守りが半分だけ残る。片側だけを正とし、
    もう片側はここが作る。
    """
    for axis, partners in declared.items():
        for partner in partners:
            if partner == axis:
                raise ValueError(f"{source}: axes.{axis}.guard.not_with に自分自身が書かれています")
            other = axes.get(partner)
            if other is None:
                available = ", ".join(axes) or "(なし)"
                raise ValueError(
                    f"{source}: axes.{axis}.guard.not_with の軸 '{partner}' が"
                    f"この位置定数にありません (定義済みの軸: {available})"
                )
            # 対称化は相手側へ guard を生やすので、guard を書けない軸は先に弾く。
            # 生やしてから弾くと、書いた覚えの無い軸を名指しする文面になる
            if other.command_mode is not ControlMode.POSITION:
                raise ValueError(
                    f"{source}: axes.{axis}.guard.not_with の軸 '{partner}' は "
                    f"command_mode={other.command_mode.value} です "
                    "(guard は位置指令の軸にしか書けません)"
                )
            if axis in declared.get(partner, ()):
                raise ValueError(
                    f"{source}: not_with が axes.{axis} と axes.{partner} の両側に"
                    "書かれています (片側だけ書けば読み込み時に対称化されます)"
                )

    resolved: dict[str, list[str]] = {}
    for axis, partners in declared.items():
        for partner in partners:
            resolved.setdefault(axis, []).append(partner)
            resolved.setdefault(partner, []).append(axis)
    return {axis: tuple(partners) for axis, partners in resolved.items()}


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


def _parse_travel(axis_name: str, raw: object) -> TravelSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"axes.{axis_name}.travel は辞書である必要があります: {raw!r}")

    unknown = set(raw) - _TRAVEL_KEYS
    if unknown:
        raise ValueError(f"axes.{axis_name}.travel に未知のキー: {', '.join(sorted(unknown))}")

    path = f"axes.{axis_name}.travel"
    min_value = _number(path, raw, "min", None)
    max_value = _number(path, raw, "max", None)
    if min_value is None or max_value is None:
        missing = ", ".join(key for key in ("min", "max") if raw.get(key) is None)
        raise ValueError(f"{path} に {missing} がありません (機械的可動域が決まりません)")
    if min_value >= max_value:
        raise ValueError(f"{path}.min は max より小さい必要があります: {min_value} >= {max_value}")
    return TravelSpec(min_value=float(min_value), max_value=float(max_value))


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
    path = f"positions.{axis}.{name}"
    if isinstance(raw, dict):
        return _parse_court_values(path, raw)
    return _to_float(path, raw)


def _parse_court_values(path: str, raw: dict) -> dict[str, float]:
    missing = [court.value for court in Court if court.value not in raw]
    if missing:
        raise ValueError(f"{path} のコート別定義に {', '.join(missing)} がありません")
    unknown = set(raw) - {court.value for court in Court}
    if unknown:
        raise ValueError(f"{path} に未知のコート: {', '.join(sorted(unknown))}")
    return {court.value: _to_float(path, raw[court.value]) for court in Court}


def _to_float(path: str, raw: object) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        raise ValueError(f"{path} が数値ではありません: {raw!r}")
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{path} が数値ではありません: {raw!r}") from exc


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
        # 軸の単位 (mm) だけを見るので、コート別 scale の符号には依らない
        _check_manual_range(source, axes[axis], positions[axis])

    # positions を 1 つも書かなかった軸も通す (退避先の書き忘れは「位置が無い」形で現れ、
    # 所要時間は manual の幅だけでも決まる)
    for axis, spec in axes.items():
        _check_retreat_position(source, spec, positions.get(axis, {}))
        _check_motion_timeout(source, spec, positions.get(axis, {}))

    # 干渉条件は他の軸の位置名を参照するので、表が全部揃った後でしか解決できない
    axes = _resolve_guard_interference(source, axes, positions, axes_raw)

    interlocks = _parse_interlocks(source, config.get("interlocks"), axes, positions)

    return PositionTable(axes, positions, interlocks=interlocks, source=source)


_INTERLOCK_KEYS = frozenset({"when", "require"})


def _parse_interlocks(
    source: str,
    raw: object,
    axes: Mapping[str, AxisSpec],
    positions: Mapping[str, Mapping[str, float | dict[str, float]]],
) -> tuple[InterlockSpec, ...]:
    """軸どうしの干渉を読む。**書かなければ歯止めは無い** (`guard` と同じ作法)。

    軸名・位置名の綴り違いは黙って通すと「その姿勢では止まらない」としてしか
    現れず、機構を壊すまで出ないので起動前に落とす。
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{source}: interlocks は並びである必要があります: {raw!r}")

    specs: list[InterlockSpec] = []
    for index, entry in enumerate(raw):
        path = f"interlocks[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{source}: {path} は辞書である必要があります: {entry!r}")
        unknown = sorted(set(entry) - _INTERLOCK_KEYS)
        if unknown:
            raise ValueError(f"{source}: {path} に未知のキー: {', '.join(unknown)}")
        missing = sorted(_INTERLOCK_KEYS - set(entry))
        if missing:
            raise ValueError(f"{source}: {path} に必須キーがありません: {', '.join(missing)}")
        try:
            specs.append(
                InterlockSpec(
                    when=_parse_interlock_side(
                        source, f"{path}.when", entry["when"], axes, positions
                    ),
                    require=_parse_interlock_side(
                        source, f"{path}.require", entry["require"], axes, positions
                    ),
                )
            )
        except ValueError as exc:
            raise ValueError(f"{source}: {path}: {exc}") from exc
    return tuple(specs)


def _parse_interlock_side(
    source: str,
    path: str,
    raw: object,
    axes: Mapping[str, AxisSpec],
    positions: Mapping[str, Mapping[str, float | dict[str, float]]],
) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{source}: {path} は軸名 → 位置名の空でない辞書: {raw!r}")

    pairs: list[tuple[str, str]] = []
    for axis, name in raw.items():
        spec = axes.get(axis)
        if spec is None:
            raise ValueError(
                f"{source}: {path} の軸 '{axis}' が axes にありません "
                f"(定義済みの軸: {', '.join(axes) or '(なし)'})"
            )
        if not isinstance(name, str) or name not in positions.get(axis, {}):
            available = ", ".join(positions.get(axis, {})) or "(なし)"
            raise ValueError(
                f"{source}: {path}.{axis} の位置 '{name}' が positions.{axis} にありません "
                f"(定義済みの位置: {available})"
            )
        if spec.command_mode is not ControlMode.POSITION:
            raise ValueError(
                f"{source}: {path}.{axis} は位置指令の軸ではありません "
                f"(command_mode={spec.command_mode.value})"
            )
        if spec.tolerance is None:
            # 「その位置に居る」を決める幅が無いと、干渉の判定そのものが立たない
            raise ValueError(
                f"{source}: {path}.{axis} には axes.{axis}.tolerance が必要です "
                "(位置に居るかどうかを判定する許容差がありません)"
            )
        pairs.append((axis, name))
    return tuple(pairs)


def _check_retreat_position(
    source: str,
    spec: AxisSpec,
    values: Mapping[str, float | dict[str, float]],
) -> None:
    """零点確定の直後に退避する位置が、**原点から離れる側**にあること。

    原点側に取ると退避したことにならず、干渉したまま次の軸を寄せることになる
    (症状は「次の軸が 1 歩も動かずに零点確定で失敗する」)。
    """
    homing = spec.homing
    if homing is None or homing.retreat_position is None:
        return

    name = homing.retreat_position
    if name not in values:
        available = ", ".join(values) or "(なし)"
        raise ValueError(
            f"{source}: axes.{spec.name}.homing.retreat_position の '{name}' が "
            f"positions.{spec.name} にありません。定義済みの位置: {available}"
        )

    value = values[name]
    candidates = list(value.values()) if isinstance(value, dict) else [float(value)]
    wrong = [candidate for candidate in candidates if homing.direction * candidate >= 0.0]
    if not wrong:
        return

    side = "正" if homing.direction < 0 else "負"
    raise ValueError(
        f"{source}: positions.{spec.name}.{name} "
        f"({', '.join(str(candidate) for candidate in wrong)}) が原点から離れる側に"
        f"ありません (homing.direction={homing.direction:+g} なので {side}の値が要ります)。"
        "退避になっていないと、干渉したまま次の軸を寄せます"
    )


def _check_motion_timeout(
    source: str,
    spec: AxisSpec,
    values: Mapping[str, float | dict[str, float]],
) -> None:
    candidates = [
        float(candidate)
        for value in values.values()
        for candidate in (value.values() if isinstance(value, dict) else (value,))
    ]
    # move_to の出発点は手動操縦で manual の端まで行きうるし、零点確定の退避は
    # スイッチ (位置名の無い場所) から始まる。位置名どうしの幅だけでは足りない
    if spec.manual is not None:
        candidates.extend((spec.manual.min_value, spec.manual.max_value))
    if len(candidates) < 2:
        return

    span = max(candidates) - min(candidates)
    if spec.motion is not None:
        required = spec.motion.duration_for(span)
        basis = "motion の制限では"
        remedy = "max_velocity / max_acceleration を上げてください"
    elif spec.min_speed is not None:
        required = span / spec.min_speed
        basis = f"min_speed ({spec.min_speed} {spec.unit}/s) では"
        remedy = "min_speed を実測で上げてください"
    else:
        return
    if required <= spec.timeout_s:
        return

    suggested = math.ceil(required * 100.0) / 100.0
    raise ValueError(
        f"{source}: axes.{spec.name} は {basis}最大移動 {span} {spec.unit} に "
        f"{required:.3f} 秒かかり、timeout_s ({spec.timeout_s}) を必ず超えます "
        f"(timeout_s を {suggested} 以上にするか、{remedy})"
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
