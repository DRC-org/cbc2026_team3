from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from lib.drivers.base import ControlMode

DRIVER_TYPES = ("m3508", "edulite05", "generic", "dm3520")

_CONTROL_MODES = {
    mode.value: mode
    for mode in (
        ControlMode.POSITION,
        ControlMode.VELOCITY,
        ControlMode.DUTY,
        ControlMode.ON_OFF,
    )
}

_EDULITE_MODES = {
    mode.value: mode for mode in (ControlMode.POSITION, ControlMode.VELOCITY, ControlMode.CURRENT)
}

_DM3520_MODES = {mode.value: mode for mode in (ControlMode.POSITION, ControlMode.VELOCITY)}

CAN_ID_RANGES: Mapping[str, tuple[int, int]] = MappingProxyType(
    {
        "m3508": (1, 4),
        "edulite05": (0x00, 0xFF),
        "generic": (0x01, 0xFE),
        "dm3520": (0x01, 0x0F),
    }
)

_SYSTEM_KEYS = frozenset({"can_buses", "health", "match"})
_HEALTH_KEYS = ("feedback_timeout_ms", "temp_warning_c", "temp_critical_c", "tx_error_threshold")
_MATCH_KEYS = frozenset({"duration_s"})

_ROBOT_KEYS = frozenset({"robot_name", "motors", "sensors"})
_SENSOR_KEYS = frozenset({"bus", "can_id", "expected_firmware"})
_COMMON_MOTOR_KEYS = frozenset({"driver", "bus", "can_id"})
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
_PID_KEYS = frozenset({"kp", "ki", "kd", "integral_limit", "dead_band", "output_limit"})

_MOVED_TO_SYSTEM = frozenset({"health", "can_buses", "match"})


@dataclass(frozen=True)
class HealthThresholds:
    feedback_timeout_ms: float = 500.0
    temp_warning_c: float = 65.0
    temp_critical_c: float = 80.0
    tx_error_threshold: int = 96


@dataclass(frozen=True)
class MatchSettings:
    duration_s: float = 180.0


@dataclass(frozen=True)
class SystemConfig:
    can_buses: Mapping[str, str]
    health: HealthThresholds
    match: MatchSettings
    source: str = "<inline>"


@dataclass(frozen=True)
class MotorConfig:
    name: str
    driver: str
    bus: str
    can_id: int
    control_type: ControlMode = ControlMode.POSITION
    expected_firmware: int | None = None
    expected_angle_range_deg: float | None = None
    host_id: int = 0xFD
    mode: ControlMode = ControlMode.POSITION
    limit_speed: float = 2.0
    limit_current: float = 5.0
    position_kp: float = 30.0
    set_zero_on_start: bool = False
    master_id: int = 0x00
    p_max: float = 12.566
    v_max: float = 45.0
    t_max: float = 10.0
    pid: Mapping[str, object] | None = None


@dataclass(frozen=True)
class SensorConfig:
    name: str
    bus: str
    can_id: int
    expected_firmware: int | None = None


@dataclass(frozen=True)
class RobotConfig:
    robot_name: str
    motors: Mapping[str, MotorConfig]
    sensors: Mapping[str, SensorConfig] = field(default_factory=dict)
    source: str = "<inline>"


DEFAULT_HEALTH = HealthThresholds()
DEFAULT_MATCH = MatchSettings()


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
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        raise ValueError(f"{source}: {path} が数値ではありません: {raw!r}")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{source}: {path} が数値ではありません: {raw!r}") from exc

    if not math.isfinite(value):
        raise ValueError(f"{source}: {path} が有限な数値ではありません: {raw!r}")
    return value


def _integer(source: str, path: str, raw: object) -> int:
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


def _parse_can_buses(source: str, raw: object) -> dict[str, str]:
    buses = _require_mapping(source, "can_buses", raw)
    if not buses:
        raise ValueError(f"{source}: can_buses に CAN バスが 1 つも定義されていません")
    seen: dict[str, str] = {}
    for alias, channel in buses.items():
        if not isinstance(channel, str) or not channel:
            raise ValueError(
                f"{source}: can_buses.{alias} は SocketCAN のインタフェース名 "
                f"(文字列) である必要があります: {channel!r}"
            )
        previous = seen.get(channel)
        if previous is not None:
            raise ValueError(
                f"{source}: can_buses.{alias} と can_buses.{previous} が同じ"
                f" インタフェース '{channel}' を指しています。"
                "別名は機種ごとの物理バスに 1 対 1 で対応させること"
                " (重ねると機種の違うノードが同じバスに乗り、"
                "一方のフィードバックが他方への指令として解釈されます)"
            )
        seen[channel] = str(alias)
    return {str(alias): channel for alias, channel in buses.items()}


