from __future__ import annotations

import math
import re
from typing import ClassVar

import pytest

from lib.drivers.base import ControlMode
from lib.match_state import Court
from lib.motion_guard import RequiredRange
from lib.sequence.positions import (
    DEFAULT_TIMEOUT_S,
    AxisSpec,
    CourtMotorSpec,
    CourtUnresolvedError,
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


_COURT_SCALE = {"blue": 2.0, "red": -2.0}


def _court_table(**axis_overrides: object) -> PositionTable:
    axis: dict = {"unit": "mm", "command_unit": "rad", "scale": dict(_COURT_SCALE)}
    axis.update(axis_overrides)
    return load_position_table(
        {"axes": {"lift": axis}, "positions": {"lift": {"home": -5.0, "up": -20.0}}},
        source="<test>",
    )


class TestCourtScale:
    def test_float_scale_is_court_independent(self) -> None:
        spec = _table().axis("lift_motor")

        assert spec.court_dependent is False
        assert spec.for_court(Court.RED) is spec
        assert spec.for_court(Court.BLUE) is spec

    def test_mapping_scale_resolves_sign_per_court(self) -> None:
        spec = _court_table().axis("lift")

        assert spec.court_dependent is True
        assert spec.for_court(Court.BLUE).to_commands(10.0) == {"lift": pytest.approx(20.0)}
        assert spec.for_court(Court.RED).to_commands(10.0) == {"lift": pytest.approx(-20.0)}
        assert spec.for_court(Court.RED).to_value({"lift": -20.0}) == pytest.approx(10.0)

    def test_table_commands_resolve_court_scale(self) -> None:
        table = _court_table()

        assert table.commands("lift", "up", court=Court.RED) == {"lift": pytest.approx(40.0)}
        with pytest.raises(CourtUnresolvedError):
            table.commands("lift", "up")

    def test_resolved_spec_is_not_court_dependent(self) -> None:
        resolved = _court_table().axis("lift").for_court(Court.RED)

        assert resolved.court_dependent is False
        assert isinstance(resolved.motors[0], MotorSpec)
        assert not isinstance(resolved.motors[0], CourtMotorSpec)

    def test_per_motor_scale_accepts_mapping(self) -> None:
        table = load_position_table(
            {
                "axes": {
                    "lift": {
                        "motors": {
                            "lift_r": {"scale": dict(_COURT_SCALE)},
                            "lift_l": {"scale": 1.0, "offset": 3.0},
                        }
                    }
                }
            }
        )
        spec = table.axis("lift")

        assert spec.court_dependent is True
        assert spec.for_court(Court.RED).to_commands(1.0) == {
            "lift_r": pytest.approx(-2.0),
            "lift_l": pytest.approx(4.0),
        }

    @pytest.mark.parametrize("method", ["to_commands", "to_commands_each", "to_value"])
    def test_unresolved_conversion_raises(self, method: str) -> None:
        spec = _court_table().axis("lift")
        arg: object = 1.0 if method == "to_commands" else {"lift": 1.0}

        with pytest.raises(CourtUnresolvedError, match="lift"):
            getattr(spec, method)(arg)

    def test_unresolved_motor_conversion_raises(self) -> None:
        motor = _court_table().axis("lift").motors[0]

        assert motor.scale == pytest.approx(2.0)
        with pytest.raises(CourtUnresolvedError):
            motor.to_command(1.0)
        with pytest.raises(CourtUnresolvedError):
            motor.to_value(1.0)
        with pytest.raises(CourtUnresolvedError):
            motor.to_tolerance(1.0)

    def test_startup_checks_do_not_depend_on_court(self) -> None:
        table = _court_table(
            manual={"min": -30.0, "max": 0.0},
            motion={"max_velocity": 100.0, "max_acceleration": 1000.0},
            timeout_s=1.0,
        )

        assert table.axis("lift").manual is not None

    def test_missing_court_key_is_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"axes\.lift\.scale.*red"):
            _court_table(scale={"blue": 2.0})

    def test_unknown_court_key_is_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"axes\.lift\.scale.*green"):
            _court_table(scale={**_COURT_SCALE, "green": 2.0})

    def test_non_numeric_court_scale_is_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"axes\.lift\.scale.*数値"):
            _court_table(scale={"blue": 2.0, "red": "minus"})

    def test_zero_court_scale_is_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"axes\.lift\.scale.*0"):
            _court_table(scale={"blue": 2.0, "red": 0.0})

    def test_unequal_magnitude_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="大きさ"):
            _court_table(scale={"blue": 2.0, "red": -3.0})

    def test_per_motor_mapping_errors_name_the_motor(self) -> None:
        with pytest.raises(ValueError, match=r"axes\.lift\.motors\.lift_r\.scale"):
            load_position_table({"axes": {"lift": {"motors": {"lift_r": {"scale": {"red": 1.0}}}}}})

    def test_sync_tolerance_with_court_scale_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="sync_tolerance とコート別の scale"):
            load_position_table(
                {
                    "axes": {
                        "lift": {
                            "sync_tolerance": 1.0,
                            "motors": {
                                "lift_r": {"scale": dict(_COURT_SCALE)},
                                "lift_l": {"scale": {"blue": -2.0, "red": 2.0}},
                            },
                        }
                    }
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


class TestTravelSpec:
    """機械的可動域。**`manual` の流用ではない。**

    意味の違うものを 1 つの値に載せると、手動の範囲を狭めた瞬間に回転数の
    一意化が黙って別の中心を選ぶ。
    """

    def test_travel_を書かない軸は一意化の対象外(self) -> None:
        assert _table().axis("lift_motor").travel is None

    def test_travel_を書いた軸だけが可動域を持つ(self) -> None:
        table = _table(
            axes={
                "lift_motor": {
                    "unit": "mm",
                    "scale": 864.0,
                    "manual": {"min": 0.0, "max": 10.0},
                    "travel": {"min": -2.0, "max": 20.0},
                },
                "arm_joint": {"unit": "deg", "scale": 1.0},
            }
        )

        travel = table.axis("lift_motor").travel
        assert travel is not None
        assert (travel.min_value, travel.max_value) == (-2.0, 20.0)
        assert travel.span == pytest.approx(22.0)

    def test_manual_とは独立に持てる(self) -> None:
        """手動の範囲を狭めても機械的可動域は動かない。"""
        table = _table(
            axes={
                "lift_motor": {
                    "unit": "mm",
                    "scale": 864.0,
                    "manual": {"min": 0.0, "max": 12.0},
                    "travel": {"min": -2.0, "max": 20.0},
                },
                "arm_joint": {"unit": "deg", "scale": 1.0},
            }
        )

        spec = table.axis("lift_motor")
        assert spec.manual is not None and spec.travel is not None
        assert (spec.manual.min_value, spec.manual.max_value) == (0.0, 12.0)
        assert (spec.travel.min_value, spec.travel.max_value) == (-2.0, 20.0)

    def test_min_が_max_以上なら起動を拒否する(self) -> None:
        with pytest.raises(ValueError, match="min は max より小さい"):
            _table(
                axes={
                    "lift_motor": {
                        "unit": "mm",
                        "scale": 864.0,
                        "travel": {"min": 10.0, "max": 10.0},
                    },
                    "arm_joint": {"unit": "deg", "scale": 1.0},
                }
            )

    def test_片側だけなら起動を拒否する(self) -> None:
        with pytest.raises(ValueError, match="機械的可動域が決まりません"):
            _table(
                axes={
                    "lift_motor": {"unit": "mm", "scale": 864.0, "travel": {"min": 0.0}},
                    "arm_joint": {"unit": "deg", "scale": 1.0},
                }
            )

    def test_未知のキーは起動を拒否する(self) -> None:
        with pytest.raises(ValueError, match="travel に未知のキー"):
            _table(
                axes={
                    "lift_motor": {
                        "unit": "mm",
                        "scale": 864.0,
                        "travel": {"min": 0.0, "max": 1.0, "steps": [1.0]},
                    },
                    "arm_joint": {"unit": "deg", "scale": 1.0},
                }
            )

    def test_duty_軸への_travel_は起動を拒否する(self) -> None:
        with pytest.raises(ValueError, match="travel は位置指令の軸にのみ"):
            load_position_table(
                {
                    "axes": {
                        "conveyor": {
                            "unit": "duty",
                            "command_mode": "duty",
                            "scale": 1.0,
                            "travel": {"min": -1.0, "max": 1.0},
                        }
                    },
                    "positions": {"conveyor": {"stop": 0.0}},
                },
                source="<test>",
            )


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


class TestBorrowedAxis:
    """複数のロボットから指令する軸 (`config/system.yaml` の `shared_axes`)。

    **貸し先は持ち主の表を引き続ける** —— 値を写すと持ち主側だけを読み直したときに
    同じ位置が 2 つに割れ、どちらの機体から指令したかで行き先が変わる。
    """

    @staticmethod
    def _one(axis: str, value: float, *, source: str) -> PositionTable:
        return load_position_table(
            {
                "axes": {axis: {"unit": "deg", "command_unit": "deg"}},
                "positions": {axis: {"open": value}},
            },
            source=source,
        )

    def _pair(self) -> tuple[PositionTable, PositionTable]:
        owner = self._one("wall_f", 80.0, source="<owner>")
        borrower = self._one("valve_1", 1.0, source="<borrower>")
        borrower.borrow_axis("wall_f", owner)
        return owner, borrower

    def test_借りた軸を持ち主と同じ値で引ける(self) -> None:
        _, borrower = self._pair()

        assert set(borrower.axes) == {"valve_1", "wall_f"}
        assert borrower.names("wall_f") == ("open",)
        assert borrower.raw("wall_f", "open") == 80.0
        assert borrower.commands("wall_f", "open") == {"wall_f": 80.0}

    def test_持ち主を読み直すと貸し先にも届く(self) -> None:
        owner, borrower = self._pair()

        owner.adopt_positions(self._one("wall_f", 70.0, source="<owner>"))

        assert borrower.raw("wall_f", "open") == 70.0

    def test_貸し先を読み直しても借りた軸は消えない(self) -> None:
        """借りた軸は `axes:` の突き合わせにも入らない (入ると「増えた軸」で拒まれる)。"""
        _, borrower = self._pair()

        assert borrower.adopt_positions(self._one("valve_1", 1.0, source="<borrower>")) == ()
        assert borrower.raw("wall_f", "open") == 80.0

    def test_統合動作確認のマージで二重定義にならない(self) -> None:
        owner, borrower = self._pair()

        merged = PositionTable.merged([owner, borrower])

        assert set(merged.axes) == {"wall_f", "valve_1"}

    def test_自分で定義した軸は借りられない(self) -> None:
        owner = self._one("wall_f", 80.0, source="<owner>")
        borrower = self._one("wall_f", 90.0, source="<borrower>")

        with pytest.raises(ValueError, match="wall_f"):
            borrower.borrow_axis("wall_f", owner)


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

    def test_manual_の端も最大移動距離に数える(self) -> None:
        # 位置名どうしの幅 (15mm) なら 1.0 秒で足りるが、manual の端からの出発 (65mm) は足りない
        config = self._axis(
            motion={"max_velocity": 60.0, "max_acceleration": 400.0},
            timeout_s=1.0,
            manual={"min": -50.0, "max": 15.0, "steps": [1.0]},
        )
        with pytest.raises(ValueError, match=r"最大移動 65\.0 mm"):
            load_position_table(config, source="<test>")


class TestMinSpeed:
    """ドライバ内蔵の位置ループが速度を決める軸の timeout_s 検算 (axes.<軸>.min_speed)。"""

    def _axis(self, *, min_speed: object, **extra: object) -> dict:
        return {
            "axes": {
                "sub_lift": {
                    "unit": "mm",
                    "command_unit": "rad",
                    "scale": 1.0668451,
                    "min_speed": min_speed,
                    **extra,
                }
            },
            "positions": {"sub_lift": {"top": -140.0, "place": -20.0}},
        }

    def test_min_speed_を書かない軸は検算しない(self) -> None:
        table = _table()

        assert table.axis("lift_motor").min_speed is None

    def test_timeout_s_に収まる設定は通る(self) -> None:
        table = load_position_table(self._axis(min_speed=20.0, timeout_s=12.0), source="<test>")

        assert table.axis("sub_lift").min_speed == pytest.approx(20.0)

    def test_必ずタイムアウトする軸は起動を拒否する(self) -> None:
        with pytest.raises(ValueError) as exc:
            load_position_table(self._axis(min_speed=20.0, timeout_s=4.0), source="<test>")

        message = str(exc.value)
        assert "axes.sub_lift" in message
        assert "min_speed (20.0 mm/s)" in message
        assert "timeout_s (4.0)" in message
        assert "timeout_s を 6.0 以上" in message

    def test_manual_の端も最大移動距離に数える(self) -> None:
        # 位置名どうしの幅 120mm は 7.0 秒で足りるが、manual の全幅 154mm は足りない
        config = self._axis(
            min_speed=20.0,
            timeout_s=7.0,
            manual={"min": -152.0, "max": 2.0, "steps": [1.0]},
        )
        with pytest.raises(ValueError, match=r"最大移動 154\.0 mm"):
            load_position_table(config, source="<test>")

    def test_位置定数を持たない軸でも_manual_の幅で検算する(self) -> None:
        config = self._axis(
            min_speed=20.0,
            timeout_s=7.0,
            manual={"min": -152.0, "max": 2.0, "steps": [1.0]},
        )
        config["positions"] = {}
        with pytest.raises(ValueError, match=r"最大移動 154\.0 mm"):
            load_position_table(config, source="<test>")

    def test_正でない_min_speed_は拒否する(self) -> None:
        with pytest.raises(ValueError, match="min_speed は正の値"):
            load_position_table(self._axis(min_speed=0.0), source="<test>")

    def test_数値でない_min_speed_は拒否する(self) -> None:
        with pytest.raises(ValueError, match="min_speed が数値ではありません"):
            load_position_table(self._axis(min_speed="fast"), source="<test>")

    def test_motion_との併記は拒否する(self) -> None:
        config = self._axis(
            min_speed=20.0,
            timeout_s=12.0,
            motion={"max_velocity": 60.0, "max_acceleration": 400.0},
        )
        with pytest.raises(ValueError, match="min_speed と motion は併記できません"):
            load_position_table(config, source="<test>")

    def test_位置指令でない軸への_min_speed_は拒否する(self) -> None:
        config = {
            "axes": {
                "pump": {
                    "unit": "duty",
                    "command_unit": "duty",
                    "command_mode": "duty",
                    "min_speed": 20.0,
                }
            }
        }
        with pytest.raises(ValueError, match="min_speed は位置指令の軸にのみ"):
            load_position_table(config, source="<test>")


class TestMotionGuardSpec:
    """指令を出す直前の歯止めの宣言 (axes.<軸>.guard)。

    判断そのものは ``lib/motion_guard.py`` が持つ (``tests/test_motion_guard.py``)。
    ここが見るのは「yaml をそのまま宣言へ運べているか」と、
    **書かなかった軸が今までどおり素通りすること**だけ。
    """

    def _axis(self, *, guard: object, **extra: object) -> dict:
        return {
            "axes": {
                "sub_y_axis": {
                    "unit": "mm",
                    "command_unit": "rad",
                    "scale": 1.0668451,
                    "guard": guard,
                    **extra,
                }
            },
            "positions": {"sub_y_axis": {"home": 0.0, "extended": 10.0}},
        }

    def test_guard_を書かない軸は素通り(self) -> None:
        """既定値で埋めない。埋めると「効いている値か書き忘れか」が読めなくなる。"""
        assert _table().axis("lift_motor").guard is None

    def test_値がそのまま_MotionGuardSpec_へ届く(self) -> None:
        table = load_position_table(
            self._axis(
                guard={
                    "limits": {"plus": "front_switch", "minus": "rear_switch"},
                    "max_step": 800.0,
                    "stall_torque": 2.0,
                }
            ),
            source="<test>",
        )
        guard = table.axis("sub_y_axis").guard

        assert guard is not None
        assert guard.limits is not None
        assert guard.limits.plus == ("front_switch",)
        assert guard.limits.minus == ("rear_switch",)
        assert guard.max_step == pytest.approx(800.0)
        assert guard.stall_torque == pytest.approx(2.0)

    def test_片端だけの宣言も通る(self) -> None:
        """片端にしかスイッチが無い機構は普通にある。書かなかった端は守られない。"""
        table = load_position_table(
            self._axis(guard={"limits": {"plus": "front_switch"}}), source="<test>"
        )
        guard = table.axis("sub_y_axis").guard

        assert guard is not None
        assert guard.limits is not None
        assert guard.limits.plus == ("front_switch",)
        assert guard.limits.minus == ()
        assert guard.max_step is None
        assert guard.stall_torque is None

    def test_未知のキーを拒否する(self) -> None:
        with pytest.raises(ValueError, match="max_stepp"):
            load_position_table(self._axis(guard={"max_stepp": 800.0}))

    def test_limits_の未知のキーを拒否する(self) -> None:
        """`plus` / `minus` 以外を黙って捨てると、綴り違いが「守っていない端」になる。"""
        with pytest.raises(ValueError, match="up"):
            load_position_table(self._axis(guard={"limits": {"up": "front_switch"}}))

    def test_一つの端に複数本を書ける(self) -> None:
        """左右直結ペアは同じ端に 1 本ずつ持つ (`y_axis` の左右の原点スイッチ)。"""
        table = load_position_table(
            self._axis(guard={"limits": {"minus": ["origin_r", "origin_l"]}}), source="<test>"
        )
        guard = table.axis("sub_y_axis").guard

        assert guard is not None
        assert guard.limits is not None
        assert guard.limits.minus == ("origin_r", "origin_l")

    def test_空の並びを拒否する(self) -> None:
        """書いたのに 1 本も無い端は、書き忘れと区別が付かない。"""
        with pytest.raises(ValueError, match=r"limits\.minus"):
            load_position_table(self._axis(guard={"limits": {"minus": []}}))

    def test_同じセンサを二度書いたら拒否する(self) -> None:
        """畳むと、2 本書いたつもりが同じ名前だったときに気付けない。"""
        with pytest.raises(ValueError, match="2 回"):
            load_position_table(self._axis(guard={"limits": {"minus": ["origin_r", "origin_r"]}}))

    def test_センサ名でない要素を拒否する(self) -> None:
        with pytest.raises(ValueError, match=r"limits\.plus"):
            load_position_table(self._axis(guard={"limits": {"plus": ["front_switch", 3]}}))

    def test_非正の_max_step_は拒否する(self) -> None:
        with pytest.raises(ValueError, match="max_step"):
            load_position_table(self._axis(guard={"max_step": 0.0}))

    def test_非正の_stall_torque_は拒否する(self) -> None:
        with pytest.raises(ValueError, match="stall_torque"):
            load_position_table(self._axis(guard={"stall_torque": -1.0}))

    def test_位置指令でない軸には書けない(self) -> None:
        """duty 軸は位置を観測できないので、現在位置が常に 0 として読める。

        書けてしまうと「守っているように見えて 1 度も判定していない」設定が通る。
        """
        with pytest.raises(ValueError, match="guard は位置指令の軸にのみ"):
            load_position_table(
                self._axis(guard={"max_step": 1.0}, command_mode="duty"),
            )


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


class TestGuardLimitsMatchHoming:
    """零点確定で当てに行くスイッチは、**同じ向きの可動端**として宣言されていること。

    両者はセンサ名の文字列でしか繋がっていないので、取り違えても yaml は読める。
    現れ方はどちらも「機構を壊すまで出ない」か「その軸だけいつも零点確定に失敗する」。
    """

    @staticmethod
    def _table(*, direction: float, limits: dict) -> dict:
        return {
            "axes": {
                "y_axis": {
                    "unit": "mm",
                    "command_unit": "deg",
                    "tolerance": 1.0,
                    "sync_tolerance": 10.0,
                    "motors": {"y_axis_r": {"scale": 55.0}, "y_axis_l": {"scale": -55.0}},
                    "homing": {
                        "sensors": {"y_axis_r": "origin_r", "y_axis_l": "origin_l"},
                        "direction": direction,
                        "search_distance": 650.0,
                        "align_distance": 5.0,
                        "step": 0.5,
                        "settle_s": 0.05,
                    },
                    "guard": {"limits": limits},
                }
            },
            "positions": {"y_axis": {"home": 0.0}},
        }

    def test_探索方向の端に全部書いてあれば通る(self) -> None:
        table = load_position_table(
            self._table(direction=-1, limits={"minus": ["origin_r", "origin_l"]}),
            source="<test>",
        )

        guard = table.axis("y_axis").guard
        assert guard is not None and guard.limits is not None
        assert guard.limits.minus == ("origin_r", "origin_l")

    def test_一本だけ書き忘れたら拒否する(self) -> None:
        """**書き忘れた 1 本は「守られていない端」として残る。**"""
        with pytest.raises(ValueError, match="origin_l"):
            load_position_table(self._table(direction=-1, limits={"minus": ["origin_r"]}))

    def test_歯止めそのものを書かない軸は対象外(self) -> None:
        raw = self._table(direction=-1, limits={"minus": ["origin_r", "origin_l"]})
        del raw["axes"]["y_axis"]["guard"]

        assert load_position_table(raw, source="<test>").axis("y_axis").guard is None

    def test_逆の端に書いたら拒否する(self) -> None:
        """逆に書くと守りが反転し、押されている端へ進む指令だけが通る。"""
        with pytest.raises(ValueError, match="plus"):
            load_position_table(
                self._table(direction=-1, limits={"plus": ["origin_r", "origin_l"]})
            )

    def test_探索方向が正なら_plus_側(self) -> None:
        table = load_position_table(
            self._table(direction=1, limits={"plus": ["origin_r", "origin_l"]}),
            source="<test>",
        )

        guard = table.axis("y_axis").guard
        assert guard is not None and guard.limits is not None
        assert guard.limits.plus == ("origin_r", "origin_l")


class TestGuardInterference:
    """軸どうしの干渉条件 (axes.<軸>.guard.requires / not_with)。

    yaml は**位置名でだけ**宣言し、数値の区間へ解決するのは読み込み時である
    (``MotionGuard`` は位置表もコートも見ない)。ここが見るのは解決の結果と、
    **誤記が警告ではなく起動拒否になること**。判断そのものは
    ``tests/test_motion_guard.py`` が見る。
    """

    _HOMING: ClassVar[dict] = {
        "sensor": "origin_sensor",
        "direction": 1,
        "search_distance": 10.0,
        "step": 1.0,
    }

    def _raw(self, **guards: object) -> dict:
        axes: dict[str, dict] = {
            "lift": {"unit": "mm", "command_unit": "rad", "scale": 1.0, "tolerance": 1.0},
            "carriage": {"unit": "mm", "command_unit": "rad", "scale": 1.0, "tolerance": 0.5},
            "arm": {"unit": "deg", "command_unit": "deg", "scale": 1.0, "tolerance": 2.0},
            "pump": {
                "unit": "duty",
                "command_unit": "duty",
                "command_mode": "duty",
                "scale": 1.0,
            },
        }
        for axis, guard in guards.items():
            axes[axis]["guard"] = guard
        return {
            "axes": axes,
            "positions": {
                "lift": {"top": -140.0, "bottom": -20.0},
                "carriage": {"retracted": -430.0, "clear": -200.0, "front": -10.0},
                "arm": {"open": 0.0, "close": 90.0},
                "pump": {"stop": 0.0, "run": 0.95},
            },
        }

    def _load(self, **guards: object) -> PositionTable:
        return load_position_table(self._raw(**guards), source="<test>")

    def _required(self, table: PositionTable, axis: str) -> RequiredRange:
        guard = table.axis(axis).guard
        assert guard is not None
        (required,) = guard.requires
        return required

    def test_at_は参照先の_tolerance_ぶん広げて解決される(self) -> None:
        """広げないと、その位置へ許容差の内側で止まった実測が区間の外になる。"""
        table = self._load(carriage={"requires": [{"axis": "lift", "at": "top"}]})

        required = self._required(table, "carriage")
        assert required.axis == "lift"
        assert required.low == pytest.approx(-141.0)
        assert required.high == pytest.approx(-139.0)
        assert required.label == "top"
        assert required.unit == "mm"

    def test_between_は両端を_tolerance_ぶん広げて解決される(self) -> None:
        table = self._load(
            arm={"requires": [{"axis": "carriage", "between": ["retracted", "clear"]}]}
        )

        required = self._required(table, "arm")
        assert required.low == pytest.approx(-430.5)
        assert required.high == pytest.approx(-199.5)
        assert required.label == "retracted〜clear"

    def test_between_は書いた順に依らない(self) -> None:
        table = self._load(
            arm={"requires": [{"axis": "carriage", "between": ["clear", "retracted"]}]}
        )

        required = self._required(table, "arm")
        assert (required.low, required.high) == (pytest.approx(-430.5), pytest.approx(-199.5))

    def test_slack_を書いた条件はその幅で解決される(self) -> None:
        """参照先の tolerance では飲めないずれ (自重で下がるなど) を条件側で吸う。"""
        table = self._load(carriage={"requires": [{"axis": "lift", "at": "top", "slack": 15.0}]})

        required = self._required(table, "carriage")
        assert required.low == pytest.approx(-155.0)
        assert required.high == pytest.approx(-125.0)

    def test_slack_を書いていない条件は参照先の_tolerance_のまま(self) -> None:
        """**書いた軸しか緩まない。** 同じ表に書いた他の条件の幅は 1mm も変わらない。"""
        table = self._load(
            carriage={"requires": [{"axis": "lift", "at": "top", "slack": 15.0}]},
            arm={"requires": [{"axis": "carriage", "between": ["retracted", "clear"]}]},
        )

        required = self._required(table, "arm")
        assert required.low == pytest.approx(-430.5)
        assert required.high == pytest.approx(-199.5)

    @pytest.mark.parametrize("slack", [0.0, -1.0, float("inf"), float("nan")])
    def test_正の有限値でない_slack_は起動拒否(self, slack: float) -> None:
        """0 幅に縮むと、到達許容差の内側で止まった実測が必ず区間の外になる。"""
        with pytest.raises(ValueError, match="正の有限値"):
            self._load(carriage={"requires": [{"axis": "lift", "at": "top", "slack": slack}]})

    @pytest.mark.parametrize("slack", ["15.0", True, [15.0]])
    def test_数値でない_slack_は起動拒否(self, slack: object) -> None:
        with pytest.raises(ValueError, match="数値"):
            self._load(carriage={"requires": [{"axis": "lift", "at": "top", "slack": slack}]})

    def test_書かない軸は素通り(self) -> None:
        """既定は空。書かなかった軸に干渉の歯止めは 1 つも掛からない。"""
        table = self._load(carriage={"max_step": 100.0})

        guard = table.axis("carriage").guard
        assert guard is not None
        assert guard.requires == ()
        assert guard.not_with == ()
        assert table.axis("arm").guard is None

    def test_参照先の軸が無ければ起動拒否(self) -> None:
        with pytest.raises(ValueError, match="ghost"):
            self._load(carriage={"requires": [{"axis": "ghost", "at": "top"}]})

    def test_零点確定する軸の参照先に_homing_が無ければ起動拒否(self) -> None:
        """参照先は零点確定の前に**位置名で寄せる**先になる。

        原点が確定していない軸へ位置名で指令すると、どこへ動くか分からない。
        """
        raw = self._raw(carriage={"requires": [{"axis": "lift", "at": "top"}]})
        raw["axes"]["carriage"]["homing"] = self._HOMING

        with pytest.raises(ValueError, match="homing がありません"):
            load_position_table(raw, source="<test>")

    def test_参照先にも_homing_があれば通る(self) -> None:
        raw = self._raw(carriage={"requires": [{"axis": "lift", "at": "top"}]})
        raw["axes"]["carriage"]["homing"] = self._HOMING
        raw["axes"]["lift"]["homing"] = self._HOMING

        table = load_position_table(raw, source="<test>")

        assert table.homing_prerequisites(["carriage"]) == {"lift": "top"}

    def test_零点確定しない軸の参照先は_homing_を要らない(self) -> None:
        """その軸は零点確定の経路を 1 度も通らないので、寄せる先にならない。"""
        raw = self._raw(arm={"requires": [{"axis": "carriage", "at": "clear"}]})

        table = load_position_table(raw, source="<test>")

        assert table.axis("arm").guard is not None

    def test_between_は寄せ先にならないので_homing_を要らない(self) -> None:
        raw = self._raw(arm={"requires": [{"axis": "carriage", "between": ["retracted", "clear"]}]})
        raw["axes"]["arm"]["homing"] = self._HOMING

        table = load_position_table(raw, source="<test>")

        assert table.homing_prerequisites(["arm"]) == {}

    def test_参照先の位置名が無ければ起動拒否(self) -> None:
        with pytest.raises(ValueError, match=re.escape("lift.middle")):
            self._load(carriage={"requires": [{"axis": "lift", "at": "middle"}]})

    def test_位置指令でない軸は参照できない(self) -> None:
        with pytest.raises(ValueError, match="command_mode=duty"):
            self._load(carriage={"requires": [{"axis": "pump", "at": "run"}]})

    def test_tolerance_の無い軸は参照できない(self) -> None:
        """区間を広げられないと、到達した実測がそのまま区間の外になる。"""
        raw = self._raw(carriage={"requires": [{"axis": "lift", "at": "top"}]})
        del raw["axes"]["lift"]["tolerance"]

        with pytest.raises(ValueError, match="tolerance"):
            load_position_table(raw, source="<test>")

    def test_コート別に分岐した位置は参照できない(self) -> None:
        """mm の座標系は両コート共通という前提を崩さない。"""
        raw = self._raw(carriage={"requires": [{"axis": "lift", "at": "top"}]})
        raw["positions"]["lift"]["top"] = {"red": -140.0, "blue": -140.0}

        with pytest.raises(ValueError, match="コート別"):
            load_position_table(raw, source="<test>")

    def test_生の数値は受け付けない(self) -> None:
        """数値を許すと同じ座標が 2 箇所に書かれ、位置を動かすと片方だけ古くなる。"""
        with pytest.raises(ValueError, match="生の数値"):
            self._load(carriage={"requires": [{"axis": "lift", "at": -140.0}]})

    def test_at_と_between_の併記は起動拒否(self) -> None:
        with pytest.raises(ValueError, match="どちらか一方"):
            self._load(
                carriage={"requires": [{"axis": "lift", "at": "top", "between": ["top", "bottom"]}]}
            )

    def test_at_も_between_も無ければ起動拒否(self) -> None:
        with pytest.raises(ValueError, match="どちらか一方"):
            self._load(carriage={"requires": [{"axis": "lift"}]})

    def test_未知のキーは起動拒否(self) -> None:
        with pytest.raises(ValueError, match="above"):
            self._load(carriage={"requires": [{"axis": "lift", "above": "top"}]})

    def test_between_に同じ位置を_2_回書いたら起動拒否(self) -> None:
        with pytest.raises(ValueError, match="2 回"):
            self._load(carriage={"requires": [{"axis": "lift", "between": ["top", "top"]}]})

    def test_between_が_2_点でなければ起動拒否(self) -> None:
        with pytest.raises(ValueError, match="2 つ"):
            self._load(
                carriage={"requires": [{"axis": "lift", "between": ["top", "bottom", "top"]}]}
            )

    def test_循環した参照は起動拒否(self) -> None:
        """一方向なら詰んでも下流を動かせば必ず解ける。循環すると抜ける手が無い。"""
        with pytest.raises(ValueError, match="循環"):
            self._load(
                carriage={"requires": [{"axis": "lift", "at": "top"}]},
                lift={"requires": [{"axis": "carriage", "at": "clear"}]},
            )

    def test_自分自身を参照したら起動拒否(self) -> None:
        with pytest.raises(ValueError, match="循環"):
            self._load(carriage={"requires": [{"axis": "carriage", "at": "clear"}]})

    def test_not_with_は片側の宣言から対称化される(self) -> None:
        """相手側に guard が無くても生える。制約は宣言されていて、書いた場所が反対なだけ。"""
        table = self._load(arm={"not_with": ["carriage"]})

        arm = table.axis("arm").guard
        carriage = table.axis("carriage").guard
        assert arm is not None and arm.not_with == ("carriage",)
        assert carriage is not None and carriage.not_with == ("arm",)

    def test_not_with_に自分自身を書いたら起動拒否(self) -> None:
        with pytest.raises(ValueError, match="自分自身"):
            self._load(arm={"not_with": ["arm"]})

    def test_not_with_の軸が無ければ起動拒否(self) -> None:
        with pytest.raises(ValueError, match="ghost"):
            self._load(arm={"not_with": ["ghost"]})

    def test_not_with_に位置指令でない軸は書けない(self) -> None:
        with pytest.raises(ValueError, match="command_mode=duty"):
            self._load(arm={"not_with": ["pump"]})

    def test_not_with_を両側に書いたら起動拒否(self) -> None:
        """片側だけを正にする。両側に書けると、片方を消したとき守りが半分だけ残る。"""
        with pytest.raises(ValueError, match="両側"):
            self._load(arm={"not_with": ["carriage"]}, carriage={"not_with": ["arm"]})
