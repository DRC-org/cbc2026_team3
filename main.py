from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import pathlib

import can
import yaml

from lib.can_manager import CANManager
from lib.drivers.base import MotorDriver
from lib.drivers.edulite05 import Edulite05Driver
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import M3508Driver
from lib.match_state import ChecklistItem, load_checklist_definitions
from lib.sequence.engine import Sequence
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

    # 2 パス目: 既存の robot 登録ロジック
    for _config_path, config in loaded:
        robot_name, can_manager, motors = _setup_robot(config, dry_run=args.dry_run)
        can_managers.append(can_manager)

        seq = _load_sequence(robot_name)
        if seq is None:
            seq = _PlaceholderSequence(robot_name)

        server.add_robot(robot_name, seq, can_manager)
        logger.info(
            "ロボット登録: %s (モータ: %d 台)",
            robot_name,
            len(motors),
        )

    try:
        for mgr in can_managers:
            await mgr.run()
        await server.start()
    except asyncio.CancelledError:
        pass
    finally:
        for mgr in can_managers:
            await mgr.shutdown()
        await server.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("終了")
