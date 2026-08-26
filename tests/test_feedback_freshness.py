"""フィードバック鮮度判定 (lib/control/feedback.py) のテスト。

「古い実測値で制御を続けない」という判断は位置制御ループと同期監視の両方が行う。
閾値の比較が 2 実装に分かれると、片方だけ直したときに気付けないためここで固定する。
"""

from __future__ import annotations

from lib.control.feedback import FeedbackFreshness


class _Clock:
    def __init__(self, start: float = 5000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


def _freshness(last: dict[str, float], clock: _Clock, timeout_ms: float = 500.0):
    return FeedbackFreshness(last.get, timeout_ms=timeout_ms, clock=clock)


class TestStaleJudgement:
    def test_never_received_is_stale(self) -> None:
        clock = _Clock()
        fresh = _freshness({}, clock)
        assert fresh.is_stale("lift", clock.now) is True

    def test_within_timeout_is_fresh(self) -> None:
        clock = _Clock()
        fresh = _freshness({"lift": clock.now}, clock)
        clock.advance(0.4)
        assert fresh.is_stale("lift", clock.now) is False

    def test_beyond_timeout_is_stale(self) -> None:
        clock = _Clock()
        fresh = _freshness({"lift": clock.now}, clock)
        clock.advance(0.6)
        assert fresh.is_stale("lift", clock.now) is True

    def test_exactly_at_timeout_is_fresh(self) -> None:
        """境界は「超えたら」。ちょうど閾値で落とすと正常時に断続的に電流が抜ける。"""
        clock = _Clock()
        fresh = _freshness({"lift": clock.now}, clock)
        clock.advance(0.5)
        assert fresh.is_stale("lift", clock.now) is False


class TestSnapshot:
    def test_now_reads_the_injected_clock(self) -> None:
        clock = _Clock()
        fresh = _freshness({}, clock)
        clock.advance(1.0)
        assert fresh.now() == 5001.0

    def test_all_motors_are_judged_against_one_instant(self) -> None:
        """1 周期の中で時刻を取り直すと、同じペアの左右が別の瞬間で判定される。"""
        clock = _Clock()
        last = {"left": clock.now, "right": clock.now}
        fresh = _freshness(last, clock)
        now = fresh.now()
        clock.advance(10.0)

        assert fresh.is_stale("left", now) is False
        assert fresh.is_stale("right", now) is False
