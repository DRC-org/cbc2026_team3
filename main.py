from __future__ import annotations

import argparse
import asyncio
import dataclasses
import functools
import importlib
import logging
import os
import pathlib
import signal
import socket
from collections.abc import Awaitable, Callable, Mapping
from types import ModuleType

import can
import yaml

from lib.axis_sync import SyncGroup
from lib.can_manager import CANManager
from lib.config_schema import (
    MotorConfig,
    RobotConfig,
    SystemConfig,
    TuningSettings,
    load_robot_config,
    load_system_config,
)
from lib.control.feedback import FeedbackFreshness
from lib.control.pid import PIDController
from lib.control.position_loop import CaptureSink, M3508PositionLoop, make_position_pid
from lib.control.sync_monitor import SyncMonitor
from lib.control.target_refresh import (
    GenericTargetRefresher,
    QueryDrivenTargetRefresher,
    TargetRefresher,
)
from lib.drivers.base import MotorDriver
from lib.drivers.dm3520 import Dm3520Driver
from lib.drivers.edulite05 import Edulite05Driver
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import CURRENT_MAX, M3508Driver
from lib.manual import ManualController
from lib.match_state import ChecklistItem, load_checklist_definitions
from lib.sequence.engine import Sequence
from lib.sequence.homing import HomingError, HomingRunner
from lib.sequence.motors import EStopChecker, MotorGroup, TargetSink, build_motor_group
from lib.sequence.positions import PositionTable, load_position_table
from lib.server import RobotServer
from robots.motor_check import REQUIRED_AXES, MotorCheckSequence

logger = logging.getLogger(__name__)

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


def _make_origin_resolver(
    loops: list[M3508PositionLoop],
    table: PositionTable,
) -> Callable[[str], Callable[[], None] | None]:
    """軸名 → その軸の原点を確定する操作。手段が無ければ None を返す解決器。

    **左右ペアはグループ単位でしか確定しない。** 別々の時刻に確定すると、その間に
    片方が動いたぶんだけ消えないオフセットが残り、正常な動作でも即座に偏差超過で
    止まる。判断は `M3508PositionLoop.set_group_origin_here` に委ねる。

    「確定できるか」と「確定する」を同じ解決器から出すのは、探索を始める前に
    可否を問えるようにするため。センサまで押し込んでから「確定できません」で
    降りると、機構を動かした意味が無いまま姿勢だけが変わる。

    **PC 側位置制御ループに載っていないモータ (EDULITE 05 / DM3520) は確定できない。**
    それらは原点をドライバ内部に持ち、切り直すには CAN フレーム (SET_ZERO) を
    送る経路が要る。手段が無いことを None として素直に返し、呼び出し側が
    起動ログと `HomingError` の両方で見せる。
    """

    def resolve(axis: str) -> Callable[[], None] | None:
        spec = table.axis(axis)
        for loop in loops:
            if axis in loop.sync_group_names:
                return functools.partial(loop.set_group_origin_here, axis)
            for motor in spec.motor_names:
                if motor in loop.motor_names:
                    return functools.partial(loop.set_origin_here, motor)
        return None

    return resolve


