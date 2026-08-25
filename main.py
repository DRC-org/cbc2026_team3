from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import pathlib

import can
import yaml

from lib.can_manager import CANManager
from lib.control.pid import PIDController
from lib.control.position_loop import M3508PositionLoop, make_position_pid
from lib.drivers.base import MotorDriver
from lib.drivers.edulite05 import Edulite05Driver
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import CURRENT_MAX, M3508Driver
from lib.match_state import ChecklistItem, load_checklist_definitions
from lib.sequence.engine import Sequence
from lib.sequence.motors import EStopChecker, TargetSink, build_motor_group
from lib.sequence.positions import PositionTable, load_position_table
from lib.server import RobotServer

logger = logging.getLogger(__name__)

_DRIVER_MAP: dict[str, type[MotorDriver]] = {
    "m3508": M3508Driver,
    "edulite05": Edulite05Driver,
    "generic": GenericDriver,
}

_CONFIG_DIR = pathlib.Path(__file__).resolve().parent / "config"
_DEFAULT_CONFIGS = ["main_hand.yaml", "sub_hand.yaml"]
_CHECKLIST_CONFIG = "checklist.yaml"
# 機構位置定数は robot config と同じディレクトリに <robot_name>_positions.yaml で置く
_POSITIONS_SUFFIX = "_positions.yaml"

# RobotServer.__init__ のキーワード引数デフォルトと一致させること。
# 値の変更は lib/server.py の RobotServer 既定値と同期する。
_DEFAULT_HEALTH: dict[str, float | int] = {
    "feedback_timeout_ms": 500.0,
    "temp_warning_c": 65.0,
    "temp_critical_c": 80.0,
    "tx_error_threshold": 96,
}

