from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import os
import pathlib
import signal
from collections.abc import Awaitable, Callable, Mapping

import can
import yaml

from lib.axis_sync import SyncGroup
from lib.can_manager import CANManager
from lib.config_schema import (
    MotorConfig,
    RobotConfig,
    SystemConfig,
    load_robot_config,
    load_system_config,
)
from lib.control.pid import PIDController
from lib.control.position_loop import M3508PositionLoop, make_position_pid
from lib.control.sync_monitor import SyncMonitor
from lib.control.target_refresh import GenericTargetRefresher
from lib.drivers.base import MotorDriver
from lib.drivers.edulite05 import Edulite05Driver
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import CURRENT_MAX, M3508Driver
from lib.manual import ManualController
from lib.match_state import ChecklistItem, load_checklist_definitions
from lib.sequence.engine import Sequence
from lib.sequence.motors import EStopChecker, MotorGroup, TargetSink, build_motor_group
from lib.sequence.positions import PositionTable, load_position_table
from lib.server import RobotServer

logger = logging.getLogger(__name__)

# ドライバ種別名 -> 実装クラス。名前の一覧は lib/config_schema.DRIVER_TYPES が持つ
_DRIVER_MAP: dict[str, type[MotorDriver]] = {
    "m3508": M3508Driver,
    "edulite05": Edulite05Driver,
    "generic": GenericDriver,
}

_CONFIG_DIR = pathlib.Path(__file__).resolve().parent / "config"
_DEFAULT_CONFIGS = ["main_hand.yaml", "sub_hand.yaml"]
_SYSTEM_CONFIG = "system.yaml"
_CHECKLIST_CONFIG = "checklist.yaml"
# 機構位置定数は robot config と同じディレクトリに <robot_name>_positions.yaml で置く
_POSITIONS_SUFFIX = "_positions.yaml"

# M3508 の PC 側位置制御 PID の既定値 (motors[name].pid が無い場合の補完値)。
# 入力は累積角 [deg]、出力は C620 の電流指令 [counts] (16384 counts ≒ 20A)。
#
# 機構が未完成でイナーシャも重力負荷も不明なため、ここでは「動かないより暴れない」を
# 優先した保守的な仮値を置いている。実機で要チューニング。
#   kp=2.0  : 100deg の偏差でも 200counts (≒0.24A) しか出ない。まず振動しない領域から始める
#   ki=0.0  : 積分は重力補償が必要と分かってから足す。機構端に当たった状態で育つと危険
#   kd=0.0  : ノイズを増幅するため、kp を上げて振動が出てから初めて入れる
#   dead_band=1.0 : M3508 の減速比 (19:1) を考えると出力軸で 0.05deg 相当。唸り防止
#   output_limit=2000 : 電流上限 ≒2.4A。C620 フルスケール (20A) の約 12%。
#                       機構が確定するまで、暴走しても人力で押さえられる領域に留める
_DEFAULT_PID: dict[str, float | None] = {
    "kp": 2.0,
    "ki": 0.0,
    "kd": 0.0,
    "integral_limit": None,
    "dead_band": 1.0,
    "output_limit": 2000.0,
}


#: 開発用コマンドを解禁する環境変数。systemd unit や shell の export で渡せるように、
#: CLI 引数と同じフラグをもう 1 経路用意してある (CLI 側が優先)。
_DEV_TOOLS_ENV = "CBC_DEV_TOOLS"
#: 真と見なす値。"0"/"false"/空文字を真にしないためにホワイトリストで判定する
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
    return parser.parse_args()


