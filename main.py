from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import functools
import importlib
import logging
import os
import pathlib
import signal
import socket
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from types import ModuleType

import can
import yaml

from lib.axis_sync import SyncGroup
from lib.can_manager import CANManager
from lib.config_schema import (
    HealthThresholds,
    MotorConfig,
    RobotConfig,
    SystemConfig,
    load_robot_config,
    load_system_config,
)
from lib.control.feedback import FeedbackFreshness
from lib.control.limit_guard import LimitGuard, combine_limit_guards
from lib.control.pid import PIDController
from lib.control.position_loop import M3508PositionLoop, make_position_pid
from lib.control.sync_monitor import SyncMonitor
from lib.control.target_refresh import (
    GenericTargetRefresher,
    QueryDrivenTargetRefresher,
    TargetRefresher,
)
from lib.control.trajectory import TrapezoidalProfile
from lib.drivers.base import MotorDriver
from lib.drivers.dm3520 import Dm3520Driver
from lib.drivers.edulite05 import Edulite05Driver
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import CURRENT_MAX, M3508Driver
from lib.logging_setup import configure_logging
from lib.manual import ManualController
from lib.match_state import ChecklistItem, load_checklist_definitions
from lib.sequence.engine import Sequence
from lib.sequence.homing import HomingError, HomingRunner, SuspendSensors
from lib.sequence.motors import (
    AxisHandle,
    EStopChecker,
    MotorGroup,
    TargetSink,
    build_axis_handle,
    build_motor_group,
)
from lib.sequence.positions import (
    AxisSpec,
    PositionLookupError,
    PositionTable,
    load_position_table,
)
from lib.server import RobotServer
from sequences.motor_check import MotorCheckSequence

logger = logging.getLogger(__name__)

_CONFIG_DIR = pathlib.Path(__file__).resolve().parent / "config"
_DEFAULT_CONFIGS = ["main_hand.yaml", "sub_hand.yaml"]
_SYSTEM_CONFIG = "system.yaml"
_CHECKLIST_CONFIG = "checklist.yaml"
_POSITIONS_SUFFIX = "_positions.yaml"

# TODO(実機で確認): 「動かないより暴れない」を優先した保守的な仮値。
_DEFAULT_PID: dict[str, float | None] = {
    "kp": 2.0,
    "ki": 0.0,
    "kd": 0.0,
    "integral_limit": None,
    "dead_band": 1.0,
    "output_limit": 2000.0,
}


_DEV_TOOLS_ENV = "CBC_DEV_TOOLS"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CBC2026 Team3 中央制御プログラム")
    parser.add_argument(
        "--config",
        nargs="*",
        help="config ファイルパス (デフォルト: config/main_hand.yaml config/sub_hand.yaml)",
    )
    parser.add_argument(
        "--system",
        help="共通設定 yaml のパス (デフォルト: config/system.yaml)",
    )
    parser.add_argument(
        "--checklist",
        help="指差喚呼チェックリストの yaml パス (デフォルト: config/checklist.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="CAN バスなしで起動 (mock バスを使用)",
    )
    parser.add_argument(
        "--dev-tools",
        action="store_true",
        help=(
            "開発用コマンドを解禁する (指差喚呼の一括チェック等)。"
            f"環境変数 {_DEV_TOOLS_ENV}=1 でも有効になる。試合運用では使わないこと"
        ),
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="サーバーバインドアドレス (デフォルト: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="サーバーポート (デフォルト: 8080)",
    )
    parser.add_argument(
        "--log-level",
        type=str.lower,
        choices=("debug", "info", "warning", "error"),
        default="info",
        help="ログの出力レベル (デフォルト: info)",
    )
    return parser.parse_args()


