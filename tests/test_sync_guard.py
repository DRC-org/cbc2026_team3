"""左右直結ペアの保護判断 (lib/control/sync_guard.py) のテスト。

「どのグループを電流 0 に落とすか」の判断だけを切り出した層。実際に電流を 0 に
するのは位置制御ループ側なので、ここでは判断とラッチの正しさだけを固定する
(統合された振る舞いは tests/test_position_loop.py が見る)。
"""

from __future__ import annotations

import logging

import pytest

from lib.axis_sync import MotorSpec, SyncGroup
from lib.control.sync_guard import SyncGuard


def _pair(name: str = "y_axis", tolerance: float = 2.0) -> SyncGroup:
    """逆回転ペア (向きは scale の符号で表す)。"""
    return SyncGroup(
        name=name,
        members=(MotorSpec(f"{name}_r", 1.0, 0.0), MotorSpec(f"{name}_l", -1.0, 0.0)),
        tolerance=tolerance,
    )


def _blocked(
    guard: SyncGuard,
    positions: dict[str, float],
    stale: set[str] | None = None,
) -> frozenset[str]:
    stale = stale or set()
    return guard.blocked(
        stale={name: name in stale for name in positions},
        position_of=positions.__getitem__,
    )


class TestRegistration:
    def test_duplicate_group_rejected(self) -> None:
        guard = SyncGuard()
        guard.add(_pair())
        with pytest.raises(ValueError, match="y_axis"):
            guard.add(_pair())

    def test_motor_in_two_groups_rejected(self) -> None:
        """どちらの許容値で止めるかが曖昧になるため、構成時点で弾く。"""
        guard = SyncGuard()
        guard.add(_pair("y_axis"))
        overlapping = SyncGroup(
            name="other",
            members=(MotorSpec("y_axis_r", 1.0, 0.0), MotorSpec("spare", 1.0, 0.0)),
            tolerance=1.0,
        )
        with pytest.raises(ValueError, match="y_axis_r"):
            guard.add(overlapping)

    def test_group_names_and_lookup(self) -> None:
        guard = SyncGuard()
        guard.add(_pair())
        assert guard.group_names == ("y_axis",)
        assert guard.group_of("y_axis_l") == "y_axis"
        assert guard.group_of("unrelated") is None
        assert guard.members_of("y_axis") == ("y_axis_r", "y_axis_l")


