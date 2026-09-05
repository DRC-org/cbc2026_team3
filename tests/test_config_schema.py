from __future__ import annotations

import pathlib
import re

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

    def test_tuning_defaults_when_section_is_absent(self) -> None:
        config = load_system_config({"can_buses": {"a_bus": "can_a"}}, source="system.yaml")

        assert config.tuning.enabled is True
        assert config.tuning.window_s == 3.0

    def test_tuning_values_are_read_from_yaml(self) -> None:
        config = load_system_config(
            {
                "can_buses": {"a_bus": "can_a"},
                "tuning": {
                    "enabled": False,
                    "window_s": 5.0,
                    "pre_trigger_s": 0.5,
                    "min_step_deg": 2.0,
                    "max_points": 120,
                },
            },
            source="system.yaml",
        )

        assert config.tuning.enabled is False
        assert config.tuning.window_s == 5.0
        assert config.tuning.pre_trigger_s == 0.5
        assert config.tuning.min_step_deg == 2.0
        assert config.tuning.max_points == 120

    def test_unknown_tuning_key_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="sample_rate"):
            load_system_config(
                {"can_buses": {"a_bus": "can_a"}, "tuning": {"sample_rate": 200}},
                source="system.yaml",
            )

    @pytest.mark.parametrize("key", ["window_s", "min_step_deg", "max_points"])
    def test_non_positive_tuning_value_is_rejected(self, key: str) -> None:
        """黙って既定値へ倒すと「設定したのに効かない」状態になり、
        波形が出ない原因が config から読めなくなる。"""
        with pytest.raises(ValueError, match=rf"tuning\.{re.escape(key)}"):
            load_system_config(
                {"can_buses": {"a_bus": "can_a"}, "tuning": {key: 0}},
                source="system.yaml",
            )

    def test_tuning_in_robot_yaml_is_rejected(self) -> None:
        """ロボットごとに書けると、読み込み側が片方しか採用できない設定になる。"""
        with pytest.raises(ValueError, match=r"system\.yaml"):
            load_robot_config(
                {"robot_name": "main_hand", "motors": {}, "tuning": {"window_s": 1.0}},
                source="main_hand.yaml",
            )

    def test_unknown_match_key_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="countdown"):
            load_system_config(
                {"can_buses": {"a_bus": "can_a"}, "match": {"countdown": True}},
                source="system.yaml",
            )

    @pytest.mark.parametrize("value", [0, -30])
    def test_non_positive_match_duration_is_rejected(self, value: int) -> None:
        """0 以下だと開始と同時に残り 0 になり、タイマーが常に時間切れを出す。
        誤記が画面の表示だけを壊すため、設定が原因だと気付けない。"""
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
        """動作確認の設定は無くなった。残っていたら「書いたのに効かない」状態になる。

        駆動量もタイムアウトも config/*_positions.yaml の位置定数が持つ
        (sequences/motor_check.py は運用と同じ位置名へ動かす)。
        """
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

    @pytest.mark.parametrize("section", ["health", "can_buses", "match"])
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

    def test_on_off_is_applied(self) -> None:
        """電磁弁の control_type: on_off (仕様書 §9.2)。

        許可表に無いと yaml に書いた瞬間に起動が拒否される。逆に許可表だけ通って
        GenericDriver 側の _MODE_MAP に無いと、起動はできるのに最初の指令で
        KeyError になる (試合中に落ちる)。
        """
        config = load_robot_config(
            _robot(valve_1=_generic(control_type="on_off")), source="test.yaml"
        )

        assert config.motors["valve_1"].control_type is ControlMode.ON_OFF

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


