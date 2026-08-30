from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# 単位換算は制御層 (同期監視・位置制御ループ) と共有する。ここで再定義すると
# 逆回転ペアの符号付き scale の逆換算がまた 2 実装に分かれる
from lib.axis_sync import MotorSpec, SyncGroup
from lib.drivers.base import ControlMode
from lib.match_state import Court

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "AxisSpec",
    "ManualSpec",
    "MotorSpec",
    "PositionLookupError",
    "PositionTable",
    "load_position_table",
]

# 軸ごとの到達待ち上限 [s] の既定値。機構が引っかかったまま試合が止まるのを避けるため、
# yaml で timeout_s を書かなかった軸にも必ずタイムアウトを与える
DEFAULT_TIMEOUT_S = 5.0


class PositionLookupError(RuntimeError):
    """位置定数の参照に失敗したときに送出される。"""


@dataclass(frozen=True)
class ManualSpec:
    """手動操縦で連続値を送ってよい範囲とジョグ量。

    通常運用 (``move_to``) は位置名でしか値を引けないため、**定義した状態以外を
    送れないことが構造的に保証されている**。手動操縦はその保証を外して任意の値を
    通す経路なので、代わりの境界をここで宣言させる。``manual:`` を書かなかった軸は
    連続操作の対象にならず、位置名によるプリセット指令だけが残る (= 今までと同じ保証)。

    ``min_value`` / ``max_value`` は機構の物理端そのものではなく、**その内側**に取る。
    手動は操縦者が端へ寄せていく操作なので、境界を物理端に合わせると
    「指令は範囲内なのに機構は突き当たっている」状態が作れてしまう。
    """

    min_value: float
    max_value: float
    #: UI が出すジョグ量の候補 [人間の単位]。先頭が既定値
    steps: tuple[float, ...]

    def clamp(self, value: float) -> float:
        """範囲内へ丸める。範囲外を拒否しないのは、端で操作そのものが効かなくなるため。"""
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
    """リミットスイッチで零点を確定する手順。**軸の機構的性質**なのでここに置く。

    動作確認固有の値ではない。「その軸はどちら向きに、どれだけ動かせば原点に
    当たるか」は機構が変われば変わるので、位置定数と同じ場所で管理する。

    **`search_distance` は省略できない。** ホーミングは「当たるまで動かす」動作で、
    配線が抜けていたりセンサが死んでいたりすると機構端まで押し込み続ける。
    探索距離を超えたら止めるのが唯一の歯止めになる (緊急停止は操縦者が押さないと
    効かないので、無人の歯止けが要る)。
    """

    #: 監視するセンサ名 (config の `sensors:` に登録された名前)
    sensor: str
    #: 探索方向。+1 か -1 のみ。人間の単位での増減方向を表す
    direction: float
    #: 探索距離の上限 [軸の unit]。ここまで動かして当たらなければ失敗として止める
    search_distance: float
    #: 1 回あたりの移動量 [軸の unit]。小さいほど原点の精度が上がり、時間が延びる
    step: float
    #: 1 ステップごとの待ち [s]。指令が機構へ届き、センサの状態が返る余裕を取る
    settle_s: float

    def __post_init__(self) -> None:
        if self.direction not in (1.0, -1.0):
            raise ValueError(f"homing.direction は +1 か -1: {self.direction!r}")
        if self.search_distance <= 0.0:
            raise ValueError(f"homing.search_distance は正の値: {self.search_distance!r}")
        if self.step <= 0.0:
            raise ValueError(f"homing.step は正の値: {self.step!r}")
        if self.settle_s < 0.0:
            raise ValueError(f"homing.settle_s は 0 以上: {self.settle_s!r}")
        if self.step > self.search_distance:
            # 1 歩も踏めないまま失敗するだけの設定を通さない
            raise ValueError(
                f"homing.step ({self.step}) が "
                f"search_distance ({self.search_distance}) を超えています"
            )