class TestDeviation:
    def test_reverse_rotation_pair_in_sync_is_not_blocked(self) -> None:
        guard = SyncGuard()
        guard.add(_pair())
        assert _blocked(guard, {"y_axis_r": 10.0, "y_axis_l": -10.0}) == frozenset()

    def test_violation_blocks_and_latches(self) -> None:
        guard = SyncGuard()
        guard.add(_pair(tolerance=2.0))

        assert _blocked(guard, {"y_axis_r": 15.0, "y_axis_l": 5.0}) == frozenset({"y_axis"})
        # 偏差が許容内に戻ってもラッチは外れない
        assert _blocked(guard, {"y_axis_r": 5.0, "y_axis_l": -5.0}) == frozenset({"y_axis"})
        assert guard.violations == frozenset({"y_axis"})

    def test_no_debounce(self) -> None:
        """200Hz の局所保護は 1 周期でも早く力を抜く方が安全側 (誤発報の代償は電流 0)。"""
        guard = SyncGuard()
        guard.add(_pair(tolerance=2.0))
        assert _blocked(guard, {"y_axis_r": 15.0, "y_axis_l": 5.0}) == frozenset({"y_axis"})

    def test_violation_is_logged_with_axis_and_values(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        guard = SyncGuard(context="bus=m3508_bus", logger=logging.getLogger("test.guard"))
        guard.add(_pair(tolerance=2.0))

        with caplog.at_level(logging.ERROR, logger="test.guard"):
            _blocked(guard, {"y_axis_r": 15.0, "y_axis_l": 5.0})

        # 試合中に「なぜ止まったか」が分からないと復旧手順を選べない
        assert "y_axis" in caplog.text
        assert "2.0" in caplog.text
        assert "m3508_bus" in caplog.text

    def test_violation_logged_once_per_latch(self, caplog: pytest.LogCaptureFixture) -> None:
        guard = SyncGuard(logger=logging.getLogger("test.guard"))
        guard.add(_pair(tolerance=2.0))

        with caplog.at_level(logging.ERROR, logger="test.guard"):
            for _ in range(3):
                _blocked(guard, {"y_axis_r": 15.0, "y_axis_l": 5.0})

        assert len(caplog.records) == 1


class TestStaleFeedback:
    def test_stale_member_blocks_whole_group(self) -> None:
        """片方だけ止めると残った側が押し続けて機構が壊れる。"""
        guard = SyncGuard()
        guard.add(_pair())
        blocked = _blocked(guard, {"y_axis_r": 0.0, "y_axis_l": 0.0}, stale={"y_axis_r"})
        assert blocked == frozenset({"y_axis"})

    def test_stale_group_is_not_judged_for_deviation(self) -> None:
        """欠けたメンバを含む比較は「ずれている」とも言えない。ラッチさせない。"""
        guard = SyncGuard()
        guard.add(_pair(tolerance=2.0))
        _blocked(guard, {"y_axis_r": 100.0, "y_axis_l": 0.0}, stale={"y_axis_r"})
        assert guard.violations == frozenset()

    def test_group_recovers_after_feedback_returns(self) -> None:
        guard = SyncGuard()
        guard.add(_pair())
        positions = {"y_axis_r": 10.0, "y_axis_l": -10.0}
        assert _blocked(guard, positions, stale={"y_axis_l"}) == frozenset({"y_axis"})
        assert _blocked(guard, positions) == frozenset()

    def test_stale_transition_is_logged_once(self, caplog: pytest.LogCaptureFixture) -> None:
        guard = SyncGuard(logger=logging.getLogger("test.guard"))
        guard.add(_pair())
        positions = {"y_axis_r": 0.0, "y_axis_l": 0.0}

        with caplog.at_level(logging.WARNING, logger="test.guard"):
            for _ in range(3):
                _blocked(guard, positions, stale={"y_axis_r"})

        assert len(caplog.records) == 1

    def test_position_is_not_read_for_stale_group(self) -> None:
        """途絶しているグループの位置は読まない (どのみち判定できない)。"""
        guard = SyncGuard()
        guard.add(_pair())
        read: list[str] = []

        def position_of(name: str) -> float:
            read.append(name)
            return 0.0

        guard.blocked(stale={"y_axis_r": True, "y_axis_l": False}, position_of=position_of)
        assert read == []


class TestReset:
    def test_reset_all(self) -> None:
        guard = SyncGuard()
        guard.add(_pair(tolerance=2.0))
        _blocked(guard, {"y_axis_r": 15.0, "y_axis_l": 5.0})
        guard.reset()
        assert guard.violations == frozenset()

    def test_reset_by_name(self) -> None:
        guard = SyncGuard()
        guard.add(_pair(tolerance=2.0))
        _blocked(guard, {"y_axis_r": 15.0, "y_axis_l": 5.0})
        guard.reset("y_axis")
        assert guard.violations == frozenset()

    def test_reset_unknown_group_raises(self) -> None:
        guard = SyncGuard()
        with pytest.raises(KeyError):
            guard.reset("y_axis")

    def test_reset_does_not_disable_detection(self) -> None:
        """解除は「監視を再び有効にする」であって「ずれを無かったことにする」ではない。"""
        guard = SyncGuard()
        guard.add(_pair(tolerance=2.0))
        positions = {"y_axis_r": 15.0, "y_axis_l": 5.0}
        _blocked(guard, positions)
        guard.reset()
        assert _blocked(guard, positions) == frozenset({"y_axis"})


def _pair_with_gain(
    name: str = "y_axis",
    *,
    sync_kp: float = 2.0,
    sync_limit: float = 1e9,
    tolerance: float = 2.0,
) -> SyncGroup:
    """同期補正を有効にした逆回転ペア。"""
    return SyncGroup(
        name=name,
        members=(MotorSpec(f"{name}_r", 1.0, 0.0), MotorSpec(f"{name}_l", -1.0, 0.0)),
        tolerance=tolerance,
        sync_kp=sync_kp,
        sync_limit=sync_limit,
    )


class TestCorrections:
    """この周期で補正を出してよいグループの選別。

    換算そのものは SyncGroup が持つので、ここが固定するのは「出さない条件」だけ。
    """

    def test_corrections_are_produced_for_a_configured_group(self) -> None:
        guard = SyncGuard()
        guard.add(_pair_with_gain())

        corrections = guard.corrections(
            position_of={"y_axis_r": 3.0, "y_axis_l": -1.0}.__getitem__,
            skip_groups=frozenset(),
        )

        # 人間の単位で r=3.0 / l=1.0 (平均 2.0)
        assert corrections["y_axis_r"] == pytest.approx(2.0 * (2.0 - 3.0) * 1.0)
        assert corrections["y_axis_l"] == pytest.approx(2.0 * (2.0 - 1.0) * -1.0)

    def test_no_corrections_without_gain(self) -> None:
        """sync_kp を設定していないグループには 1 台も出さない。"""
        guard = SyncGuard()
        guard.add(_pair())

        corrections = guard.corrections(
            position_of={"y_axis_r": 3.0, "y_axis_l": -1.0}.__getitem__,
            skip_groups=frozenset(),
        )

        assert corrections == {}

    def test_skipped_group_gets_no_corrections(self) -> None:
        """電流 0 に落とすグループへ補正だけが生き残ってはならない。

        力を抜いたはずの周期で左右が押し合う。
        """
        guard = SyncGuard()
        guard.add(_pair_with_gain())

        corrections = guard.corrections(
            position_of={"y_axis_r": 3.0, "y_axis_l": -1.0}.__getitem__,
            skip_groups=frozenset({"y_axis"}),
        )

        assert corrections == {}

    def test_position_is_not_read_for_skipped_group(self) -> None:
        """途絶したグループでは現在位置を読みに行かない。

        未受信のモータの 0.0 を現在位置として平均へ混ぜると、実在しない補正が出る。
        """
        guard = SyncGuard()
        guard.add(_pair_with_gain())
        read: list[str] = []

        def position_of(name: str) -> float:
            read.append(name)
            return 0.0

        guard.corrections(position_of=position_of, skip_groups=frozenset({"y_axis"}))

        assert read == []

    def test_only_the_requested_group_is_skipped(self) -> None:
        """グループが複数あるとき、止めた側だけが落ちる。"""
        guard = SyncGuard()
        guard.add(_pair_with_gain("y_axis"))
        guard.add(_pair_with_gain("rotate"))
        positions = {
            "y_axis_r": 3.0,
            "y_axis_l": -1.0,
            "rotate_r": 3.0,
            "rotate_l": -1.0,
        }

        corrections = guard.corrections(
            position_of=positions.__getitem__,
            skip_groups=frozenset({"y_axis"}),
        )

        assert set(corrections) == {"rotate_r", "rotate_l"}

    def test_latched_violation_can_be_skipped_by_the_caller(self) -> None:
        """偏差ラッチ中のグループは blocked() 経由で skip_groups に入る。

        ラッチは「人間がずれを直すまで力を抜く」宣言なので、補正で自動的に
        揃えにいってはならない (人間が原因に気付かないまま駆動が続く)。
        """
        guard = SyncGuard()
        guard.add(_pair_with_gain(tolerance=2.0))
        positions = {"y_axis_r": 5.0, "y_axis_l": 0.0}

        blocked = _blocked(guard, positions)
        assert "y_axis" in blocked

        corrections = guard.corrections(position_of=positions.__getitem__, skip_groups=blocked)

        assert corrections == {}
