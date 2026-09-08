from __future__ import annotations

import pathlib

import pytest
import yaml

import main
from lib.config_schema import (
    CAN_ID_RANGES,
    DEFAULT_HEALTH,
    DEFAULT_MATCH,
    DRIVER_TYPES,
    load_robot_config,
    load_system_config,
)
from lib.drivers.base import ControlMode
from lib.drivers.dm3520 import Dm3520Driver
from lib.drivers.edulite05 import Edulite05Driver
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import M3508Driver
from lib.match_state import ALL_ROLES
from lib.sequence.positions import load_position_table

_CONFIG_DIR = pathlib.Path(__file__).resolve().parent.parent / "config"

_BUSES = {
    "m3508_bus": "can_m3508",
    "edulite_bus": "can_edulite",
    "generic_bus": "can_generic",
    "dm3520_bus": "can_dm3520",
}


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
            },
            source="system.yaml",
        )

        assert config.can_buses == {"a_bus": "can_a"}
        assert config.health.feedback_timeout_ms == 250.0
        assert config.health.temp_warning_c == 50.0
        assert config.health.temp_critical_c == 70.0
        assert config.health.tx_error_threshold == 64

    def test_missing_sections_fall_back_to_defaults(self) -> None:
        config = load_system_config({"can_buses": {"a_bus": "can_a"}}, source="system.yaml")

        assert config.health == DEFAULT_HEALTH
        assert config.match == DEFAULT_MATCH

    def test_match_duration_is_read_from_yaml(self) -> None:
        config = load_system_config(
            {"can_buses": {"a_bus": "can_a"}, "match": {"duration_s": 120}},
            source="system.yaml",
        )

        assert config.match.duration_s == 120.0

    def test_unknown_match_key_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="countdown"):
            load_system_config(
                {"can_buses": {"a_bus": "can_a"}, "match": {"countdown": True}},
                source="system.yaml",
            )

    @pytest.mark.parametrize("value", [0, -30])
    def test_non_positive_match_duration_is_rejected(self, value: int) -> None:
        with pytest.raises(ValueError, match=r"match\.duration_s"):
            load_system_config(
                {"can_buses": {"a_bus": "can_a"}, "match": {"duration_s": value}},
                source="system.yaml",
            )

    def test_partial_health_override_fills_defaults(self) -> None:
        config = load_system_config(
            {"can_buses": {"a_bus": "can_a"}, "health": {"temp_critical_c": 90}},
            source="system.yaml",
        )

        assert config.health.temp_critical_c == 90.0
        assert config.health.feedback_timeout_ms == DEFAULT_HEALTH.feedback_timeout_ms

    def test_motor_check_section_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="motor_check"):
            load_system_config(
                {"can_buses": {"a_bus": "can_a"}, "motor_check": {"per_motor_timeout_ms": 1500}},
                source="system.yaml",
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

    @pytest.mark.parametrize(
        "key",
        ["feedback_timeout_ms", "temp_warning_c", "temp_critical_c", "tx_error_threshold"],
    )
    @pytest.mark.parametrize("value", [0, -1])
    def test_non_positive_health_value_is_rejected(self, key: str, value: float) -> None:
        with pytest.raises(ValueError, match=key):
            load_system_config(
                {"can_buses": {"a_bus": "can_a"}, "health": {key: value}},
                source="system.yaml",
            )

    @pytest.mark.parametrize(
        "key",
        ["feedback_timeout_ms", "temp_warning_c", "temp_critical_c", "tx_error_threshold"],
    )
    @pytest.mark.parametrize("value", [float("nan"), ".nan", "nan", float("inf"), ".inf"])
    def test_non_finite_health_value_is_rejected(self, key: str, value: object) -> None:
        with pytest.raises(ValueError, match=key):
            load_system_config(
                {"can_buses": {"a_bus": "can_a"}, "health": {key: value}},
                source="system.yaml",
            )

    @pytest.mark.parametrize("value", [float("nan"), ".nan", "nan", float("inf"), ".inf"])
    def test_non_finite_match_duration_is_rejected(self, value: object) -> None:
        with pytest.raises(ValueError, match="duration_s"):
            load_system_config(
                {"can_buses": {"a_bus": "can_a"}, "match": {"duration_s": value}},
                source="system.yaml",
            )

    def test_inverted_temperature_thresholds_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="temp_warning_c"):
            load_system_config(
                {
                    "can_buses": {"a_bus": "can_a"},
                    "health": {"temp_warning_c": 90, "temp_critical_c": 60},
                },
                source="system.yaml",
            )

    def test_missing_can_buses_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="can_buses"):
            load_system_config({}, source="system.yaml")

    def test_non_string_bus_channel_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="a_bus"):
            load_system_config({"can_buses": {"a_bus": 3}}, source="system.yaml")

    def test_duplicate_bus_channel_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="can0"):
            load_system_config(
                {"can_buses": {"can_m3508": "can0", "can_edulite": "can0"}},
                source="system.yaml",
            )


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

    @pytest.mark.parametrize("section", ["health", "can_buses", "match"])
    def test_shared_sections_point_at_system_yaml(self, section: str) -> None:
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
        config = load_robot_config(_robot(gripper=_generic(bus="bus_a")), source="test.yaml")

        assert config.motors["gripper"].bus == "bus_a"


