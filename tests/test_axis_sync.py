from __future__ import annotations

import pytest

from lib.axis_sync import MotorSpec, SyncGroup
from lib.sequence.positions import load_position_table

SCALE = 55.02

_PAIRED_CONFIG = {
    "axes": {
        "y_axis": {
            "unit": "mm",
            "command_unit": "deg",
            "tolerance": 0.5,
            "sync_tolerance": 2.0,
            "motors": {
                "y_axis_r": {"scale": SCALE},
                "y_axis_l": {"scale": -SCALE},
            },
        },
        "gripper": {"unit": "state", "command_unit": "deg", "scale": 2.0, "tolerance": 0.5},
    },
    "positions": {
        "y_axis": {"home": 0.0, "work": 10.0},
        "gripper": {"open": 30.0},
    },
}


class TestMotorSpecIsShared:
    def test_positions_module_reexports_the_same_type(self) -> None:
        from lib.sequence import positions as positions_module

        assert positions_module.MotorSpec is MotorSpec

    def test_axis_motors_are_reused_as_sync_members(self) -> None:
        table = load_position_table(_PAIRED_CONFIG, source="<test>")
        spec = table.axis("y_axis")

        group = spec.sync_group

        assert group is not None
        assert group.members == spec.motors


class TestMotorSpecConversion:
    def test_to_value_is_inverse_of_to_command(self) -> None:
        motor = MotorSpec(name="y_axis_l", scale=-SCALE, offset=12.5)

        assert motor.to_value(motor.to_command(7.5)) == pytest.approx(7.5)

    def test_to_tolerance_is_positive_for_reverse_motor(self) -> None:
        motor = MotorSpec(name="y_axis_l", scale=-10.0, offset=0.0)

        assert motor.to_tolerance(0.5) == pytest.approx(5.0)


class TestSyncGroupVerdict:
    def _group(self, tolerance: float = 2.0) -> SyncGroup:
        return SyncGroup(
            name="y_axis",
            members=(
                MotorSpec(name="y_axis_r", scale=SCALE, offset=0.0),
                MotorSpec(name="y_axis_l", scale=-SCALE, offset=0.0),
            ),
            tolerance=tolerance,
        )

    def test_violation_is_none_within_tolerance(self) -> None:
        group = self._group()

        assert group.violation({"y_axis_r": 10.0 * SCALE, "y_axis_l": -11.0 * SCALE}) is None

    def test_violation_returns_deviation_when_exceeded(self) -> None:
        group = self._group()

        deviation = group.violation({"y_axis_r": 10.0 * SCALE, "y_axis_l": -7.0 * SCALE})

        assert deviation == pytest.approx(3.0)

    def test_violation_is_none_when_comparison_is_impossible(self) -> None:
        group = self._group()

        assert group.violation({"y_axis_r": 10.0 * SCALE}) is None
        assert group.violation({}) is None

    def test_violation_is_exclusive_of_the_tolerance_itself(self) -> None:
        group = self._group(tolerance=2.0)

        assert group.violation({"y_axis_r": 10.0 * SCALE, "y_axis_l": -8.0 * SCALE}) is None


class TestAxisSpecSyncGroup:
    def test_single_motor_axis_has_no_group(self) -> None:
        table = load_position_table(_PAIRED_CONFIG, source="<test>")

        assert table.axis("gripper").sync_group is None

    def test_group_carries_axis_name_and_tolerance(self) -> None:
        table = load_position_table(_PAIRED_CONFIG, source="<test>")

        group = table.axis("y_axis").sync_group

        assert group is not None
        assert group.name == "y_axis"
        assert group.tolerance == pytest.approx(2.0)


class TestConversionIsAlwaysPerMotor:
    def test_paired_axis_converts_each_motor_with_its_own_scale(self) -> None:
        table = load_position_table(_PAIRED_CONFIG, source="<test>")

        commands = table.commands("y_axis", "work")

        assert set(commands) == {"y_axis_r", "y_axis_l"}
        assert commands["y_axis_r"] == pytest.approx(-commands["y_axis_l"])

    def test_tolerance_is_converted_per_motor_without_sign(self) -> None:
        spec = load_position_table(_PAIRED_CONFIG, source="<test>").axis("y_axis")
        assert spec.tolerance is not None

        widths = [motor.to_tolerance(spec.tolerance) for motor in spec.motors]

        assert all(width > 0.0 for width in widths)

    def test_single_motor_axis_is_keyed_by_axis_name(self) -> None:
        table = load_position_table(_PAIRED_CONFIG, source="<test>")

        assert table.commands("gripper", "open") == {"gripper": pytest.approx(60.0)}