def _parse_health(source: str, raw: object) -> HealthThresholds:
    health = _require_mapping(source, "health", raw)
    _reject_unknown(source, "health", health, frozenset(_HEALTH_KEYS))

    values: dict[str, float] = {}
    for key in _HEALTH_KEYS:
        if key not in health or health[key] is None:
            continue
        values[key] = _number(source, f"health.{key}", health[key])

    thresholds = HealthThresholds(
        feedback_timeout_ms=values.get("feedback_timeout_ms", DEFAULT_HEALTH.feedback_timeout_ms),
        temp_warning_c=values.get("temp_warning_c", DEFAULT_HEALTH.temp_warning_c),
        temp_critical_c=values.get("temp_critical_c", DEFAULT_HEALTH.temp_critical_c),
        tx_error_threshold=int(values.get("tx_error_threshold", DEFAULT_HEALTH.tx_error_threshold)),
    )

    for key, value in (
        ("feedback_timeout_ms", thresholds.feedback_timeout_ms),
        ("temp_warning_c", thresholds.temp_warning_c),
        ("temp_critical_c", thresholds.temp_critical_c),
        ("tx_error_threshold", thresholds.tx_error_threshold),
    ):
        if value <= 0:
            raise ValueError(f"{source}: health.{key} は正の値である必要があります: {value!r}")

    if thresholds.temp_warning_c > thresholds.temp_critical_c:
        raise ValueError(
            f"{source}: health.temp_warning_c ({thresholds.temp_warning_c}) は"
            f" health.temp_critical_c ({thresholds.temp_critical_c}) 以下である必要があります"
        )

    return thresholds


def _parse_match(source: str, raw: object) -> MatchSettings:
    section = _require_mapping(source, "match", raw)
    _reject_unknown(source, "match", section, _MATCH_KEYS)

    value = section.get("duration_s")
    if value is None:
        return MatchSettings()

    duration = _number(source, "match.duration_s", value)
    if duration <= 0:
        raise ValueError(f"{source}: match.duration_s は正の秒数である必要があります: {value!r}")
    return MatchSettings(duration_s=duration)


def load_system_config(config: Mapping | None, *, source: str = "<inline>") -> SystemConfig:
    raw = _require_mapping(source, "(最上位)", config)
    _reject_unknown(source, "(最上位)", raw, _SYSTEM_KEYS)

    return SystemConfig(
        can_buses=MappingProxyType(_parse_can_buses(source, raw.get("can_buses"))),
        health=_parse_health(source, raw.get("health")),
        match=_parse_match(source, raw.get("match")),
        source=source,
    )


def _parse_pid(source: str, motor_name: str, raw: object) -> Mapping[str, object] | None:
    if raw is None:
        return None
    path = f"motors.{motor_name}.pid"
    section = _require_mapping(source, path, raw)
    _reject_unknown(source, path, section, _PID_KEYS)

    for key, value in section.items():
        if value is None:
            continue
        _number(source, f"{path}.{key}", value)

    return MappingProxyType(dict(section))


def _optional[T](
    parse: Callable[[str, str, object], T],
    source: str,
    path: str,
    raw: Mapping,
    key: str,
    default: T,
) -> T:
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
    return SensorConfig(
        name=sensor_name,
        bus=bus,
        can_id=can_id,
        expected_firmware=_parse_expected_firmware(source, path, sensor),
    )


def _parse_expected_firmware(source: str, path: str, raw: Mapping) -> int | None:
    value = _optional(_integer, source, path, raw, "expected_firmware", None)
    if value is not None and not 0 <= value <= 0xFF:
        raise ValueError(
            f"{source}: {path}.expected_firmware が uint8 の範囲外です: {value} "
            "(INFO の Byte0 は 1 バイト。仕様書 §3.4)"
        )
    return value


def _parse_expected_angle_range(
    source: str, path: str, motor: Mapping, control_type: ControlMode
) -> float | None:
    value = _optional(_number, source, path, motor, "expected_angle_range_deg", None)
    if value is None:
        return None

    if value <= 0:
        raise ValueError(
            f"{source}: {path}.expected_angle_range_deg は正の値です: {value} "
            "(0 以下だと角度 → パルス幅の変換そのものが定義できない)"
        )

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


def _check_dm3520_master_id_collisions(
    source: str,
    motors: Mapping[str, MotorConfig],
    sensors: Mapping[str, SensorConfig],
) -> None:
    all_nodes: list[tuple[str, str, int]] = [
        (name, motor.bus, motor.can_id) for name, motor in motors.items()
    ] + [(name, sensor.bus, sensor.can_id) for name, sensor in sensors.items()]

    for motor_name, motor in motors.items():
        if motor.driver != "dm3520":
            continue
        master_low = motor.master_id & 0xFF
        for other_name, other_bus, other_can_id in all_nodes:
            if other_bus != motor.bus:
                continue
            if (other_can_id & 0xFF) != master_low:
                continue
            raise ValueError(
                f"{source}: motors.{motor_name}.master_id (0x{motor.master_id:03X}) の"
                f"下位 8bit が同じバス '{motor.bus}' 上の '{other_name}' の "
                f"can_id (0x{other_can_id:02X}) と衝突しています。本機は受信 ID の"
                "下位 8bit だけを見て自分宛かを判定するため、一致するとフィードバックが"
                "指令として解釈されます (仕様書 §2.2)。master_id (レジスタ 0x07) を、"
                f"同じバス上のどの can_id (ESC_ID) の下位 8bit とも異なる値へ"
                "書き換えてください"
            )


def load_robot_config(
    config: Mapping | None,
    *,
    source: str = "<inline>",
    buses: Mapping[str, str] | None = None,
) -> RobotConfig:
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
        raise ValueError(
            f"{source}: motors と sensors で名前が重複しています: {', '.join(overlap)}"
        )

    _check_dm3520_master_id_collisions(source, motors, sensors)

    return RobotConfig(
        robot_name=robot_name,
        motors=MappingProxyType(motors),
        sensors=MappingProxyType(sensors),
        source=source,
    )