class TestControlType:
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

    def test_on_off_is_applied(self) -> None:
        config = load_robot_config(
            _robot(valve_1=_generic(control_type="on_off")), source="test.yaml"
        )

        assert config.motors["valve_1"].control_type is ControlMode.ON_OFF

    def test_current_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="control_type"):
            load_robot_config(_robot(gripper=_generic(control_type="current")), source="test.yaml")

    def test_control_type_on_non_generic_driver_is_rejected(self) -> None:
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


class TestCanIdRange:
    @pytest.mark.parametrize("can_id", [0x00, 0xFF, 0x100, -1])
    def test_generic_id_out_of_range_is_rejected(self, can_id: int) -> None:
        with pytest.raises(ValueError, match="can_id"):
            load_robot_config(_robot(gripper=_generic(can_id=can_id)), source="test.yaml")

    @pytest.mark.parametrize("can_id", [0xC0, 0xC5, 0xFE])
    def test_solenoid_board_ids_are_accepted(self, can_id: int) -> None:
        config = load_robot_config(
            _robot(valve_1=_generic(can_id=can_id, control_type="on_off")), source="test.yaml"
        )

        assert config.motors["valve_1"].can_id == can_id

    @pytest.mark.parametrize("can_id", [0, 5])
    def test_m3508_id_out_of_range_is_rejected(self, can_id: int) -> None:
        with pytest.raises(ValueError, match="can_id"):
            load_robot_config(
                _robot(y_axis_r={"driver": "m3508", "bus": "m3508_bus", "can_id": can_id}),
                source="test.yaml",
            )

    def test_edulite_id_out_of_range_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="can_id"):
            load_robot_config(
                _robot(arm={"driver": "edulite05", "bus": "edulite_bus", "can_id": 0x100}),
                source="test.yaml",
            )

    def test_ranges_match_what_the_drivers_accept(self) -> None:
        builders = {
            "m3508": lambda i: M3508Driver("m", i),
            "edulite05": lambda i: Edulite05Driver("m", i),
            "generic": lambda i: GenericDriver("m", i),
            "dm3520": lambda i: Dm3520Driver("m", i),
        }
        assert set(builders) == set(DRIVER_TYPES)

        for driver, (low, high) in CAN_ID_RANGES.items():
            build = builders[driver]
            build(low)
            build(high)
            for outside in (low - 1, high + 1):
                with pytest.raises(ValueError):
                    build(outside)


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

    def test_dm3520_keys_are_parsed(self) -> None:
        config = load_robot_config(
            _robot(
                slide={
                    "driver": "dm3520",
                    "bus": "dm3520_bus",
                    "can_id": "0x05",
                    "master_id": "0x11",
                    "mode": "position",
                    "limit_speed": 3.0,
                    "p_max": 12.5,
                    "v_max": 40.0,
                    "t_max": 8.0,
                    "set_zero_on_start": True,
                }
            ),
            source="test.yaml",
            buses=_BUSES,
        )
        motor = config.motors["slide"]

        assert motor.can_id == 5
        assert motor.master_id == 0x11
        assert motor.mode is ControlMode.POSITION
        assert motor.limit_speed == 3.0
        assert (motor.p_max, motor.v_max, motor.t_max) == (12.5, 40.0, 8.0)
        assert motor.set_zero_on_start is True

    def test_dm3520_mit_mode_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="mit"):
            load_robot_config(
                _robot(
                    slide={
                        "driver": "dm3520",
                        "bus": "dm3520_bus",
                        "can_id": 1,
                        "mode": "mit",
                    }
                ),
                source="test.yaml",
                buses=_BUSES,
            )

    def test_dm3520_rejects_edulite_only_keys(self) -> None:
        with pytest.raises(ValueError, match="host_id"):
            load_robot_config(
                _robot(
                    slide={
                        "driver": "dm3520",
                        "bus": "dm3520_bus",
                        "can_id": 1,
                        "host_id": "0xFD",
                    }
                ),
                source="test.yaml",
                buses=_BUSES,
            )

    def test_dm3520_master_id_colliding_with_another_can_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="master_id"):
            load_robot_config(
                _robot(
                    sub_y_axis={
                        "driver": "dm3520",
                        "bus": "dm3520_bus",
                        "can_id": 1,
                        "master_id": 2,
                    },
                    sub_lift={
                        "driver": "dm3520",
                        "bus": "dm3520_bus",
                        "can_id": 2,
                        "master_id": 1,
                    },
                ),
                source="test.yaml",
                buses=_BUSES,
            )

    def test_dm3520_master_id_equal_to_own_can_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="master_id"):
            load_robot_config(
                _robot(
                    sub_y_axis={
                        "driver": "dm3520",
                        "bus": "dm3520_bus",
                        "can_id": 1,
                        "master_id": 1,
                    },
                    sub_lift={
                        "driver": "dm3520",
                        "bus": "dm3520_bus",
                        "can_id": 2,
                        "master_id": 0x12,
                    },
                ),
                source="test.yaml",
                buses=_BUSES,
            )

    def test_dm3520_master_id_colliding_on_a_different_bus_is_accepted(self) -> None:
        config = load_robot_config(
            _robot(
                sub_y_axis={
                    "driver": "dm3520",
                    "bus": "dm3520_bus",
                    "can_id": 1,
                    "master_id": 0x11,
                },
                arm={
                    "driver": "generic",
                    "bus": "generic_bus",
                    "can_id": 0x11,
                },
            ),
            source="test.yaml",
            buses=_BUSES,
        )

        assert config.motors["sub_y_axis"].master_id == 0x11

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

    def test_pid_null_values_are_kept_for_the_pid_loader(self) -> None:
        config = load_robot_config(
            _robot(
                y_axis_r={
                    "driver": "m3508",
                    "bus": "m3508_bus",
                    "can_id": 1,
                    "pid": {"kp": None, "integral_limit": None},
                }
            ),
            source="test.yaml",
        )

        assert config.motors["y_axis_r"].pid == {"kp": None, "integral_limit": None}

    def test_non_numeric_pid_value_is_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"motors\.y_axis_r\.pid\.kp"):
            load_robot_config(
                _robot(
                    y_axis_r={
                        "driver": "m3508",
                        "bus": "m3508_bus",
                        "can_id": 1,
                        "pid": {"kp": "abc"},
                    }
                ),
                source="test.yaml",
            )

    # bool は int の派生なので float(True) == 1.0 が例外なく通る。
    def test_bool_pid_value_is_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"motors\.y_axis_r\.pid\.kp"):
            load_robot_config(
                _robot(
                    y_axis_r={
                        "driver": "m3508",
                        "bus": "m3508_bus",
                        "can_id": 1,
                        "pid": {"kp": True},
                    }
                ),
                source="test.yaml",
            )

    def test_non_finite_pid_value_is_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"motors\.y_axis_r\.pid\.kd"):
            load_robot_config(
                _robot(
                    y_axis_r={
                        "driver": "m3508",
                        "bus": "m3508_bus",
                        "can_id": 1,
                        "pid": {"kd": float("inf")},
                    }
                ),
                source="test.yaml",
            )


