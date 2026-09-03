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


class TestConversionIsAlwaysPerMotor:
    """換算はモータごとにしか行えない (軸単位の scale を返す API を公開しない)。

    かつては軸単位の ``scale`` / ``to_command`` / ``command_tolerance`` があり、
    ペア軸に使うと先頭モータの scale が左右の両方へ当たって、左のモータが右向きに
    全ストローク動いた。ValueError で塞いでいたが API ごと削除したため、
    残っているのはモータごとに換算する道だけになった。
    """

    def test_paired_axis_converts_each_motor_with_its_own_scale(self) -> None:
        table = load_position_table(_PAIRED_CONFIG, source="<test>")

        commands = table.commands("y_axis", "work")

        assert set(commands) == {"y_axis_r", "y_axis_l"}
        # 逆回転ペアは符号が反転する (向きは scale の符号で表す)
        assert commands["y_axis_r"] == pytest.approx(-commands["y_axis_l"])

    def test_tolerance_is_converted_per_motor_without_sign(self) -> None:
        spec = load_position_table(_PAIRED_CONFIG, source="<test>").axis("y_axis")
        assert spec.tolerance is not None

        widths = [motor.to_tolerance(spec.tolerance) for motor in spec.motors]

        # 許容差は幅であって向きを持たない。符号が残ると逆回転側だけ到達判定が素通りする
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
    """左右のずれを縮める補正量 (SyncGroup.corrections)。

    3 層の ``violation`` は「ずれたら止める」しかできず、駆動中にずれを縮める力は
    どこにも無かった。ここが返すのがその力なので、**符号と単位が正しいことは
    機構の安全に直結する**。符号を落とすと、軸ごと押し動かしながらずれは縮まない。
    """

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
        """既定 (sync_kp=0.0) では 1 台にも補正を出さない = 従来どおり独立に動く。"""
        group = self._group(sync_kp=0.0, sync_limit=None)

        assert group.corrections({"y_axis_r": 10.0 * SCALE, "y_axis_l": -7.0 * SCALE}) == {}

    def test_reversed_pair_gets_identical_corrections(self) -> None:
        """逆回転ペアの補正は同符号・同じ大きさ。**この方式が成立する根拠そのもの。**

        人間の単位では ``e_l = -e_r`` だが ``scale_l = -scale_r`` なので、指令単位へ
        戻すと一致する。つまり補正は軸としての運動を動かさず、左右の内部のずれだけを
        縮める。``scale`` の符号を落とすと補正が逆符号になり、ずれを縮めないまま
        軸ごと押し動かす力になる。
        """
        group = self._group(sync_kp=2.0)

        # 人間の単位で r=10.0mm / l=7.0mm (平均 8.5mm)
        corrections = group.corrections({"y_axis_r": 10.0 * SCALE, "y_axis_l": -7.0 * SCALE})

        expected = 2.0 * (8.5 - 10.0) * SCALE
        assert corrections["y_axis_r"] == pytest.approx(expected)
        assert corrections["y_axis_l"] == pytest.approx(expected)

    def test_correction_pulls_advanced_motor_back(self) -> None:
        """進んでいる側には戻す向き、遅れている側には進める向きの補正が出る。"""
        group = self._group(sync_kp=2.0)

        corrections = group.corrections({"y_axis_r": 10.0 * SCALE, "y_axis_l": -7.0 * SCALE})

        # r は平均より進んでいるので指令を減らす向き (scale が正なので負の操作量)
        assert corrections["y_axis_r"] < 0.0
        # l は平均より遅れている。人間の単位で進める向きは scale が負なので負の操作量
        assert corrections["y_axis_l"] < 0.0

    def test_no_correction_when_aligned(self) -> None:
        """揃っていれば補正は 0 (揃っている機体へ余計な電流を出さない)。"""
        group = self._group(sync_kp=2.0)

        corrections = group.corrections({"y_axis_r": 9.0 * SCALE, "y_axis_l": -9.0 * SCALE})

        assert corrections["y_axis_r"] == pytest.approx(0.0)
        assert corrections["y_axis_l"] == pytest.approx(0.0)

    def test_corrections_sum_to_zero_with_three_members(self) -> None:
        """平均を基準にするので、メンバが増えても補正の総和は 0 = 軸の運動に中立。

        先頭モータとの差を基準にすると総和が 0 にならず、軸ごと押し動かす力が残る。
        """
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
        """押し合いの唯一の歯止め。大きなずれでも上限を超える補正は出さない。"""
        group = self._group(sync_kp=100.0, sync_limit=250.0)

        corrections = group.corrections({"y_axis_r": 10.0 * SCALE, "y_axis_l": -7.0 * SCALE})

        assert corrections["y_axis_r"] == pytest.approx(-250.0)
        assert corrections["y_axis_l"] == pytest.approx(-250.0)

    def test_offset_is_removed_before_averaging(self) -> None:
        """offset を持つ軸でも人間の単位へ戻してから平均を取る。

        指令単位のまま平均すると offset の差がそのままずれとして現れ、揃っている
        機体に恒常的な補正が出続ける。
        """
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

        # どちらも人間の単位で 5.0mm
        corrections = group.corrections({"r": 5.0 * SCALE + 100.0, "l": -5.0 * SCALE - 40.0})

        assert corrections["r"] == pytest.approx(0.0)
        assert corrections["l"] == pytest.approx(0.0)

    def test_no_corrections_when_a_member_is_missing(self) -> None:
        """1 台でも位置が欠けたら 1 台にも出さない。

        欠けたメンバを外して平均を取ると、残った側だけが「ずれている」と判定されて
        実在しない補正が出る。
        """
        group = self._group(sync_kp=2.0)

        assert group.corrections({"y_axis_r": 10.0 * SCALE}) == {}
        assert group.corrections({}) == {}


class TestSyncGainValidation:
    """ゲインと歯止めの対を型の段階で守る (yaml を経由しない組み立ても塞ぐ)。"""

    def _members(self) -> tuple[MotorSpec, ...]:
        return (
            MotorSpec(name="y_axis_r", scale=SCALE, offset=0.0),
            MotorSpec(name="y_axis_l", scale=-SCALE, offset=0.0),
        )

    def test_gain_without_limit_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="sync_limit"):
            SyncGroup(name="y_axis", members=self._members(), tolerance=2.0, sync_kp=1.0)

    def test_negative_gain_is_rejected(self) -> None:
        """負のゲインは正帰還。ずれを縮めるどころか発散させる。"""
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
        """既定 (補正なし) では歯止めを書かなくてよい = 既存の構成がそのまま通る。"""
        group = SyncGroup(name="y_axis", members=self._members(), tolerance=2.0)

        assert group.sync_kp == 0.0
        assert group.sync_limit is None
