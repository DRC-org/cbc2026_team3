from __future__ import annotations

import math
import re

import pytest

from lib.drivers.base import ControlMode
from lib.match_state import Court
from lib.sequence.positions import (
    DEFAULT_TIMEOUT_S,
    AxisSpec,
    LimitSpec,
    MotorSpec,
    PositionLookupError,
    PositionTable,
    load_position_table,
)


def _table(**overrides: object) -> PositionTable:
    data: dict = {
        "axes": {
            "lift_motor": {
                "unit": "mm",
                "command_unit": "deg",
                "scale": 864.0,
                "offset": 0.0,
            },
            "arm_joint": {
                "unit": "deg",
                "command_unit": "rad",
                "scale": math.pi / 180.0,
            },
        },
        "positions": {
            "lift_motor": {"home": 0.0, "work": 10.0},
            "arm_joint": {"home": 0.0, "extended": 15.0},
        },
    }
    data.update(overrides)
    return load_position_table(data, source="<test>")


def _command(table: PositionTable, axis: str, name: str, *, court: Court | None = None) -> float:
    return table.commands(axis, name, court=court)[axis]


class TestUnitConversion:
    def test_scale_is_applied(self) -> None:
        table = _table()

        assert _command(table, "lift_motor", "work") == pytest.approx(10.0 * 864.0)

    def test_offset_is_applied(self) -> None:
        table = load_position_table(
            {
                "axes": {"lift_motor": {"scale": 2.0, "offset": 100.0}},
                "positions": {"lift_motor": {"home": 5.0}},
            }
        )

        assert _command(table, "lift_motor", "home") == pytest.approx(5.0 * 2.0 + 100.0)

    def test_deg_to_rad_conversion(self) -> None:
        table = _table()

        assert _command(table, "arm_joint", "extended") == pytest.approx(math.radians(15.0))

    def test_scale_and_offset_default_to_identity(self) -> None:
        table = load_position_table(
            {
                "axes": {"gripper": {"unit": "deg", "command_unit": "deg"}},
                "positions": {"gripper": {"open": 12.0}},
            }
        )

        assert _command(table, "gripper", "open") == pytest.approx(12.0)

    def test_raw_returns_human_unit_value(self) -> None:
        table = _table()

        assert table.raw("lift_motor", "work") == pytest.approx(10.0)


class TestTimeoutAndTolerance:
    def test_timeout_defaults(self) -> None:
        table = _table()

        assert table.axis("lift_motor").timeout_s == pytest.approx(DEFAULT_TIMEOUT_S)

    def test_timeout_from_yaml(self) -> None:
        table = load_position_table(
            {
                "axes": {"lift_motor": {"timeout_s": 2.5}},
                "positions": {"lift_motor": {"home": 0.0}},
            }
        )

        assert table.axis("lift_motor").timeout_s == pytest.approx(2.5)

    def test_tolerance_defaults_to_none(self) -> None:
        table = _table()

        assert table.axis("lift_motor").tolerance is None

    def test_tolerance_is_converted_by_scale(self) -> None:
        table = load_position_table(
            {
                "axes": {"lift_motor": {"scale": -864.0, "tolerance": 0.5}},
                "positions": {"lift_motor": {"home": 0.0}},
            }
        )

        spec = table.axis("lift_motor")
        assert spec.tolerance == pytest.approx(0.5)
        assert spec.motors[0].to_tolerance(spec.tolerance) == pytest.approx(0.5 * 864.0)


class TestCourtVariants:
    def test_scalar_value_is_court_independent(self) -> None:
        table = _table()

        assert _command(table, "lift_motor", "work", court=Court.RED) == pytest.approx(8640.0)
        assert _command(table, "lift_motor", "work", court=Court.BLUE) == pytest.approx(8640.0)

    def test_mapping_value_resolves_per_court(self) -> None:
        table = load_position_table(
            {
                "axes": {"lift_motor": {"scale": 1.0}},
                "positions": {"lift_motor": {"place": {"red": 10.0, "blue": -10.0}}},
            }
        )

        assert _command(table, "lift_motor", "place", court=Court.RED) == pytest.approx(10.0)
        assert _command(table, "lift_motor", "place", court=Court.BLUE) == pytest.approx(-10.0)

    def test_mapping_value_without_court_raises(self) -> None:
        table = load_position_table(
            {
                "axes": {"lift_motor": {}},
                "positions": {"lift_motor": {"place": {"red": 10.0, "blue": -10.0}}},
            }
        )

        with pytest.raises(PositionLookupError):
            _command(table, "lift_motor", "place")

    def test_mapping_missing_court_key_raises_at_load(self) -> None:
        with pytest.raises(ValueError, match="blue"):
            load_position_table(
                {
                    "axes": {"lift_motor": {}},
                    "positions": {"lift_motor": {"place": {"red": 10.0}}},
                }
            )


class TestLookupErrors:
    def test_unknown_axis_lists_available_axes(self) -> None:
        table = _table()

        with pytest.raises(PositionLookupError) as excinfo:
            _command(table, "no_such_axis", "home")

        assert "lift_motor" in str(excinfo.value)

    def test_unknown_position_lists_available_names(self) -> None:
        table = _table()

        with pytest.raises(PositionLookupError) as excinfo:
            _command(table, "lift_motor", "no_such_position")

        assert "work" in str(excinfo.value)

    def test_source_is_included_in_error(self) -> None:
        table = _table()

        with pytest.raises(PositionLookupError) as excinfo:
            _command(table, "lift_motor", "no_such_position")

        assert "<test>" in str(excinfo.value)


class TestLoadValidation:
    def test_positions_for_undefined_axis_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown_axis"):
            load_position_table(
                {
                    "axes": {"lift_motor": {}},
                    "positions": {"unknown_axis": {"home": 0.0}},
                }
            )

    def test_non_numeric_value_raises(self) -> None:
        with pytest.raises(ValueError):
            load_position_table(
                {
                    "axes": {"lift_motor": {}},
                    "positions": {"lift_motor": {"home": "abc"}},
                }
            )

    def test_empty_config_yields_empty_table(self) -> None:
        table = load_position_table({})

        assert table.axes == ()

    def test_empty_helper(self) -> None:
        table = PositionTable.empty(source="missing.yaml")

        assert table.axes == ()
        with pytest.raises(PositionLookupError, match=r"missing\.yaml"):
            _command(table, "lift_motor", "home")