class TestCanIdRange:
    """can_id の範囲は起動時に見る。ドライバ生成まで待つと yaml のどこが悪いか出ない。

    範囲外の generic can_id は静かに壊れる (0xFF は緊急停止**解除**の
    ブロードキャストになり、共有バス上の全基板のラッチを外す)。
    """

    @pytest.mark.parametrize("can_id", [0x00, 0xFF, 0x100, -1])
    def test_generic_id_out_of_range_is_rejected(self, can_id: int) -> None:
        with pytest.raises(ValueError, match="can_id"):
            load_robot_config(_robot(gripper=_generic(can_id=can_id)), source="test.yaml")

    @pytest.mark.parametrize("can_id", [0xC0, 0xC5, 0xFE])
    def test_solenoid_board_ids_are_accepted(self, can_id: int) -> None:
        """電磁弁基板の帯 (0xC0-0xFE) が generic の範囲に収まっていること。

        範囲は仕様書 §2.2 と揃えてある。ここが狭いと、実在する基板の ID を
        yaml に書けないまま「範囲外」で起動を拒否される。
        """
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
        """config 側の表とドライバ側の検査がずれていないこと。

        2 箇所に範囲を書く以上、片方だけが古くなる経路を塞いでおく
        (config_schema は lib.drivers.base しか import しない約束なので、
        表そのものを共有できない)。
        """
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
        """MIT モードを書けるようにしない。

        書けてしまうと Kp/Kd を PC 側で持つ構成が config だけで成立し、
        「ドライバ内蔵の三重ループを使う」という本機を選んだ理由が消える。
        しかも指令フレームの形が変わるので、症状は「まったく動かない」になる。
        """
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
        """host_id は EDULITE 05 のキー。DM3520 に書いても効かない。

        効かないキーを黙って捨てると、「書いたのに反映されない」を config からは
        読めない (本機の宛先は MST_ID であって host_id ではない)。
        """
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
        """MST_ID の下位 8bit が別モータの ESC_ID と交差する構成は拒否する。

        本機は受信 ID の下位 8bit だけを見て自分宛かを判定するため、一致すると
        フィードバックが指令として解釈される (CLAUDE.md
        「MST_ID はどの ESC_ID とも下位 8bit が一致しない値にする」)。
        """
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
        """出荷値のまま (ESC_ID == MST_ID) 残った個体も拒否する。

        DM3520 の出荷値は 2 台とも ESC_ID == MST_ID。ESC_ID だけ書き換えて
        MST_ID を書き換え忘れた個体がそのまま config に混ざる事故を検出する。
        """
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
        """バスが違えば下位 8bit が一致しても衝突ではない。

        フレームは同じ物理バスに繋がったノードにしか届かないため、他バスの
        can_id とたまたま一致しても無害 (過剰検出で無関係な構成まで拒否しない)。
        """
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


class TestMotorCheckIsNotAMotorSetting:
    """モータごとの動作確認設定は無くなった。

    両ハンドを 1 本のシーケンスで駆動する形 (sequences/motor_check.py) へ変えたので、
    確認は運用と同じ位置名へ動かす。**確認専用の駆動量が存在しない**ため、
    位置定数と食い違いようがない。書いてあったら起動時に落とす。
    """

    def test_motor_check_key_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="motor_check"):
            load_robot_config(
                _robot(gripper=_generic(motor_check={"magnitude": 5.0})), source="test.yaml"
            )


class TestShippedConfigs:
    """同梱 config が新しいスキーマを満たすこと (会場で読む設定なので必ず検証する)。"""

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
        """A10: 書いても効かない共通設定が robot yaml に残っていないこと。"""
        for name in ("main_hand.yaml", "sub_hand.yaml"):
            raw = yaml.safe_load((_CONFIG_DIR / name).read_text())

            assert {"health", "motor_check", "can_buses", "match"} & set(raw) == set()

    def test_every_bus_alias_maps_to_a_defined_interface(self) -> None:
        """バス別名の実インタフェース名は can_buses.yaml に定義済みのものであること。

        ここが typo すると SocketCAN のインタフェースが存在せず、実機では起動時に
        全モータが繋がらない (dry-run では virtual バスが何でも受けるため気付けない)。
        """
        system = self._system()
        defined = set(yaml.safe_load((_CONFIG_DIR / "can_buses.yaml").read_text())["buses"] or {})

        assert set(system.can_buses.values()) <= defined


#: 机上ベンチの config セット。追加したらここへ 1 行足せば 3 種類の検証が全部かかる
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

#: 自分の robot yaml / positions を持たず、本番 config (config/<値>.yaml,
#: config/<値>_positions.yaml) をそのまま使うベンチセット。
#: main_hand は実機が完成して実測値が本番 config へ移ったことで、bench 側に
#: あった robot yaml (本番と完全一致) と positions (仮値のコピー) が
#: 二重管理の複製でしかなくなったため削除した。ここへ載せないセットは従来どおり
#: bench_dir 内の robot yaml / positions を要求する
#: (test_bench_config_set_loads / test_bench_opens_only_the_buses_on_the_desk が見る)。
_BENCH_USES_PRODUCTION_CONFIG: dict[str, str] = {
    "main_hand": "main_hand",
}


def _bench_robot_yaml_path(bench: str, bench_dir: pathlib.Path) -> pathlib.Path:
    """このベンチセットが読む robot yaml のパス。"""
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
    """このベンチセットが読む位置定数 yaml のパス。

    本番 config を使うセットは main.py の _positions_path と同じ規則
    (robot yaml と同じディレクトリの <robot_name>_positions.yaml) を本番側でたどる。
    """
    if bench in _BENCH_USES_PRODUCTION_CONFIG:
        return _CONFIG_DIR / f"{robot_name}_positions.yaml"

    return bench_dir / f"{robot_name}_positions.yaml"