class TestMotorCheckIsNotAMotorSetting:
    def test_motor_check_key_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="motor_check"):
            load_robot_config(
                _robot(gripper=_generic(motor_check={"magnitude": 5.0})), source="test.yaml"
            )


class TestShippedConfigs:
    def _system(self):
        return load_system_config(
            yaml.safe_load((_CONFIG_DIR / "system.yaml").read_text()), source="system.yaml"
        )

    def test_system_yaml_loads(self) -> None:
        system = self._system()

        assert set(system.can_buses) == {
            "m3508_bus",
            "edulite_bus",
            "generic_bus",
            "dm3520_bus",
        }
        assert system.health.feedback_timeout_ms == 500.0
        assert system.health.temp_warning_c == 65.0
        assert system.health.temp_critical_c == 80.0
        assert system.health.tx_error_threshold == 96
        assert system.match.duration_s == 180.0

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
        for name in ("main_hand.yaml", "sub_hand.yaml"):
            raw = yaml.safe_load((_CONFIG_DIR / name).read_text())

            assert {"health", "motor_check", "can_buses", "match"} & set(raw) == set()

    def test_every_bus_alias_maps_to_a_defined_interface(self) -> None:
        system = self._system()
        defined = set(yaml.safe_load((_CONFIG_DIR / "can_buses.yaml").read_text())["buses"] or {})

        assert set(system.can_buses.values()) <= defined


_BENCH_DIRS = (
    "m3508",
    "edulite",
    "main_hand",
    "dc",
    "servo",
    "solenoid",
    "dm3520",
    "y_axis_tuning",
)

_BENCH_USES_PRODUCTION_CONFIG: dict[str, str] = {
    "main_hand": "main_hand",
}


