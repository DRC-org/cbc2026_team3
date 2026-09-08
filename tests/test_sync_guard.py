from __future__ import annotations

import logging

import pytest

from lib.axis_sync import MotorSpec, SyncGroup
from lib.control.sync_guard import SyncGuard


def _pair(name: str = "y_axis", tolerance: float = 2.0) -> SyncGroup:
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
        assert _blocked(guard, {"y_axis_r": 5.0, "y_axis_l": -5.0}) == frozenset({"y_axis"})
        assert guard.violations == frozenset({"y_axis"})

    def test_no_debounce(self) -> None:
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
        guard = SyncGuard()
        guard.add(_pair())
        blocked = _blocked(guard, {"y_axis_r": 0.0, "y_axis_l": 0.0}, stale={"y_axis_r"})
        assert blocked == frozenset({"y_axis"})

    def test_stale_group_is_not_judged_for_deviation(self) -> None:
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
    return SyncGroup(
        name=name,
        members=(MotorSpec(f"{name}_r", 1.0, 0.0), MotorSpec(f"{name}_l", -1.0, 0.0)),
        tolerance=tolerance,
        sync_kp=sync_kp,
        sync_limit=sync_limit,
    )


class TestCorrections:
    def test_corrections_are_produced_for_a_configured_group(self) -> None:
        guard = SyncGuard()
        guard.add(_pair_with_gain())

        corrections = guard.corrections(
            position_of={"y_axis_r": 3.0, "y_axis_l": -1.0}.__getitem__,
            skip_groups=frozenset(),
        )

        assert corrections["y_axis_r"] == pytest.approx(2.0 * (2.0 - 3.0) * 1.0)
        assert corrections["y_axis_l"] == pytest.approx(2.0 * (2.0 - 1.0) * -1.0)

    def test_no_corrections_without_gain(self) -> None:
        guard = SyncGuard()
        guard.add(_pair())

        corrections = guard.corrections(
            position_of={"y_axis_r": 3.0, "y_axis_l": -1.0}.__getitem__,
            skip_groups=frozenset(),
        )

        assert corrections == {}

    def test_skipped_group_gets_no_corrections(self) -> None:
        guard = SyncGuard()
        guard.add(_pair_with_gain())

        corrections = guard.corrections(
            position_of={"y_axis_r": 3.0, "y_axis_l": -1.0}.__getitem__,
            skip_groups=frozenset({"y_axis"}),
        )

        assert corrections == {}

    def test_position_is_not_read_for_skipped_group(self) -> None:
        guard = SyncGuard()
        guard.add(_pair_with_gain())
        read: list[str] = []

        def position_of(name: str) -> float:
            read.append(name)
            return 0.0

        guard.corrections(position_of=position_of, skip_groups=frozenset({"y_axis"}))

        assert read == []

    def test_only_the_requested_group_is_skipped(self) -> None:
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
        guard = SyncGuard()
        guard.add(_pair_with_gain(tolerance=2.0))
        positions = {"y_axis_r": 5.0, "y_axis_l": 0.0}

        blocked = _blocked(guard, positions)
        assert "y_axis" in blocked

        corrections = guard.corrections(position_of=positions.__getitem__, skip_groups=blocked)

        assert corrections == {}