def _load_config(path: pathlib.Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _load_all_configs(
    system_path: pathlib.Path, config_paths: list[pathlib.Path]
) -> tuple[SystemConfig, list[tuple[pathlib.Path, RobotConfig]]]:
    if not system_path.exists():
        raise SystemExit(f"共通設定ファイルが見つかりません: {system_path}")

    try:
        system = load_system_config(_load_config(system_path) or {}, source=str(system_path))
    except (ValueError, yaml.YAMLError) as exc:
        raise SystemExit(f"設定を読み込めません: {exc}") from exc

    loaded: list[tuple[pathlib.Path, RobotConfig]] = []
    for config_path in config_paths:
        if not config_path.exists():
            logger.warning("config ファイルが見つかりません: %s (スキップ)", config_path)
            continue
        try:
            robot = load_robot_config(
                _load_config(config_path) or {},
                source=str(config_path),
                buses=system.can_buses,
            )
        except (ValueError, yaml.YAMLError) as exc:
            raise SystemExit(f"設定を読み込めません: {exc}") from exc
        loaded.append((config_path, robot))

    return system, loaded


def _load_checklist_definitions(path: pathlib.Path) -> dict[str, list[ChecklistItem]]:
    if not path.exists():
        logger.warning("チェックリスト設定が見つかりません: %s (項目なしで起動)", path)
        return load_checklist_definitions({})
    try:
        return load_checklist_definitions(_load_config(path) or {})
    except (ValueError, yaml.YAMLError) as exc:
        raise SystemExit(f"設定を読み込めません: {exc}") from exc


def _positions_path(config_path: pathlib.Path, robot_name: str) -> pathlib.Path:
    return config_path.with_name(f"{robot_name}{_POSITIONS_SUFFIX}")


def _load_position_table_file(path: pathlib.Path) -> PositionTable:
    if not path.exists():
        logger.warning("位置定数ファイルが見つかりません: %s (定数なしで起動)", path)
        return PositionTable.empty(source=str(path))
    try:
        return load_position_table(_load_config(path) or {}, source=str(path))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        logger.error("位置定数ファイルを読み込めません: %s (%s) — 定数なしで起動", path, exc)
        return PositionTable.empty(source=str(path))


@contextlib.contextmanager
def _suspend_sync_monitoring(monitors: list[SyncMonitor], axis: str) -> Iterator[None]:
    with contextlib.ExitStack() as stack:
        for monitor in monitors:
            if axis in monitor.group_names:
                stack.enter_context(monitor.suspend_group(axis))
        yield


def _make_origin_resolver(
    loops: list[M3508PositionLoop],
    table: PositionTable,
    *,
    can_managers: list[CANManager] | None = None,
    sync_monitors: list[SyncMonitor] | None = None,
) -> Callable[[str], Callable[[], Awaitable[None]] | None]:
    managers = can_managers or []
    monitors = sync_monitors or []

    def _resolve_via_set_zero(axis: str) -> Callable[[], Awaitable[None]] | None:
        names = table.axis(axis).motor_names
        for manager in managers:
            drivers = [manager.motors.get(name) for name in names]
            if any(driver is None for driver in drivers):
                continue
            if not all(driver.supports_origin_capture() for driver in drivers if driver):
                return None

            async def capture(manager: CANManager = manager) -> None:
                with _suspend_sync_monitoring(monitors, axis):
                    await manager.capture_origin_via_set_zero(names)

            return capture
        return None

    def resolve(axis: str) -> Callable[[], Awaitable[None]] | None:
        spec = table.axis(axis)
        for loop in loops:
            if axis in loop.sync_group_names:
                return _as_async(functools.partial(loop.set_group_origin_here, axis))
            for motor in spec.motor_names:
                if motor in loop.motor_names:
                    return _as_async(functools.partial(loop.set_origin_here, motor))
        return _resolve_via_set_zero(axis)

    return resolve


def _as_async(capture: Callable[[], None]) -> Callable[[], Awaitable[None]]:

    async def run() -> None:
        capture()

    return run


@dataclasses.dataclass(frozen=True)
class _SensorView:
    """全 CANManager 横断のセンサ読み取り口。零点確定とリミット保護が共有する。

    **同じ組み立てを 2 箇所へ書き写さない。** 未登録センサの扱い (途絶へ倒す) と
    回数の読み方 (読んでも減らない) は、片方だけ直せる形にすると必ず食い違う ——
    症状は「動作確認では止まるのに常駐保護は止まらない」で、どちらが正しいのか
    コードから決められなくなる。
    """

    #: センサ名 → **今**接触しているか
    active: Callable[[str], bool]
    #: センサ名 → 立ち上がりを数えた接触回数 (読んでも減らない)
    contact_count: Callable[[str], int]
    #: センサ名 → フィードバックが途絶しているか
    is_stale: Callable[[str], bool]
    #: モータ名 → フィードバックが途絶しているか
    motor_is_stale: Callable[[str], bool]
    #: センサ名 → config の `sensors:` に登録されているか。**途絶とは別物**で、
    #: 起動時点ではどのセンサもまだ 1 通も受けていない (= 全部が途絶) ので、
    #: 配線の誤りを起動ログで名指しできるのはこちらだけである
    known: Callable[[str], bool]


def _build_sensor_view(
    can_managers: list[CANManager], *, feedback_timeout_ms: float
) -> _SensorView:
    """センサ読み取りの注入口を組み立てる。

    センサ名はロボット横断に一意なので、全 CANManager から引ける形にする
    (どのマネージャが持っていても答えは 1 つ)。
    """
    sensors = {name: sensor for mgr in can_managers for name, sensor in mgr.sensors.items()}
    freshness = FeedbackFreshness(
        _merged_last_feedback_at(can_managers), timeout_ms=feedback_timeout_ms
    )

    def sensor_active(name: str) -> bool:
        sensor = sensors.get(name)
        # 未登録のセンサは「触れていない」ではなく途絶として扱わせる
        # (下の sensor_is_stale が True を返すので零点確定は 1 歩も動かさず、
        #  リミット保護は blind_sensors として「効いていない」を主張する)
        return sensor is not None and bool(getattr(sensor, "sensor_active", False))

    def sensor_contact_count(name: str) -> int:
        # ON 区間が観測周期より狭いと「今 ON か」では通過を丸ごと取りこぼす。
        # 読んでも減らないので、零点確定とリミット保護が同じセンサを見ていても
        # 互いの接触を消し合わない
        sensor = sensors.get(name)
        if sensor is None:
            return 0
        count = getattr(sensor, "sensor_contact_count", None)
        if count is not None:
            return int(count)
        # 回数を持たないドライバでは現在値へ落ちる (触れている間だけ 1 になる
        # ので、区間を跨いだ接触は取りこぼしうる。それでも「一度も到達しない
        # 探索」にはしない)。歯止めは search_distance が持つ
        return int(bool(getattr(sensor, "sensor_active", False)))

    def sensor_is_stale(name: str) -> bool:
        if name not in sensors:
            logger.error("センサ '%s' が config の sensors: に居ません", name)
            return True
        return freshness.is_stale(name, freshness.now())

    def motor_is_stale(name: str) -> bool:
        # 対象軸の実測位置が読めるかを、探索を始める前に問う。未受信の 0.0 を
        # 現在位置と信じると、1 歩目が原点近傍への 1 回のジャンプになり、
        # その移動は search_distance の歯止めに 1mm も掛からない
        return freshness.is_stale(name, freshness.now())

    return _SensorView(
        active=sensor_active,
        contact_count=sensor_contact_count,
        is_stale=sensor_is_stale,
        motor_is_stale=motor_is_stale,
        known=lambda name: name in sensors,
    )


def _build_limit_guard(
    positions: PositionTable,
    motors: MotorGroup | None,
    sensors: _SensorView,
    *,
    axis_handle: Callable[[str], AxisHandle | None],
) -> LimitGuard | None:
    """``limits:`` を書いた軸のリミット保護を組み立てる。対象が無ければ None。

    対象が 1 本も無いロボットで空のタスクを立てない (``SyncMonitor`` を
    ``sync_groups`` が空なら作らないのと同じ)。

    **センサが `sensors:` に登録されていないことは起動ログの ERROR で名指しする。**
    黙って通すと、保護を書いたつもりの軸が「触れても止まらない」まま試合に入る。
    起動そのものは拒否しない —— 実行時には `blind_sensors` として画面にも出る
    (未登録センサは `sensor_is_stale` が常に True を返すため) ので、片ハンドだけの
    構成や机上ベンチを起動できなくするほうが害が大きい。

    **見るのは登録の有無であって鮮度ではない。** 起動時点ではどのセンサもまだ 1 通も
    受けていないので、鮮度で見ると**毎回必ず ERROR が出る** —— 毎起動出る ERROR は
    読まれなくなり、本物の配線ミスまで一緒に流れる。
    """
    if motors is None:
        return None

    specs: list[AxisSpec] = []
    for axis_name in positions.limit_axes():
        spec = positions.axis(axis_name)
        missing = [motor.name for motor in spec.motors if motor.name not in motors]
        if missing:
            # 黙って飛ばすと「保護しているつもり」で機構破損に至るため必ず残す
            logger.warning(
                "リミット保護をスキップ: 軸 %s のモータ %s がこのロボットに存在しません",
                axis_name,
                ", ".join(missing),
            )
            continue
        specs.append(spec)

    if not specs:
        return None

    for spec in specs:
        # **どの軸のどのセンサがどちら向きの保護に付いたか**を起動ログに残す。
        # limits を書いていない軸があること自体は正常なので警告にはしない
        logger.info(
            "リミット保護: %s (%s)",
            spec.name,
            ", ".join(f"{limit.sensor} → {limit.direction:+g} 方向" for limit in spec.limits),
        )

    unregistered = sorted(
        {limit.sensor for spec in specs for limit in spec.limits if not sensors.known(limit.sensor)}
    )
    if unregistered:
        logger.error(
            "リミット保護: センサ %s が config の sensors: に居ません"
            " (この向きの保護は永久に効かない。触れても止まらない)",
            ", ".join(unregistered),
        )

    return LimitGuard(
        specs,
        sensor_active=sensors.active,
        sensor_contact_count=sensors.contact_count,
        sensor_is_stale=sensors.is_stale,
        axis_handle=axis_handle,
    )


def _make_axis_handle_lookup(
    positions: PositionTable, group: MotorGroup | None
) -> Callable[[str], AxisHandle | None]:
    """軸名 → 指令口。**モータ単位の指令口はここからも生まれない。**

    組み立ては `move_to` / 手動操縦と同じ `build_axis_handle` に委ねる。**遅延して
    引くこと自体に意味がある** —— リミット保護は自分を止めるために軸ハンドルを要り、
    軸ハンドルは保護の結ばれたモータ群を要るので、生成順が循環する。ここが呼ばれた
    時点でモータ群を読めば、`bind_limit_guard` が後から結んだ保護がそのまま乗る。
    """

    def lookup(axis: str) -> AxisHandle | None:
        if group is None:
            return None
        try:
            return build_axis_handle(positions.axis(axis), group)
        except (AttributeError, PositionLookupError):
            # このロボットに無い軸・揃っていないモータ (片ハンドだけの構成)
            return None

    return lookup


def _suspend_limit_guards(guards: list[LimitGuard]) -> SuspendSensors:
    """零点確定へ渡す「そのセンサの保護だけを外す」口 (全ロボット横断)。

    零点確定は両ハンド 1 本のシーケンスから走るので、どのロボットの保護を外すかは
    センサ名からしか決まらない。**軸まるごとではなくセンサ単位**なのは、両端に
    スイッチのある軸で反対端の保護まで消さないため。
    """

    @contextlib.contextmanager
    def suspend(names: Iterable[str]) -> Iterator[None]:
        names = tuple(names)
        with contextlib.ExitStack() as stack:
            for guard in guards:
                owned = [name for name in names if guard.watches(name)]
                if owned:
                    stack.enter_context(guard.suspend_sensors(owned))
            yield

    return suspend


def _wire_motor_check_sequence(
    server: RobotServer,
    groups: list[MotorGroup],
    tables: list[PositionTable],
    *,
    loops: list[M3508PositionLoop],
    can_managers: list[CANManager],
    sync_monitors: list[SyncMonitor],
    limit_guards: list[LimitGuard],
    sensors: _SensorView,
) -> None:
    if not tables:
        logger.info("統合動作確認: 位置定数が 1 つも無いため登録しない")
        return

    merged = PositionTable.merged(tables)

    sequence = MotorCheckSequence(available_axes=merged.axes)
    for excluded in sequence.excluded_steps:
        logger.warning(
            "統合動作確認: ステップ '%s' を除外 (構成に無い軸: %s)",
            excluded.label,
            ", ".join(excluded.missing_axes),
        )
    if not any(info.axes for info in sequence.steps):
        logger.warning(
            "統合動作確認: 指令できる軸が 1 本も無いため登録しない (位置定数の軸: %s)",
            sorted(merged.axes),
        )
        return

    motors = MotorGroup()
    for group in groups:
        for handle in group.handles:
            motors.add(handle)
    # **束ねたモータ群にも保護を結ぶ。** ここは両ハンドを混ぜた別のグループなので、
    # ロボットごとの `bind_limit_guard` は届かない。結び忘れると、全アクチュエータを
    # 順に駆動する動作確認だけが保護の外で走る
    motors.bind_limit_guard(combine_limit_guards(limit_guards))

    sequence.bind_motors(motors)
    sequence.bind_positions(merged)

    homing_axes = [name for name in merged.axes if merged.axis(name).homing is not None]
    if homing_axes:
        resolve_origin = _make_origin_resolver(
            loops, merged, can_managers=can_managers, sync_monitors=sync_monitors
        )

        def _origin_capturable(axis: str) -> bool:
            return resolve_origin(axis) is not None

        async def _capture_origin(axis: str) -> None:
            capture = resolve_origin(axis)
            if capture is None:
                raise HomingError(
                    f"軸 '{axis}' の原点を確定できません"
                    " (PC 側位置制御ループに載らず、SET_ZERO を受け付けるドライバでもない)"
                )
            await capture()

        unsupported = [name for name in homing_axes if not _origin_capturable(name)]
        if unsupported:
            logger.error(
                "零点確定: 軸 %s は原点を確定する手段がありません"
                " (PC 側位置制御ループに載らず SET_ZERO も受け付けない。"
                "動作確認はこの軸で失敗する)",
                unsupported,
            )

        sequence.bind_homing(
            HomingRunner(
                sensor_active=sensors.active,
                sensor_contact_count=sensors.contact_count,
                sensor_is_stale=sensors.is_stale,
                motor_is_stale=sensors.motor_is_stale,
                origin_capturable=_origin_capturable,
                capture_origin=_capture_origin,
                # 「当たるまで動かす」あいだ、そのセンサの保護だけを外す。
                # 外さないと触れた瞬間に自分の指令が引き戻され原点へ到達できない
                suspend_sensors=_suspend_limit_guards(limit_guards),
            )
        )

    server.set_motor_check_sequence(sequence)
    logger.info(
        "統合動作確認シーケンス登録: %d ステップ (除外 %d, モータ %d 台, 軸 %d 本, 零点確定: %s)",
        len(sequence.steps),
        len(sequence.excluded_steps),
        len(motors),
        len(merged.axes),
        ", ".join(homing_axes) or "なし",
    )


def _merged_last_feedback_at(managers: list[CANManager]) -> Callable[[str], float | None]:

    def last_feedback_at(name: str) -> float | None:
        for mgr in managers:
            at = mgr.last_feedback_at(name)
            if at is not None:
                return at
        return None

    return last_feedback_at


_NET_SYSFS_ROOT = pathlib.Path("/sys/class/net")


def _read_operstate(channel: str, *, root: pathlib.Path = _NET_SYSFS_ROOT) -> str | None:
    try:
        return (root / channel / "operstate").read_text(errors="replace").strip()
    except OSError:
        return None


def _create_bus(channel: str, *, dry_run: bool) -> can.Bus:
    if dry_run:
        return can.Bus(interface="virtual", channel=channel)
    if _read_operstate(channel) == "down":
        logger.error(
            "CAN インタフェース '%s' は down です (起動は続けます)。"
            " scripts/setup_can.sh を実行してください",
            channel,
        )
    try:
        return can.Bus(interface="socketcan", channel=channel)
    except (OSError, can.CanError) as exc:
        raise SystemExit(
            f"CAN インタフェース '{channel}' を開けません ({exc})。"
            " scripts/setup_can.sh を実行してバスが up しているか確認してください"
            " (点検は scripts/setup_can.sh --strict)"
        ) from exc


def _robot_bus_names(robot: RobotConfig, can_buses: Mapping[str, str]) -> list[str]:
    used = {cfg.bus for cfg in robot.motors.values()}
    used |= {cfg.bus for cfg in robot.sensors.values()}
    return [name for name in can_buses if name in used]


def _make_m3508(motor: MotorConfig) -> MotorDriver:
    return M3508Driver(name=motor.name, can_id=motor.can_id)


def _make_edulite05(motor: MotorConfig) -> MotorDriver:
    return Edulite05Driver(
        name=motor.name,
        can_id=motor.can_id,
        host_id=motor.host_id,
        mode=motor.mode,
        limit_speed=motor.limit_speed,
        limit_current=motor.limit_current,
        position_kp=motor.position_kp,
        set_zero_on_start=motor.set_zero_on_start,
    )


def _make_dm3520(motor: MotorConfig) -> MotorDriver:
    return Dm3520Driver(
        name=motor.name,
        can_id=motor.can_id,
        master_id=motor.master_id,
        mode=motor.mode,
        limit_speed=motor.limit_speed,
        p_max=motor.p_max,
        v_max=motor.v_max,
        t_max=motor.t_max,
        set_zero_on_start=motor.set_zero_on_start,
    )


def _make_generic(motor: MotorConfig) -> MotorDriver:
    return GenericDriver(
        name=motor.name,
        can_id=motor.can_id,
        control_type=motor.control_type,
        expected_firmware=motor.expected_firmware,
        expected_angle_range_deg=motor.expected_angle_range_deg,
    )


_DRIVER_MAP: dict[str, Callable[[MotorConfig], MotorDriver]] = {
    "m3508": _make_m3508,
    "edulite05": _make_edulite05,
    "generic": _make_generic,
    "dm3520": _make_dm3520,
}


def _create_motor(motor: MotorConfig) -> MotorDriver:
    return _DRIVER_MAP[motor.driver](motor)


def _setup_robot(
    robot: RobotConfig, can_buses: Mapping[str, str], *, dry_run: bool
) -> tuple[CANManager, dict[str, MotorDriver]]:
    can_manager = CANManager()

    for bus_name in _robot_bus_names(robot, can_buses):
        can_manager.add_bus(bus_name, _create_bus(can_buses[bus_name], dry_run=dry_run))

    motors: dict[str, MotorDriver] = {}
    for motor_name, motor_cfg in robot.motors.items():
        motor = _create_motor(motor_cfg)
        can_manager.add_motor(motor_cfg.bus, motor)
        motors[motor_name] = motor

    for sensor_cfg in robot.sensors.values():
        can_manager.add_sensor(
            sensor_cfg.bus,
            GenericDriver(
                sensor_cfg.name,
                sensor_cfg.can_id,
                expected_firmware=sensor_cfg.expected_firmware,
            ),
        )

    return can_manager, motors


def _load_pid_config(
    motor_name: str, pid_cfg: Mapping[str, object] | None
) -> dict[str, float | None]:
    result: dict[str, float | None] = dict(_DEFAULT_PID)
    if not isinstance(pid_cfg, Mapping):
        return result

    for key, value in pid_cfg.items():
        if key not in _DEFAULT_PID:
            logger.warning("未知の pid キー: motors.%s.pid.%s (無視)", motor_name, key)
            continue
        if value is None and key == "integral_limit":
            result[key] = None
            continue
        if value is None:
            logger.warning(
                "motors.%s.pid.%s が null です。既定値 %s を使います。",
                motor_name,
                key,
                _DEFAULT_PID[key],
            )
            continue
        result[key] = float(value)
    return result


def _build_position_pid(motor: MotorConfig) -> PIDController:
    params = _load_pid_config(motor.name, motor.pid)
    pid = make_position_pid(
        params["kp"],
        params["ki"],
        params["kd"],
        integral_limit=params["integral_limit"],
        dead_band=params["dead_band"],
    )

    limit = min(abs(float(params["output_limit"])), float(CURRENT_MAX))
    pid.output_min = -limit
    pid.output_max = limit
    return pid


def _build_position_loops(
    robot: RobotConfig,
    can_manager: CANManager,
    motors: dict[str, MotorDriver],
    *,
    feedback_timeout_ms: float,
    is_estop_active: EStopChecker,
) -> dict[str, M3508PositionLoop]:
    loops: dict[str, M3508PositionLoop] = {}

    for motor_name, motor_cfg in robot.motors.items():
        driver = motors.get(motor_name)
        if not isinstance(driver, M3508Driver):
            continue

        bus_name = motor_cfg.bus
        loop = loops.get(bus_name)
        if loop is None:
            loop = M3508PositionLoop(
                can_manager,
                bus_name,
                feedback_timeout_ms=feedback_timeout_ms,
                is_estop_active=is_estop_active,
            )
            loops[bus_name] = loop
        loop.add_motor(motor_name, driver, _build_position_pid(motor_cfg))

    return loops


def _wire_robot_motors(
    robot: RobotConfig,
    can_manager: CANManager,
    motors: dict[str, MotorDriver],
    sequence: Sequence,
    *,
    feedback_timeout_ms: float,
    is_estop_active: EStopChecker,
) -> list[M3508PositionLoop]:
    loops = _build_position_loops(
        robot,
        can_manager,
        motors,
        feedback_timeout_ms=feedback_timeout_ms,
        is_estop_active=is_estop_active,
    )

    target_sinks: dict[str, TargetSink] = {}
    for loop in loops.values():
        target_sinks.update(loop.target_sinks())

    sequence.bind_motors(
        build_motor_group(
            can_manager,
            motors,
            is_estop_active=is_estop_active,
            target_sinks=target_sinks,
        )
    )
    return list(loops.values())


def _build_target_refreshers(
    group: MotorGroup,
    motors: dict[str, MotorDriver],
    can_manager: CANManager,
    *,
    is_estop_active: EStopChecker,
) -> list[TargetRefresher]:
    refreshers: list[TargetRefresher] = []

    generic = [group[name] for name, drv in motors.items() if isinstance(drv, GenericDriver)]
    if generic:
        refreshers.append(GenericTargetRefresher(generic, is_estop_active=is_estop_active))

    query_driven = [
        group[name]
        for name, drv in motors.items()
        if isinstance(drv, Dm3520Driver | Edulite05Driver)
    ]
    if query_driven:
        refreshers.append(
            QueryDrivenTargetRefresher(query_driven, can_manager, is_estop_active=is_estop_active)
        )

    return refreshers


def _build_manual_controller(sequence: Sequence, positions: PositionTable) -> ManualController:
    return ManualController(sequence.motors, positions)


def _build_sync_groups(positions: PositionTable, motors: dict[str, MotorDriver]) -> list[SyncGroup]:
    groups: list[SyncGroup] = []
    for axis_name in positions.paired_axes():
        spec = positions.axis(axis_name)
        missing = [motor.name for motor in spec.motors if motor.name not in motors]
        if missing:
            logger.warning(
                "同期監視をスキップ: 軸 %s のモータ %s がこのロボットに存在しません",
                axis_name,
                ", ".join(missing),
            )
            continue
        group = spec.sync_group
        if group is not None:
            groups.append(group)
    return groups


def _attach_sync_groups(groups: list[SyncGroup], loops: list[M3508PositionLoop]) -> None:
    for group in groups:
        member_names = {member.name for member in group.members}
        target = next(
            (loop for loop in loops if member_names <= set(loop.motor_names)),
            None,
        )
        if target is None:
            logger.info("同期監視: %s は位置制御ループ外 (SyncMonitor のみで監視)", group.name)
            continue
        target.add_sync_group(group)
        logger.info("同期監視: %s を位置制御ループ (bus=%s) に登録", group.name, target.bus_name)


def _attach_motion_profiles(positions: PositionTable, loops: list[M3508PositionLoop]) -> None:
    for axis_name in positions.axes:
        spec = positions.axis(axis_name)
        motion = spec.motion
        if motion is None:
            continue

        attached: list[str] = []
        for motor in spec.motors:
            loop = next(
                (candidate for candidate in loops if motor.name in candidate.motor_names), None
            )
            if loop is None:
                logger.info(
                    "台形プロファイル: 軸 %s のモータ %s は位置制御ループ外 "
                    "(ドライバ内蔵の位置ループが持つため中間目標は要らない)",
                    axis_name,
                    motor.name,
                )
                continue
            scale = abs(motor.scale)
            loop.set_motion_profile(
                motor.name,
                TrapezoidalProfile(
                    max_velocity=motion.max_velocity * scale,
                    max_acceleration=motion.max_acceleration * scale,
                ),
                velocity_ff=motion.velocity_ff,
            )
            attached.append(motor.name)

        if not attached:
            continue
        logger.info(
            "台形プロファイル: %s (%s) v<=%.1f %s/s, a<=%.1f %s/s^2, velocity_ff=%g",
            axis_name,
            ", ".join(attached),
            motion.max_velocity,
            spec.unit,
            motion.max_acceleration,
            spec.unit,
            motion.velocity_ff,
        )


def _make_sync_violation_handler(
    server: RobotServer,
    robot_name: str,
    positions: PositionTable,
    tasks: set[asyncio.Task[None]],
) -> Callable[[str, float], None]:

    def on_violation(axis_name: str, deviation: float) -> None:
        spec = positions.axis(axis_name)
        unit = spec.unit
        tolerance = float(spec.sync_tolerance or 0.0)
        reason = (
            f"{robot_name} の {axis_name} の左右ずれ {deviation:.3f}{unit} が"
            f" 許容 {tolerance:.3f}{unit} を超えました"
        )
        task = asyncio.create_task(server.activate_e_stop(reason=reason))
        tasks.add(task)
        server.watch_task(
            task, context=f"{robot_name} の同期ずれ検出 → 緊急停止", robots=server.robot_names
        )
        task.add_done_callback(tasks.discard)

    return on_violation


def _load_sequence(robot_name: str) -> Sequence | None:
    module_name = f"sequences.{robot_name}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        logger.info("シーケンスモジュール %s が見つかりません。ダミーを使用します。", module_name)
        return None

    sequence_cls = _sequence_class_defined_in(module)
    if sequence_cls is None:
        logger.warning("モジュール %s に Sequence サブクラスが見つかりません。", module_name)
        return None
    return sequence_cls(robot_name)


def _sequence_class_defined_in(module: ModuleType) -> type[Sequence] | None:
    found = [
        attr
        for attr_name in dir(module)
        if isinstance(attr := getattr(module, attr_name), type)
        and issubclass(attr, Sequence)
        and attr is not Sequence
        and attr.__module__ == module.__name__
    ]
    if len(found) > 1:
        names = ", ".join(cls.__name__ for cls in found)
        raise SystemExit(
            f"モジュール {module.__name__} が Sequence サブクラスを複数定義しています"
            f" ({names})。どれを登録すべきか決められないため起動できません。"
        )
    return found[0] if found else None


class _PlaceholderSequence(Sequence):
    pass


def _install_stop_signal_handler() -> None:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    if task is None:
        return

    stopping = False

    def _request_stop() -> None:
        nonlocal stopping
        if stopping:
            logger.warning("後始末の実行中です。停止シグナルを無視します")
            return
        stopping = True
        logger.info("SIGTERM を受信しました。後始末を開始します")
        task.cancel()

    loop.add_signal_handler(signal.SIGTERM, _request_stop)


async def _shutdown_step(label: str, awaitable: Awaitable[None]) -> None:
    try:
        await awaitable
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("終了処理に失敗しました (%s)。残りの後始末は続行します", label)


@dataclasses.dataclass(frozen=True)
class _RobotWiring:
    name: str
    sequence: Sequence
    can_manager: CANManager
    positions: PositionTable
    position_loops: list[M3508PositionLoop]
    sync_monitors: list[SyncMonitor]
    target_refreshers: list[TargetRefresher]
    motor_group: MotorGroup | None
    #: 手動操縦の指令口 (調整時・緊急時の退避路)。**既定値を持たせない** ——
    #: 渡し忘れが「手動へ切り替えられない機体」として静かに成立し、症状は
    #: 試合中に退避しようとした瞬間にしか出ない
    manual: ManualController | None
    #: リミットスイッチ保護。`limits:` を書いた軸が 1 本も無い構成では None。
    #: **配線は `_wire_one_robot` の後**なので (センサをロボット横断で引くため
    #: 全 CANManager が要る)、`_wire_limit_guards` が `dataclasses.replace` で入れる
    limit_guard: LimitGuard | None = None


def _wire_one_robot(
    server: RobotServer,
    config_path: pathlib.Path,
    robot: RobotConfig,
    system: SystemConfig,
    *,
    dry_run: bool,
    is_estop_active: EStopChecker,
    e_stop_tasks: set[asyncio.Task[None]],
) -> _RobotWiring:
    # サーバーへの登録はここではない (`_register_robots`)。リミット保護は全機の
    # CANManager が揃うまで組めないので、1 台ぶんの配線が済んだ時点ではまだ渡せない。
    # どの部品も「作って渡す」だけで、ここでは 1 つも起動しない —— 起動は
    # `_start_all` が全機ぶんまとめて行う (CAN の受信ループが立つ前に位置制御ループを
    # 回すと、フィードバック未受信のまま途絶判定を踏んで警告が出る)
    robot_name = robot.robot_name
    can_manager, motors = _setup_robot(robot, system.can_buses, dry_run=dry_run)

    seq = _load_sequence(robot_name)
    if seq is None:
        seq = _PlaceholderSequence(robot_name)

    positions = _load_position_table_file(_positions_path(config_path, robot_name))
    seq.bind_positions(positions)

    loops = _wire_robot_motors(
        robot,
        can_manager,
        motors,
        seq,
        feedback_timeout_ms=system.health.feedback_timeout_ms,
        is_estop_active=is_estop_active,
    )

    refreshers = _build_target_refreshers(
        seq.motors,
        motors,
        can_manager,
        is_estop_active=is_estop_active,
    )

    sync_groups = _build_sync_groups(positions, motors)
    _attach_sync_groups(sync_groups, loops)
    _attach_motion_profiles(positions, loops)
    monitors: list[SyncMonitor] = []
    if sync_groups:
        monitors.append(
            SyncMonitor(
                sync_groups,
                motors,
                last_feedback_at=can_manager.last_feedback_at,
                feedback_timeout_ms=system.health.feedback_timeout_ms,
                on_violation=_make_sync_violation_handler(
                    server, robot_name, positions, e_stop_tasks
                ),
            )
        )

    manual = _build_manual_controller(seq, positions)

    return _RobotWiring(
        name=robot_name,
        sequence=seq,
        can_manager=can_manager,
        positions=positions,
        position_loops=loops,
        sync_monitors=monitors,
        target_refreshers=refreshers,
        motor_group=seq.motors if seq.has_motors else None,
        manual=manual,
    )


def _register_robots(server: RobotServer, wirings: list[_RobotWiring]) -> None:
    """配線し終えた部品をサーバーへ登録する。

    **リミット保護を組んだ後でなければ登録できない。** 保護の読み取り口はセンサ名
    がロボット横断に一意であることに依っており、全機の `CANManager` が揃うまで
    組めない (`_wire_limit_guards`)。登録を `_wire_one_robot` の中に残したまま
    保護だけを後から差し込む口を足すと、部品ごとに渡し方が 2 通りになり、
    次に足す人が「渡したのに配信されない」側を選べてしまう。

    監視と保護をサーバーへ渡さないと、緊急停止解除で同期ずれのラッチを外す経路が
    存在せず (一度ずれを検知した軸は再起動するまで無監視・不動のまま残る)、
    リミット保護が止めている軸も画面のどこにも出ない。
    """
    for wiring in wirings:
        server.add_robot(
            wiring.name,
            wiring.sequence,
            wiring.can_manager,
            position_loops=wiring.position_loops,
            sync_monitors=wiring.sync_monitors,
            target_refreshers=wiring.target_refreshers,
            limit_guard=wiring.limit_guard,
            manual=wiring.manual,
        )
        # INFO は要約だけ。名前を全部並べると 1 行が 300 桁を超えて端末で折り返し、
        # 起動ログ全体が読めなくなる。**同期監視の対象名だけは残す** —— 左右ペアの
        # 保護が実際に何に掛かったかは、機構を壊す前に起動時点で確かめたい
        # (リミット保護の対象軸は `_build_limit_guard` が軸ごとに 1 行出す)
        logger.info(
            "ロボット登録: %s (モータ %d 台 / 軸 %d 本 / 位置制御ループ %s / 同期監視 %s)",
            wiring.name,
            len(wiring.can_manager.motors),
            len(wiring.positions.axes),
            ", ".join(loop.bus_name for loop in wiring.position_loops) or "なし",
            ", ".join(name for monitor in wiring.sync_monitors for name in monitor.group_names)
            or "なし",
        )
        # 名前の一覧は DEBUG。平常時は要約で足り、食い違いを疑ったときだけ
        # `--log-level debug` で読めればよい (config を読むより速い、という以上の
        # 役割は持たない情報である)
        logger.debug(
            "ロボット登録 %s の内訳: 位置定数軸 %s / 目標値再送 %s / 手動連続操作 %s",
            wiring.name,
            ", ".join(wiring.positions.axes) or "なし",
            ", ".join(name for r in wiring.target_refreshers for name in r.motor_names) or "なし",
            ", ".join(wiring.positions.manual_axes()) or "なし",
        )


def _wire_limit_guards(wirings: list[_RobotWiring], sensors: _SensorView) -> list[_RobotWiring]:
    """各ロボットへリミット保護を後付けする。

    **ロボットごとの配線 (`_wire_one_robot`) では組めない。** センサ名はロボット
    横断に一意なので読み取り口は全 CANManager から引く必要があり、その全部が
    揃うのは全機の配線が終わった後である。

    **組んだ保護はモータ群へ結ぶ (`bind_limit_guard`)。** 結ばないと 50Hz の常駐層
    (層①) だけが効き、シーケンス・手動操縦が出した指令は押し戻されるまでの 1 周期
    ぶん禁止方向へ進む。結ぶ先をモータ群にしてあるので、軸ハンドルを組む 4 経路の
    どれから来ても保護が付いてくる (渡し忘れうる引数が 1 つも無い)。
    """
    wired: list[_RobotWiring] = []
    for wiring in wirings:
        guard = _build_limit_guard(
            wiring.positions,
            wiring.motor_group,
            sensors,
            axis_handle=_make_axis_handle_lookup(wiring.positions, wiring.motor_group),
        )
        if wiring.motor_group is not None:
            wiring.motor_group.bind_limit_guard(guard)
        wired.append(dataclasses.replace(wiring, limit_guard=guard))
    return wired


def _ensure_port_available(host: str, port: int) -> None:
    try:
        family, socktype, proto, _canonname, sockaddr = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )[0]
    except OSError as exc:
        raise SystemExit(f"サーバーのアドレス {host}:{port} を解決できません ({exc})") from exc

    with socket.socket(family, socktype, proto) as probe:
        # aiohttp の TCPSite と同じ条件で試す (POSIX では既定で reuse_address が立つ)。
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(sockaddr)
        except OSError as exc:
            raise SystemExit(
                f"ポート {port} は既に使用中です ({exc})。"
                " 制御プログラムが既に起動していないか確認してください"
                " (systemctl status cbc-control / ss -ltnp)"
            ) from exc


