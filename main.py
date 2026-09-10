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
from lib.bus_lock import BusClaim, BusClaimedError, claim_bus
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
from lib.control.limit_monitor import LimitMonitor
from lib.control.pid import PIDController
from lib.control.position_loop import M3508PositionLoop, make_position_pid
from lib.control.sync_monitor import SyncMonitor
from lib.control.target_refresh import (
    GenericTargetRefresher,
    QueryDrivenTargetRefresher,
    TargetRefresher,
    hold_target_refresh,
)
from lib.control.trajectory import TrapezoidalProfile
from lib.drivers.base import MotorDriver
from lib.drivers.dm3520 import Dm3520Driver
from lib.drivers.edulite05 import Edulite05Driver
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import CURRENT_MAX, M3508Driver
from lib.logging_setup import configure_logging
from lib.manual import ManualController
from lib.match_state import ChecklistItem, Court, load_checklist_definitions
from lib.motion_guard import AxisStateReader, PressedTowardReader, SensorSuspension
from lib.sequence.engine import (
    NO_LIMIT_INTERVENTION,
    LimitIntervention,
    LimitInterventions,
    Sequence,
)
from lib.sequence.homing import HomingError, HomingRunner, homing_axis_names
from lib.sequence.motors import (
    EStopChecker,
    MotorGroup,
    TargetSink,
    build_axis_state_reader,
    build_motor_group,
)
from lib.sequence.positions import PositionTable, load_position_table
from lib.server import RobotServer
from lib.server_homing import HomingSource
from lib.suction import suction_of
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
    target_refreshers: list[TargetRefresher] | None = None,
    limit_monitors: list[LimitMonitor] | None = None,
    is_estop_active: EStopChecker,
) -> Callable[[str], Callable[[], Awaitable[None]] | None]:
    """軸名 → その軸の原点を確定する操作。手段が無ければ None を返す解決器。

    「確定できるか」と「確定する」を同じ解決器から出すのは、探索を始める前に
    可否を問えるようにするため。可否はドライバ自身の `supports_origin_capture()`
    が答える (`main.py` にドライバ種別を書き写さない)。

    `is_estop_active` に既定値を置かないのは、渡し忘れが「緊急停止を見ない
    零点確定」として黙って通るため。

    確定した後に可動端監視へ付け替えを伝える (`LimitMonitor.origin_replaced`)。
    伝えないと、前の座標で覚えた接触位置が付け替え後の実測と比べられ、スイッチに
    載ったままの端が退避の向きで「当たった」と覚えられる。
    """
    managers = can_managers or []
    monitors = sync_monitors or []
    refreshers = target_refreshers or []
    guards = limit_monitors or []

    def _resolve_via_driver(axis: str) -> Callable[[], Awaitable[None]] | None:
        names = table.axis(axis).motor_names
        for manager in managers:
            drivers = [manager.motors.get(name) for name in names]
            if any(driver is None for driver in drivers):
                continue
            present = [driver for driver in drivers if driver]
            if not all(driver.supports_origin_capture() for driver in present):
                return None
            # 原点の持ち主はドライバが答える (`main.py` にドライバ種別を書き写さない)。
            owners = [driver.has_local_origin() for driver in present]
            if any(owners) and not all(owners):
                # 混ざったまま片方の経路へ流すと、もう片方は原点が動かないまま
                # 「成功した」と返る (黙って誤った原点ができる)。
                logger.error(
                    "軸 '%s' は原点の持ち方が違うモータが混ざっているので零点確定できません", axis
                )
                return None
            local = all(owners)

            async def capture(manager: CANManager = manager, local: bool = local) -> None:
                # **窓が短くなっても両方要る。** 再送は付け替えの瞬間に旧座標系の
                # 論理値をキャッシュしていると、新しいオフセットで `encode_target` を
                # 通った瞬間に差のぶん機構が走る。偏差監視は、`HomingRunner._align`
                # が左右を意図的にずらした直後の姿勢を控えるので直前まで立っている。
                with _suspend_sync_monitoring(monitors, axis):
                    async with hold_target_refresh(refreshers, names, reason="原点の付け替え"):
                        if local:
                            await manager.capture_origin_in_place(
                                names, should_abort=is_estop_active
                            )
                        else:
                            await manager.capture_origin_via_set_zero(
                                names, should_abort=is_estop_active
                            )

            return capture
        return None

    def _resolve_capture(axis: str) -> Callable[[], Awaitable[None]] | None:
        spec = table.axis(axis)
        for loop in loops:
            if axis in loop.sync_group_names:
                return _as_async(functools.partial(loop.set_group_origin_here, axis))
            for motor in spec.motor_names:
                if motor in loop.motor_names:
                    return _as_async(functools.partial(loop.set_origin_here, motor))
        return _resolve_via_driver(axis)

    def resolve(axis: str) -> Callable[[], Awaitable[None]] | None:
        capture = _resolve_capture(axis)
        if capture is None:
            return None

        async def run() -> None:
            await capture()
            for guard in guards:
                guard.origin_replaced(axis)

        return run

    return resolve