def _load_config(path: pathlib.Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _load_all_configs(
    system_path: pathlib.Path, config_paths: list[pathlib.Path]
) -> tuple[SystemConfig, list[tuple[pathlib.Path, RobotConfig]]]:
    """共通設定と各ロボット設定を検証して読み込む。誤記があれば起動を拒否する。

    検証に落ちた設定で起動を続けないのは、control_type の誤記のような
    「指令の種類そのものが変わる」誤りを警告ログでは止められないため
    (duty 0.3 のつもりの値が position 0.3deg としてファームに受理される)。
    SystemExit で抜けるのは、会場で読むのが操縦者であり、traceback より
    1 行のメッセージのほうが直せるため。
    """
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


def _collect_per_motor_overrides(
    robots: list[RobotConfig],
) -> dict[str, dict[str, float]]:
    """各 config の motors[name].motor_check を集約してフラットな辞書に変換する。

    返り値の例:
        {
            "lift_motor": {"magnitude": 800.0, "timeout_ms": 2000.0},
            "gripper": {"timeout_ms": 2500.0},
        }

    モータ名衝突は実機構成では起きない想定だが、もし発生した場合は後勝ちとなる。
    """
    overrides: dict[str, dict[str, float]] = {}
    for robot in robots:
        for motor_name, motor in robot.motors.items():
            entry = motor.motor_check.as_dict()
            if entry:
                overrides[motor_name] = entry
    return overrides


def _load_checklist_definitions(path: pathlib.Path) -> dict[str, list[ChecklistItem]]:
    """チェックリスト yaml を読み込む。存在しなければ空定義で起動する。

    項目ゼロのロールは「常に完了」とみなされるため、yaml が無くても試合には
    入れる。逆に yaml があれば全項目のチェックが試合開始の前提条件になる。
    """
    if not path.exists():
        logger.warning("チェックリスト設定が見つかりません: %s (項目なしで起動)", path)
        return load_checklist_definitions({})
    return load_checklist_definitions(_load_config(path) or {})


def _positions_path(config_path: pathlib.Path, robot_name: str) -> pathlib.Path:
    """robot config と同じディレクトリの位置定数 yaml のパスを返す。"""
    return config_path.with_name(f"{robot_name}{_POSITIONS_SUFFIX}")


def _load_position_table_file(path: pathlib.Path) -> PositionTable:
    """位置定数 yaml を読み込む。読めなければ空表で起動する。

    起動自体は通す (モータ動作確認やヘルス監視は定数なしでもやりたい) が、
    定数が壊れているときに推測で動かすと機構を壊すため、空表のまま先へ進めて
    シーケンスが値を引いた時点で明示的に失敗させる。
    """
    if not path.exists():
        logger.warning("位置定数ファイルが見つかりません: %s (定数なしで起動)", path)
        return PositionTable.empty(source=str(path))
    try:
        return load_position_table(_load_config(path) or {}, source=str(path))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        logger.error("位置定数ファイルを読み込めません: %s (%s) — 定数なしで起動", path, exc)
        return PositionTable.empty(source=str(path))


def _create_bus(channel: str, *, dry_run: bool) -> can.Bus:
    if dry_run:
        return can.Bus(interface="virtual", channel=channel)
    return can.Bus(interface="socketcan", channel=channel)


def _create_motor(motor: MotorConfig) -> MotorDriver:
    """検証済み設定からモータを生成する。

    未対応のドライバ種別や control_type の誤記は lib/config_schema が起動時に弾くため、
    ここには常に生成可能な設定しか来ない。
    """
    if motor.driver == "edulite05":
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

    if motor.driver == "generic":
        # control_type を渡さないと duty 指令の DC モータが位置制御で生成され、
        # 動作確認も reset も config と別物の指令になる
        return GenericDriver(
            name=motor.name,
            can_id=motor.can_id,
            control_type=motor.control_type,
        )

    return _DRIVER_MAP[motor.driver](name=motor.name, can_id=motor.can_id)


def _setup_robot(
    robot: RobotConfig, can_buses: Mapping[str, str], *, dry_run: bool
) -> tuple[CANManager, dict[str, MotorDriver]]:
    """検証済み設定から CANManager とモータ群をセットアップする。"""
    can_manager = CANManager()

    for bus_name, channel in can_buses.items():
        can_manager.add_bus(bus_name, _create_bus(channel, dry_run=dry_run))

    motors: dict[str, MotorDriver] = {}
    for motor_name, motor_cfg in robot.motors.items():
        motor = _create_motor(motor_cfg)
        can_manager.add_motor(motor_cfg.bus, motor)
        motors[motor_name] = motor

    # センサは motors には入れない (仕様書 §5.2)。受信の振り分けとヘルス監視だけを
    # 登録し、動作確認・目標値再送・UI のモータ一覧には並べない。
    # **登録を忘れると受信ループがそのフレームを誰にも配らず、接触が PC まで届かない。**
    for sensor_cfg in robot.sensors.values():
        can_manager.add_sensor(sensor_cfg.bus, GenericDriver(sensor_cfg.name, sensor_cfg.can_id))

    return can_manager, motors


def _load_pid_config(
    motor_name: str, pid_cfg: Mapping[str, object] | None
) -> dict[str, float | None]:
    """motors[name].pid を読み、未指定キーを _DEFAULT_PID で補完する。

    pid セクションが無い M3508 は既定ゲインで動かす (エラーにしない)。
    起動できないと動作確認そのものができず、機構調整中の実機で困るため。
    既定値は安全側に振ってあるので、無指定でも暴れない。
    キー名の誤記は lib/config_schema が起動時に弾く (書いても効かないゲインを作らない)。
    """
    result: dict[str, float | None] = dict(_DEFAULT_PID)
    if not isinstance(pid_cfg, Mapping):
        return result

    for key, value in pid_cfg.items():
        if key not in _DEFAULT_PID:
            logger.warning("未知の pid キー: motors.%s.pid.%s (無視)", motor_name, key)
            continue
        if value is None and key == "integral_limit":
            # integral_limit の null は「制限なし」という正当な指定
            result[key] = None
            continue
        if value is None:
            # 他のキーの null は書きかけの yaml とみなし、既定値のまま使う
            logger.warning(
                "motors.%s.pid.%s が null です。既定値 %s を使います。",
                motor_name,
                key,
                _DEFAULT_PID[key],
            )
            continue
        try:
            result[key] = float(value)
        except (TypeError, ValueError):
            logger.warning(
                "motors.%s.pid.%s が数値ではありません: %r。既定値 %s を使います。",
                motor_name,
                key,
                value,
                _DEFAULT_PID[key],
            )
    return result


def _build_position_pid(motor: MotorConfig) -> PIDController:
    """M3508 1 台分の位置制御 PID を config から組み立てる。"""
    params = _load_pid_config(motor.name, motor.pid)
    pid = make_position_pid(
        params["kp"],
        params["ki"],
        params["kd"],
        integral_limit=params["integral_limit"],
        dead_band=params["dead_band"],
    )

    # make_position_pid の出力レンジは C620 のフルスケール (±16384 = ±20A)。
    # 機構が確定するまでフルトルクを許すと、暴走時に人力で止められず機構を壊すため、
    # config の output_limit まで絞り込む。ハード上限は決して超えない。
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
    """config 中の M3508 をバス単位でまとめた位置制御ループ群を作る。

    バス単位で 1 ループにする理由: C620 の電流指令フレーム (0x200) は 1 通に
    4 モータ分のスロットを持つ。モータごとに送ると他モータのスロットを 0 で
    上書きしてしまうため、同一バス上の M3508 は必ず 1 ループが束ねる。
    """
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
                # 緊急停止インターロック: 実行中ステップが出した目標を破棄し電流 0 に落とす
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
    """シーケンスにモータアクセス層を注入し、必要な位置制御ループを返す。"""
    loops = _build_position_loops(
        robot,
        can_manager,
        motors,
        feedback_timeout_ms=feedback_timeout_ms,
        is_estop_active=is_estop_active,
    )

    # M3508 は電流指令しか受け付けないため、目標値は PC 側 PID ループへ迂回させる
    target_sinks: dict[str, TargetSink] = {}
    for loop in loops.values():
        target_sinks.update(loop.target_sinks())

    sequence.bind_motors(
        build_motor_group(
            can_manager,
            motors,
            # 緊急停止インターロック: 停止中はシーケンスからの指令自体を拒否する
            is_estop_active=is_estop_active,
            target_sinks=target_sinks,
        )
    )
    return list(loops.values())


def _build_target_refresher(
    group: MotorGroup,
    motors: dict[str, MotorDriver],
    *,
    is_estop_active: EStopChecker,
) -> GenericTargetRefresher | None:
    """自作モタドラのモータだけを集めた目標値再送タスクを作る (居なければ None)。

    自作モタドラのファームは 500ms 自分宛の SET_TARGET が来ないと出力を止める
    (docs/motor_driver_can_protocol.md §5.1)。PC 側は目標値が変わったときにしか
    送らないため、再送が無いとコンベアは回し始めて 500ms で止まる。
    M3508 は位置制御ループが 200Hz で電流指令を送り続けるので対象外、EDULITE は
    ドライバ内蔵の位置ループが目標を保持するので対象外。
    """
    handles = [group[name] for name, driver in motors.items() if isinstance(driver, GenericDriver)]
    if not handles:
        return None
    return GenericTargetRefresher(handles, is_estop_active=is_estop_active)


def _build_manual_controller(sequence: Sequence, positions: PositionTable) -> ManualController:
    """手動操縦の指令口を組み立てる。

    ``MotorGroup`` はシーケンスへ bind したものをそのまま共有する。手動用に別の
    グループを組むと、緊急停止インターロック・M3508 の PID 迂回・自作モタドラの
    再送対象がシーケンス側と 2 セットに分かれ、片方だけ配線を落としても起動できて
    しまう (落ちた側から出した指令だけが停止中も通る、といった形で現れる)。
    """
    return ManualController(sequence.motors, positions)


def _build_sync_groups(positions: PositionTable, motors: dict[str, MotorDriver]) -> list[SyncGroup]:
    """位置定数のペア軸のうち、このロボットに実在するものだけを監視対象にする。

    グループ自体は ``AxisSpec.sync_group`` が返す (単位換算はモータ定義をそのまま
    使うため、ここで詰め替えない)。この関数の責務は実在確認だけ。
    """
    groups: list[SyncGroup] = []
    for axis_name in positions.paired_axes():
        spec = positions.axis(axis_name)
        missing = [motor.name for motor in spec.motors if motor.name not in motors]
        if missing:
            # 黙って飛ばすと「監視しているつもり」で機構破損に至るため必ず残す
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
    """全メンバが同一の位置制御ループに載るグループだけをループへ登録する。

    ループ側の保護 (偏差超過で即電流 0・途絶をペア単位で判定) は、そのループが
    両方のモータの電流を握っている場合にしか成立しない。EDULITE のペアや
    バスをまたぐペアは SyncMonitor による全体緊急停止だけで守る。
    """
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


def _make_sync_violation_handler(
    server: RobotServer,
    robot_name: str,
    positions: PositionTable,
    tasks: set[asyncio.Task[None]],
) -> Callable[[str, float], None]:
    """同期ずれの検出を全体緊急停止に接続するハンドラを作る。"""

    def on_violation(axis_name: str, deviation: float) -> None:
        spec = positions.axis(axis_name)
        unit = spec.unit
        tolerance = float(spec.sync_tolerance or 0.0)
        reason = (
            f"{robot_name} の {axis_name} の左右ずれ {deviation:.3f}{unit} が"
            f" 許容 {tolerance:.3f}{unit} を超えました"
        )
        # SyncMonitor のコールバックは同期関数なので停止処理をタスクへ逃がす。
        # 参照を保持しないと GC でタスクが消え、緊急停止が発火しないことがある
        task = asyncio.create_task(server.activate_e_stop(reason=reason))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    return on_violation


def _load_sequence(robot_name: str) -> Sequence | None:
    """robots/<robot_name>.py からシーケンスクラスを動的にロードする。"""
    module_name = f"robots.{robot_name}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        logger.info("シーケンスモジュール %s が見つかりません。ダミーを使用します。", module_name)
        return None

    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if isinstance(attr, type) and issubclass(attr, Sequence) and attr is not Sequence:
            return attr(robot_name)

    logger.warning("モジュール %s に Sequence サブクラスが見つかりません。", module_name)
    return None


class _PlaceholderSequence(Sequence):
    """シーケンスが未実装のロボット用プレースホルダー。"""

    pass


def _install_stop_signal_handler() -> None:
    """systemd の停止 (SIGTERM) を SIGINT と同じ後始末経路へ合流させる。

    既定の SIGTERM はプロセスを即死させるため、`main()` の `finally` に並べた
    後始末 (位置制御ループ停止 → 目標値再送停止 → 同期監視停止 → CAN shutdown)
    が 1 段も走らない。`systemctl stop` / `restart` のたびに規定の停止経路を
    外れることになるので、main タスクの cancel へ変換して既存の経路に載せる。
    """
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    if task is None:  # asyncio.run 配下では起こらない
        return

    stopping = False

    def _request_stop() -> None:
        # 2 通目以降はハンドラを残したまま無視する。後始末の最中に再 cancel が
        # 入ると `_shutdown_step` が CancelledError を再送出して以降の手順が飛び、
        # CAN を開いたままプロセスが落ちる (systemctl restart の連打で起きる)。
        # remove_signal_handler では外してはならない —— SIGTERM の扱いが SIG_DFL に
        # 戻り、2 通目が「無視」ではなく「即死」になって後始末ごと消える
        nonlocal stopping
        if stopping:
            logger.warning("後始末の実行中です。停止シグナルを無視します")
            return
        stopping = True
        logger.info("SIGTERM を受信しました。後始末を開始します")
        task.cancel()

    loop.add_signal_handler(signal.SIGTERM, _request_stop)


async def _shutdown_step(label: str, awaitable: Awaitable[None]) -> None:
    """終了処理の 1 手順を実行する。失敗しても残りの手順へ進む。

    後始末は「全ループを止める → 全 CAN を落とす」の順に並んでおり、途中の 1 つが
    例外を投げた時点で以降が丸ごと飛ぶと、2 台目のロボットのバスが開いたまま
    残る。止める処理が止まる形は安全側ではないので、失敗は記録して先へ進める。
    """
    try:
        await awaitable
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("終了処理に失敗しました (%s)。残りの後始末は続行します", label)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # CAN を開くより前に登録する。config 読み込み中に停止されても経路を揃えるため
    _install_stop_signal_handler()

    args = _parse_args()

    if args.config:
        config_paths = [pathlib.Path(p) for p in args.config]
    else:
        config_paths = [_CONFIG_DIR / name for name in _DEFAULT_CONFIGS]
    system_path = pathlib.Path(args.system) if args.system else _CONFIG_DIR / _SYSTEM_CONFIG

    # 1 パス目: yaml をすべて読み込んで検証する。RobotServer 生成時にしきい値を渡す
    # 必要があるため、ロボット登録より先に全 config を確定させる。
    system, loaded = _load_all_configs(system_path, config_paths)
    health = system.health
    logger.info("health しきい値: %s", health)

    motor_check_overrides = _collect_per_motor_overrides([robot for _, robot in loaded])
    logger.info(
        "motor_check 設定: per_motor_timeout_ms=%s default_magnitude=%s overrides=%s",
        system.motor_check.per_motor_timeout_ms,
        dict(system.motor_check.default_magnitude),
        motor_check_overrides,
    )

    # 試合時間は当日ルールで変わりうる。起動ログに出しておくと試合前点検で確認できる
    logger.info("試合時間: %s 秒", system.match.duration_s)

    # CLI 引数が優先。どちらか一方でも立っていれば解禁する
    dev_tools = args.dev_tools or _env_flag(_DEV_TOOLS_ENV)
    if dev_tools:
        # 試合当日に開発用フラグのまま起動していないかを、起動ログだけで判断できるようにする
        logger.warning("開発用コマンドが有効です (指差喚呼の一括チェック等)。試合運用では外すこと")

    checklist_path = (
        pathlib.Path(args.checklist) if args.checklist else _CONFIG_DIR / _CHECKLIST_CONFIG
    )
    checklist_definitions = _load_checklist_definitions(checklist_path)
    logger.info(
        "チェックリスト項目数: %s",
        {role: len(items) for role, items in checklist_definitions.items()},
    )

    server = RobotServer(
        host=args.host,
        port=args.port,
        health=health,
        motor_check_per_motor_timeout_ms=system.motor_check.per_motor_timeout_ms,
        motor_check_default_magnitude=dict(system.motor_check.default_magnitude),
        motor_check_per_motor_overrides=motor_check_overrides,
        checklist_definitions=checklist_definitions,
        match_settings=system.match,
        dry_run=args.dry_run,
        dev_tools=dev_tools,
    )
    can_managers: list[CANManager] = []
    position_loops: list[M3508PositionLoop] = []
    sync_monitors: list[SyncMonitor] = []
    target_refreshers: list[GenericTargetRefresher] = []
    # 同期ずれから起動した緊急停止タスクの強参照置き場 (GC で消えると停止しない)
    e_stop_tasks: set[asyncio.Task[None]] = set()

    # モータ指令経路に渡す緊急停止インターロック。server の状態を遅延参照するため
    # クロージャにしている (server は add_robot より先に生成済み)
    def is_estop_active() -> bool:
        return server.e_stop_active

    # 2 パス目: 既存の robot 登録ロジック
    for config_path, robot in loaded:
        robot_name = robot.robot_name
        can_manager, motors = _setup_robot(robot, system.can_buses, dry_run=args.dry_run)
        can_managers.append(can_manager)

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
            feedback_timeout_ms=health.feedback_timeout_ms,
            is_estop_active=is_estop_active,
        )
        position_loops.extend(loops)

        # 自作モタドラはコマンドウォッチドッグを持つため、目標値を定期再送しないと
        # 500ms で出力が止まる (docs/motor_driver_can_protocol.md §5.1)
        refresher = _build_target_refresher(
            seq.motors,
            motors,
            is_estop_active=is_estop_active,
        )
        robot_refreshers = [refresher] if refresher is not None else []
        target_refreshers.extend(robot_refreshers)

        # 同期監視はシーケンスから独立した常駐監視。動作確認中・待機中のずれも拾う
        sync_groups = _build_sync_groups(positions, motors)
        _attach_sync_groups(sync_groups, loops)
        robot_monitors: list[SyncMonitor] = []
        if sync_groups:
            robot_monitors.append(
                SyncMonitor(
                    sync_groups,
                    motors,
                    last_feedback_at=can_manager.last_feedback_at,
                    feedback_timeout_ms=health.feedback_timeout_ms,
                    on_violation=_make_sync_violation_handler(
                        server, robot_name, positions, e_stop_tasks
                    ),
                )
            )
            sync_monitors.extend(robot_monitors)

        # 監視をサーバーへ渡さないと、緊急停止解除でラッチを外す経路が存在せず、
        # 一度ずれを検知した軸は再起動するまで無監視・不動のまま残る
        # 手動操縦 (調整時・緊急時の補助操縦)。シーケンスと同じ MotorGroup を共有する
        manual = _build_manual_controller(seq, positions)

        server.add_robot(
            robot_name,
            seq,
            can_manager,
            position_loops=loops,
            sync_monitors=robot_monitors,
            target_refreshers=robot_refreshers,
            manual=manual,
        )
        logger.info(
            "ロボット登録: %s (モータ: %d 台, 位置制御ループ: %s, 位置定数軸: %s, "
            "同期監視: %s, 目標値再送: %s, 手動連続操作: %s)",
            robot_name,
            len(motors),
            [loop.bus_name for loop in loops] or "なし",
            list(positions.axes) or "なし",
            [group.name for group in sync_groups] or "なし",
            list(refresher.motor_names) if refresher is not None else "なし",
            list(positions.manual_axes()) or "なし",
        )

    try:
        for mgr in can_managers:
            await mgr.run()
        # 受信ループ起動後に始める。フィードバック未受信のまま PID を回すと
        # 途絶判定で電流 0 に落ちるだけだが、無駄な警告ログを避ける
        for loop in position_loops:
            loop.start()
        for monitor in sync_monitors:
            monitor.start()
        for refresher in target_refreshers:
            refresher.start()
        await server.start()
    except asyncio.CancelledError:
        pass
    finally:
        # 例外・キャンセルのどちらで抜けてもここを通す。ループが生き残ると
        # 電流指令が出続けるため、CAN シャットダウンより先に必ず止める
        for loop in position_loops:
            await _shutdown_step(f"位置制御ループ (bus={loop.bus_name})", loop.stop())
        # 再送を止めればファーム側のウォッチドッグが 500ms 以内に出力を落とす。
        # 停止指令をここから送らないのは、PC が落ちた場合と経路を 1 本に保つため
        for refresher in target_refreshers:
            await _shutdown_step("目標値再送", refresher.stop())
        # 監視だけが生き残ると、停止済みのモータのフィードバックを見て誤発報する
        for monitor in sync_monitors:
            await _shutdown_step("同期監視", monitor.stop())
        for mgr in can_managers:
            await _shutdown_step("CAN シャットダウン", mgr.shutdown())
        await _shutdown_step("サーバー終了処理", server.cleanup())
        # 完走したことを journal に残す。この行が無いまま終了していれば、
        # 後始末の途中で SIGKILL された (TimeoutStopSec 超過) と判別できる
        logger.info("後始末完了")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("終了")
