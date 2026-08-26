from __future__ import annotations

import pathlib

import pytest
import yaml

import main
from lib.config_schema import (
    DEFAULT_HEALTH,
    DEFAULT_MOTOR_CHECK,
    DRIVER_TYPES,
    load_robot_config,
    load_system_config,
)
from lib.drivers.base import ControlMode

_CONFIG_DIR = pathlib.Path(__file__).resolve().parent.parent / "config"

_BUSES = {"m3508_bus": "can_m3508", "edulite_bus": "can_edulite", "generic_bus": "can_generic"}


def _robot(**motors: dict) -> dict:
    return {"robot_name": "test_hand", "motors": motors}


def _generic(**extra: object) -> dict:
    cfg: dict = {"driver": "generic", "bus": "generic_bus", "can_id": 1}
    cfg.update(extra)
    return cfg


class TestSystemConfig:
    def test_values_are_read_from_yaml(self) -> None:
        config = load_system_config(
            {
                "can_buses": {"a_bus": "can_a"},
                "health": {
                    "feedback_timeout_ms": 250,
                    "temp_warning_c": 50,
                    "temp_critical_c": 70,
                    "tx_error_threshold": 64,
                },
                "motor_check": {
                    "per_motor_timeout_ms": 2000,
                    "default_magnitude": {"m3508": 600, "edulite05": 7.5, "generic": 0.2},
                },
            },
            source="system.yaml",
        )

        assert config.can_buses == {"a_bus": "can_a"}
        assert config.health.feedback_timeout_ms == 250.0
        assert config.health.temp_warning_c == 50.0
        assert config.health.temp_critical_c == 70.0
        assert config.health.tx_error_threshold == 64
        assert config.motor_check.per_motor_timeout_ms == 2000.0
        assert config.motor_check.default_magnitude["m3508"] == 600.0

    def test_missing_sections_fall_back_to_defaults(self) -> None:
        config = load_system_config({"can_buses": {"a_bus": "can_a"}}, source="system.yaml")

        assert config.health == DEFAULT_HEALTH
        assert config.motor_check == DEFAULT_MOTOR_CHECK

    def test_partial_health_override_fills_defaults(self) -> None:
        config = load_system_config(
            {"can_buses": {"a_bus": "can_a"}, "health": {"temp_critical_c": 90}},
            source="system.yaml",
        )

        assert config.health.temp_critical_c == 90.0
        assert config.health.feedback_timeout_ms == DEFAULT_HEALTH.feedback_timeout_ms

    def test_partial_default_magnitude_fills_defaults(self) -> None:
        config = load_system_config(
            {"can_buses": {"a_bus": "can_a"}, "motor_check": {"default_magnitude": {"m3508": 750}}},
            source="system.yaml",
        )

        assert config.motor_check.default_magnitude["m3508"] == 750.0
        assert (
            config.motor_check.default_magnitude["generic"]
            == DEFAULT_MOTOR_CHECK.default_magnitude["generic"]
        )

    def test_unknown_top_level_key_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="motors"):
            load_system_config(
                {"can_buses": {"a_bus": "can_a"}, "motors": {}}, source="system.yaml"
            )

    def test_unknown_health_key_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="temp_warn_c"):
            load_system_config(
                {"can_buses": {"a_bus": "can_a"}, "health": {"temp_warn_c": 60}},
                source="system.yaml",
            )

    def test_non_numeric_health_value_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="temp_warning_c"):
            load_system_config(
                {"can_buses": {"a_bus": "can_a"}, "health": {"temp_warning_c": "hot"}},
                source="system.yaml",
            )

    def test_unknown_driver_in_default_magnitude_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="edulight05"):
            load_system_config(
                {
                    "can_buses": {"a_bus": "can_a"},
                    "motor_check": {"default_magnitude": {"edulight05": 5.0}},
                },
                source="system.yaml",
            )

    def test_missing_can_buses_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="can_buses"):
            load_system_config({}, source="system.yaml")

    def test_non_string_bus_channel_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="a_bus"):
            load_system_config({"can_buses": {"a_bus": 3}}, source="system.yaml")