class TestSyncGroupDeviation:
    def _group(self) -> SyncGroup:
        return SyncGroup(
            name="y_axis",
            members=(
                MotorSpec(name="y_axis_r", scale=SCALE, offset=0.0),
                MotorSpec(name="y_axis_l", scale=-SCALE, offset=0.0),
            ),
            tolerance=2.0,
        )

    def test_reverse_pair_in_sync_has_zero_deviation(self) -> None:
        group = self._group()

        assert group.deviation({"y_axis_r": 10.0 * SCALE, "y_axis_l": -10.0 * SCALE}) == (
            pytest.approx(0.0)
        )

    def test_deviation_reflects_mismatch_in_human_units(self) -> None:
        group = self._group()

        assert group.deviation({"y_axis_r": 10.0 * SCALE, "y_axis_l": -7.0 * SCALE}) == (
            pytest.approx(3.0)
        )

    def test_deviation_is_none_with_fewer_than_two_members(self) -> None:
        group = self._group()

        assert group.deviation({"y_axis_r": 0.0}) is None
        assert group.deviation({}) is None

    def test_to_value_of_member_handles_offset(self) -> None:
        member = MotorSpec(name="y_axis_r", scale=SCALE, offset=100.0)

        assert member.to_value(10.0 * SCALE + 100.0) == pytest.approx(10.0)


class TestSyncCorrections:
    def _group(self, *, sync_kp: float = 2.0, sync_limit: float | None = 1e9) -> SyncGroup:
        return SyncGroup(
            name="y_axis",
            members=(
                MotorSpec(name="y_axis_r", scale=SCALE, offset=0.0),
                MotorSpec(name="y_axis_l", scale=-SCALE, offset=0.0),
            ),
            tolerance=2.0,
            sync_kp=sync_kp,
            sync_limit=sync_limit,
        )

    def test_no_corrections_without_gain(self) -> None:
        group = self._group(sync_kp=0.0, sync_limit=None)

        assert group.corrections({"y_axis_r": 10.0 * SCALE, "y_axis_l": -7.0 * SCALE}) == {}

    def test_reversed_pair_gets_identical_corrections(self) -> None:
        group = self._group(sync_kp=2.0)

        corrections = group.corrections({"y_axis_r": 10.0 * SCALE, "y_axis_l": -7.0 * SCALE})

        expected = 2.0 * (8.5 - 10.0) * SCALE
        assert corrections["y_axis_r"] == pytest.approx(expected)
        assert corrections["y_axis_l"] == pytest.approx(expected)

    def test_correction_pulls_advanced_motor_back(self) -> None:
        group = self._group(sync_kp=2.0)

        corrections = group.corrections({"y_axis_r": 10.0 * SCALE, "y_axis_l": -7.0 * SCALE})

        assert corrections["y_axis_r"] < 0.0
        assert corrections["y_axis_l"] < 0.0

    def test_no_correction_when_aligned(self) -> None:
        group = self._group(sync_kp=2.0)

        corrections = group.corrections({"y_axis_r": 9.0 * SCALE, "y_axis_l": -9.0 * SCALE})

        assert corrections["y_axis_r"] == pytest.approx(0.0)
        assert corrections["y_axis_l"] == pytest.approx(0.0)

    def test_corrections_sum_to_zero_with_three_members(self) -> None:
        group = SyncGroup(
            name="triple",
            members=(
                MotorSpec(name="a", scale=1.0, offset=0.0),
                MotorSpec(name="b", scale=1.0, offset=0.0),
                MotorSpec(name="c", scale=1.0, offset=0.0),
            ),
            tolerance=10.0,
            sync_kp=3.0,
            sync_limit=1e9,
        )

        corrections = group.corrections({"a": 0.0, "b": 1.0, "c": 2.0})

        assert sum(corrections.values()) == pytest.approx(0.0)
        assert corrections["a"] == pytest.approx(3.0)
        assert corrections["c"] == pytest.approx(-3.0)

    def test_correction_is_clamped_by_sync_limit(self) -> None:
        group = self._group(sync_kp=100.0, sync_limit=250.0)

        corrections = group.corrections({"y_axis_r": 10.0 * SCALE, "y_axis_l": -7.0 * SCALE})

        assert corrections["y_axis_r"] == pytest.approx(-250.0)
        assert corrections["y_axis_l"] == pytest.approx(-250.0)

    def test_offset_is_removed_before_averaging(self) -> None:
        group = SyncGroup(
            name="offset_pair",
            members=(
                MotorSpec(name="r", scale=SCALE, offset=100.0),
                MotorSpec(name="l", scale=-SCALE, offset=-40.0),
            ),
            tolerance=2.0,
            sync_kp=2.0,
            sync_limit=1e9,
        )

        corrections = group.corrections({"r": 5.0 * SCALE + 100.0, "l": -5.0 * SCALE - 40.0})

        assert corrections["r"] == pytest.approx(0.0)
        assert corrections["l"] == pytest.approx(0.0)

    def test_no_corrections_when_a_member_is_missing(self) -> None:
        group = self._group(sync_kp=2.0)

        assert group.corrections({"y_axis_r": 10.0 * SCALE}) == {}
        assert group.corrections({}) == {}


