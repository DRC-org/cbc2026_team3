from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum

from lib.config_schema import DEFAULT_MATCH, MatchSettings


class Court(StrEnum):
    RED = "red"
    BLUE = "blue"


class Phase(StrEnum):
    SETUP = "setup"
    READY = "ready"
    MATCH = "match"
    FINISHED = "finished"


PHASES_ANY: frozenset[Phase] = frozenset(Phase)

PHASES_DURING_MATCH: frozenset[Phase] = frozenset({Phase.MATCH})

PHASES_OUTSIDE_MATCH: frozenset[Phase] = frozenset({Phase.SETUP, Phase.READY, Phase.FINISHED})

PHASES_START_GATE: frozenset[Phase] = frozenset({Phase.READY})


class MatchState:
    def __init__(
        self,
        *,
        court: Court | None = None,
        settings: MatchSettings = DEFAULT_MATCH,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._court: Court | None = court
        self._settings = settings
        self._clock = clock
        self._started_at: float | None = None
        self._frozen_elapsed_s: float | None = None
        self._phase = Phase.SETUP
        self._sync_phase()

    @property
    def court(self) -> Court | None:
        """操縦者が選んだコート。**選ぶまでは `None` (未確定)。**

        既定を赤にすると、青コートで選び忘れたことが画面にもログにも現れない
        まま `sub_lift` の `scale` が符号ごと反転して昇降が逆へ走る。
        """
        return self._court

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def can_start_match(self) -> bool:
        """試合を開始できるか。**開始可否の単一情報源で、判定を外に増やさない。**

        コート未確定をここへ載せると `_sync_phase` が拾って SETUP に留まるので、
        開始ゲートは自動的に閉じる。
        """
        return self._court is not None

    def allows(self, phases: frozenset[Phase]) -> bool:
        return self._phase in phases

    @property
    def timer_running(self) -> bool:
        return self._started_at is not None and self._frozen_elapsed_s is None

    @property
    def elapsed_s(self) -> float:
        if self._frozen_elapsed_s is not None:
            return self._frozen_elapsed_s
        if self._started_at is None:
            return 0.0
        return self._clock() - self._started_at

    def set_court(self, court: Court) -> bool:
        if not self.allows(PHASES_OUTSIDE_MATCH):
            return False
        self._court = court
        self._sync_phase()
        return True

    def match_start(self) -> bool:
        if not self.allows(PHASES_START_GATE):
            return False
        self._phase = Phase.MATCH
        self._started_at = self._clock()
        return True

    def match_finish(self) -> bool:
        if not self.allows(PHASES_DURING_MATCH):
            return False
        self._phase = Phase.FINISHED
        self._frozen_elapsed_s = self.elapsed_s
        return True

    def match_reset(self) -> bool:
        # 試合ごとに必ず選び直させる。前の試合のコートが残ると、次の試合で
        # 選び忘れたことが画面に現れない
        self._court = None
        self._phase = Phase.SETUP
        self._started_at = None
        self._frozen_elapsed_s = None
        self._sync_phase()
        return True

    def _sync_phase(self) -> None:
        if self._phase in (Phase.MATCH, Phase.FINISHED):
            return
        self._phase = Phase.READY if self.can_start_match else Phase.SETUP

    def to_dict(self) -> dict:
        return {
            "type": "match_state",
            "court": None if self._court is None else self._court.value,
            "phase": self._phase.value,
            "can_start_match": self.can_start_match,
            "timer": {
                "running": self.timer_running,
                "elapsed_ms": round(self.elapsed_s * 1000),
                "duration_ms": round(self._settings.duration_s * 1000),
            },
        }