class TestIntrospection:
    def test_axes_and_names(self) -> None:
        table = _table()

        assert set(table.axes) == {"lift_motor", "arm_joint"}
        assert set(table.names("lift_motor")) == {"home", "work"}

    def test_axis_spec_exposes_units(self) -> None:
        spec = _table().axis("lift_motor")

        assert spec.unit == "mm"
        assert spec.command_unit == "deg"


_PAIRED_CONFIG: dict = {
    "axes": {
        "y_axis": {
            "unit": "mm",
            "command_unit": "deg",
            "tolerance": 1.0,
            "sync_tolerance": 2.0,
            "motors": {
                "y_axis_r": {"scale": 864.15, "offset": 0.0},
                "y_axis_l": {"scale": -864.15, "offset": 0.0},
            },
        },
        "gripper": {"unit": "state", "command_unit": "deg", "scale": 1.0},
    },
    "positions": {
        "y_axis": {"home": 0.0, "work": 10.0},
        "gripper": {"open": 30.0, "closed": 0.0},
    },
}


class TestMotorSpec:
    def test_to_value_is_inverse_of_to_command(self) -> None:
        motor = MotorSpec(name="y_axis_l", scale=-864.15, offset=12.5)

        assert motor.to_value(motor.to_command(7.5)) == pytest.approx(7.5)

    def test_to_command_applies_scale_and_offset(self) -> None:
        motor = MotorSpec(name="y_axis_r", scale=2.0, offset=100.0)

        assert motor.to_command(5.0) == pytest.approx(110.0)


class TestPairedAxis:
    def test_commands_applies_per_motor_scale(self) -> None:
        table = load_position_table(_PAIRED_CONFIG, source="<test>")

        assert table.commands("y_axis", "work") == {
            "y_axis_r": pytest.approx(10.0 * 864.15),
            "y_axis_l": pytest.approx(-10.0 * 864.15),
        }

    def test_commands_for_single_motor_axis_is_keyed_by_axis_name(self) -> None:
        table = load_position_table(_PAIRED_CONFIG, source="<test>")

        assert table.commands("gripper", "open") == {"gripper": pytest.approx(30.0)}

    def test_motor_names(self) -> None:
        table = load_position_table(_PAIRED_CONFIG, source="<test>")

        assert table.axis("y_axis").motor_names == ("y_axis_r", "y_axis_l")
        assert table.axis("gripper").motor_names == ("gripper",)

    def test_sync_tolerance_and_paired_axes(self) -> None:
        table = load_position_table(_PAIRED_CONFIG, source="<test>")

        assert table.sync_tolerance("y_axis") == pytest.approx(2.0)
        assert table.sync_tolerance("gripper") is None
        assert table.paired_axes() == ("y_axis",)

    def test_commands_resolves_court_variants(self) -> None:
        table = load_position_table(
            {
                "axes": {
                    "y_axis": {
                        "motors": {
                            "y_axis_r": {"scale": 2.0},
                            "y_axis_l": {"scale": -2.0},
                        }
                    }
                },
                "positions": {"y_axis": {"place": {"red": 10.0, "blue": -10.0}}},
            }
        )

        assert table.commands("y_axis", "place", court=Court.BLUE) == {
            "y_axis_r": pytest.approx(-20.0),
            "y_axis_l": pytest.approx(20.0),
        }


class TestCommandMode:
    def test_defaults_to_position(self) -> None:
        table = load_position_table(_PAIRED_CONFIG, source="<test>")

        assert table.axis("gripper").command_mode is ControlMode.POSITION
        assert table.axis("gripper").settle_s == pytest.approx(0.0)

    def test_duty_mode_with_settle(self) -> None:
        table = load_position_table(
            {
                "axes": {
                    "conveyor": {
                        "unit": "duty",
                        "command_unit": "duty",
                        "command_mode": "duty",
                        "settle_s": 0.3,
                    }
                },
                "positions": {"conveyor": {"run": 0.6, "stop": 0.0}},
            }
        )

        assert table.axis("conveyor").command_mode is ControlMode.DUTY
        assert table.axis("conveyor").settle_s == pytest.approx(0.3)

    def test_velocity_mode(self) -> None:
        table = load_position_table(
            {
                "axes": {"conveyor": {"command_mode": "velocity"}},
                "positions": {"conveyor": {"run": 100.0}},
            }
        )

        assert table.axis("conveyor").command_mode is ControlMode.VELOCITY

    def test_on_off_mode_with_settle(self) -> None:
        table = load_position_table(
            {
                "axes": {
                    "valve_1": {
                        "unit": "on_off",
                        "command_unit": "on_off",
                        "command_mode": "on_off",
                        "settle_s": 0.2,
                    }
                },
                "positions": {"valve_1": {"open": 1.0, "closed": 0.0}},
            }
        )

        assert table.axis("valve_1").command_mode is ControlMode.ON_OFF
        assert table.axis("valve_1").settle_s == pytest.approx(0.2)
        assert table.commands("valve_1", "open") == {"valve_1": 1.0}
        assert table.commands("valve_1", "closed") == {"valve_1": 0.0}

    def test_on_off_axis_rejects_manual_range(self) -> None:
        with pytest.raises(ValueError, match="command_mode: position"):
            load_position_table(
                {
                    "axes": {
                        "valve_1": {
                            "unit": "on_off",
                            "command_unit": "on_off",
                            "command_mode": "on_off",
                            "manual": {"min": 0.0, "max": 1.0},
                        }
                    },
                    "positions": {"valve_1": {"open": 1.0, "closed": 0.0}},
                }
            )

    def test_current_mode_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="command_mode"):
            load_position_table({"axes": {"conveyor": {"command_mode": "current"}}})

    def test_unknown_mode_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="command_mode"):
            load_position_table({"axes": {"conveyor": {"command_mode": "torque"}}})


