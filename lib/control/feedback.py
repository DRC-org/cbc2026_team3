"""フィードバック鮮度の判定を制御層で共有する。

位置制御ループ (電流を 0 に落とす) と同期監視 (偏差判定から除外する) は、
どちらも「この実測値はまだ信じてよいか」を同じ閾値で判断する。かつては
``(now - last_rx) * 1000.0 > timeout_ms`` の比較が両方に書き写されており、
片方だけ境界を直しても気付けない状態だった。

判定できないものを異常として扱わないのがこの層の原則である。未受信のモータを
位置 0 とみなすと、起動直後にいきなり偏差超過と判定して緊急停止する。
"""

from __future__ import annotations

import time
from collections.abc import Callable

__all__ = ["FeedbackFreshness"]

LastFeedbackAt = Callable[[str], float | None]
Clock = Callable[[], float]


class FeedbackFreshness:
    """モータ名から「最終受信からの経過が許容内か」を答える。"""

    def __init__(
        self,
        last_feedback_at: LastFeedbackAt,
        *,
        timeout_ms: float,
        clock: Clock = time.time,
    ) -> None:
        """
        Args:
            last_feedback_at: モータ名 → 最終受信時刻 (``CANManager.last_feedback_at``)
            timeout_ms: これを超えて受信が無ければ古いとみなす
            clock: ``last_feedback_at`` と同じ時間軸のクロック (既定は壁時計)
        """
        self._last_feedback_at = last_feedback_at
        self._timeout_ms = timeout_ms
        self._clock = clock

    def now(self) -> float:
        """判定に使う現在時刻。1 周期に 1 回だけ取ること。

        モータごとに取り直すと、左右直結ペアの 2 台が別の瞬間で判定され、
        「片方だけ途絶扱い」という起きてはならない状態が一瞬生まれる。
        """
        return self._clock()

    def is_stale(self, name: str, now: float) -> bool:
        """``now`` 時点でフィードバックが古い (または未受信) なら True。"""
        last_rx = self._last_feedback_at(name)
        if last_rx is None:
            return True
        return (now - last_rx) * 1000.0 > self._timeout_ms