def _wire_motor_check_sequence(
    server: RobotServer,
    groups: list[MotorGroup],
    tables: list[PositionTable],
    *,
    loops: list[M3508PositionLoop],
    can_managers: list[CANManager],
    feedback_timeout_ms: float,
) -> None:
    """統合動作確認シーケンスを組み立ててサーバーへ登録する。

    **必要な軸が揃っていない構成では登録しない。** 机上ベンチ (config/bench/*) は
    本番の機構を持たないので、登録すると押した瞬間に `PositionLookupError` で
    止まる。未登録なら動作確認は「シーケンスが読み込まれていません」として
    拒否されるだけで、UI もその理由を表示できる。

    軸名の衝突 (`PositionTable.merged`) はここで起動ごと落とす。動作確認が意図した
    側とは別の機体の軸へ指令を飛ばす構成を、黙って起動させてはならない。
    """
    if not tables:
        logger.info("統合動作確認: 位置定数が 1 つも無いため登録しない")
        return

    merged = PositionTable.merged(tables)

    missing = REQUIRED_AXES - set(merged.axes)
    if missing:
        logger.warning("統合動作確認: 必要な軸が足りないため登録しない (不足: %s)", sorted(missing))
        return

    motors = MotorGroup()
    for group in groups:
        for handle in group.handles:
            motors.add(handle)

    sequence = MotorCheckSequence()
    sequence.bind_motors(motors)
    sequence.bind_positions(merged)

    homing_axes = [name for name in merged.axes if merged.axis(name).homing is not None]
    if homing_axes:
        # センサはロボットをまたいで一意なので、全 CANManager から引ける形にする
        sensors = {name: sensor for mgr in can_managers for name, sensor in mgr.sensors.items()}
        freshness = FeedbackFreshness(
            _merged_last_feedback_at(can_managers), timeout_ms=feedback_timeout_ms
        )

        def _sensor_active(name: str) -> bool:
            sensor = sensors.get(name)
            # 未登録のセンサは「触れていない」ではなく途絶として扱わせる
            # (下の _sensor_is_stale が True を返すので 1 歩も動かさない)
            return sensor is not None and bool(getattr(sensor, "sensor_active", False))

        def _sensor_is_stale(name: str) -> bool:
            if name not in sensors:
                logger.error("零点確定: センサ '%s' が config の sensors: に居ません", name)
                return True
            return freshness.is_stale(name, freshness.now())

        def _motor_is_stale(name: str) -> bool:
            # 対象軸の実測位置が読めるかを、探索を始める前に問う。未受信の 0.0 を
            # 現在位置と信じると、1 歩目が原点近傍への 1 回のジャンプになり、
            # その移動は search_distance の歯止めに 1mm も掛からない
            return freshness.is_stale(name, freshness.now())

        resolve_origin = _make_origin_resolver(loops, merged)

        def _origin_capturable(axis: str) -> bool:
            return resolve_origin(axis) is not None

        def _capture_origin(axis: str) -> None:
            capture = resolve_origin(axis)
            if capture is None:
                raise HomingError(
                    f"軸 '{axis}' の原点を確定できません"
                    " (PC 側位置制御ループに載っていないモータでは零点確定を実行できない)"
                )
            capture()

        unsupported = [name for name in homing_axes if not _origin_capturable(name)]
        if unsupported:
            # 起動ログに出す。押した瞬間に失敗する構成のまま試合当日を迎えないため
            logger.error(
                "零点確定: 軸 %s は原点を確定する手段がありません"
                " (PC 側位置制御ループに載っていないモータ。動作確認はこの軸で失敗する)",
                unsupported,
            )

        sequence.bind_homing(
            HomingRunner(
                sensor_active=_sensor_active,
                sensor_is_stale=_sensor_is_stale,
                motor_is_stale=_motor_is_stale,
                origin_capturable=_origin_capturable,
                capture_origin=_capture_origin,
            )
        )

    server.set_motor_check_sequence(sequence)
    logger.info(
        "統合動作確認シーケンス登録: %d ステップ (モータ %d 台, 軸 %d 本, 零点確定: %s)",
        len(sequence.steps),
        len(motors),
        len(merged.axes),
        homing_axes or "なし",
    )


def _merged_last_feedback_at(managers: list[CANManager]) -> Callable[[str], float | None]:
    """全 CANManager を横断して最終受信時刻を引く。

    センサ名はロボット横断に一意なので、どのマネージャが持っていても答えは 1 つ。
    """

    def last_feedback_at(name: str) -> float | None:
        for mgr in managers:
            at = mgr.last_feedback_at(name)
            if at is not None:
                return at
        return None

    return last_feedback_at


def _create_bus(channel: str, *, dry_run: bool) -> can.Bus:
    """1 本の CAN インタフェースを開く。開けなければ 1 行のメッセージで落とす。

    down しているインタフェース (CANable が 1 本抜けている・`setup_can.sh` を
    流していない) を開こうとすると python-can は `OSError [Errno 19]` を投げる。
    この呼び出しは `main()` の try の外にあるので、素通しすると生の traceback で
    落ちるうえ後始末も 1 段も走らない。会場で読むのは操縦者なので、
    config 系のエラー (`_load_all_configs`) と同じく直し方まで書いて止める。
    """
    if dry_run:
        return can.Bus(interface="virtual", channel=channel)
    try:
        return can.Bus(interface="socketcan", channel=channel)
    except (OSError, can.CanError) as exc:
        raise SystemExit(
            f"CAN インタフェース '{channel}' を開けません ({exc})。"
            " scripts/setup_can.sh を実行してバスが up しているか確認してください"
            " (点検は scripts/setup_can.sh --strict)"
        ) from exc