class TestAxisSchemaValidation:
    def test_motors_with_axis_level_scale_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="motors"):
            load_position_table(
                {"axes": {"y_axis": {"scale": 2.0, "motors": {"y_axis_r": {"scale": 2.0}}}}}
            )

    def test_motors_with_axis_level_offset_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="motors"):
            load_position_table(
                {"axes": {"y_axis": {"offset": 1.0, "motors": {"y_axis_r": {"scale": 2.0}}}}}
            )

    def test_empty_motors_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="motors"):
            load_position_table({"axes": {"y_axis": {"motors": {}}}})

    def test_unknown_key_under_motor_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="invert"):
            load_position_table(
                {"axes": {"y_axis": {"motors": {"y_axis_r": {"scale": 2.0, "invert": True}}}}}
            )

    def test_zero_scale_on_any_motor_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="scale"):
            load_position_table(
                {
                    "axes": {
                        "y_axis": {
                            "motors": {
                                "y_axis_r": {"scale": 2.0},
                                "y_axis_l": {"scale": 0.0},
                            }
                        }
                    }
                }
            )

    def test_sync_tolerance_on_single_motor_axis_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="sync_tolerance"):
            load_position_table({"axes": {"gripper": {"scale": 1.0, "sync_tolerance": 2.0}}})

    def test_negative_sync_tolerance_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="sync_tolerance"):
            load_position_table(
                {
                    "axes": {
                        "y_axis": {
                            "sync_tolerance": -1.0,
                            "motors": {"a": {"scale": 1.0}, "b": {"scale": -1.0}},
                        }
                    }
                }
            )

    def test_negative_settle_s_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="settle_s"):
            load_position_table({"axes": {"conveyor": {"settle_s": -0.1}}})

    def test_motors_must_be_mapping(self) -> None:
        with pytest.raises(ValueError, match="motors"):
            load_position_table({"axes": {"y_axis": {"motors": ["y_axis_r"]}}})

    def test_motor_entry_must_be_mapping(self) -> None:
        with pytest.raises(ValueError, match="y_axis_r"):
            load_position_table({"axes": {"y_axis": {"motors": {"y_axis_r": 2.0}}}})


class TestManualSpec:
    def test_manual_を書かない軸は連続操作の対象外(self) -> None:
        table = _table()
        assert table.axis("lift_motor").manual is None
        assert table.manual_axes() == ()

    def test_manual_を書いた軸だけが連続操作の対象になる(self) -> None:
        table = _table(
            axes={
                "lift_motor": {
                    "unit": "mm",
                    "command_unit": "deg",
                    "scale": 864.0,
                    "manual": {"min": -2.0, "max": 20.0, "steps": [0.5, 2.0]},
                },
                "arm_joint": {"unit": "deg", "command_unit": "rad", "scale": math.pi / 180.0},
            }
        )
        manual = table.axis("lift_motor").manual
        assert manual is not None
        assert (manual.min_value, manual.max_value) == (-2.0, 20.0)
        assert manual.steps == (0.5, 2.0)
        assert table.manual_axes() == ("lift_motor",)

    def test_steps_を省くと既定のジョグ量が入る(self) -> None:
        table = _table(
            axes={
                "lift_motor": {
                    "unit": "mm",
                    "command_unit": "deg",
                    "scale": 864.0,
                    "manual": {"min": 0.0, "max": 20.0},
                },
                "arm_joint": {"unit": "deg", "command_unit": "rad", "scale": math.pi / 180.0},
            }
        )
        manual = table.axis("lift_motor").manual
        assert manual is not None
        assert len(manual.steps) >= 1
        assert all(step > 0 for step in manual.steps)

    def test_clamp_は範囲内へ丸める(self) -> None:
        table = _table(
            axes={
                "lift_motor": {
                    "unit": "mm",
                    "command_unit": "deg",
                    "scale": 864.0,
                    "manual": {"min": -2.0, "max": 20.0},
                },
                "arm_joint": {"unit": "deg", "command_unit": "rad", "scale": math.pi / 180.0},
            }
        )
        manual = table.axis("lift_motor").manual
        assert manual is not None
        assert manual.clamp(-99.0) == -2.0
        assert manual.clamp(99.0) == 20.0
        assert manual.clamp(5.0) == 5.0

    def test_min_が_max_以上なら起動を拒否する(self) -> None:
        with pytest.raises(ValueError, match="min は max より小さい"):
            _table(
                axes={
                    "lift_motor": {
                        "unit": "mm",
                        "scale": 864.0,
                        "manual": {"min": 10.0, "max": 10.0},
                    },
                    "arm_joint": {"unit": "deg", "scale": 1.0},
                }
            )

    def test_duty_軸への_manual_は起動を拒否する(self) -> None:
        with pytest.raises(ValueError, match="command_mode: position"):
            load_position_table(
                {
                    "axes": {
                        "conveyor": {
                            "unit": "duty",
                            "command_mode": "duty",
                            "scale": 1.0,
                            "manual": {"min": -1.0, "max": 1.0},
                        }
                    },
                    "positions": {"conveyor": {"stop": 0.0}},
                },
                source="<test>",
            )

    def test_steps_に非正の値があれば起動を拒否する(self) -> None:
        with pytest.raises(ValueError, match="正の値"):
            _table(
                axes={
                    "lift_motor": {
                        "unit": "mm",
                        "scale": 864.0,
                        "manual": {"min": 0.0, "max": 20.0, "steps": [1.0, 0.0]},
                    },
                    "arm_joint": {"unit": "deg", "scale": 1.0},
                }
            )

    def test_範囲外のプリセット位置があれば起動を拒否する(self) -> None:
        with pytest.raises(ValueError, match="manual の範囲"):
            _table(
                axes={
                    "lift_motor": {
                        "unit": "mm",
                        "scale": 864.0,
                        "manual": {"min": 0.0, "max": 5.0},
                    },
                    "arm_joint": {"unit": "deg", "scale": 1.0},
                }
            )

    def test_コート別のプリセットも両方が範囲内であることを見る(self) -> None:
        with pytest.raises(ValueError, match="manual の範囲"):
            load_position_table(
                {
                    "axes": {
                        "lift_motor": {
                            "unit": "mm",
                            "scale": 1.0,
                            "manual": {"min": 0.0, "max": 10.0},
                        }
                    },
                    "positions": {"lift_motor": {"work": {"red": 5.0, "blue": 50.0}}},
                },
                source="<test>",
            )