async def _start_all(server: RobotServer, wirings: list[_RobotWiring]) -> None:
    for wiring in wirings:
        server.set_initial_inactive_motors(wiring.name, await wiring.can_manager.run())
    for wiring in wirings:
        for loop in wiring.position_loops:
            loop.start()
    for wiring in wirings:
        for monitor in wiring.sync_monitors:
            monitor.start()
    for wiring in wirings:
        if wiring.limit_guard is not None:
            wiring.limit_guard.start()
    for wiring in wirings:
        for refresher in wiring.target_refreshers:
            refresher.start()
    await server.start()


async def _shutdown_all(server: RobotServer, wirings: list[_RobotWiring]) -> None:
    """後始末。**1 手順が失敗しても残りを必ず続ける** (`_shutdown_step`)。

    順序に意味がある:
      1. リミット保護 —— 新しい目標値を書きうる唯一の常駐タスクなので最初に止める。
         後回しにすると、他を止めた後の 1 周期が「止めたはずの軸」へ目標を 1 通書く
      2. 位置制御ループ —— 生き残ると電流指令が出続けるので CAN より先に止める
      3. 目標値再送 —— 止めればファーム側のウォッチドッグが 500ms 以内に出力を落とす。
         停止指令をここから送らないのは、PC が落ちた場合と経路を 1 本に保つため
      4. 同期監視 —— これだけ生き残ると、停止済みのモータのフィードバックを見て誤発報する
      5. CAN シャットダウン
      6. サーバー終了処理
    """
    for wiring in wirings:
        if wiring.limit_guard is not None:
            await _shutdown_step("リミット保護", wiring.limit_guard.stop())
    for wiring in wirings:
        for loop in wiring.position_loops:
            await _shutdown_step(f"位置制御ループ (bus={loop.bus_name})", loop.stop())
    for wiring in wirings:
        for refresher in wiring.target_refreshers:
            await _shutdown_step("目標値再送", refresher.stop())
    for wiring in wirings:
        for monitor in wiring.sync_monitors:
            await _shutdown_step("同期監視", monitor.stop())
    for wiring in wirings:
        await _shutdown_step("CAN シャットダウン", wiring.can_manager.shutdown())
    await _shutdown_step("サーバー終了処理", server.cleanup())
    logger.info("後始末完了")


