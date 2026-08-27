from __future__ import annotations

from lib.config_schema import RobotConfig, load_robot_config
from lib.drivers.base import ControlMode
from lib.drivers.edulite05 import Edulite05Driver
from main import _collect_per_motor_overrides, _create_motor


def _robot(name: str, motors: dict) -> RobotConfig:
    return load_robot_config({"robot_name": name, "motors": motors}, source=f"{name}.yaml")


def test_collect_per_motor_overrides_empty() -> None:
    """motor_check を書いていないモータは上書き辞書に現れない。"""
    configs = [_robot("r1", {"lift_motor": {"driver": "m3508", "bus": "m3508_bus", "can_id": 1}})]

    assert _collect_per_motor_overrides(configs) == {}


def test_collect_per_motor_overrides_single() -> None:
    """1 モータの motor_check 上書きが正しく辞書化される。"""
    configs = [
        _robot(
            "r1",
            {
                "lift_motor": {
                    "driver": "m3508",
                    "bus": "m3508_bus",
                    "can_id": 1,
                    "motor_check": {"magnitude": 800, "timeout_ms": 2000},
                },
                "arm_joint": {
                    "driver": "edulite05",
                    "bus": "edulite_bus",
                    "can_id": 1,
                },
            },
        )
    ]

    overrides = _collect_per_motor_overrides(configs)

    assert overrides == {"lift_motor": {"magnitude": 800.0, "timeout_ms": 2000.0}}


def test_collect_per_motor_overrides_multi_robots() -> None:
    """複数ロボット config からの上書きが 1 つの辞書にマージされる。"""
    configs = [
        _robot(
            "main_hand",
            {
                "lift_motor": {
                    "driver": "m3508",
                    "bus": "m3508_bus",
                    "can_id": 1,
                    "motor_check": {"magnitude": 800},
                }
            },
        ),
        _robot(
            "sub_hand",
            {
                "gripper": {
                    "driver": "generic",
                    "bus": "generic_bus",
                    "can_id": 2,
                    "motor_check": {"timeout_ms": 2500},
                }
            },
        ),
    ]

    overrides = _collect_per_motor_overrides(configs)

    assert overrides == {
        "lift_motor": {"magnitude": 800.0},
        "gripper": {"timeout_ms": 2500.0},
    }


def test_magnitude_zero_is_kept_as_an_override() -> None:
    """左右ペア軸の magnitude: 0 は「動作確認から除外する」という有効な指定。

    falsy だからと捨てると片側だけが駆動され、左右直結の機構がその場で壊れる。
    """
    configs = [
        _robot(
            "main_hand",
            {
                "y_axis_r": {
                    "driver": "m3508",
                    "bus": "m3508_bus",
                    "can_id": 1,
                    "motor_check": {"magnitude": 0},
                }
            },
        )
    ]

    assert _collect_per_motor_overrides(configs) == {"y_axis_r": {"magnitude": 0.0}}


def test_create_edulite_motor_applies_driver_specific_config() -> None:
    robot = _robot(
        "r1",
        {
            "arm_joint": {
                "driver": "edulite05",
                "bus": "edulite_bus",
                "can_id": "0x02",
                "host_id": "0xFD",
                "mode": "position",
                "limit_speed": 3.0,
                "limit_current": 4.0,
                "position_kp": 25.0,
                "set_zero_on_start": False,
            }
        },
    )

    motor = _create_motor(robot.motors["arm_joint"])

    assert isinstance(motor, Edulite05Driver)
    assert motor.can_id == 2
    assert motor.host_id == 0xFD
    assert motor.mode is ControlMode.POSITION
    assert motor.limit_speed == 3.0
    assert motor.limit_current == 4.0
    assert motor.position_kp == 25.0
    assert motor.set_zero_on_start is False