class TestManualAlwaysAxis:
    def test_書かない軸は対象外(self) -> None:
        table = _table()
        assert table.axis("lift_motor").manual_always is False
        assert table.manual_always_axes() == ()

    @pytest.mark.parametrize("mode", ["duty", "on_off"])
    def test_到達判定を持たない軸には書ける(self, mode: str) -> None:
        table = load_position_table(
            {
                "axes": {
                    "conveyor": {
                        "unit": mode,
                        "command_mode": mode,
                        "scale": 1.0,
                        "manual_always": True,
                    },
                    "lift_motor": {"unit": "mm", "scale": 1.0},
                },
                "positions": {"conveyor": {"stop": 0.0}, "lift_motor": {"home": 0.0}},
            },
            source="<test>",
        )
        assert table.axis("conveyor").manual_always is True
        assert table.manual_always_axes() == ("conveyor",)

    @pytest.mark.parametrize("mode", ["position", "velocity"])
    def test_位置指令の軸へ書いたら起動を拒否する(self, mode: str) -> None:
        with pytest.raises(ValueError, match="manual_always は duty / on_off"):
            load_position_table(
                {
                    "axes": {
                        "lift_motor": {
                            "unit": "mm",
                            "command_mode": mode,
                            "scale": 1.0,
                            "manual_always": True,
                        }
                    },
                    "positions": {"lift_motor": {"home": 0.0}},
                },
                source="<test>",
            )

    def test_真偽値でなければ起動を拒否する(self) -> None:
        with pytest.raises(ValueError, match="manual_always は真偽値"):
            load_position_table(
                {
                    "axes": {
                        "conveyor": {
                            "unit": "duty",
                            "command_mode": "duty",
                            "scale": 1.0,
                            "manual_always": "yes",
                        }
                    },
                    "positions": {"conveyor": {"stop": 0.0}},
                },
                source="<test>",
            )

    def test_AxisSpec_を直接組み立てても拒否する(self) -> None:
        with pytest.raises(ValueError, match="manual_always は duty / on_off"):
            AxisSpec(
                name="lift_motor",
                unit="mm",
                command_unit="deg",
                timeout_s=1.0,
                tolerance=None,
                motors=(MotorSpec(name="lift_motor", scale=1.0, offset=0.0),),
                manual_always=True,
            )


class TestAxisToValue:
    def test_単一モータ軸は_to_commands_の逆になる(self) -> None:
        spec = _table().axis("lift_motor")
        commands = spec.to_commands(12.5)
        assert spec.to_value(commands) == pytest.approx(12.5)

    def test_逆回転ペアでも符号を落とさず同じ値へ戻る(self) -> None:
        table = load_position_table(
            {
                "axes": {
                    "y_axis": {
                        "unit": "mm",
                        "motors": {
                            "y_axis_r": {"scale": 55.02},
                            "y_axis_l": {"scale": -55.02},
                        },
                    }
                },
                "positions": {"y_axis": {"home": 0.0}},
            },
            source="<test>",
        )
        spec = table.axis("y_axis")
        commands = spec.to_commands(8.0)
        assert commands["y_axis_r"] == pytest.approx(-commands["y_axis_l"])
        assert spec.to_value(commands) == pytest.approx(8.0)

    def test_値が揃わなければ例外にする(self) -> None:
        spec = _table().axis("lift_motor")
        with pytest.raises(PositionLookupError):
            spec.to_value({})


class TestMerged:
    @staticmethod
    def _one(axis: str, position: str) -> PositionTable:
        return load_position_table(
            {
                "axes": {axis: {"unit": "deg", "command_unit": "deg"}},
                "positions": {axis: {position: 1.0}},
            },
            source=f"<{axis}>",
        )

    def test_両方の軸を引ける(self) -> None:
        merged = PositionTable.merged([self._one("y_axis", "home"), self._one("valve_1", "open")])

        assert set(merged.axes) == {"y_axis", "valve_1"}
        assert merged.raw("y_axis", "home") == 1.0
        assert merged.raw("valve_1", "open") == 1.0

    def test_軸名が衝突したら拒否する(self) -> None:
        with pytest.raises(ValueError, match="gripper"):
            PositionTable.merged([self._one("gripper", "open"), self._one("gripper", "closed")])

    def test_出どころを引き継ぐ(self) -> None:
        merged = PositionTable.merged([self._one("y_axis", "home")])
        assert "y_axis" in merged.source

    def test_空でも成立する(self) -> None:
        assert PositionTable.merged([]).axes == ()


class TestSyncGain:
    def _paired(self, **extra: object) -> dict:
        return {
            "axes": {
                "y_axis": {
                    "unit": "mm",
                    "command_unit": "deg",
                    "tolerance": 1.0,
                    "sync_tolerance": 2.0,
                    "motors": {
                        "y_axis_r": {"scale": 55.0},
                        "y_axis_l": {"scale": -55.0},
                    },
                    **extra,
                }
            },
            "positions": {"y_axis": {"home": 0.0}},
        }

    def test_defaults_to_no_correction(self) -> None:
        table = load_position_table(self._paired(), source="<test>")
        spec = table.axis("y_axis")

        assert spec.sync_kp == pytest.approx(0.0)
        assert spec.sync_limit is None

    def test_gain_reaches_the_sync_group(self) -> None:
        table = load_position_table(self._paired(sync_kp=1.5, sync_limit=400.0), source="<test>")
        group = table.axis("y_axis").sync_group

        assert group is not None
        assert group.sync_kp == pytest.approx(1.5)
        assert group.sync_limit == pytest.approx(400.0)

    def test_gain_without_limit_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="sync_limit"):
            load_position_table(self._paired(sync_kp=1.5))

    def test_limit_without_gain_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="sync_limit"):
            load_position_table(self._paired(sync_limit=400.0))

    def test_zero_gain_may_declare_a_limit(self) -> None:
        table = load_position_table(self._paired(sync_kp=0.0, sync_limit=400.0), source="<test>")

        assert table.axis("y_axis").sync_kp == pytest.approx(0.0)
        assert table.axis("y_axis").sync_limit == pytest.approx(400.0)

    def test_negative_gain_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="sync_kp"):
            load_position_table(self._paired(sync_kp=-1.0, sync_limit=400.0))

    def test_negative_limit_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="sync_limit"):
            load_position_table(self._paired(sync_kp=1.0, sync_limit=-1.0))

    def test_gain_on_single_motor_axis_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="sync_kp"):
            load_position_table(
                {"axes": {"gripper": {"scale": 1.0, "sync_kp": 1.0, "sync_limit": 100.0}}}
            )

    def test_gain_without_sync_tolerance_is_rejected(self) -> None:
        config = {
            "axes": {
                "y_axis": {
                    "motors": {
                        "y_axis_r": {"scale": 55.0},
                        "y_axis_l": {"scale": -55.0},
                    },
                    "sync_kp": 1.0,
                    "sync_limit": 400.0,
                }
            }
        }
        with pytest.raises(ValueError, match="sync_tolerance"):
            load_position_table(config)

    def test_unknown_key_is_still_rejected(self) -> None:
        with pytest.raises(ValueError, match="sync_gain"):
            load_position_table(self._paired(sync_gain=1.0))