class TestRobotConfigStructure:
    def test_minimal_config_is_accepted(self) -> None:
        config = load_robot_config(_robot(gripper=_generic()), source="test.yaml", buses=_BUSES)

        assert config.robot_name == "test_hand"
        assert config.motors["gripper"].driver == "generic"
        assert config.motors["gripper"].bus == "generic_bus"
        assert config.motors["gripper"].can_id == 1

    def test_missing_robot_name_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="robot_name"):
            load_robot_config({"motors": {"gripper": _generic()}}, source="test.yaml")

    def test_missing_motors_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="motors"):
            load_robot_config({"robot_name": "test_hand"}, source="test.yaml")

    def test_unknown_top_level_key_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="sequence"):
            load_robot_config(
                {"robot_name": "r", "motors": {"g": _generic()}, "sequence": "x"},
                source="test.yaml",
            )

    @pytest.mark.parametrize("section", ["health", "motor_check", "can_buses"])
    def test_shared_sections_point_at_system_yaml(self, section: str) -> None:
        """共通設定を robot yaml に書いても効かない。黙って無視せず移動先を教える。"""
        with pytest.raises(ValueError, match=r"system\.yaml"):
            load_robot_config(
                {"robot_name": "r", "motors": {"g": _generic()}, section: {}},
                source="test.yaml",
            )

    @pytest.mark.parametrize("key", ["driver", "bus", "can_id"])
    def test_missing_required_motor_key_is_rejected(self, key: str) -> None:
        motor = _generic()
        del motor[key]

        with pytest.raises(ValueError, match=key):
            load_robot_config(_robot(gripper=motor), source="test.yaml")

    def test_unknown_driver_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="m3508"):
            load_robot_config(_robot(gripper=_generic(driver="genric")), source="test.yaml")

    def test_unknown_motor_key_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="conrol_type"):
            load_robot_config(_robot(gripper=_generic(conrol_type="duty")), source="test.yaml")

    def test_hex_string_can_id_is_parsed(self) -> None:
        config = load_robot_config(_robot(gripper=_generic(can_id="0x05")), source="test.yaml")

        assert config.motors["gripper"].can_id == 5

    def test_non_integer_can_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="can_id"):
            load_robot_config(_robot(gripper=_generic(can_id="one")), source="test.yaml")

    def test_undefined_bus_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="generic_buss"):
            load_robot_config(
                _robot(gripper=_generic(bus="generic_buss")), source="test.yaml", buses=_BUSES
            )

    def test_bus_is_not_checked_without_bus_definitions(self) -> None:
        """バス定義を渡さない呼び出し (単体試験・部分検証) では別名を突き合わせない。"""
        config = load_robot_config(_robot(gripper=_generic(bus="bus_a")), source="test.yaml")

        assert config.motors["gripper"].bus == "bus_a"


class TestControlType:
    """A9: control_type のタイポは position へ落とさず起動を拒否する。

    duty のつもりの 0.3 が position 0.3deg としてファームへ届き、ファームも素直に
    受理するため、警告ログだけでは事故を止められない。
    """

    def test_control_type_is_applied(self) -> None:
        config = load_robot_config(
            _robot(conveyor=_generic(control_type="duty")), source="test.yaml"
        )

        assert config.motors["conveyor"].control_type is ControlMode.DUTY

    def test_default_is_position(self) -> None:
        config = load_robot_config(_robot(gripper=_generic()), source="test.yaml")

        assert config.motors["gripper"].control_type is ControlMode.POSITION

    def test_typo_is_rejected(self) -> None:
        with pytest.raises(ValueError) as exc:
            load_robot_config(_robot(conveyor=_generic(control_type="duy")), source="test.yaml")

        message = str(exc.value)
        assert "test.yaml" in message
        assert "conveyor" in message
        assert "control_type" in message
        assert "duy" in message
        assert "duty" in message

    def test_current_is_rejected(self) -> None:
        """GenericDriver は電流指令フレームを持たない。"""
        with pytest.raises(ValueError, match="control_type"):
            load_robot_config(_robot(gripper=_generic(control_type="current")), source="test.yaml")

    def test_control_type_on_non_generic_driver_is_rejected(self) -> None:
        """書いても効かないキーを黙って受け取らない。"""
        with pytest.raises(ValueError, match="control_type"):
            load_robot_config(
                _robot(
                    y_axis_r={
                        "driver": "m3508",
                        "bus": "m3508_bus",
                        "can_id": 1,
                        "control_type": "duty",
                    }
                ),
                source="test.yaml",
            )