class TestShippedBenchConfigs:
    """机上ベンチ用の config セット (config/bench/<対象>/) も同じスキーマで読めること。

    **ベンチ config は誰も検証していなかった。** 本番の config は
    TestShippedConfigs が守っているが、bench/ はスキーマを変えても壊れたことに
    気付けない —— 気付くのは机上に基板を並べた当日で、しかも症状は
    「起動しない」だけになる。実機が来る日は試合前で、そこで config の書き直しを
    始める余裕は無い。

    8 セットとも「system / robot / positions / checklist が揃っていて読める」ことだけを
    見る。値そのものは対象ごとに違ってよい (それが分ける理由なので)。
    **robot yaml / positions が bench_dir に無いセットは `_BENCH_USES_PRODUCTION_CONFIG`
    に載っていて本番 config を指す** —— 黙って検証を素通りさせると、他のセットで
    誤って config を消したときに検出できなくなるため、どちらの構成であるかを
    宣言させている (`test_every_shipped_bench_dir_is_covered` の穴と同じ理由)。
    """

    def test_every_shipped_bench_dir_is_covered(self) -> None:
        """同梱の bench ディレクトリが漏れなく _BENCH_DIRS に載っていること。

        _BENCH_DIRS は手書きの一覧なので、セットを 1 つ足して**ここへ書き足し忘れると
        その 1 セットだけ誰も検証しない**。しかも症状は「テストは全部緑」なので、
        気付くのは机上に基板を並べた当日になる (このクラスを置いた理由と同じ穴が、
        一覧の側に開く)。
        """
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

        # 位置定数は「robot config と同じディレクトリの <robot_name>_positions.yaml」を読む
        # (main.py の _positions_path)。名前がずれると本番の位置定数が読まれてしまい、
        # **机上に無い軸へ指令が飛ぶ**
        positions_path = _bench_positions_path(bench, bench_dir, config.robot_name)
        assert positions_path.exists(), f"{positions_path} がありません"

        table = load_position_table(
            yaml.safe_load(positions_path.read_text()), source=str(positions_path)
        )

        # 登録したモータはすべて位置定数から指令できること。
        # 片方だけ足すと「UI には出るのに動かせないモータ」になる
        axis_motors = {name for axis in table.axes for name in table.axis(axis).motor_names}
        assert set(config.motors) == axis_motors

    @pytest.mark.parametrize("bench", _BENCH_DIRS)
    def test_bench_checklist_uses_a_known_role(self, bench: str) -> None:
        """チェックリストのロールが ALL_ROLES に含まれること。

        知らないロール名で書いた項目はどこにも読み込まれない。しかも定義の無い
        ロールは「完了」とみなされるので、**指差喚呼を 1 項目も踏まないまま
        試合フェーズへ入れてしまう**。
        """
        bench_dir = _CONFIG_DIR / "bench" / bench
        checklist = yaml.safe_load((bench_dir / "checklist.yaml").read_text())["checklists"]

        assert set(checklist) <= set(ALL_ROLES)
        assert any(checklist.values())

    @pytest.mark.parametrize("bench", _BENCH_DIRS)
    def test_bench_opens_only_the_buses_on_the_desk(self, bench: str) -> None:
        """ベンチが開くバスは、そのセットで使うものだけであること。

        main.py の _setup_robot() は can_buses に並んだバスを**すべて** socketcan で
        open するため、机上に挿していない CANable が 1 本でも書いてあると
        [Errno 19] No such device で起動そのものが落ちる。
        """
        bench_dir = _CONFIG_DIR / "bench" / bench

        system = load_system_config(
            yaml.safe_load((bench_dir / "system.yaml").read_text()),
            source=f"bench/{bench}/system.yaml",
        )
        robot_yaml = _bench_robot_yaml_path(bench, bench_dir)
        used = {motor["bus"] for motor in yaml.safe_load(robot_yaml.read_text())["motors"].values()}

        assert set(system.can_buses) == used


def test_driver_types_match_the_driver_map() -> None:
    """スキーマが許すドライバ種別と実装クラスの対応表がずれていないこと。

    片方だけ増えると「検証は通るのに生成できない」または「生成できるのに書けない」
    ドライバができる。
    """
    assert set(DRIVER_TYPES) == set(main._DRIVER_MAP)


class TestExpectedInfoValues:
    """INFO と突き合わせる期待値の検証 (仕様書 §3.4 / §7.7)。"""

    def _load(self, **extra: object):
        raw = _robot(gripper=_generic(can_id=0x40, control_type="position", **extra))
        return load_robot_config(raw, source="test.yaml", buses=_BUSES)

    def test_values_are_accepted(self) -> None:
        motor = self._load(expected_firmware=2, expected_angle_range_deg=270.0).motors["gripper"]
        assert motor.expected_firmware == 2
        assert motor.expected_angle_range_deg == pytest.approx(270.0)

    def test_omitted_values_stay_none(self) -> None:
        """書かない軸は照合しない (既存 config をそのまま起動できる)。"""
        motor = self._load().motors["gripper"]
        assert motor.expected_firmware is None
        assert motor.expected_angle_range_deg is None

    def test_angle_range_rejected_on_duty_axis(self) -> None:
        """**角度を持たない基板に書けると「書いたのに効かない設定」になる。**

        DC 基板と電磁弁基板は可動レンジを申告しないので、照合は永久に
        「申告なし」と判定し続け、そのモータは起動直後から FAULT のまま復帰しない。
        """
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
        """M3508 に書いても効かないので、混在は起動時に弾く。"""
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