class TestMotionSpec:
    def _axis(self, *, motion: object, **extra: object) -> dict:
        return {
            "axes": {
                "y_axis": {
                    "unit": "mm",
                    "command_unit": "deg",
                    "scale": 55.0,
                    "motion": motion,
                    **extra,
                }
            },
            "positions": {"y_axis": {"home": 0.0, "work": 15.0}},
        }

    def _suggested_timeout(self, message: str) -> float:
        found = re.search(r"timeout_s を ([0-9.]+) 以上", message)
        assert found is not None, message
        return float(found.group(1))

    def test_motion_を書かない軸は従来どおりステップ入力(self) -> None:
        table = _table()

        assert table.axis("lift_motor").motion is None

    def test_値がそのまま_MotionSpec_へ届く(self) -> None:
        table = load_position_table(
            self._axis(motion={"max_velocity": 60.0, "max_acceleration": 400.0}),
            source="<test>",
        )
        motion = table.axis("y_axis").motion

        assert motion is not None
        assert motion.max_velocity == pytest.approx(60.0)
        assert motion.max_acceleration == pytest.approx(400.0)
        assert motion.velocity_ff == pytest.approx(0.0)

    def test_velocity_ff_を指定できる(self) -> None:
        table = load_position_table(
            self._axis(
                motion={"max_velocity": 60.0, "max_acceleration": 400.0, "velocity_ff": 1.5}
            ),
            source="<test>",
        )
        motion = table.axis("y_axis").motion

        assert motion is not None
        assert motion.velocity_ff == pytest.approx(1.5)

    def test_max_velocity_だけの指定は拒否する(self) -> None:
        with pytest.raises(ValueError, match="max_acceleration"):
            load_position_table(self._axis(motion={"max_velocity": 60.0}))

    def test_max_acceleration_だけの指定は拒否する(self) -> None:
        with pytest.raises(ValueError, match="max_velocity"):
            load_position_table(self._axis(motion={"max_acceleration": 400.0}))

    def test_非正の_max_velocity_は拒否する(self) -> None:
        with pytest.raises(ValueError, match="max_velocity"):
            load_position_table(self._axis(motion={"max_velocity": 0.0, "max_acceleration": 400.0}))

    def test_非正の_max_acceleration_は拒否する(self) -> None:
        with pytest.raises(ValueError, match="max_acceleration"):
            load_position_table(self._axis(motion={"max_velocity": 60.0, "max_acceleration": -1.0}))

    def test_負の_velocity_ff_は拒否する(self) -> None:
        with pytest.raises(ValueError, match="velocity_ff"):
            load_position_table(
                self._axis(
                    motion={
                        "max_velocity": 60.0,
                        "max_acceleration": 400.0,
                        "velocity_ff": -1.0,
                    }
                )
            )

    def test_未知のキーは拒否する(self) -> None:
        with pytest.raises(ValueError, match="max_vel"):
            load_position_table(self._axis(motion={"max_vel": 60.0, "max_acceleration": 400.0}))

    def test_辞書でない_motion_は拒否する(self) -> None:
        with pytest.raises(ValueError, match="辞書"):
            load_position_table(self._axis(motion=60.0))

    def test_位置指令でない軸への_motion_は拒否する(self) -> None:
        config = {
            "axes": {
                "conveyor": {
                    "scale": 1.0,
                    "command_mode": "duty",
                    "settle_s": 0.5,
                    "motion": {"max_velocity": 60.0, "max_acceleration": 400.0},
                }
            },
            "positions": {"conveyor": {"stop": 0.0, "run": 0.6}},
        }
        with pytest.raises(ValueError, match="位置指令"):
            load_position_table(config)

    def test_timeout_s_に収まる設定は通る(self) -> None:
        table = load_position_table(
            self._axis(motion={"max_velocity": 60.0, "max_acceleration": 400.0}),
            source="<test>",
        )

        assert table.axis("y_axis").motion is not None

    def test_必ずタイムアウトする軸は起動を拒否する(self) -> None:
        with pytest.raises(ValueError) as exc:
            load_position_table(
                self._axis(
                    motion={"max_velocity": 10.0, "max_acceleration": 400.0},
                    timeout_s=1.0,
                ),
                source="<test>",
            )

        message = str(exc.value)
        assert "timeout_s" in message
        assert self._suggested_timeout(message) >= 1.525

    def test_提示された_timeout_s_をそのまま書けば通る(self) -> None:
        with pytest.raises(ValueError) as exc:
            load_position_table(
                self._axis(
                    motion={"max_velocity": 10.0, "max_acceleration": 400.0},
                    timeout_s=1.0,
                ),
                source="<test>",
            )
        suggested = self._suggested_timeout(str(exc.value))

        table = load_position_table(
            self._axis(
                motion={"max_velocity": 10.0, "max_acceleration": 400.0},
                timeout_s=suggested,
            ),
            source="<test>",
        )

        assert table.axis("y_axis").motion is not None

    def test_三角プロファイルの所要時間で判定する(self) -> None:
        table = load_position_table(
            self._axis(
                motion={"max_velocity": 1000.0, "max_acceleration": 400.0},
                timeout_s=1.0,
            ),
            source="<test>",
        )

        assert table.axis("y_axis").motion is not None

    def test_コート別の位置も最大移動距離に数える(self) -> None:
        config = {
            "axes": {
                "y_axis": {
                    "unit": "mm",
                    "scale": 55.0,
                    "timeout_s": 1.0,
                    "motion": {"max_velocity": 10.0, "max_acceleration": 400.0},
                }
            },
            "positions": {"y_axis": {"pick": {"blue": 0.0, "red": 30.0}}},
        }
        with pytest.raises(ValueError, match="timeout_s"):
            load_position_table(config, source="<test>")

    def test_位置定数を持たない軸は距離が決まらないので通る(self) -> None:
        table = load_position_table(
            {
                "axes": {
                    "y_axis": {
                        "scale": 55.0,
                        "timeout_s": 0.1,
                        "motion": {"max_velocity": 1.0, "max_acceleration": 1.0},
                    }
                }
            },
            source="<test>",
        )

        assert table.axis("y_axis").motion is not None