class TestDriverSpecificKeys:
    def test_edulite_keys_are_parsed(self) -> None:
        config = load_robot_config(
            _robot(
                arm={
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
            ),
            source="test.yaml",
        )
        motor = config.motors["arm"]

        assert motor.can_id == 2
        assert motor.host_id == 0xFD
        assert motor.mode is ControlMode.POSITION
        assert motor.limit_speed == 3.0
        assert motor.limit_current == 4.0
        assert motor.position_kp == 25.0
        assert motor.set_zero_on_start is False

    def test_edulite_mode_typo_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="postion"):
            load_robot_config(
                _robot(
                    arm={
                        "driver": "edulite05",
                        "bus": "edulite_bus",
                        "can_id": 1,
                        "mode": "postion",
                    }
                ),
                source="test.yaml",
            )

    def test_edulite_key_on_generic_motor_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="host_id"):
            load_robot_config(_robot(gripper=_generic(host_id=0xFD)), source="test.yaml")

    def test_pid_on_non_m3508_motor_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="pid"):
            load_robot_config(_robot(gripper=_generic(pid={"kp": 1.0})), source="test.yaml")

    def test_unknown_pid_key_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="kf"):
            load_robot_config(
                _robot(
                    y_axis_r={
                        "driver": "m3508",
                        "bus": "m3508_bus",
                        "can_id": 1,
                        "pid": {"kp": 1.0, "kf": 9.0},
                    }
                ),
                source="test.yaml",
            )

    def test_pid_is_kept_for_the_pid_loader(self) -> None:
        config = load_robot_config(
            _robot(
                y_axis_r={
                    "driver": "m3508",
                    "bus": "m3508_bus",
                    "can_id": 1,
                    "pid": {"kp": 3.0},
                }
            ),
            source="test.yaml",
        )

        assert config.motors["y_axis_r"].pid == {"kp": 3.0}


class TestMotorCheckOverride:
    def test_values_are_parsed(self) -> None:
        config = load_robot_config(
            _robot(gripper=_generic(motor_check={"magnitude": 5.0, "timeout_ms": 2500})),
            source="test.yaml",
        )
        override = config.motors["gripper"].motor_check

        assert override.magnitude == 5.0
        assert override.timeout_ms == 2500.0

    def test_absent_override_is_empty(self) -> None:
        config = load_robot_config(_robot(gripper=_generic()), source="test.yaml")
        override = config.motors["gripper"].motor_check

        assert override.magnitude is None
        assert override.timeout_ms is None

    def test_unknown_key_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="tolerance"):
            load_robot_config(
                _robot(gripper=_generic(motor_check={"tolerance": 1.0})), source="test.yaml"
            )

    def test_non_numeric_magnitude_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="magnitude"):
            load_robot_config(
                _robot(gripper=_generic(motor_check={"magnitude": "open"})), source="test.yaml"
            )


class TestShippedConfigs:
    """同梱 config が新しいスキーマを満たすこと (会場で読む設定なので必ず検証する)。"""

    def _system(self):
        return load_system_config(
            yaml.safe_load((_CONFIG_DIR / "system.yaml").read_text()), source="system.yaml"
        )

    def test_system_yaml_loads(self) -> None:
        system = self._system()

        assert set(system.can_buses) == {"m3508_bus", "edulite_bus", "generic_bus"}
        assert system.health.feedback_timeout_ms == 500.0
        assert system.health.temp_warning_c == 65.0
        assert system.health.temp_critical_c == 80.0
        assert system.health.tx_error_threshold == 96
        assert system.motor_check.per_motor_timeout_ms == 1500.0
        assert system.motor_check.default_magnitude == {
            "m3508": 500.0,
            "edulite05": 5.0,
            "generic": 0.1,
        }

    @pytest.mark.parametrize("name", ["main_hand.yaml", "sub_hand.yaml"])
    def test_robot_yaml_loads(self, name: str) -> None:
        system = self._system()

        config = load_robot_config(
            yaml.safe_load((_CONFIG_DIR / name).read_text()),
            source=name,
            buses=system.can_buses,
        )

        assert config.motors

    def test_shared_sections_are_not_duplicated_in_robot_yaml(self) -> None:
        """A10: 書いても効かない共通設定が robot yaml に残っていないこと。"""
        for name in ("main_hand.yaml", "sub_hand.yaml"):
            raw = yaml.safe_load((_CONFIG_DIR / name).read_text())

            assert {"health", "motor_check", "can_buses"} & set(raw) == set()

    def test_every_bus_alias_maps_to_a_defined_interface(self) -> None:
        """バス別名の実インタフェース名は can_buses.yaml に定義済みのものであること。

        ここが typo すると SocketCAN のインタフェースが存在せず、実機では起動時に
        全モータが繋がらない (dry-run では virtual バスが何でも受けるため気付けない)。
        """
        system = self._system()
        defined = set(yaml.safe_load((_CONFIG_DIR / "can_buses.yaml").read_text())["buses"] or {})

        assert set(system.can_buses.values()) <= defined


def test_driver_types_match_the_driver_map() -> None:
    """スキーマが許すドライバ種別と実装クラスの対応表がずれていないこと。

    片方だけ増えると「検証は通るのに生成できない」または「生成できるのに書けない」
    ドライバができる。
    """
    assert set(DRIVER_TYPES) == set(main._DRIVER_MAP)
