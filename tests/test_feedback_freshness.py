from __future__ import annotations

from lib.control.feedback import FeedbackFreshness
from tests.fake_clock import FakeClock


def _freshness(last: dict[str, float], clock: FakeClock, timeout_ms: float = 500.0):
    return FeedbackFreshness(last.get, timeout_ms=timeout_ms, clock=clock)


class TestStaleJudgement:
    def test_never_received_is_stale(self) -> None:
        clock = FakeClock(start=5000.0)
        fresh = _freshness({}, clock)
        assert fresh.is_stale("lift", clock.now) is True

    def test_within_timeout_is_fresh(self) -> None:
        clock = FakeClock(start=5000.0)
        fresh = _freshness({"lift": clock.now}, clock)
        clock.advance(0.4)
        assert fresh.is_stale("lift", clock.now) is False

    def test_beyond_timeout_is_stale(self) -> None:
        clock = FakeClock(start=5000.0)
        fresh = _freshness({"lift": clock.now}, clock)
        clock.advance(0.6)
        assert fresh.is_stale("lift", clock.now) is True

    def test_exactly_at_timeout_is_fresh(self) -> None:
        clock = FakeClock(start=5000.0)
        fresh = _freshness({"lift": clock.now}, clock)
        clock.advance(0.5)
        assert fresh.is_stale("lift", clock.now) is False


class TestSnapshot:
    def test_now_reads_the_injected_clock(self) -> None:
        clock = FakeClock(start=5000.0)
        fresh = _freshness({}, clock)
        clock.advance(1.0)
        assert fresh.now() == 5001.0

    def test_all_motors_are_judged_against_one_instant(self) -> None:
        clock = FakeClock(start=5000.0)
        last = {"left": clock.now, "right": clock.now}
        fresh = _freshness(last, clock)
        now = fresh.now()
        clock.advance(10.0)

        assert fresh.is_stale("left", now) is False
        assert fresh.is_stale("right", now) is False