class TestHomingSensorMap:
    @staticmethod
    def _paired(**homing_overrides: object) -> dict:
        homing: dict = {
            "sensors": {
                "y_axis_r": "y_axis_r_origin_sensor",
                "y_axis_l": "y_axis_l_origin_sensor",
            },
            "direction": -1,
            "search_distance": 650.0,
            "align_distance": 5.0,
            "step": 0.5,
            "settle_s": 0.05,
        }
        homing.update(homing_overrides)
        for key in [name for name, value in homing.items() if value is None]:
            del homing[key]
        return {
            "axes": {
                "y_axis": {
                    "unit": "mm",
                    "command_unit": "deg",
                    "tolerance": 1.0,
                    "sync_tolerance": 10.0,
                    "motors": {
                        "y_axis_r": {"scale": 55.0},
                        "y_axis_l": {"scale": -55.0},
                    },
                    "homing": homing,
                }
            },
            "positions": {"y_axis": {"home": 0.0}},
        }

    def test_モータごとのセンサを宣言順で読める(self) -> None:
        homing = load_position_table(self._paired(), source="<test>").axis("y_axis").homing

        assert homing is not None
        assert homing.sensor is None
        assert dict(homing.sensors or {}) == {
            "y_axis_r": "y_axis_r_origin_sensor",
            "y_axis_l": "y_axis_l_origin_sensor",
        }
        assert homing.sensor_names == ("y_axis_r_origin_sensor", "y_axis_l_origin_sensor")
        assert homing.align_distance == pytest.approx(5.0)

    def test_単数形の設定は今までどおり読める(self) -> None:
        table = load_position_table(
            {
                "axes": {
                    "rotate": {
                        "unit": "deg",
                        "homing": {
                            "sensor": "rotate_origin_sensor",
                            "direction": -1,
                            "search_distance": 180.0,
                            "step": 1.0,
                            "settle_s": 0.05,
                        },
                    }
                },
                "positions": {"rotate": {"home": 0.0}},
            },
            source="<test>",
        )
        homing = table.axis("rotate").homing

        assert homing is not None
        assert homing.sensor == "rotate_origin_sensor"
        assert homing.sensors is None
        assert homing.sensor_names == ("rotate_origin_sensor",)
        assert homing.align_distance is None

    def test_対応表は書き換えられない(self) -> None:
        homing = load_position_table(self._paired(), source="<test>").axis("y_axis").homing

        assert homing is not None
        assert homing.sensors is not None
        with pytest.raises(TypeError):
            homing.sensors["y_axis_r"] = "別のセンサ"  # type: ignore[index]

    def test_sensor_と_sensors_の併記は拒否する(self) -> None:
        with pytest.raises(ValueError, match="併記"):
            load_position_table(self._paired(sensor="y_axis_r_origin_sensor"), source="<test>")

    def test_どちらも書かなければ拒否する(self) -> None:
        with pytest.raises(ValueError, match="sensors"):
            load_position_table(self._paired(sensors=None, align_distance=None), source="<test>")

    def test_空の_sensors_は拒否する(self) -> None:
        with pytest.raises(ValueError, match="空"):
            load_position_table(self._paired(sensors={}), source="<test>")

    def test_sensors_が辞書でなければ型を示して拒否する(self) -> None:
        with pytest.raises(ValueError, match="辞書"):
            load_position_table(self._paired(sensors=["y_axis_r_origin_sensor"]), source="<test>")

    def test_センサ名が文字列でなければ拒否する(self) -> None:
        with pytest.raises(ValueError, match="y_axis_r"):
            load_position_table(self._paired(sensors={"y_axis_r": 3}), source="<test>")

    def test_sensors_に_align_distance_が無ければ拒否する(self) -> None:
        with pytest.raises(ValueError, match="align_distance"):
            load_position_table(self._paired(align_distance=None), source="<test>")

    def test_sensors_の無い軸の_align_distance_は拒否する(self) -> None:
        config = {
            "axes": {
                "rotate": {
                    "unit": "deg",
                    "homing": {
                        "sensor": "rotate_origin_sensor",
                        "direction": -1,
                        "search_distance": 180.0,
                        "step": 1.0,
                        "align_distance": 5.0,
                    },
                }
            },
        }
        with pytest.raises(ValueError, match="align_distance"):
            load_position_table(config, source="<test>")

    def test_非正の_align_distance_は拒否する(self) -> None:
        with pytest.raises(ValueError, match="align_distance"):
            load_position_table(self._paired(align_distance=0.0), source="<test>")

    def test_step_より小さい_align_distance_は拒否する(self) -> None:
        with pytest.raises(ValueError, match="align_distance"):
            load_position_table(self._paired(step=2.0, align_distance=1.0), source="<test>")

    def test_モータが不足していれば拒否する(self) -> None:
        with pytest.raises(ValueError, match="y_axis_l"):
            load_position_table(
                self._paired(sensors={"y_axis_r": "y_axis_r_origin_sensor"}), source="<test>"
            )

    def test_余分なモータ名があれば拒否する(self) -> None:
        sensors = {
            "y_axis_r": "y_axis_r_origin_sensor",
            "y_axis_l": "y_axis_l_origin_sensor",
            "y_axis_x": "y_axis_x_origin_sensor",
        }
        with pytest.raises(ValueError, match="y_axis_x"):
            load_position_table(self._paired(sensors=sensors), source="<test>")

    def test_align_distance_が_sync_tolerance_以上なら拒否する(self) -> None:
        with pytest.raises(ValueError, match="sync_tolerance"):
            load_position_table(self._paired(align_distance=10.5), source="<test>")

    def test_境界_align_distance_が_sync_tolerance_と等しくても拒否する(self) -> None:
        with pytest.raises(ValueError, match="sync_tolerance"):
            load_position_table(self._paired(align_distance=10.0), source="<test>")

    def test_sync_tolerance_の内側なら通る(self) -> None:
        table = load_position_table(self._paired(align_distance=9.999), source="<test>")

        assert table.axis("y_axis").homing is not None