def _bench_robot_yaml_path(bench: str, bench_dir: pathlib.Path) -> pathlib.Path:
    production_robot = _BENCH_USES_PRODUCTION_CONFIG.get(bench)
    if production_robot is not None:
        return _CONFIG_DIR / f"{production_robot}.yaml"

    return next(
        path
        for path in bench_dir.iterdir()
        if path.name.endswith(".yaml")
        and not path.name.endswith("_positions.yaml")
        and path.name not in ("system.yaml", "checklist.yaml")
    )


def _bench_positions_path(bench: str, bench_dir: pathlib.Path, robot_name: str) -> pathlib.Path:
    if bench in _BENCH_USES_PRODUCTION_CONFIG:
        return _CONFIG_DIR / f"{robot_name}_positions.yaml"

    return bench_dir / f"{robot_name}_positions.yaml"


class TestShippedBenchConfigs:
    def test_every_shipped_bench_dir_is_covered(self) -> None:
        shipped = {path.name for path in (_CONFIG_DIR / "bench").iterdir() if path.is_dir()}

        assert shipped == set(_BENCH_DIRS)

    @pytest.mark.parametrize("bench", _BENCH_DIRS)
    def test_bench_config_set_loads(self, bench: str) -> None:
        bench_dir = _CONFIG_DIR / "bench" / bench

        system = load_system_config(
            yaml.safe_load((bench_dir / "system.yaml").read_text()),
            source=f"bench/{bench}/system.yaml",
        )

        robot_yaml = _bench_robot_yaml_path(bench, bench_dir)
        config = load_robot_config(
            yaml.safe_load(robot_yaml.read_text()),
            source=f"bench/{bench}/{robot_yaml.name}",
            buses=system.can_buses,
        )

        assert config.motors

        positions_path = _bench_positions_path(bench, bench_dir, config.robot_name)
        assert positions_path.exists(), f"{positions_path} がありません"

        table = load_position_table(
            yaml.safe_load(positions_path.read_text()), source=str(positions_path)
        )

        axis_motors = {name for axis in table.axes for name in table.axis(axis).motor_names}
        assert set(config.motors) == axis_motors

    @pytest.mark.parametrize("bench", _BENCH_DIRS)
    def test_bench_checklist_uses_a_known_role(self, bench: str) -> None:
        bench_dir = _CONFIG_DIR / "bench" / bench
        checklist = yaml.safe_load((bench_dir / "checklist.yaml").read_text())["checklists"]

        assert set(checklist) <= set(ALL_ROLES)
        assert any(checklist.values())

    @pytest.mark.parametrize("bench", _BENCH_DIRS)
    def test_bench_opens_only_the_buses_on_the_desk(self, bench: str) -> None:
        bench_dir = _CONFIG_DIR / "bench" / bench

        system = load_system_config(
            yaml.safe_load((bench_dir / "system.yaml").read_text()),
            source=f"bench/{bench}/system.yaml",
        )
        robot_yaml = _bench_robot_yaml_path(bench, bench_dir)
        used = {motor["bus"] for motor in yaml.safe_load(robot_yaml.read_text())["motors"].values()}

        assert set(system.can_buses) == used


def test_driver_types_match_the_driver_map() -> None:
    assert set(DRIVER_TYPES) == set(main._DRIVER_MAP)


class TestExpectedInfoValues:
    def _load(self, **extra: object):
        raw = _robot(gripper=_generic(can_id=0x40, control_type="position", **extra))
        return load_robot_config(raw, source="test.yaml", buses=_BUSES)

    def test_values_are_accepted(self) -> None:
        motor = self._load(expected_firmware=2, expected_angle_range_deg=270.0).motors["gripper"]
        assert motor.expected_firmware == 2
        assert motor.expected_angle_range_deg == pytest.approx(270.0)

    def test_omitted_values_stay_none(self) -> None:
        motor = self._load().motors["gripper"]
        assert motor.expected_firmware is None
        assert motor.expected_angle_range_deg is None

    def test_angle_range_rejected_on_duty_axis(self) -> None:
        raw = _robot(
            conveyor=_generic(can_id=0x80, control_type="duty", expected_angle_range_deg=270.0)
        )
        with pytest.raises(ValueError, match="expected_angle_range_deg"):
            load_robot_config(raw, source="test.yaml", buses=_BUSES)

    def test_non_positive_angle_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="expected_angle_range_deg"):
            self._load(expected_angle_range_deg=0.0)

    def test_firmware_out_of_uint8_rejected(self) -> None:
        with pytest.raises(ValueError, match="expected_firmware"):
            self._load(expected_firmware=256)

    def test_expected_keys_rejected_on_other_drivers(self) -> None:
        raw = _robot(
            y_axis_r={
                "driver": "m3508",
                "bus": "m3508_bus",
                "can_id": 1,
                "expected_firmware": 2,
            }
        )
        with pytest.raises(ValueError):
            load_robot_config(raw, source="test.yaml", buses=_BUSES)