class TestTargetsShareAxisValue:
    def _group(self) -> SyncGroup:
        return SyncGroup(
            name="y_axis",
            members=(
                MotorSpec(name="y_axis_r", scale=SCALE, offset=0.0),
                MotorSpec(name="y_axis_l", scale=-SCALE, offset=0.0),
            ),
            tolerance=2.0,
            sync_kp=2.0,
            sync_limit=1e9,
        )

    def test_same_axis_value_is_shared(self) -> None:
        group = self._group()

        assert group.targets_share_axis_value({"y_axis_r": 10.0 * SCALE, "y_axis_l": -10.0 * SCALE})

    def test_one_sided_step_is_not_shared(self) -> None:
        group = self._group()

        assert not group.targets_share_axis_value(
            {"y_axis_r": 10.5 * SCALE, "y_axis_l": -10.0 * SCALE}
        )

    def test_missing_member_is_not_shared(self) -> None:
        group = self._group()

        assert not group.targets_share_axis_value({"y_axis_r": 10.0 * SCALE})
        assert not group.targets_share_axis_value({})

    def test_round_trip_error_is_absorbed(self) -> None:
        group = self._group()
        members = {member.name: member for member in group.members}
        targets = {name: member.to_command(10.0) for name, member in members.items()}

        assert group.targets_share_axis_value(targets)

    def test_tolerance_sized_skew_is_not_shared(self) -> None:
        group = self._group()

        assert not group.targets_share_axis_value(
            {"y_axis_r": 11.0 * SCALE, "y_axis_l": -10.0 * SCALE}
        )


class TestSyncGainValidation:
    def _members(self) -> tuple[MotorSpec, ...]:
        return (
            MotorSpec(name="y_axis_r", scale=SCALE, offset=0.0),
            MotorSpec(name="y_axis_l", scale=-SCALE, offset=0.0),
        )

    def test_gain_without_limit_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="sync_limit"):
            SyncGroup(name="y_axis", members=self._members(), tolerance=2.0, sync_kp=1.0)

    def test_negative_gain_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="sync_kp"):
            SyncGroup(
                name="y_axis",
                members=self._members(),
                tolerance=2.0,
                sync_kp=-1.0,
                sync_limit=100.0,
            )

    def test_negative_limit_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="sync_limit"):
            SyncGroup(
                name="y_axis",
                members=self._members(),
                tolerance=2.0,
                sync_kp=1.0,
                sync_limit=-1.0,
            )

    def test_zero_gain_needs_no_limit(self) -> None:
        group = SyncGroup(name="y_axis", members=self._members(), tolerance=2.0)

        assert group.sync_kp == 0.0
        assert group.sync_limit is None