class TestLimitSpec:
    """リミットスイッチ保護の宣言 (axes.<軸>.limits)。

    「そのスイッチに触れたら、そのスイッチのある側へ進む指令を止める」ことだけを
    宣言する。検証は「書いたのに効かない保護」を読み込みの時点で潰すためにある ——
    保護は平常時には一切姿を現さないので、効いていないことは機構が壊れるまで分からない。

    **どの検証も起動そのものは止めない** (`main._load_position_table_file` が
    読み込み失敗を「定数なしで起動」へ倒す) ので、config が拒まれた結果は
    「そのロボットの軸が 0 本になる」形で現れる。
    """

    def _axis(self, *, limits: object, **extra: object) -> dict:
        return {
            "axes": {
                "y_axis": {
                    "unit": "mm",
                    "command_unit": "deg",
                    "scale": 55.0,
                    "limits": limits,
                    **extra,
                }
            },
            "positions": {"y_axis": {"home": 0.0, "work": 15.0}},
        }

    def test_limits_を書かない軸は保護なし(self) -> None:
        table = _table()

        assert table.axis("lift_motor").limits == ()
        assert table.limit_axes() == ()

    def test_1_本の宣言がそのまま届く(self) -> None:
        table = load_position_table(
            self._axis(limits=[{"sensor": "y_axis_r_origin_sensor", "direction": -1}]),
            source="<test>",
        )
        limits = table.limits("y_axis")

        assert len(limits) == 1
        assert limits[0].sensor == "y_axis_r_origin_sensor"
        assert limits[0].direction == pytest.approx(-1.0)
        assert table.limit_axes() == ("y_axis",)

    def test_同じ軸に_2_本書ける(self) -> None:
        """左右直結ペアはモータごとにスイッチが 1 本ずつ付く (向きは同じ)。"""
        table = load_position_table(
            self._axis(
                limits=[
                    {"sensor": "y_axis_r_origin_sensor", "direction": -1},
                    {"sensor": "y_axis_l_origin_sensor", "direction": -1},
                ]
            ),
            source="<test>",
        )
        limits = table.limits("y_axis")

        assert [limit.sensor for limit in limits] == [
            "y_axis_r_origin_sensor",
            "y_axis_l_origin_sensor",
        ]

    def test_両端のスイッチは向きを分けて書ける(self) -> None:
        table = load_position_table(
            self._axis(
                limits=[
                    {"sensor": "sub_y_axis_f_limit_sensor", "direction": 1},
                    {"sensor": "sub_y_axis_r_limit_sensor", "direction": -1},
                ]
            ),
            source="<test>",
        )

        assert [limit.direction for limit in table.limits("y_axis")] == [1.0, -1.0]

    def test_direction_が_プラスマイナス1_以外なら拒否する(self) -> None:
        """0 は「どちらも止めない」、それ以外は端の側を表せない。"""
        with pytest.raises(ValueError, match="direction"):
            load_position_table(self._axis(limits=[{"sensor": "sw", "direction": 0}]))
        with pytest.raises(ValueError, match="direction"):
            load_position_table(self._axis(limits=[{"sensor": "sw", "direction": 2}]))

    def test_direction_の省略は拒否する(self) -> None:
        """既定値で埋めると「どちら側の端か」を config が決めていない保護になる。"""
        with pytest.raises(ValueError, match="direction"):
            load_position_table(self._axis(limits=[{"sensor": "sw"}]))

    def test_sensor_の省略は拒否する(self) -> None:
        """センサを省くと、どのスイッチも見ない保護が config に書ける。"""
        with pytest.raises(ValueError, match="sensor"):
            load_position_table(self._axis(limits=[{"direction": -1}]))

    def test_空のセンサ名は拒否する(self) -> None:
        with pytest.raises(ValueError, match="sensor"):
            load_position_table(self._axis(limits=[{"sensor": "", "direction": -1}]))

    def test_未知のキーは拒否する(self) -> None:
        """綴り間違いが「黙って無視される保護」にならないこと。"""
        with pytest.raises(ValueError, match="sensor_name"):
            load_position_table(
                self._axis(limits=[{"sensor_name": "sw", "sensor": "sw", "direction": -1}])
            )

    def test_空のリストは拒否する(self) -> None:
        """`limits: []` は書き忘れと区別が付かない。"""
        with pytest.raises(ValueError, match="limits"):
            load_position_table(self._axis(limits=[]))

    def test_リストでなければ拒否する(self) -> None:
        with pytest.raises(ValueError, match="リスト"):
            load_position_table(self._axis(limits={"sensor": "sw", "direction": -1}))

    def test_要素が辞書でなければ拒否する(self) -> None:
        with pytest.raises(ValueError, match="辞書"):
            load_position_table(self._axis(limits=["sw"]))

    def test_同じセンサの重複は拒否する(self) -> None:
        """片方を直しても、もう片方が残る形にしかならない。"""
        with pytest.raises(ValueError, match="sw"):
            load_position_table(
                self._axis(
                    limits=[
                        {"sensor": "sw", "direction": -1},
                        {"sensor": "sw", "direction": 1},
                    ]
                )
            )

    def test_duty_軸への_limits_は拒否する(self) -> None:
        """現在位置も到達も観測できない軸では「これ以上進めない」が成立しない。"""
        config = {
            "axes": {
                "conveyor": {
                    "scale": 1.0,
                    "command_mode": "duty",
                    "settle_s": 0.5,
                    "limits": [{"sensor": "sw", "direction": -1}],
                }
            },
            "positions": {"conveyor": {"stop": 0.0, "run": 0.6}},
        }
        with pytest.raises(ValueError, match="位置指令"):
            load_position_table(config)

    def test_on_off_軸への_limits_は拒否する(self) -> None:
        config = {
            "axes": {
                "valve": {
                    "scale": 1.0,
                    "command_mode": "on_off",
                    "settle_s": 0.2,
                    "limits": [{"sensor": "sw", "direction": 1}],
                }
            },
            "positions": {"valve": {"closed": 0.0, "open": 1.0}},
        }
        with pytest.raises(ValueError, match="位置指令"):
            load_position_table(config)

    def test_homing_を持たない軸にも書ける(self) -> None:
        """零点確定と保護は別物 (sub_* は 4 本のスイッチを持つが homing を持たない)。"""
        table = load_position_table(
            self._axis(limits=[{"sensor": "sub_lift_b_limit_sensor", "direction": -1}]),
            source="<test>",
        )

        assert table.axis("y_axis").homing is None
        assert len(table.limits("y_axis")) == 1

    def _homed(self, *, limit_direction: float, homing_direction: float = -1) -> dict:
        """同じスイッチを homing と limits の両方が指す軸。"""
        return self._axis(
            limits=[{"sensor": "origin_sensor", "direction": limit_direction}],
            homing={
                "sensor": "origin_sensor",
                "direction": homing_direction,
                "search_distance": 30.0,
                "step": 1.0,
            },
        )

    def test_同じセンサを指す_homing_と_limits_は同じ向きなら通る(self) -> None:
        table = load_position_table(self._homed(limit_direction=-1), source="<test>")

        assert table.axis("y_axis").homing is not None
        assert len(table.limits("y_axis")) == 1

    def test_同じセンサで向きが食い違えばこのファイルを読めなくする(self) -> None:
        """食い違うと「零点確定に向かう向きの指令が保護で止まる」形で必ず失敗する。
        しかも零点確定の最中はその保護を外しているので、**症状が出るのは零点確定が
        終わった後**であり、切り分けが難しい。

        **起動そのものは止まらない** —— `main._load_position_table_file` が
        読み込み失敗を「定数なしで起動」へ倒すので、実際に起きるのはこのロボットの
        位置定数がまるごと消えることである。
        """
        with pytest.raises(ValueError, match="direction"):
            load_position_table(self._homed(limit_direction=1))

    def test_homing_が見ないセンサの向きは自由(self) -> None:
        """反対端のリミットは零点確定と無関係なので、逆向きで正しい。"""
        table = load_position_table(
            self._axis(
                limits=[
                    {"sensor": "origin_sensor", "direction": -1},
                    {"sensor": "far_end_sensor", "direction": 1},
                ],
                homing={
                    "sensor": "origin_sensor",
                    "direction": -1,
                    "search_distance": 30.0,
                    "step": 1.0,
                },
            ),
            source="<test>",
        )

        assert [limit.direction for limit in table.limits("y_axis")] == [-1.0, 1.0]

    def test_複数センサの_homing_でも向きを突き合わせる(self) -> None:
        """`homing.sensors` (左右に 1 本ずつ) でも見る対象は変わらない。"""
        config = {
            "axes": {
                "y_axis": {
                    "unit": "mm",
                    "motors": {"y_axis_r": {"scale": 55.0}, "y_axis_l": {"scale": -55.0}},
                    "limits": [
                        {"sensor": "sensor_r", "direction": -1},
                        {"sensor": "sensor_l", "direction": 1},
                    ],
                    "homing": {
                        "sensors": {"y_axis_r": "sensor_r", "y_axis_l": "sensor_l"},
                        "direction": -1,
                        "search_distance": 30.0,
                        "step": 1.0,
                        "align_distance": 5.0,
                    },
                }
            },
            "positions": {"y_axis": {"home": 0.0}},
        }
        with pytest.raises(ValueError, match="sensor_l"):
            load_position_table(config)

    def test_LimitSpec_を直接組んでも同じ規則が効く(self) -> None:
        """yaml を経由しない組み立てにだけ緩い規則が効く状態を作らない。"""
        with pytest.raises(ValueError, match="direction"):
            LimitSpec(sensor="sw", direction=0.0)
        with pytest.raises(ValueError, match="sensor"):
            LimitSpec(sensor="", direction=1.0)