def _format_number(value: float) -> str:
    return f"{value:g}"


def _describe_thresholds(health: HealthThresholds) -> str:
    return (
        f"途絶 {_format_number(health.feedback_timeout_ms)}ms"
        f" / 温度 警告 {_format_number(health.temp_warning_c)}℃"
        f" 異常 {_format_number(health.temp_critical_c)}℃"
        f" / TX エラー {health.tx_error_threshold}"
    )


def _describe_checklist(definitions: Mapping[str, list[ChecklistItem]]) -> str:
    if not definitions:
        return "なし"
    if len(definitions) == 1:
        return f"{len(next(iter(definitions.values())))} 項目"
    return ", ".join(f"{role} {len(items)} 項目" for role, items in definitions.items())


def _build_server(args: argparse.Namespace, system: SystemConfig) -> RobotServer:
    dev_tools = args.dev_tools or _env_flag(_DEV_TOOLS_ENV)
    if dev_tools:
        logger.warning("開発用コマンドが有効です (指差喚呼の一括チェック等)。試合運用では外すこと")

    checklist_path = (
        pathlib.Path(args.checklist) if args.checklist else _CONFIG_DIR / _CHECKLIST_CONFIG
    )
    checklist_definitions = _load_checklist_definitions(checklist_path)
    logger.info("指差喚呼: %s", _describe_checklist(checklist_definitions))

    return RobotServer(
        host=args.host,
        port=args.port,
        health=system.health,
        checklist_definitions=checklist_definitions,
        match_settings=system.match,
        dry_run=args.dry_run,
        dev_tools=dev_tools,
    )