# motor_check セクションのデフォルト値。
# lib/motor_check.py の DEFAULT_PER_MOTOR_TIMEOUT_MS / DEFAULT_MAGNITUDES と同期する。
_DEFAULT_MOTOR_CHECK: dict[str, object] = {
    "per_motor_timeout_ms": 1500.0,
    "default_magnitude": {
        "m3508": 500.0,
        "edulite05": 5.0,
        "generic": 0.1,
    },
}


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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CBC2026 Team3 中央制御プログラム")
    parser.add_argument(
        "--config",
        nargs="*",
        help="config ファイルパス (デフォルト: config/main_hand.yaml config/sub_hand.yaml)",
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


def _load_health_config(configs: list[dict]) -> dict[str, float | int]:
    """全 config の health セクションを集約してしきい値辞書を返す。

    運用上の決定事項:
    - 最初に見つかった health セクションを基本値として採用する
    - 後続の config に異なる値があれば WARNING ログを出した上で最初の値を維持する
    - yaml に存在しないキーは _DEFAULT_HEALTH で補完する
      (segment-by-segment の部分上書きを許す)
    """
    result: dict[str, float | int] = dict(_DEFAULT_HEALTH)
    first_health: dict | None = None
    first_robot: str | None = None

    for cfg in configs:
        health = cfg.get("health")
        if not isinstance(health, dict):
            continue

        if first_health is None:
            first_health = health
            first_robot = cfg.get("robot_name")
            for key in _DEFAULT_HEALTH:
                if key in health:
                    result[key] = health[key]
            continue

        # 2 つ目以降の health セクション: 最初の値と比較して衝突を検出
        for key in _DEFAULT_HEALTH:
            first_val = first_health.get(key, _DEFAULT_HEALTH[key])
            this_val = health.get(key, _DEFAULT_HEALTH[key])
            if first_val != this_val:
                logger.warning(
                    "health.%s が config 間で不一致 (%s=%s, %s=%s)。前者を採用します。",
                    key,
                    first_robot,
                    first_val,
                    cfg.get("robot_name"),
                    this_val,
                )

    return result


def _load_motor_check_config(configs: list[dict]) -> dict[str, object]:
    """全 config の motor_check セクションを集約してアクチュエータ動作確認設定を返す。

    運用上の決定事項 (health と同じ方針):
    - 最初に見つかった motor_check セクションを基本値として採用する
    - 後続の config に異なる値があれば WARNING ログを出した上で最初の値を維持する
    - yaml に存在しないキーは _DEFAULT_MOTOR_CHECK で補完する
    """
    default_magnitude_default: dict[str, float] = dict(
        _DEFAULT_MOTOR_CHECK["default_magnitude"]  # type: ignore[arg-type]
    )
    result: dict[str, object] = {
        "per_motor_timeout_ms": _DEFAULT_MOTOR_CHECK["per_motor_timeout_ms"],
        "default_magnitude": default_magnitude_default,
    }
    first_mc: dict | None = None
    first_robot: str | None = None

    for cfg in configs:
        mc = cfg.get("motor_check")
        if not isinstance(mc, dict):
            continue

        if first_mc is None:
            first_mc = mc
            first_robot = cfg.get("robot_name")
            if "per_motor_timeout_ms" in mc:
                result["per_motor_timeout_ms"] = float(mc["per_motor_timeout_ms"])
            dm = mc.get("default_magnitude")
            if isinstance(dm, dict):
                magnitude_map: dict[str, float] = result["default_magnitude"]  # type: ignore[assignment]
                for key, value in dm.items():
                    magnitude_map[key] = float(value)
            continue

        # 2 つ目以降の motor_check セクション: 最初の値と比較して衝突を検出
        first_timeout = first_mc.get(
            "per_motor_timeout_ms", _DEFAULT_MOTOR_CHECK["per_motor_timeout_ms"]
        )
        this_timeout = mc.get("per_motor_timeout_ms", _DEFAULT_MOTOR_CHECK["per_motor_timeout_ms"])
        if first_timeout != this_timeout:
            logger.warning(
                "motor_check.per_motor_timeout_ms が config 間で不一致 "
                "(%s=%s, %s=%s)。前者を採用します。",
                first_robot,
                first_timeout,
                cfg.get("robot_name"),
                this_timeout,
            )

        first_dm = first_mc.get("default_magnitude") or {}
        this_dm = mc.get("default_magnitude") or {}
        all_keys = set(first_dm) | set(this_dm)
        for key in all_keys:
            first_val = first_dm.get(key, _DEFAULT_MOTOR_CHECK["default_magnitude"].get(key))  # type: ignore[union-attr]
            this_val = this_dm.get(key, _DEFAULT_MOTOR_CHECK["default_magnitude"].get(key))  # type: ignore[union-attr]
            if first_val != this_val:
                logger.warning(
                    "motor_check.default_magnitude.%s が config 間で不一致 "
                    "(%s=%s, %s=%s)。前者を採用します。",
                    key,
                    first_robot,
                    first_val,
                    cfg.get("robot_name"),
                    this_val,
                )

    return result


def _collect_per_motor_overrides(
    configs: list[dict],
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
    for cfg in configs:
        motors_cfg = cfg.get("motors") or {}
        if not isinstance(motors_cfg, dict):
            continue
        for motor_name, motor_cfg in motors_cfg.items():
            if not isinstance(motor_cfg, dict):
                continue
            mc = motor_cfg.get("motor_check")
            if not isinstance(mc, dict):
                continue
            entry: dict[str, float] = {}
            if "magnitude" in mc:
                entry["magnitude"] = float(mc["magnitude"])
            if "timeout_ms" in mc:
                entry["timeout_ms"] = float(mc["timeout_ms"])
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


def _create_motor(motor_name: str, motor_cfg: dict) -> MotorDriver | None:
    """設定からモータを生成し、ドライバ固有設定も反映する。"""
    driver_type = motor_cfg["driver"]
    driver_cls = _DRIVER_MAP.get(driver_type)
    if driver_cls is None:
        logger.warning("未知のドライバタイプ: %s (スキップ)", driver_type)
        return None

    can_id = motor_cfg["can_id"]
    if isinstance(can_id, str):
        can_id = int(can_id, 0)

    if driver_type == "edulite05":
        host_id = motor_cfg.get("host_id", 0xFD)
        if isinstance(host_id, str):
            host_id = int(host_id, 0)
        return Edulite05Driver(
            name=motor_name,
            can_id=can_id,
            host_id=host_id,
            mode=motor_cfg.get("mode", "position"),
            limit_speed=float(motor_cfg.get("limit_speed", 2.0)),
            limit_current=float(motor_cfg.get("limit_current", 5.0)),
            position_kp=float(motor_cfg.get("position_kp", 30.0)),
            set_zero_on_start=bool(motor_cfg.get("set_zero_on_start", False)),
        )

    return driver_cls(name=motor_name, can_id=can_id)


def _setup_robot(config: dict, *, dry_run: bool) -> tuple[str, CANManager, dict[str, MotorDriver]]:
    """config dict からロボット名・CANManager・モータ群をセットアップする。"""
    robot_name: str = config["robot_name"]
    can_manager = CANManager()

    bus_map: dict[str, str] = config.get("can_buses", {})
    for bus_name, channel in bus_map.items():
        bus = _create_bus(channel, dry_run=dry_run)
        can_manager.add_bus(bus_name, bus)

    motors: dict[str, MotorDriver] = {}
    motor_configs: dict = config.get("motors") or {}
    for motor_name, motor_cfg in motor_configs.items():
        motor = _create_motor(motor_name, motor_cfg)
        if motor is None:
            continue
        bus_name = motor_cfg["bus"]
        can_manager.add_motor(bus_name, motor)
        motors[motor_name] = motor

    return robot_name, can_manager, motors


def _load_pid_config(motor_name: str, motor_cfg: dict) -> dict[str, float | None]:
    """motors[name].pid を読み、未指定キーを _DEFAULT_PID で補完する。

    pid セクションが無い M3508 は既定ゲインで動かす (エラーにしない)。
    起動できないと動作確認そのものができず、機構調整中の実機で困るため。
    既定値は安全側に振ってあるので、無指定でも暴れない。
    """
    result: dict[str, float | None] = dict(_DEFAULT_PID)
    pid_cfg = motor_cfg.get("pid")
    if not isinstance(pid_cfg, dict):
        return result

    for key, value in pid_cfg.items():
        if key not in _DEFAULT_PID:
            logger.warning("未知の pid キー: motors.%s.pid.%s (無視)", motor_name, key)
            continue
        if value is None:
            # integral_limit だけは「制限なし」を null で表現できる。
            # 他のキーの null は書きかけの yaml とみなし、既定値のまま使う
            if key != "integral_limit":
                logger.warning(
                    "motors.%s.pid.%s が null です。既定値 %s を使います。",
                    motor_name,
                    key,
                    _DEFAULT_PID[key],
                )
                continue
            result[key] = None
            continue
        result[key] = float(value)
    return result


def _build_position_pid(motor_name: str, motor_cfg: dict) -> PIDController:
    """M3508 1 台分の位置制御 PID を config から組み立てる。"""
    params = _load_pid_config(motor_name, motor_cfg)
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
    config: dict,
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
    motor_configs: dict = config.get("motors") or {}

    for motor_name, motor_cfg in motor_configs.items():
        driver = motors.get(motor_name)
        if not isinstance(driver, M3508Driver):
            continue

        bus_name = motor_cfg["bus"]
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
        loop.add_motor(motor_name, driver, _build_position_pid(motor_name, motor_cfg))

    return loops


def _wire_robot_motors(
    config: dict,
    can_manager: CANManager,
    motors: dict[str, MotorDriver],
    sequence: Sequence,
    *,
    feedback_timeout_ms: float,
    is_estop_active: EStopChecker,
) -> list[M3508PositionLoop]:
    """シーケンスにモータアクセス層を注入し、必要な位置制御ループを返す。"""
    loops = _build_position_loops(
        config,
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


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    args = _parse_args()

    if args.config:
        config_paths = [pathlib.Path(p) for p in args.config]
    else:
        config_paths = [_CONFIG_DIR / name for name in _DEFAULT_CONFIGS]

    # 1 パス目: yaml をすべて読み込み health しきい値だけを先に確定させる。
    # RobotServer 生成時にしきい値を渡す必要があるため、ロボット登録より先に
    # 全 config を辞書化しておく。
    loaded: list[tuple[pathlib.Path, dict]] = []
    for config_path in config_paths:
        if not config_path.exists():
            logger.warning("config ファイルが見つかりません: %s (スキップ)", config_path)
            continue
        loaded.append((config_path, _load_config(config_path)))

    health_thresholds = _load_health_config([cfg for _, cfg in loaded])
    logger.info("health しきい値: %s", health_thresholds)

    motor_check_settings = _load_motor_check_config([cfg for _, cfg in loaded])
    motor_check_overrides = _collect_per_motor_overrides([cfg for _, cfg in loaded])
    logger.info(
        "motor_check 設定: per_motor_timeout_ms=%s default_magnitude=%s overrides=%s",
        motor_check_settings["per_motor_timeout_ms"],
        motor_check_settings["default_magnitude"],
        motor_check_overrides,
    )

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
        **health_thresholds,
        motor_check_per_motor_timeout_ms=motor_check_settings["per_motor_timeout_ms"],
        motor_check_default_magnitude=motor_check_settings["default_magnitude"],
        motor_check_per_motor_overrides=motor_check_overrides,
        checklist_definitions=checklist_definitions,
        dry_run=args.dry_run,
    )
    can_managers: list[CANManager] = []
    position_loops: list[M3508PositionLoop] = []

    # モータ指令経路に渡す緊急停止インターロック。server の状態を遅延参照するため
    # クロージャにしている (server は add_robot より先に生成済み)
    def is_estop_active() -> bool:
        return server.e_stop_active

    # 2 パス目: 既存の robot 登録ロジック
    for config_path, config in loaded:
        robot_name, can_manager, motors = _setup_robot(config, dry_run=args.dry_run)
        can_managers.append(can_manager)

        seq = _load_sequence(robot_name)
        if seq is None:
            seq = _PlaceholderSequence(robot_name)

        positions = _load_position_table_file(_positions_path(config_path, robot_name))
        seq.bind_positions(positions)

        loops = _wire_robot_motors(
            config,
            can_manager,
            motors,
            seq,
            feedback_timeout_ms=float(health_thresholds["feedback_timeout_ms"]),
            is_estop_active=is_estop_active,
        )
        position_loops.extend(loops)

        server.add_robot(robot_name, seq, can_manager, position_loops=loops)
        logger.info(
            "ロボット登録: %s (モータ: %d 台, 位置制御ループ: %s, 位置定数軸: %s)",
            robot_name,
            len(motors),
            [loop.bus_name for loop in loops] or "なし",
            list(positions.axes) or "なし",
        )

    try:
        for mgr in can_managers:
            await mgr.run()
        # 受信ループ起動後に始める。フィードバック未受信のまま PID を回すと
        # 途絶判定で電流 0 に落ちるだけだが、無駄な警告ログを避ける
        for loop in position_loops:
            loop.start()
        await server.start()
    except asyncio.CancelledError:
        pass
    finally:
        # 例外・キャンセルのどちらで抜けてもここを通す。ループが生き残ると
        # 電流指令が出続けるため、CAN シャットダウンより先に必ず止める
        for loop in position_loops:
            await loop.stop()
        for mgr in can_managers:
            await mgr.shutdown()
        await server.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("終了")