class TestToCommandsEach:
    @staticmethod
    def _spec() -> AxisSpec:
        table = load_position_table(
            {
                "axes": {
                    "y_axis": {
                        "unit": "mm",
                        "motors": {
                            "y_axis_r": {"scale": 55.0, "offset": 3.0},
                            "y_axis_l": {"scale": -55.0, "offset": -3.0},
                        },
                    }
                },
                "positions": {"y_axis": {"home": 0.0}},
            },
            source="<test>",
        )
        return table.axis("y_axis")

    def test_モータごとに違う値を換算する(self) -> None:
        commands = self._spec().to_commands_each({"y_axis_r": 2.0, "y_axis_l": 5.0})

        assert commands["y_axis_r"] == pytest.approx(2.0 * 55.0 + 3.0)
        assert commands["y_axis_l"] == pytest.approx(5.0 * -55.0 - 3.0)

    def test_同じ値を渡せば_to_commands_と一致する(self) -> None:
        spec = self._spec()

        assert spec.to_commands_each({"y_axis_r": 4.0, "y_axis_l": 4.0}) == pytest.approx(
            spec.to_commands(4.0)
        )

    def test_モータが不足していれば_KeyError(self) -> None:
        with pytest.raises(KeyError, match="y_axis_l"):
            self._spec().to_commands_each({"y_axis_r": 1.0})

    def test_知らないモータ名があれば_KeyError(self) -> None:
        with pytest.raises(KeyError, match="y_axis_x"):
            self._spec().to_commands_each({"y_axis_r": 1.0, "y_axis_l": 1.0, "y_axis_x": 1.0})
