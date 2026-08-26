"""左右直結ペアの単位換算とずれ判定 (lib/axis_sync.py) のテスト。

この機体は同じ ``sync_tolerance`` を 3 層 (シーケンス停止 / 電流 0 / 全体緊急停止) で
参照する。換算と判定が層ごとに別実装になると、片方だけ直したときに気付けないまま
機構が壊れるため、単一実装であること自体をここで固定する。
"""

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
        """シーケンス層と制御層が同じ型を使う (換算のコピーを作らせない)。"""
        from lib.sequence import positions as positions_module

        assert positions_module.MotorSpec is MotorSpec

    def test_axis_motors_are_reused_as_sync_members(self) -> None:
        """SyncGroup のメンバは AxisSpec のモータそのもの (詰め替えを挟まない)。"""
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
        """許容差は幅であって向きを持たない。符号が残ると比較が常に成立してしまう。"""
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
        """比較対象が 1 台以下では「ずれている」と言えない (途絶時に誤発報させない)。"""
        group = self._group()

        assert group.violation({"y_axis_r": 10.0 * SCALE}) is None
        assert group.violation({}) is None

    def test_violation_is_exclusive_of_the_tolerance_itself(self) -> None:
        """許容差ちょうどは超過ではない (3 層で境界の扱いがずれないよう固定する)。"""
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


class TestFirstMotorApiRejectsPairedAxis:
    """先頭モータだけを見る API をペア軸に使うと左のモータへ右の scale が当たる。"""

    def test_to_command_rejects_paired_axis(self) -> None:
        table = load_position_table(_PAIRED_CONFIG, source="<test>")

        with pytest.raises(ValueError, match="y_axis"):
            table.axis("y_axis").to_command(10.0)

    def test_command_rejects_paired_axis(self) -> None:
        table = load_position_table(_PAIRED_CONFIG, source="<test>")

        with pytest.raises(ValueError, match="y_axis"):
            table.command("y_axis", "work")

    def test_scale_and_offset_reject_paired_axis(self) -> None:
        table = load_position_table(_PAIRED_CONFIG, source="<test>")
        spec = table.axis("y_axis")

        with pytest.raises(ValueError, match="y_axis"):
            _ = spec.scale
        with pytest.raises(ValueError, match="y_axis"):
            _ = spec.offset

    def test_command_tolerance_rejects_paired_axis(self) -> None:
        table = load_position_table(_PAIRED_CONFIG, source="<test>")

        with pytest.raises(ValueError, match="y_axis"):
            table.tolerance("y_axis")

    def test_single_motor_axis_is_unaffected(self) -> None:
        table = load_position_table(_PAIRED_CONFIG, source="<test>")

        assert table.command("gripper", "open") == pytest.approx(60.0)
        assert table.tolerance("gripper") == pytest.approx(1.0)


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