def _robot_bus_names(robot: RobotConfig, can_buses: Mapping[str, str]) -> list[str]:
    """そのロボットが実際に使うバス名 (`can_buses` の宣言順)。

    **全バスを開いてはならない。** メインハンドは DM3520 を 1 台も持たないのに
    `can_dm3520` を、サブハンドは `can_m3508` を開くことになり、CANable が 1 本
    欠けているだけで**どちらのハンドも起動できなくなる**。片方だけの運用も
    動作確認も UI の起動もできない。

    副次的に、受信ループが物理バス 1 本につき 2 本立つ (executor スレッドを
    常時 8 本占有し、E-STOP のブロードキャストも 2 通ずつ出る) のも解消する。
    """
    used = {cfg.bus for cfg in robot.motors.values()}
    used |= {cfg.bus for cfg in robot.sensors.values()}
    return [name for name in can_buses if name in used]


def _make_m3508(motor: MotorConfig) -> MotorDriver:
    # 位置制御は PC 側の PID ループ (lib/control/position_loop.py) が持つので、
    # ドライバへ渡す設定は無い (C620 は電流指令しか受け付けない)
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
        # フィードバックの固定小数点レンジ。実機のレジスタ 0x15/0x16/0x17 と
        # ずれると位置が比例倍で読め、指令どおり動いても到達判定を通らない
        p_max=motor.p_max,
        v_max=motor.v_max,
        t_max=motor.t_max,
        set_zero_on_start=motor.set_zero_on_start,
    )


def _make_generic(motor: MotorConfig) -> MotorDriver:
    # control_type を渡さないと duty 指令の DC モータが位置制御で生成され、
    # 指令が config と別物になる
    return GenericDriver(
        name=motor.name,
        can_id=motor.can_id,
        control_type=motor.control_type,
        # 焼き忘れとサーボの型違いは、この照合以外に気付く手段が無い
        # (機体は指令どおり動いたようにしか見えない。仕様書 §3.4 / §7.7)
        expected_firmware=motor.expected_firmware,
        expected_angle_range_deg=motor.expected_angle_range_deg,
    )


#: ドライバ種別名 -> 生成関数。**全種別がこの表を通る。**
#: かつては 3 種を if 連鎖で個別に生成し、表に届くのは m3508 だけだった。
#: 名前は「種別 → 実装クラスの対応表」なのに実態は 1 行のフォールバックで、
#: lib/config_schema.DRIVER_TYPES が「この表と対で維持する」と言っている以上、
#: 読んだ人は全種別がここで生成されると読む。
#: 種別を足す人は DRIVER_TYPES とこの表の両方を触ることになり、
#: 対応は tests/test_config_schema.py が検証する。
_DRIVER_MAP: dict[str, Callable[[MotorConfig], MotorDriver]] = {
    "m3508": _make_m3508,
    "edulite05": _make_edulite05,
    "generic": _make_generic,
    "dm3520": _make_dm3520,
}


def _create_motor(motor: MotorConfig) -> MotorDriver:
    """検証済み設定からモータを生成する。

    未対応のドライバ種別や control_type の誤記は lib/config_schema が起動時に弾くため、
    ここには常に生成可能な設定しか来ない。
    """
    return _DRIVER_MAP[motor.driver](motor)


