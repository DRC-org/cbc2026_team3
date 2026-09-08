from __future__ import annotations

import time
from collections.abc import Callable

__all__ = ["FeedbackFreshness"]

LastFeedbackAt = Callable[[str], float | None]
Clock = Callable[[], float]


class FeedbackFreshness:
    def __init__(
        self,
        last_feedback_at: LastFeedbackAt,
        *,
        timeout_ms: float,
        clock: Clock = time.time,
    ) -> None:
        self._last_feedback_at = last_feedback_at
        self._timeout_ms = timeout_ms
        self._clock = clock

    def now(self) -> float:
        return self._clock()

    def is_stale(self, name: str, now: float) -> bool:
        last_rx = self._last_feedback_at(name)
        if last_rx is None:
            return True
        return (now - last_rx) * 1000.0 > self._timeout_ms
