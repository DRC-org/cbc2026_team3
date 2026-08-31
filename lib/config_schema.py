"""``config/system.yaml`` と ``config/<robot>.yaml`` のスキーマ検証付き読み込み。

検証で例外を投げるのは ``lib/sequence/positions.py`` と同じ理由による。
設定の誤記をそのまま通すと、意図と違う種類の指令が機構へ流れる。特に
``control_type`` の誤記は致命的で、duty 0.3 のつもりの指令が position 0.3deg として
ファームへ届き、ファーム側も正当なフレームとして受理する。誤記を警告ログに
落として起動を続けると、操縦者はログを読まない限り気付けない。
「壊れていても起動する」方針 (checklist.yaml) はここでは取らない。

このモジュールは yaml が黙っていたときに使う既定値 (``DEFAULT_HEALTH`` /
``DEFAULT_MATCH``) の単一情報源でもある。しきい値を使う側 (can_manager /
server / control) は自前のリテラルを持たず、ここを参照する。
同じ数値を各所に書くと、config を配線し忘れた 1 経路だけが古い境界で判定を続け、
どこにも異常として現れない。
そのため上位モジュールを import してはならない (依存は lib.drivers.base のみ)。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from lib.drivers.base import ControlMode

# 対応するドライバ種別。main._DRIVER_MAP (種別 → 生成関数) と対で維持する。
# 全種別がその表を通って生成されるので、片方だけ足すと起動時に KeyError になる
# (対応が崩れていないことは tests/test_config_schema.py が検証する)
DRIVER_TYPES = ("m3508", "edulite05", "generic", "dm3520")

# generic ドライバの control_type に書ける制御モード。CURRENT を除くのは
# GenericDriver が電流指令フレームを持たないため。
# ON_OFF は電磁弁基板専用 (仕様書 §9.2)。他の 2 枚は受け取っても黙って捨てるので、
# control_type の書き間違いはファーム側で「動かない」として現れる
_CONTROL_MODES = {
    mode.value: mode
    for mode in (
        ControlMode.POSITION,
        ControlMode.VELOCITY,
        ControlMode.DUTY,
        ControlMode.ON_OFF,
    )
}

# EDULITE 05 の mode に書ける制御モード (Edulite05Driver._CONTROL_TO_RUN_MODE と対)
_EDULITE_MODES = {
    mode.value: mode for mode in (ControlMode.POSITION, ControlMode.VELOCITY, ControlMode.CURRENT)
}

# DM3520 の mode に書ける制御モード (Dm3520Driver._CONTROL_TO_CTRL_MODE と対)。
# MIT モードを載せないのは、Kp/Kd を PC 側で持つことになり「ドライバ内蔵の三重ループを
# 使う」という本機を選んだ理由そのものが消えるため
_DM3520_MODES = {mode.value: mode for mode in (ControlMode.POSITION, ControlMode.VELOCITY)}

# ドライバ種別ごとの can_id の範囲 (両端を含む)。
#
# 範囲そのものは各ドライバの __init__ も持っているが、そちらで捕まえると
# 「yaml のどのモータが悪いのか」が出ないまま起動が落ちる。config を読んだ時点で
# ファイル名とモータ名つきで拒否する。
# このモジュールは lib.drivers.base 以外を import しない約束なので表を共有できず、
# 数値が 2 箇所にある。ずれていないことは tests/test_config_schema.py が
# 実際にドライバを生成して検証する。
#
#   m3508     … C620 の電流指令フレームが 1 通に 4 台分のスロットしか持たない
#   edulite05 … Extended Frame のモータ ID フィールドが 8bit
#   generic   … 仕様書 §2.2 (0x00=未設定 / 0xFF=E_STOP ブロードキャストの予約)
#   dm3520    … **フィードバックには CAN ID の下位 4bit しか載らない**。MST_ID を
#               共有する 2 台を見分ける手掛かりはそれだけなので、下位 4bit が重なる
#               ID (0x01 と 0x11 など) を許すと「2 台目のフィードバックが 1 台目の
#               状態を上書きする」構成が書けてしまう。範囲を 1 バイト目の下位ニブルに
#               閉じれば「ID が違う = 下位 4bit も違う」が構造的に成立する
CAN_ID_RANGES: Mapping[str, tuple[int, int]] = MappingProxyType(
    {
        "m3508": (1, 4),
        "edulite05": (0x00, 0xFF),
        "generic": (0x01, 0xFE),
        "dm3520": (0x01, 0x0F),
    }
)

_SYSTEM_KEYS = frozenset({"can_buses", "health", "match", "tuning"})
_HEALTH_KEYS = ("feedback_timeout_ms", "temp_warning_c", "temp_critical_c", "tx_error_threshold")
_MATCH_KEYS = frozenset({"duration_s"})
_TUNING_KEYS = frozenset({"enabled", "window_s", "pre_trigger_s", "min_step_deg", "max_points"})

_ROBOT_KEYS = frozenset({"robot_name", "motors", "sensors"})
_SENSOR_KEYS = frozenset({"bus", "can_id"})
_COMMON_MOTOR_KEYS = frozenset({"driver", "bus", "can_id"})
# ドライバ固有キー。他のドライバに書いても効かないため、混在は起動時に拒否する
_DRIVER_MOTOR_KEYS: dict[str, frozenset[str]] = {
    "m3508": frozenset({"pid"}),
    "edulite05": frozenset(
        {"host_id", "mode", "limit_speed", "limit_current", "position_kp", "set_zero_on_start"}
    ),
    "generic": frozenset({"control_type", "expected_firmware", "expected_angle_range_deg"}),
    "dm3520": frozenset(
        {"master_id", "mode", "limit_speed", "p_max", "v_max", "t_max", "set_zero_on_start"}
    ),
}
# 値の解釈 (null 許容・既定値補完) は main._load_pid_config が持つ。ここではキー名だけ見る
_PID_KEYS = frozenset({"kp", "ki", "kd", "integral_limit", "dead_band", "output_limit"})

# robot yaml から system.yaml へ移した共通設定。移動前の yaml をそのまま起動すると
# 「書いたのに効かない」状態になるため、残っていたら移動先を示して拒否する
_MOVED_TO_SYSTEM = frozenset({"health", "can_buses", "match", "tuning"})


@dataclass(frozen=True)
class HealthThresholds:
    """ヘルス判定のしきい値。この 4 値は必ず 1 組で運ぶ。

    バラの数値として配ると、配線側が 4 本のうち 3 本だけ渡した経路を作れてしまい、
    残る 1 本だけが既定値のまま黙って効く。「フィードバック途絶は config どおり
    250ms で見ているのに、温度警告だけ既定の 65℃ を見ている」という状態は
    ログにも UI にも現れない。1 つの値として渡せば部分配線が構文的に作れない。
    """

    feedback_timeout_ms: float = 500.0
    temp_warning_c: float = 65.0
    temp_critical_c: float = 80.0
    tx_error_threshold: int = 96


@dataclass(frozen=True)
class MatchSettings:
    """試合そのものの設定 (config/system.yaml の match セクション)。

    競技ルールで決まる 1 つの値なので、ロボットごとの yaml には書けない
    (両ハンドで違う試合時間という状態は存在しない)。
    """

    duration_s: float = 180.0


@dataclass(frozen=True)
class TuningSettings:
    """PID 調整支援 (ステップ応答の記録) の設定。

    記録器は PC 側 PID を持つモータ、すなわち M3508 の位置制御ループにしか無く、
    しかも 1 台の PC に 1 組しか存在しない。ロボットごとの yaml に書けると
    「メインハンドは 3 秒・サブハンドは 5 秒」という、読み込み側が片方しか
    採用できない設定が書けてしまう (health / match と同じ理由で system.yaml)。

    ``health`` の 4 値と同じく**必ず 1 組で運ぶ**。バラの引数に分解すると、
    一部だけ配線した経路が作れて残りが黙って既定値のまま効く。
    """

    #: 記録そのものの有無。切ると波形も指標も出ない (機体の動作には影響しない)
    enabled: bool = True
    #: ステップ 1 回あたりの記録長 [s]。整定まで含められる長さにする
    window_s: float = 3.0
    #: ステップ直前も残す長さ [s]。静止していたか既に動いていたかで応答の読み方が変わる
    pre_trigger_s: float = 0.2
    #: これ以上目標が動いたらステップとみなす [deg] (モータの累積角。軸の単位ではない)。
    #: 小さすぎると微小な揺れで窓が開き続け、大きすぎるとジョグ 1 目盛を取りこぼす
    min_step_deg: float = 0.5
    #: 配信する波形の最大点数。解析は常に全点で行い、間引くのは表示だけ
    max_points: int = 300


@dataclass(frozen=True)
class SystemConfig:
    """両ロボットで共有する設定 (config/system.yaml)。"""

    can_buses: Mapping[str, str]
    health: HealthThresholds
    match: MatchSettings
    tuning: TuningSettings
    source: str = "<inline>"


@dataclass(frozen=True)
class MotorConfig:
    """モータ 1 台分の検証済み設定。ドライバ固有値は既定値まで解決済み。

    動作確認の駆動量はここに持たない。両ハンドを 1 本のシーケンスで駆動する形
    (robots/motor_check.py) へ変えたので、確認は運用と同じ位置名へ動かす。
    確認専用の値が存在しない = 位置定数とずれようがない。
    """

    name: str
    driver: str
    bus: str
    can_id: int
    # generic
    control_type: ControlMode = ControlMode.POSITION
    # INFO (1Hz の自己申告, 仕様書 §3.4) と突き合わせる期待値。**書かなければ照合しない。**
    # サーボの型 (180/270) は実物を測る手段が無く、照合できるのは「ファームに書いた値」と
    # 「yaml に書いた値」の一致まで。それでも、型を取り違えたまま指令の 1.5 倍動く状態が
    # PC からは正常にしか見えない (仕様書 §7.7) ので、この一致だけが検出の足がかりになる
    expected_firmware: int | None = None
    expected_angle_range_deg: float | None = None
    # edulite05
    host_id: int = 0xFD
    mode: ControlMode = ControlMode.POSITION
    limit_speed: float = 2.0
    limit_current: float = 5.0
    position_kp: float = 30.0
    set_zero_on_start: bool = False
    # dm3520。mode / limit_speed / set_zero_on_start は edulite05 と共有する
    # (どちらも「ドライバ内蔵の位置ループへ rad で指令する」同じ形なので、別名を
    # 与えると同じ概念が 2 つの名前で config に並ぶ)
    master_id: int = 0x00
    # フィードバックの固定小数点レンジ。**実機のレジスタ 0x15/0x16/0x17 と一致させること。**
    # 指令は float なのでずれても効かないが、位置・速度・トルクが比例倍で読める。
    # 症状は「指令した量だけ動いたのに到達判定を通らない」で、動作確認が検出する
    p_max: float = 12.566
    v_max: float = 45.0
    t_max: float = 10.0
    # m3508。ゲイン値の解釈は main._load_pid_config が持つためここでは生のまま運ぶ
    pid: Mapping[str, object] | None = None


@dataclass(frozen=True)
class SensorConfig:
    """自作基板のセンサ入力 1 つ分の設定 (config/<robot>.yaml の sensors)。

    **センサはモータではない。** 自作基板は 1 スロット = 1 CAN デバイスで、センサも
    自分のデバイス ID で FEEDBACK を送る (仕様書 §5.2)。motors に書くと動作確認・
    目標値再送・UI のモータ一覧に「常に 0 のモータ」として並んでしまうので、
    受信登録とヘルス監視だけを行う別のセクションに分ける。
    """

    name: str
    bus: str
    can_id: int


@dataclass(frozen=True)
class RobotConfig:
    """ロボット 1 台分の検証済み設定 (config/<robot>.yaml)。"""

    robot_name: str
    motors: Mapping[str, MotorConfig]
    sensors: Mapping[str, SensorConfig] = field(default_factory=dict)
    source: str = "<inline>"


DEFAULT_HEALTH = HealthThresholds()
DEFAULT_MATCH = MatchSettings()
DEFAULT_TUNING = TuningSettings()


def _require_mapping(source: str, path: str, raw: object) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{source}: {path} は辞書である必要があります: {raw!r}")
    return raw


def _reject_unknown(source: str, path: str, raw: Mapping, allowed: frozenset[str]) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(
            f"{source}: {path} に未知のキー: {', '.join(unknown)} "
            f"(指定できるのは {', '.join(sorted(allowed))})"
        )


def _number(source: str, path: str, raw: object) -> float:
    # yaml の true は float() を通ってしまい 1.0 として静かに効く
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        raise ValueError(f"{source}: {path} が数値ではありません: {raw!r}")
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{source}: {path} が数値ではありません: {raw!r}") from exc


def _integer(source: str, path: str, raw: object) -> int:
    """整数を読む。CAN ID は yaml に 0x05 形式でも書けるため 16 進文字列も許す。"""
    if isinstance(raw, bool):
        raise ValueError(f"{source}: {path} が整数ではありません: {raw!r}")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw, 0)
        except ValueError as exc:
            raise ValueError(f"{source}: {path} が整数ではありません: {raw!r}") from exc
    raise ValueError(f"{source}: {path} が整数ではありません: {raw!r}")


def _boolean(source: str, path: str, raw: object) -> bool:
    if not isinstance(raw, bool):
        raise ValueError(f"{source}: {path} が true / false ではありません: {raw!r}")
    return raw


def _mode(source: str, path: str, raw: object, allowed: dict[str, ControlMode]) -> ControlMode:
    mode = allowed.get(str(raw).strip().lower()) if raw is not None else None
    if mode is None:
        raise ValueError(
            f"{source}: {path} に未対応の値: {raw!r} (指定できるのは {', '.join(allowed)})"
        )
    return mode


# ---- system.yaml ----


def _parse_can_buses(source: str, raw: object) -> dict[str, str]:
    buses = _require_mapping(source, "can_buses", raw)
    if not buses:
        # バス定義が無いとモータを 1 台も登録できず、静かに「何も動かない機体」になる
        raise ValueError(f"{source}: can_buses に CAN バスが 1 つも定義されていません")
    for alias, channel in buses.items():
        if not isinstance(channel, str) or not channel:
            raise ValueError(
                f"{source}: can_buses.{alias} は SocketCAN のインタフェース名 "
                f"(文字列) である必要があります: {channel!r}"
            )
    return {str(alias): channel for alias, channel in buses.items()}


def _parse_health(source: str, raw: object) -> HealthThresholds:
    health = _require_mapping(source, "health", raw)
    _reject_unknown(source, "health", health, frozenset(_HEALTH_KEYS))

    values: dict[str, float] = {}
    for key in _HEALTH_KEYS:
        if key not in health or health[key] is None:
            continue
        values[key] = _number(source, f"health.{key}", health[key])

    return HealthThresholds(
        feedback_timeout_ms=values.get("feedback_timeout_ms", DEFAULT_HEALTH.feedback_timeout_ms),
        temp_warning_c=values.get("temp_warning_c", DEFAULT_HEALTH.temp_warning_c),
        temp_critical_c=values.get("temp_critical_c", DEFAULT_HEALTH.temp_critical_c),
        tx_error_threshold=int(values.get("tx_error_threshold", DEFAULT_HEALTH.tx_error_threshold)),
    )


def _parse_match(source: str, raw: object) -> MatchSettings:
    section = _require_mapping(source, "match", raw)
    _reject_unknown(source, "match", section, _MATCH_KEYS)

    value = section.get("duration_s")
    if value is None:
        return MatchSettings()

    duration = _number(source, "match.duration_s", value)
    # 0 以下だと試合開始と同時に残り 0 になり、タイマーが常に「時間切れ」を出す。
    # 誤記を通すと画面の表示だけが壊れ、原因が設定だと気付けない
    if duration <= 0:
        raise ValueError(f"{source}: match.duration_s は正の秒数である必要があります: {value!r}")
    return MatchSettings(duration_s=duration)


#: tuning の各キーが 0 を許すか。**0 以下だと記録が成立しない**のが既定で、
#: pre_trigger_s だけが 0 (トリガー前を記録しない) を意味のある設定として許す。
#: 黙って既定値へ倒さないのは、「設定したのに効かない」状態になると波形が出ない
#: 原因が config から読めなくなるため。
_TUNING_ALLOWS_ZERO: dict[str, bool] = {
    "window_s": False,
    "pre_trigger_s": True,
    "min_step_deg": False,
    "max_points": False,
}


def _parse_tuning(source: str, raw: object) -> TuningSettings:
    if raw is None:
        return TuningSettings()
    section = _require_mapping(source, "tuning", raw)
    _reject_unknown(source, "tuning", section, _TUNING_KEYS)

    defaults = TuningSettings()
    enabled = section.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError(f"{source}: tuning.enabled は真偽値である必要があります: {enabled!r}")

    values: dict[str, float] = {}
    for key, allows_zero in _TUNING_ALLOWS_ZERO.items():
        value = section.get(key)
        if value is None:
            continue
        number = _number(source, f"tuning.{key}", value)
        if number < 0 or (number == 0 and not allows_zero):
            bound = " 0 以上" if allows_zero else "正の値"
            raise ValueError(f"{source}: tuning.{key} は{bound}である必要があります: {value!r}")
        values[key] = number

    return TuningSettings(
        enabled=defaults.enabled if enabled is None else enabled,
        window_s=values.get("window_s", defaults.window_s),
        pre_trigger_s=values.get("pre_trigger_s", defaults.pre_trigger_s),
        min_step_deg=values.get("min_step_deg", defaults.min_step_deg),
        max_points=int(values.get("max_points", defaults.max_points)),
    )


def load_system_config(config: Mapping | None, *, source: str = "<inline>") -> SystemConfig:
    """両ロボット共通の設定 yaml を検証して読み込む。"""
    raw = _require_mapping(source, "(最上位)", config)
    _reject_unknown(source, "(最上位)", raw, _SYSTEM_KEYS)

    return SystemConfig(
        can_buses=MappingProxyType(_parse_can_buses(source, raw.get("can_buses"))),
        health=_parse_health(source, raw.get("health")),
        match=_parse_match(source, raw.get("match")),
        tuning=_parse_tuning(source, raw.get("tuning")),
        source=source,
    )


# ---- <robot>.yaml ----


def _parse_pid(source: str, motor_name: str, raw: object) -> Mapping[str, object] | None:
    if raw is None:
        return None
    path = f"motors.{motor_name}.pid"
    section = _require_mapping(source, path, raw)
    # 書いても効かないゲインを黙って捨てないため、キー名だけは起動時に突き合わせる。
    # 値そのものは main._load_pid_config が既定値で補完する (書きかけの yaml を許す)
    _reject_unknown(source, path, section, _PID_KEYS)
    return MappingProxyType(dict(section))


def _optional[T](
    parse: Callable[[str, str, object], T],
    source: str,
    path: str,
    raw: Mapping,
    key: str,
    default: T,
) -> T:
    """未指定・null は既定値。書いてある値だけを検証する (書きかけの yaml を許す)。"""
    value = raw.get(key)
    return default if value is None else parse(source, f"{path}.{key}", value)


def _optional_mode(
    source: str,
    path: str,
    raw: Mapping,
    key: str,
    allowed: dict[str, ControlMode],
    default: ControlMode,
) -> ControlMode:
    value = raw.get(key)
    return default if value is None else _mode(source, f"{path}.{key}", value, allowed)


def _parse_sensor(
    source: str, sensor_name: str, raw: object, buses: Mapping[str, str] | None
) -> SensorConfig:
    path = f"sensors.{sensor_name}"
    sensor = _require_mapping(source, path, raw)

    for key in ("bus", "can_id"):
        if sensor.get(key) is None:
            raise ValueError(f"{source}: {path}.{key} が指定されていません")
    _reject_unknown(source, path, sensor, _SENSOR_KEYS)

    bus = str(sensor["bus"])
    if buses is not None and bus not in buses:
        raise ValueError(
            f"{source}: {path}.bus に未定義のバス別名: {bus!r} "
            f"(config/system.yaml の can_buses に定義済みなのは {', '.join(buses)})"
        )

    can_id = _integer(source, f"{path}.can_id", sensor["can_id"])
    low, high = CAN_ID_RANGES["generic"]
    if not low <= can_id <= high:
        raise ValueError(
            f"{source}: {path}.can_id が範囲外です: {can_id} "
            f"(指定できるのは {low:#04x}〜{high:#04x})"
        )
    return SensorConfig(name=sensor_name, bus=bus, can_id=can_id)


def _parse_expected_firmware(source: str, path: str, motor: Mapping) -> int | None:
    """INFO の Byte0 と突き合わせるファーム版 (仕様書 §3.4)。"""
    value = _optional(_integer, source, path, motor, "expected_firmware", None)
    if value is not None and not 0 <= value <= 0xFF:
        raise ValueError(
            f"{source}: {path}.expected_firmware が uint8 の範囲外です: {value} "
            "(INFO の Byte0 は 1 バイト。仕様書 §3.4)"
        )
    return value


def _parse_expected_angle_range(
    source: str, path: str, motor: Mapping, control_type: ControlMode
) -> float | None:
    """INFO の Byte3-4 と突き合わせるサーボ可動レンジ [deg] (仕様書 §3.4 / §7.7)。"""
    value = _optional(_number, source, path, motor, "expected_angle_range_deg", None)
    if value is None:
        return None

    if value <= 0:
        raise ValueError(
            f"{source}: {path}.expected_angle_range_deg は正の値です: {value} "
            "(0 以下だと角度 → パルス幅の変換そのものが定義できない)"
        )

    # **角度を持たない基板に書けてしまうと「書いたのに効かない設定」になる。**
    # DC 基板と電磁弁基板は可動レンジを申告しないので、照合は永久に「申告なし」と
    # 判定し続け、モータが起動直後から FAULT のまま復帰しない
    if control_type is not ControlMode.POSITION:
        raise ValueError(
            f"{source}: {path}.expected_angle_range_deg は control_type: position の軸に"
            f"しか書けません (この軸は {control_type.value})。角度を持たない基板は "
            "INFO でも可動レンジを申告しない (仕様書 §3.4)"
        )
    return value


def _parse_motor(
    source: str, motor_name: str, raw: object, buses: Mapping[str, str] | None
) -> MotorConfig:
    path = f"motors.{motor_name}"
    motor = _require_mapping(source, path, raw)

    for key in ("driver", "bus", "can_id"):
        if motor.get(key) is None:
            raise ValueError(f"{source}: {path}.{key} が指定されていません")

    driver = str(motor["driver"])
    if driver not in _DRIVER_MOTOR_KEYS:
        raise ValueError(
            f"{source}: {path}.driver に未対応の値: {motor['driver']!r} "
            f"(指定できるのは {', '.join(DRIVER_TYPES)})"
        )

    _reject_unknown(source, path, motor, _COMMON_MOTOR_KEYS | _DRIVER_MOTOR_KEYS[driver])

    bus = str(motor["bus"])
    if buses is not None and bus not in buses:
        raise ValueError(
            f"{source}: {path}.bus に未定義のバス別名: {bus!r} "
            f"(config/system.yaml の can_buses に定義済みなのは {', '.join(buses)})"
        )

    can_id = _integer(source, f"{path}.can_id", motor["can_id"])
    low, high = CAN_ID_RANGES[driver]
    if not low <= can_id <= high:
        raise ValueError(
            f"{source}: {path}.can_id が {driver} の範囲外です: {can_id} "
            f"(指定できるのは {low:#04x}〜{high:#04x})"
        )

    if driver == "generic":
        control_type = _optional_mode(
            source, path, motor, "control_type", _CONTROL_MODES, ControlMode.POSITION
        )
        return MotorConfig(
            name=motor_name,
            driver=driver,
            bus=bus,
            can_id=can_id,
            control_type=control_type,
            expected_firmware=_parse_expected_firmware(source, path, motor),
            expected_angle_range_deg=_parse_expected_angle_range(source, path, motor, control_type),
        )

    if driver == "dm3520":
        return MotorConfig(
            name=motor_name,
            driver=driver,
            bus=bus,
            can_id=can_id,
            master_id=_optional(_integer, source, path, motor, "master_id", 0x00),
            mode=_optional_mode(source, path, motor, "mode", _DM3520_MODES, ControlMode.POSITION),
            limit_speed=_optional(_number, source, path, motor, "limit_speed", 2.0),
            p_max=_optional(_number, source, path, motor, "p_max", 12.566),
            v_max=_optional(_number, source, path, motor, "v_max", 45.0),
            t_max=_optional(_number, source, path, motor, "t_max", 10.0),
            set_zero_on_start=_optional(_boolean, source, path, motor, "set_zero_on_start", False),
        )

    if driver == "edulite05":
        return MotorConfig(
            name=motor_name,
            driver=driver,
            bus=bus,
            can_id=can_id,
            host_id=_optional(_integer, source, path, motor, "host_id", 0xFD),
            mode=_optional_mode(source, path, motor, "mode", _EDULITE_MODES, ControlMode.POSITION),
            limit_speed=_optional(_number, source, path, motor, "limit_speed", 2.0),
            limit_current=_optional(_number, source, path, motor, "limit_current", 5.0),
            position_kp=_optional(_number, source, path, motor, "position_kp", 30.0),
            set_zero_on_start=_optional(_boolean, source, path, motor, "set_zero_on_start", False),
        )

    return MotorConfig(
        name=motor_name,
        driver=driver,
        bus=bus,
        can_id=can_id,
        pid=_parse_pid(source, motor_name, motor.get("pid")),
    )


def load_robot_config(
    config: Mapping | None,
    *,
    source: str = "<inline>",
    buses: Mapping[str, str] | None = None,
) -> RobotConfig:
    """ロボット 1 台分の設定 yaml を検証して読み込む。

    ``buses`` を渡すと ``motors.*.bus`` の別名がその中に在ることまで確認する
    (別名の誤記は CANManager 側で KeyError になるだけで、どこが悪いか分からない)。
    """
    raw = _require_mapping(source, "(最上位)", config)

    moved = sorted(_MOVED_TO_SYSTEM & set(raw))
    if moved:
        raise ValueError(
            f"{source}: {', '.join(moved)} は config/system.yaml へ移動しました "
            "(ここに書いても読まれません)"
        )
    _reject_unknown(source, "(最上位)", raw, _ROBOT_KEYS)

    robot_name = raw.get("robot_name")
    if not isinstance(robot_name, str) or not robot_name:
        raise ValueError(f"{source}: robot_name が指定されていません")

    motors_raw = _require_mapping(source, "motors", raw.get("motors"))
    if not motors_raw:
        # モータ 0 台のロボットは登録できてしまうが、操縦者からは「動かない機体」に見える
        raise ValueError(f"{source}: motors にモータが 1 台も定義されていません")

    motors = {
        str(name): _parse_motor(source, str(name), motor_raw, buses)
        for name, motor_raw in motors_raw.items()
    }

    sensors_raw = raw.get("sensors")
    sensors = (
        {}
        if sensors_raw is None
        else {
            str(name): _parse_sensor(source, str(name), sensor_raw, buses)
            for name, sensor_raw in _require_mapping(source, "sensors", sensors_raw).items()
        }
    )

    overlap = sorted(set(motors) & set(sensors))
    if overlap:
        # 同じ名前でモータとセンサが並ぶと、ヘルスの表示も CAN の登録も
        # どちらを指しているのか読めなくなる
        raise ValueError(
            f"{source}: motors と sensors で名前が重複しています: {', '.join(overlap)}"
        )

    return RobotConfig(
        robot_name=robot_name,
        motors=MappingProxyType(motors),
        sensors=MappingProxyType(sensors),
        source=source,
    )