def _setup_robot(
    robot: RobotConfig, can_buses: Mapping[str, str], *, dry_run: bool
) -> tuple[CANManager, dict[str, MotorDriver]]:
    """検証済み設定から CANManager とモータ群をセットアップする。

    開くのは**このロボットが実際に使うバスだけ** (`_robot_bus_names`)。
    """
    can_manager = CANManager()

    for bus_name in _robot_bus_names(robot, can_buses):
        can_manager.add_bus(bus_name, _create_bus(can_buses[bus_name], dry_run=dry_run))

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
    # 既定は「記録しない」に倒す。配線を忘れた経路の症状が「波形が出ない」に
    # なり、config と違う設定が黙って効く形にはならない
    tuning: TuningSettings | None = None,
    capture_sink: CaptureSink | None = None,
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
                # PID 調整支援。目標値のステップを検出して応答を記録する
                # (記録のために機体を動かす経路は無い)
                tuning=tuning,
                capture_sink=capture_sink,
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
    # 既定は「記録しない」に倒す。配線を忘れた経路の症状が「波形が出ない」に
    # なり、config と違う設定が黙って効く形にはならない
    tuning: TuningSettings | None = None,
    capture_sink: CaptureSink | None = None,
) -> list[M3508PositionLoop]:
    """シーケンスにモータアクセス層を注入し、必要な位置制御ループを返す。"""
    loops = _build_position_loops(
        robot,
        can_manager,
        motors,
        feedback_timeout_ms=feedback_timeout_ms,
        is_estop_active=is_estop_active,
        tuning=tuning,
        capture_sink=capture_sink,
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


def _build_target_refreshers(
    group: MotorGroup,
    motors: dict[str, MotorDriver],
    can_manager: CANManager,
    *,
    is_estop_active: EStopChecker,
) -> list[TargetRefresher]:
    """周期的に指令を送り続ける必要があるモータを種別ごとに束ねる (居なければ空)。

    自作モタドラのファームは 500ms 自分宛の SET_TARGET が来ないと出力を止める
    (docs/motor_driver_can_protocol.md §5.1)。PC 側は目標値が変わったときにしか
    送らないため、再送が無いとコンベアは回し始めて 500ms で止まる。

    DM3520 と EDULITE 05 は理由が違う。**フィードバックが問い合わせ駆動**で、
    自分宛のフレームを受けたときにしか状態を返さない。送らなければ操縦していない
    間じゅう ``MotorHealth.STALE`` になり、症状は「手動操縦すると動くのに常に赤い」
    だけで配線不良と区別が付かない。自作モタドラと 1 つのタスクにまとめないのは、
    目標を持たないモータの扱いが正反対のため (自作モタドラは送ってはならず、
    問い合わせ駆動の 2 種は送らなければならない)。

    **EDULITE 05 を対象外にしてはならない。** かつて「ドライバ内蔵の位置ループが
    目標を保持し、かつ自発的にフィードバックを返す」として除外していたが、後半が
    誤りだった (実機で確認: 励磁したまま 13 秒放置してフィードバックは 0 通)。
    前半は正しいので位置制御ループは要らず、要るのは生存問い合わせだけになる。

    M3508 だけが対象外。位置制御ループが 200Hz で電流指令を送り続けるうえ、
    C620 はフィードバックを自発的に送るため問い合わせも要らない。
    """
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
    """robots/<robot_name>.py からシーケンスクラスを動的にロードする。

    **候補は「そのモジュールが定義した」クラスに限り、2 つ以上あったら起動を拒否する。**
    かつては ``dir()`` の並び (アルファベット順) で最初に見つかったサブクラスを
    返していたため、``robots/sub_hand.py`` が何かの都合で ``MotorCheckSequence`` を
    import しただけで ``"MotorCheckSequence" < "SubHandSequence"`` が成立し、
    サブハンドとして動作確認シーケンスが登録される。症状は「sub_hand の
    sequence_start でなぜか両ハンドが動く」だけで、config からもログからも
    理由が読めない。曖昧な構成は黙って起動させず、その場で落とす。
    """
    module_name = f"robots.{robot_name}"
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
    """そのモジュール自身が定義した Sequence サブクラス。無ければ None。

    2 つ以上あったら ``SystemExit``。どちらを登録すべきかは構成からしか決まらず、
    黙って片方を選ぶと「意図した側とは別の機体のシーケンス」がそのロボットとして
    動き出す。
    """
    found = [
        attr
        for attr_name in dir(module)
        if isinstance(attr := getattr(module, attr_name), type)
        and issubclass(attr, Sequence)
        and attr is not Sequence
        # import しただけの他モジュール由来のクラスを候補にしない
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


@dataclasses.dataclass(frozen=True)
class _RobotWiring:
    """ロボット 1 台ぶんの配線結果。

    起動・後始末・統合動作確認への受け渡しが、これ 1 つを回すだけで済むようにする。
    かつては `main()` の中でロボットごとの部品を 5 本のリストへ ``extend`` していた
    ため、「どの部品がどの機体のものか」がループを抜けた時点で失われていた。
    """

    name: str
    sequence: Sequence
    can_manager: CANManager
    positions: PositionTable
    position_loops: list[M3508PositionLoop]
    sync_monitors: list[SyncMonitor]
    target_refreshers: list[TargetRefresher]
    #: 統合動作確認へ渡すモータ束。モータを 1 台も bind できなかった構成では None
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
) -> _RobotWiring:
    """1 台ぶんの CAN・シーケンス・制御ループ・監視・手動を組み、サーバーへ登録する。

    **どの部品も「作って渡す」だけで、ここでは 1 つも起動しない。** 起動は
    `_start_all` が全機ぶんまとめて行う —— CAN の受信ループが立つ前に位置制御
    ループを回すと、フィードバック未受信のまま途絶判定を踏んで警告が出る。
    """
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
        tuning=system.tuning,
        # 記録はロボット名とセットで運ぶ。モータ名はロボット横断に一意だが、
        # 画面はロボットごとに分けて出すので、配信の時点で決めておく
        capture_sink=functools.partial(server.record_tuning_capture, robot_name),
    )

    # 自作モタドラはコマンドウォッチドッグを持つため、目標値を定期再送しないと
    # 500ms で出力が止まる (docs/motor_driver_can_protocol.md §5.1)
    refreshers = _build_target_refreshers(
        seq.motors,
        motors,
        can_manager,
        is_estop_active=is_estop_active,
    )

    # 同期監視はシーケンスから独立した常駐監視。動作確認中・待機中のずれも拾う
    sync_groups = _build_sync_groups(positions, motors)
    _attach_sync_groups(sync_groups, loops)
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

    # 手動操縦 (調整時・緊急時の補助操縦)。シーケンスと同じ MotorGroup を共有する
    manual = _build_manual_controller(seq, positions)

    # 監視をサーバーへ渡さないと、緊急停止解除でラッチを外す経路が存在せず、
    # 一度ずれを検知した軸は再起動するまで無監視・不動のまま残る
    server.add_robot(
        robot_name,
        seq,
        can_manager,
        position_loops=loops,
        sync_monitors=monitors,
        target_refreshers=refreshers,
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
        [name for r in refreshers for name in r.motor_names] or "なし",
        list(positions.manual_axes()) or "なし",
    )

    return _RobotWiring(
        name=robot_name,
        sequence=seq,
        can_manager=can_manager,
        positions=positions,
        position_loops=loops,
        sync_monitors=monitors,
        target_refreshers=refreshers,
        motor_group=seq.motors if seq.has_motors else None,
    )