async def main() -> None:
    args = _parse_args()
    configure_logging(args.log_level)

    _install_stop_signal_handler()

    if args.config:
        config_paths = [pathlib.Path(p) for p in args.config]
    else:
        config_paths = [_CONFIG_DIR / name for name in _DEFAULT_CONFIGS]
    system_path = pathlib.Path(args.system) if args.system else _CONFIG_DIR / _SYSTEM_CONFIG

    system, loaded = _load_all_configs(system_path, config_paths)
    logger.info("しきい値: %s", _describe_thresholds(system.health))
    logger.info("試合時間: %s 秒", _format_number(system.match.duration_s))

    _ensure_port_available(args.host, args.port)

    server = _build_server(args, system)

    e_stop_tasks: set[asyncio.Task[None]] = set()

    def is_estop_active() -> bool:
        return server.e_stop_active

    wirings = [
        _wire_one_robot(
            server,
            config_path,
            robot,
            system,
            dry_run=args.dry_run,
            is_estop_active=is_estop_active,
            e_stop_tasks=e_stop_tasks,
        )
        for config_path, robot in loaded
    ]

    # センサ読み取りは零点確定とリミット保護が共有する。センサ名はロボット横断に
    # 一意なので、全機の配線が済んでからでないと組めない
    sensors = _build_sensor_view(
        [w.can_manager for w in wirings], feedback_timeout_ms=system.health.feedback_timeout_ms
    )
    wirings = _wire_limit_guards(wirings, sensors)

    # 登録はここ。リミット保護まで揃ってからでないと、保護だけが配信に載らない
    _register_robots(server, wirings)

    # 統合動作確認シーケンス。**両ハンドを 1 本の順序で駆動する**ので、
    # どのロボットにも属さない。機体ごとに独立した確認だと 2 つを同時に起動でき、
    # 可動域の重なる位置で干渉しうる
    _wire_motor_check_sequence(
        server,
        [w.motor_group for w in wirings if w.motor_group is not None],
        [w.positions for w in wirings],
        loops=[loop for w in wirings for loop in w.position_loops],
        can_managers=[w.can_manager for w in wirings],
        sync_monitors=[monitor for w in wirings for monitor in w.sync_monitors],
        limit_guards=[w.limit_guard for w in wirings if w.limit_guard is not None],
        sensors=sensors,
    )

    try:
        await _start_all(server, wirings)
    except asyncio.CancelledError:
        pass
    finally:
        await _shutdown_all(server, wirings)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