@dataclass(frozen=True)
class AxisSpec:
    """1 論理軸の単位換算とデフォルト待ち条件。

    ``command = value * scale + offset`` で人間の単位からモータ指令値へ換算する。
    3 種類のモータ (M3508=モータ軸 deg / EDULITE 05=rad / 自作=deg) で指令単位が
    異なるため、換算はここに一元化してシーケンス本体には持ち込まない。

    1 つの論理軸が複数モータで駆動される場合 (左右直結のラックアンドピニオン等) は
    ``motors`` に複数を並べる。換算はモータごとに行う API しか公開しない
    (``to_commands`` / ``MotorSpec.to_tolerance``)。「軸 = モータ 1 台」を前提に
    先頭モータの値だけを返す API を置くと、逆回転ペアで左のモータに右の scale が
    当たり、左が右向きに全ストローク動いて機構を壊す。
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
    # 手動操縦の可動範囲。None ならこの軸は連続操作の対象外 (プリセット指令のみ)
    manual: ManualSpec | None = None
    # リミットスイッチによる零点確定。None ならこの軸はホーミングしない
    # (電源投入位置をそのまま原点として使う)
    homing: HomingSpec | None = None

    def __post_init__(self) -> None:
        if not self.motors:
            raise ValueError(f"axes.{self.name} にモータがありません")
        # 到達判定を持たない軸へホーミングを書かせない。duty / on_off は指令が
        # 届いたかを観測できないので、「当たるまで少しずつ動かす」が成立しない
        if self.homing is not None and self.command_mode is not ControlMode.POSITION:
            raise ValueError(
                f"axes.{self.name}: homing は位置指令の軸にのみ書けます "
                f"(command_mode={self.command_mode.value})"
            )

    @property
    def motor_names(self) -> tuple[str, ...]:
        return tuple(motor.name for motor in self.motors)

    def to_commands(self, value: float) -> dict[str, float]:
        return {motor.name: motor.to_command(value) for motor in self.motors}

    def to_value(self, commands: Mapping[str, float]) -> float:
        """モータの指令値・フィードバックを人間の単位の軸位置へ戻す (``to_commands`` の逆)。

        逆換算そのものは ``MotorSpec.to_value`` に委ねる。ここで書き直すと、
        逆回転ペアの符号付き ``scale`` の扱いが 2 実装に分かれる
        (lib/axis_sync.py のモジュール docstring を参照)。

        複数モータ軸では平均を返す。左右がずれていればどちらか一方の値は必ず
        誤りなので「片側を代表にする」根拠が無く、ずれ自体は ``sync_group`` を
        見る 3 層が別に検出する。値が 1 つも揃わなければ ``PositionLookupError``。
        """
        values = [
            motor.to_value(commands[motor.name]) for motor in self.motors if motor.name in commands
        ]
        if not values:
            raise PositionLookupError(f"軸 '{self.name}' の位置を算出できる値がありません")
        return sum(values) / len(values)

    @property
    def sync_group(self) -> SyncGroup | None:
        """同期監視の単位。``sync_tolerance`` を持たない軸は None。

        ``motors`` をそのままメンバにするため、監視側で単位換算を詰め替える経路が
        存在しない (詰め替えを挟むと逆回転の符号を落とす余地が生まれる)。
        """
        if self.sync_tolerance is None:
            return None
        return SyncGroup(name=self.name, members=self.motors, tolerance=self.sync_tolerance)


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
        "manual",
        "homing",
    }
)

_MOTOR_KEYS = frozenset({"scale", "offset"})

_MANUAL_KEYS = frozenset({"min", "max", "steps"})

_HOMING_KEYS = frozenset({"sensor", "direction", "search_distance", "step", "settle_s"})
#: 省略を許さないキー。探索距離を既定値で埋めると、配線が抜けた状態で機構端まで
#: 押し込む経路ができる (ホーミングの唯一の無人の歯止めがこれ)
_HOMING_REQUIRED = frozenset({"sensor", "direction", "search_distance", "step"})

#: 手動のジョグ量候補を書かなかった軸に与える既定 (人間の単位)。
#: UI は必ず 1 つ以上の候補を要求するため、空にはしない
_DEFAULT_MANUAL_STEPS: tuple[float, ...] = (1.0,)

# 位置定数から出してよい指令モード。CURRENT を除くのは、位置名に紐付けて開ループの
# トルク指令を出す用途が無く、誤記のまま機構へ流れると破損に直結するため。
# ON_OFF は電磁弁のような離散状態アクチュエータ用で、DUTY と同じく到達判定を
# 持たない (指令後は settle_s で待つ)
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
    """機構位置の定数表。軸ごとの単位換算とコート差異を吸収する。

    公開するのは「軸を引く」(``axis``) と「位置名を指令値へ換算する」(``commands``)
    の 2 つに絞る。待ち条件や指令モードは ``axis(name)`` が返す ``AxisSpec`` から
    直接読むこと。同じ値を読む道を 2 本置くと、どちらが正しいのかを毎回確かめる
    ことになる。
    """

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
        """複数の位置定数表を 1 つに束ねる。

        統合動作確認シーケンス (robots/motor_check.py) は両ハンドのアクチュエータを
        1 つの順序で駆動するため、両機の軸を同じ表から引く必要がある。

        **軸名の衝突は起動時に拒否する。** 後勝ちで上書きすると、動作確認が意図した
        側とは別の機体の軸へ指令が飛ぶ。症状は「指令したのに動かない機構」と
        「触っていないのに動く機構」が同時に出る形で、しかもどちらの config を見ても
        間違いが書かれていないので原因にたどり着けない。

        Raises:
            ValueError: 同じ軸名が 2 つ以上の表にある
        """
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

    def manual(self, axis: str) -> ManualSpec | None:
        """手動操縦の可動範囲。連続操作を許していない軸は None。"""
        return self.axis(axis).manual

    def manual_axes(self) -> tuple[str, ...]:
        """連続操作を許した軸 (``manual:`` を持つ軸)。"""
        return tuple(name for name, spec in self._axes.items() if spec.manual is not None)

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
        manual=_parse_manual(name, raw.get("manual"), command_mode),
        homing=_parse_homing(name, raw.get("homing")),
    )


def _parse_homing(axis_name: str, raw: object) -> HomingSpec | None:
    """リミットスイッチによる零点確定の設定を読む。書かない軸は None。

    値の妥当性 (方向が ±1 か、探索距離が正か) は ``HomingSpec.__post_init__`` が見る。
    ここで見るのはキーの綴りと型だけ。
    """
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
        # 探索距離を省けるようにすると、既定値のまま機構端まで押し込む経路ができる
        raise ValueError(f"axes.{axis_name}.homing に必須キーがありません: {', '.join(missing)}")

    sensor = raw["sensor"]
    if not isinstance(sensor, str) or not sensor:
        raise ValueError(f"axes.{axis_name}.homing.sensor はセンサ名の文字列: {sensor!r}")

    path = f"axes.{axis_name}.homing"
    try:
        return HomingSpec(
            sensor=sensor,
            direction=float(raw["direction"]),
            search_distance=float(raw["search_distance"]),
            step=float(raw["step"]),
            settle_s=float(raw.get("settle_s", 0.05)),
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
    """手動操縦の可動範囲を読む。``manual:`` を書かない軸は None (連続操作の対象外)。"""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"axes.{axis_name}.manual は辞書である必要があります: {raw!r}")

    unknown = set(raw) - _MANUAL_KEYS
    if unknown:
        raise ValueError(f"axes.{axis_name}.manual に未知のキー: {', '.join(sorted(unknown))}")

    if command_mode is not ControlMode.POSITION:
        # duty / velocity 指令の軸に「可動範囲」は存在しない。書けてしまうと
        # UI がジョグ行を描き、押しても機構が位置決めされない操作面が出来上がる
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
            # 0 や負のジョグ量は「押しても動かない」「押すと逆へ動く」ボタンになる
            raise ValueError(f"{path}.steps は正の値である必要があります: {entry!r}")
        steps.append(float(entry))
    return tuple(steps)


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
        _check_manual_range(source, axes[axis], positions[axis])

    return PositionTable(axes, positions, source=source)


def _check_manual_range(
    source: str,
    spec: AxisSpec,
    values: Mapping[str, float | dict[str, float]],
) -> None:
    """プリセット位置が手動の可動範囲に収まっているか検証する。

    範囲外の位置定数を通すと「シーケンスは行ける場所へ手動では行けない」軸ができる。
    症状は「手動で戻そうとしても途中で止まる」だけで、原因が config からは見えない
    (クランプは黙って効くため、指令値と実際に送られた値の食い違いがどこにも現れない)。
    """
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