def _ensure_port_available(host: str, port: int) -> None:
    """サーバーの bind 可否を **CAN を開くより前に**確かめる。

    立ち上げ順 (`_start_all`) は「CAN → 制御ループ → 目標値再送 → サーバー bind」で、
    これは変えられない —— フィードバック未受信のまま PID を回すと、起動のたびに
    途絶の警告ログが出る。だがその順序のままだと、ポートが埋まっているときに
    **機体を励磁して 200Hz の制御ループを回し始めた後**で bind が失敗する。
    「起動したか分からず二度叩く」は会場で普通に起きる操作で、そのたびに機体が
    数百 ms 励磁される。順序は変えず、bind の可能性だけを先に見る。

    ここで確保した socket は閉じて手放す (aiohttp が自分で bind し直す)。その間に
    別プロセスが割り込む余地は残るが、防ぎたいのは「自分の二重起動」であって
    競合そのものではない。
    """
    try:
        family, socktype, proto, _canonname, sockaddr = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )[0]
    except OSError as exc:
        raise SystemExit(f"サーバーのアドレス {host}:{port} を解決できません ({exc})") from exc

    with socket.socket(family, socktype, proto) as probe:
        # aiohttp の TCPSite と同じ条件で試す (POSIX では既定で reuse_address が立つ)。
        # 揃えないと TIME_WAIT のポートを「使用中」と誤判定して起動を拒否する
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
    """CAN → 制御ループ → 監視 → 再送 → サーバー、の順に立ち上げる。

    **CAN の受信ループを先に立てる。** フィードバック未受信のまま PID を回しても
    途絶判定で電流 0 に落ちるだけだが、起動のたびに無駄な警告ログが出る。

    **起動時に励磁できなかったモータは必ずサーバーへ渡す。** 捨てると
    `safety.unenergized_motors` は緊急停止解除の経路でしか埋まらず、起動時の
    励磁失敗は画面のどこにも出ない。フィードバックは問い合わせ駆動の再送で
    流れ出すのでヘルスは OK のまま、症状は「指令しても動かない」だけになる。
    """
    for wiring in wirings:
        server.set_initial_inactive_motors(wiring.name, await wiring.can_manager.run())
    for wiring in wirings:
        for loop in wiring.position_loops:
            loop.start()
    for wiring in wirings:
        for monitor in wiring.sync_monitors:
            monitor.start()
    for wiring in wirings:
        for refresher in wiring.target_refreshers:
            refresher.start()
    await server.start()