def _as_async(capture: Callable[[], None]) -> Callable[[], Awaitable[None]]:

    async def run() -> None:
        capture()

    return run


def _wire_motor_check_sequence(
    server: RobotServer,
    groups: list[MotorGroup],
    tables: Mapping[str, PositionTable],
    *,
    loops: list[M3508PositionLoop],
    can_managers: list[CANManager],
    sync_monitors: list[SyncMonitor],
    limit_monitors: list[LimitMonitor],
    target_refreshers: list[TargetRefresher],
    feedback_timeout_ms: float,
    is_estop_active: EStopChecker,
    sensor_suspension: SensorSuspension,
) -> None:
    if not tables:
        logger.info("統合動作確認: 位置定数が 1 つも無いため登録しない")
        return

    merged = PositionTable.merged(list(tables.values()))

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

    # 統合動作確認も同じ歯止めを通す。センサ名はロボット横断に一意なので、
    # 全 CANManager を横断した 1 つの読み口で両ハンドぶんに答えられる。
    # **零点確定もこの同じ読み口を使う** —— 可動端インターロックと零点確定で
    # 別々に組むと、片方だけが `None` (読めていない) を `False` へ丸めた状態が作れる
    # **零点確定には生の読み口を、歯止めには覆いを掛けた口を渡す。** 覆いは整列段の
    # あいだ「押されていない」と答えるので、零点確定自身がそれを読むと自分の目を塞ぐ
    # (探索も離脱も「今 ON か」で進む)
    sensor_read = _make_sensor_reader(can_managers, feedback_timeout_ms=feedback_timeout_ms)
    motors = MotorGroup(sensor_active=sensor_suspension.wrap(sensor_read))
    for group in groups:
        for handle in group.handles:
            motors.add(handle)
    # **軸間干渉の読み口はここにも要る。** ロボットごとの束とは別の束なので、
    # `_wire_robot_motors` の配線は引き継がれない —— 忘れると毎セッション回す
    # 動作確認と零点確定だけが「条件の軸が読めていない」で全軸拒否になる
    motors.bind_axis_state(
        _make_axis_state_reader(
            merged,
            motors,
            court=lambda: sequence.court,
            managers=can_managers,
            feedback_timeout_ms=feedback_timeout_ms,
        )
    )

    # 当たった向きの記憶もこの束へ配線する。忘れると、宣言と食い違う端からの退避を
    # 監視は通すのに動作確認と零点確定の入口だけが宣言された側として拒む
    motors.bind_pressed_toward(_make_pressed_toward_reader(limit_monitors))

    sequence.bind_motors(motors)
    sequence.bind_positions(merged)
    # 動作確認も `move_to` で駆動するので、保護に曲げられた移動はここでも失敗させる
    # (黙って進むと、軸が途中に居るまま全ステップ PASSED になる)
    sequence.bind_limit_interventions(_make_limit_interventions(limit_monitors))

    homing_axes = homing_axis_names(merged)
    if homing_axes:
        sensors = {name: sensor for mgr in can_managers for name, sensor in mgr.sensors.items()}
        freshness = FeedbackFreshness(
            _merged_last_feedback_at(can_managers), timeout_ms=feedback_timeout_ms
        )

        def _sensor_is_stale(name: str) -> bool:
            if name not in sensors:
                logger.error("零点確定: センサ '%s' が config の sensors: に居ません", name)
                return True
            # **鮮度の正は可動端インターロックと同じ読み口 (None = 読めていない)。**
            # 別々に組むと、片方だけが途絶を見落とす状態が作れる
            return sensor_read(name) is None

        def _motor_is_energized(name: str) -> bool | None:
            # 三値をそのまま運ぶ。M3508 のように励磁を報告しないドライバは None で、
            # HomingRunner はそれを無励磁として扱わない
            for manager in can_managers:
                motor = manager.motors.get(name)
                if motor is not None:
                    return motor.is_energized()
            return None

        def _motor_is_stale(name: str) -> bool:
            return freshness.is_stale(name, freshness.now())

        resolve_origin = _make_origin_resolver(
            loops,
            merged,
            can_managers=can_managers,
            sync_monitors=sync_monitors,
            target_refreshers=target_refreshers,
            limit_monitors=limit_monitors,
            is_estop_active=is_estop_active,
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

        runner = HomingRunner(
            sensor_active=sensor_read,
            sensor_contact_count=_make_sensor_contact_reader(can_managers),
            sensor_is_stale=_sensor_is_stale,
            motor_is_stale=_motor_is_stale,
            motor_is_energized=_motor_is_energized,
            origin_capturable=_origin_capturable,
            capture_origin=_capture_origin,
            suspend_sensors=sensor_suspension.suspend,
        )
        sequence.bind_homing(runner)
        # 束ねると「サブハンドのつもりでメインハンドが動く」が作れる
        server.set_homing_source(
            HomingSource(
                runner=runner,
                table=merged,
                motors=motors,
                court=lambda: sequence.court,
                # 寄せる口は動作確認と同じもの。別に組むと、パネル経由だけが
                # 可動端保護や到達判定の外側に出る
                move_to=sequence.move_to,
                axes_by_robot={
                    robot: axes
                    for robot, table in tables.items()
                    if (axes := tuple(homing_axis_names(table)))
                },
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


def _make_sensor_reader(
    managers: list[CANManager], *, feedback_timeout_ms: float
) -> Callable[[str], bool | None]:
    """可動端インターロックが読むセンサ状態を組む。**三値を返す。**

    `True` = 押されている / `False` = 押されていない / **`None` = 読めていない**。
    最後の 1 つは「登録されていない」と「途絶している」の両方で返る。どちらも
    `False` へ丸めてはならない —— 丸めた瞬間に、**配線が抜けたセンサが
    「押されていない = 進んでよい」に化ける** (2026-09-09 の事故はスイッチが
    1 スロットずれていて PC へ届いていなかった)。安全側は「止まる」である。

    **零点確定 (`HomingRunner`) も同じ読み口を使う。** かつては bool を返す別の
    クロージャを持っていたが、同じ問い (「そのセンサは今どうなっているか」) に
    答える口が 2 つあると、片方だけが `None` を `False` へ丸めた状態が作れる ——
    零点確定側では「途絶したセンサが離脱完了に化ける」形で現れる。
    鮮度の判定 (`_sensor_is_stale`) もこの口の `None` から作る。
    """
    sensors = {name: sensor for mgr in managers for name, sensor in mgr.sensors.items()}
    freshness = FeedbackFreshness(
        _merged_last_feedback_at(managers), timeout_ms=feedback_timeout_ms
    )

    def sensor_active(name: str) -> bool | None:
        sensor = sensors.get(name)
        if sensor is None or freshness.is_stale(name, freshness.now()):
            return None
        return bool(getattr(sensor, "sensor_active", False))

    return sensor_active


def _make_axis_state_reader(
    positions: PositionTable,
    motors: MotorGroup,
    *,
    court: Callable[[], Court | None],
    managers: list[CANManager],
    feedback_timeout_ms: float,
) -> AxisStateReader:
    """軸間干渉の判定が読む「他の軸は今どこか」を組む。**鮮度を通す。**

    受信が途絶えた軸は `MotorState` の既定 0.0 を運び続けるので、鮮度を掛けないと
    **1 通も届いていない軸が「原点に居る」として条件を満たす**。ここが `None` を
    返せば歯止めは拒否側へ倒れ、`requires` を書いた軸が動かないという形で表に出る
    (`_make_sensor_reader` が三値の `None` を運ぶのと同じ約束)。

    **判定の中身は `lib/sequence/motors.py` が 1 つだけ持つ。** ここは鮮度の材料
    (どの `CANManager` から `last_feedback_at` を取るか) を合わせるだけで、
    書き写すと本番と動作確認で `None` の条件が食い違う。
    """
    freshness = FeedbackFreshness(
        _merged_last_feedback_at(managers), timeout_ms=feedback_timeout_ms
    )
    return build_axis_state_reader(
        positions,
        motors,
        court=court,
        is_stale=lambda name: freshness.is_stale(name, freshness.now()),
    )


def _make_sensor_contact_reader(managers: list[CANManager]) -> Callable[[str], int | None]:
    """接触 (OFF→ON) の累計の読み口。**カウンタを持たないドライバは `None`。**

    現在値へ落とすと、ON 区間が観測周期より狭い接触が黙って取りこぼされる。
    **零点確定と移動中の可動端監視が同じ読み口を使う** —— カウンタは読んでも
    減らないので、読み手が何人いても互いのぶんを消さない (基準値は読み手が控える)。
    """
    sensors = {name: sensor for mgr in managers for name, sensor in mgr.sensors.items()}

    def contact_count(name: str) -> int | None:
        sensor = sensors.get(name)
        if sensor is None:
            return None
        count = getattr(sensor, "sensor_contact_count", None)
        if not isinstance(count, int):
            return None
        return count

    return contact_count


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


def _claim_can_buses(
    can_buses: Mapping[str, str], robots: list[RobotConfig], *, dry_run: bool
) -> list[BusClaim]:
    """このプロセスが開く CAN バス全部の持ち主を、開く前にまとめて主張する。

    ロボット単位ではなくプロセス単位で 1 回にするのは、両ハンドが同じバス
    (`can_generic`) をそれぞれの `CANManager` で開くため。
    """
    if dry_run:
        return []
    used = {name for robot in robots for name in _robot_bus_names(robot, can_buses)}
    claims: list[BusClaim] = []
    for bus_name in can_buses:
        if bus_name not in used:
            continue
        channel = can_buses[bus_name]
        try:
            claims.append(claim_bus(channel))
        except BusClaimedError as exc:
            raise SystemExit(str(exc)) from exc
        except OSError as exc:
            # 主張できないことを理由に機体を動かせなくしない。検出が効いていないことだけを残す
            logger.error(
                "CAN バス '%s' の持ち主を主張できません (%s)。二重起動の検出が効いていません"
                " —— 同じバスへ別のプロセスが指令すると互いの目標を上書きし合い、"
                "機構の故障に見えます (起動は続けます)",
                channel,
                exc,
            )
    return claims


def _release_can_buses(claims: Iterable[BusClaim]) -> None:
    for claim in claims:
        try:
            claim.release()
        except Exception:
            logger.exception("CAN バスの持ち主の解除に失敗: channel=%s", claim.channel)


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
    positions: PositionTable,
    *,
    feedback_timeout_ms: float,
    is_estop_active: EStopChecker,
    sensor_active: Callable[[str], bool | None],
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

    group = build_motor_group(
        can_manager,
        motors,
        is_estop_active=is_estop_active,
        target_sinks=target_sinks,
        # 可動端インターロック: `guard:` を書いた軸が押されている端へ進むのを止める。
        # 配線しないと三値の `None` (読めていない) しか返らず、その軸は 1 歩も動けない
        sensor_active=sensor_active,
    )
    # 軸間干渉: `guard.requires` を書いた軸が、条件の軸を読めるようにする。読み口は
    # 束そのものを引くので**束ね終わってから**配線する。**配線先はここと
    # `_wire_motor_check_sequence` の 2 箇所**で、片方を忘れるとその経路だけが全軸拒否
    group.bind_axis_state(
        _make_axis_state_reader(
            positions,
            group,
            court=lambda: sequence.court,
            managers=[can_manager],
            feedback_timeout_ms=feedback_timeout_ms,
        )
    )
    sequence.bind_motors(group)
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


def _build_limit_monitors(
    positions: PositionTable,
    sequence: Sequence,
    *,
    sensor_active: Callable[[str], bool | None],
    sensor_contact_count: Callable[[str], int | None],
) -> list[LimitMonitor]:
    """移動中の可動端監視。`guard.limits` を書いた軸が 1 本も無ければ回さない。

    コートを毎周期問い直すのは、`scale` がコートで鏡になる軸では**進む向きの符号
    そのものが変わる**ため。起動時に固めると、試合中のコート切り替えで反対端の
    センサを見るようになる。
    """
    if not sequence.has_motors:
        return []
    monitor = LimitMonitor(
        positions,
        sequence.motors,
        sensor_active=sensor_active,
        sensor_contact_count=sensor_contact_count,
        court=lambda: sequence.court,
    )
    if not monitor.axis_names:
        return []
    return [monitor]


def _make_pressed_toward_reader(monitors: list[LimitMonitor]) -> PressedTowardReader:
    """指令の入口が読む「端センサが当たった向き」。監視の記憶をそのまま引く。

    入口と監視で別々に持つと、宣言と食い違う端で監視は退避を通すのに入口が拒む状態が
    作れる。センサ名はロボット横断に一意なので、最初に覚えていると答えた監視の値でよい。
    """

    def read(name: str) -> int | None:
        for monitor in monitors:
            direction = monitor.pressed_toward(name)
            if direction is not None:
                return direction
        return None

    return read


def _make_limit_interventions(monitors: list[LimitMonitor]) -> LimitInterventions:
    """`move_to` が「保護に曲げられた移動」を知る読み口。

    保護が目標を実測へ書き直すと到達判定は必ず成立するので、配線しないと**軸は
    途中に居るのにシーケンスだけが先へ進む**。軸名はロボット横断に一意なので、
    その軸を見ている監視 1 つが答える。
    """

    def interventions(axis: str) -> LimitIntervention:
        for monitor in monitors:
            if axis in monitor.axis_names:
                return monitor.intervention(axis)
        return NO_LIMIT_INTERVENTION

    return interventions


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


def _attach_travel_ranges(positions: PositionTable, motors: dict[str, MotorDriver]) -> None:
    """軸の機械的可動域をモータ座標へ直してドライバへ渡す。

    使うかどうかはドライバ契約の既定実装 (`MotorDriver.set_travel_range`) が決める。
    ここでドライバ種別を見ると、同じ判断が config とコードの 2 箇所に生える。
    """
    for axis_name in positions.axes:
        travel = positions.axis(axis_name).travel
        if travel is None:
            continue
        for motor in positions.axis(axis_name).motors:
            driver = motors.get(motor.name)
            if driver is None:
                logger.warning(
                    "可動域の受け渡しをスキップ: 軸 %s のモータ %s がこのロボットに存在しません",
                    axis_name,
                    motor.name,
                )
                continue
            # scale が負のモータでは軸の min/max が入れ替わる
            ends = (motor.to_command(travel.min_value), motor.to_command(travel.max_value))
            driver.set_travel_range(min(ends), max(ends))
            logger.debug(
                "可動域を配線: 軸 %s のモータ %s へ %.4f..%.4f (指令単位)",
                axis_name,
                motor.name,
                min(ends),
                max(ends),
            )


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
) -> Callable[[str, float, int], None]:

    def on_violation(axis_name: str, deviation: float, retrips: int = 1) -> None:
        spec = positions.axis(axis_name)
        unit = spec.unit
        tolerance = float(spec.sync_tolerance or 0.0)
        reason = (
            f"{robot_name} の {axis_name} の左右ずれ {deviation:.3f}{unit} が"
            f" 許容 {tolerance:.3f}{unit} を超えました"
        )
        # 解除するたび即停止するループは、機構のずれと左右の原点の食い違いの区別が
        # 画面から付かない。再発回数と次の一手だけが操縦者を原因へ導く手掛かりになる。
        # 改行は EStopOverlay の 1 要素で詰まるので 1 行に収める。
        if retrips >= 2:
            reason += (
                f"。解除しても {retrips} 回続けて再発しています"
                " —— 左右の原点が食い違っている可能性があります。零点合わせを実行してください"
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
    limit_monitors: list[LimitMonitor]
    target_refreshers: list[TargetRefresher]
    motor_group: MotorGroup | None


def _wire_one_robot(
    server: RobotServer,
    config_path: pathlib.Path,
    robot: RobotConfig,
    system: SystemConfig,
    *,
    dry_run: bool,
    is_estop_active: EStopChecker,
    e_stop_tasks: set[asyncio.Task[None]],
    sensor_suspension: SensorSuspension,
) -> _RobotWiring:
    robot_name = robot.robot_name
    can_manager, motors = _setup_robot(robot, system.can_buses, dry_run=dry_run)

    seq = _load_sequence(robot_name)
    if seq is None:
        seq = _PlaceholderSequence(robot_name)

    positions = _load_position_table_file(_positions_path(config_path, robot_name))
    seq.bind_positions(positions)

    # 歯止め (指令の入口と 50Hz 監視) が読む口は覆いを通す。零点確定の整列段が
    # 「まだ押されていない側だけを進める」あいだ、その軸の原点センサだけを外す
    sensor_read = sensor_suspension.wrap(
        _make_sensor_reader([can_manager], feedback_timeout_ms=system.health.feedback_timeout_ms)
    )
    loops = _wire_robot_motors(
        robot,
        can_manager,
        motors,
        seq,
        positions,
        feedback_timeout_ms=system.health.feedback_timeout_ms,
        is_estop_active=is_estop_active,
        sensor_active=sensor_read,
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
    _attach_travel_ranges(positions, motors)
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

    limit_monitors = _build_limit_monitors(
        positions,
        seq,
        sensor_active=sensor_read,
        # 接触の累計も同じ覆いを通す。現在値だけを覆うと、整列段のあいだに数えた接触が
        # その周期だけ「押されている」に化け、覆ったはずの軸の目標が実測へ書き直される
        sensor_contact_count=sensor_suspension.wrap_count(
            _make_sensor_contact_reader([can_manager])
        ),
    )
    seq.bind_limit_interventions(_make_limit_interventions(limit_monitors))
    # 配線先はここと `_wire_motor_check_sequence` の 2 箇所 (`bind_axis_state` と同じ)
    seq.motors.bind_pressed_toward(_make_pressed_toward_reader(limit_monitors))

    server.add_robot(
        robot_name,
        seq,
        can_manager,
        position_loops=loops,
        sync_monitors=monitors,
        limit_monitors=limit_monitors,
        target_refreshers=refreshers,
        manual=manual,
        suction=suction_of(seq),
    )
    logger.info(
        "ロボット登録: %s (モータ %d 台 / 軸 %d 本 / 位置制御ループ %s / 同期監視 %s"
        " / 可動端監視 %s)",
        robot_name,
        len(motors),
        len(positions.axes),
        ", ".join(loop.bus_name for loop in loops) or "なし",
        ", ".join(group.name for group in sync_groups) or "なし",
        ", ".join(a for m in limit_monitors for a in m.axis_names) or "なし",
    )
    logger.debug(
        "ロボット登録 %s の内訳: 位置定数軸 %s / 目標値再送 %s / 手動連続操作 %s",
        robot_name,
        ", ".join(positions.axes) or "なし",
        ", ".join(name for r in refreshers for name in r.motor_names) or "なし",
        ", ".join(positions.manual_axes()) or "なし",
    )

    return _RobotWiring(
        name=robot_name,
        sequence=seq,
        can_manager=can_manager,
        positions=positions,
        position_loops=loops,
        sync_monitors=monitors,
        limit_monitors=limit_monitors,
        target_refreshers=refreshers,
        motor_group=seq.motors if seq.has_motors else None,
    )


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
        for monitor in wiring.limit_monitors:
            monitor.start()
    for wiring in wirings:
        for refresher in wiring.target_refreshers:
            refresher.start()
    await server.start()


async def _shutdown_all(
    server: RobotServer, wirings: list[_RobotWiring], *, bus_claims: Iterable[BusClaim] = ()
) -> None:
    for wiring in wirings:
        for loop in wiring.position_loops:
            await _shutdown_step(f"位置制御ループ (bus={loop.bus_name})", loop.stop())
    for wiring in wirings:
        for refresher in wiring.target_refreshers:
            await _shutdown_step("目標値再送", refresher.stop())
    for wiring in wirings:
        for monitor in wiring.limit_monitors:
            await _shutdown_step("可動端監視", monitor.stop())
    for wiring in wirings:
        for monitor in wiring.sync_monitors:
            await _shutdown_step("同期監視", monitor.stop())
    for wiring in wirings:
        await _shutdown_step("CAN シャットダウン", wiring.can_manager.shutdown())
    _release_can_buses(bus_claims)
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
    bus_claims = _claim_can_buses(
        system.can_buses, [robot for _path, robot in loaded], dry_run=args.dry_run
    )

    server = _build_server(args, system)

    e_stop_tasks: set[asyncio.Task[None]] = set()

    def is_estop_active() -> bool:
        return server.e_stop_active

    # **1 つを両ハンドで共有する。** 零点確定は両ハンドを 1 本のシーケンスで
    # 走らせるので、覆いが機体ごとに別物だと整列段で外したつもりのセンサが
    # もう一方の読み口では押されたまま残る (症状はその軸だけ整列段で必ず失敗)
    sensor_suspension = SensorSuspension()

    wirings = [
        _wire_one_robot(
            server,
            config_path,
            robot,
            system,
            dry_run=args.dry_run,
            is_estop_active=is_estop_active,
            e_stop_tasks=e_stop_tasks,
            sensor_suspension=sensor_suspension,
        )
        for config_path, robot in loaded
    ]

    _wire_motor_check_sequence(
        server,
        [w.motor_group for w in wirings if w.motor_group is not None],
        {w.name: w.positions for w in wirings},
        loops=[loop for w in wirings for loop in w.position_loops],
        can_managers=[w.can_manager for w in wirings],
        sync_monitors=[monitor for w in wirings for monitor in w.sync_monitors],
        limit_monitors=[monitor for w in wirings for monitor in w.limit_monitors],
        target_refreshers=[r for w in wirings for r in w.target_refreshers],
        feedback_timeout_ms=system.health.feedback_timeout_ms,
        # 付け替えの窓で停止が入ると、停止の disable の後に enable が届いて励磁が残る。
        is_estop_active=is_estop_active,
        sensor_suspension=sensor_suspension,
    )

    try:
        await _start_all(server, wirings)
    except asyncio.CancelledError:
        pass
    finally:
        await _shutdown_all(server, wirings, bus_claims=bus_claims)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