async def _shutdown_all(server: RobotServer, wirings: list[_RobotWiring]) -> None:
    """後始末。**1 手順が失敗しても残りを必ず続ける** (`_shutdown_step`)。

    順序に意味がある:
      1. 位置制御ループ —— 生き残ると電流指令が出続けるので CAN より先に止める
      2. 目標値再送 —— 止めればファーム側のウォッチドッグが 500ms 以内に出力を落とす。
         停止指令をここから送らないのは、PC が落ちた場合と経路を 1 本に保つため
      3. 同期監視 —— これだけ生き残ると、停止済みのモータのフィードバックを見て誤発報する
      4. CAN シャットダウン
      5. サーバー終了処理
    """
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
    # 完走したことを journal に残す。この行が無いまま終了していれば、
    # 後始末の途中で SIGKILL された (TimeoutStopSec 超過) と判別できる
    logger.info("後始末完了")


def _build_server(args: argparse.Namespace, system: SystemConfig) -> RobotServer:
    """CLI 引数と共通設定からサーバーを 1 台建てる (ロボットはまだ登録しない)。"""
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

    return RobotServer(
        host=args.host,
        port=args.port,
        health=system.health,
        checklist_definitions=checklist_definitions,
        match_settings=system.match,
        tuning=system.tuning,
        dry_run=args.dry_run,
        dev_tools=dev_tools,
    )


async def main() -> None:
    """読む → 配線する → 起動する → 畳む。"""
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
    logger.info("health しきい値: %s", system.health)
    # 試合時間は当日ルールで変わりうる。起動ログに出しておくと試合前点検で確認できる
    logger.info("試合時間: %s 秒", system.match.duration_s)

    # **CAN を開くより前に bind の可否を見る。** ポートが埋まっているときに
    # 機体を励磁してから落ちる経路を作らない (会場での二重起動)
    _ensure_port_available(args.host, args.port)

    server = _build_server(args, system)

    # 同期ずれから起動した緊急停止タスクの強参照置き場 (GC で消えると停止しない)
    e_stop_tasks: set[asyncio.Task[None]] = set()

    # モータ指令経路に渡す緊急停止インターロック。server の状態を遅延参照するため
    # クロージャにしている (server は add_robot より先に生成済み)
    def is_estop_active() -> bool:
        return server.e_stop_active

    # 2 パス目: ロボットごとの配線
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

    # 統合動作確認シーケンス。**両ハンドを 1 本の順序で駆動する**ので、
    # どのロボットにも属さない。機体ごとに独立した確認だと 2 つを同時に起動でき、
    # 可動域の重なる位置で干渉しうる
    _wire_motor_check_sequence(
        server,
        [w.motor_group for w in wirings if w.motor_group is not None],
        [w.positions for w in wirings],
        loops=[loop for w in wirings for loop in w.position_loops],
        can_managers=[w.can_manager for w in wirings],
        feedback_timeout_ms=system.health.feedback_timeout_ms,
    )

    try:
        await _start_all(server, wirings)
    except asyncio.CancelledError:
        pass
    finally:
        # 例外・キャンセルのどちらで抜けてもここを通す
        await _shutdown_all(server, wirings)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("終了")
