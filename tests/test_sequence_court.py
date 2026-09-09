from __future__ import annotations

import asyncio

from lib.match_state import Court
from lib.sequence.engine import Sequence, step


class CourtAwareSequence(Sequence):
    def __init__(self) -> None:
        super().__init__("court_seq")
        self.executed: list[str] = []
        self.seen_courts: list[Court] = []

    @step("初期位置")
    async def home(self) -> None:
        self.executed.append("home")
        self.seen_courts.append(self.court)

    @step("前進", require_trigger=True)
    async def advance(self) -> None:
        self.executed.append("advance")

    @step("把持", require_trigger=True)
    async def grip(self) -> None:
        self.executed.append("grip")

    @step("復帰")
    async def back(self) -> None:
        self.executed.append("back")


class TestCourt:
    def test_default_court_is_unresolved(self) -> None:
        seq = CourtAwareSequence()
        assert seq.court is None

    def test_set_court(self) -> None:
        seq = CourtAwareSequence()
        seq.set_court(Court.BLUE)
        assert seq.court is Court.BLUE

    async def test_step_can_read_court(self) -> None:
        seq = CourtAwareSequence()
        seq.set_court(Court.BLUE)
        task = asyncio.create_task(seq.run())
        await asyncio.sleep(0.05)
        assert seq.seen_courts == [Court.BLUE]
        task.cancel()


class TestTriggerGate:
    async def test_stops_at_every_trigger_step(self) -> None:
        seq = CourtAwareSequence()
        task = asyncio.create_task(seq.run())
        await asyncio.sleep(0.05)

        assert seq.executed == ["home"]
        assert seq.waiting_trigger is True

        seq.trigger()
        await asyncio.sleep(0.05)
        assert seq.executed == ["home", "advance"]
        assert seq.waiting_trigger is True
        assert seq.current_step is not None
        assert seq.current_step.label == "把持"

        seq.trigger()
        await asyncio.sleep(0.05)
        assert seq.executed == ["home", "advance", "grip", "back"]
        task.cancel()


class TestStepMetadata:
    def test_require_trigger_exposed(self) -> None:
        seq = CourtAwareSequence()
        infos = seq.steps_info
        assert infos[0] == {"index": 0, "label": "初期位置", "require_trigger": False}
        assert infos[1] == {"index": 1, "label": "前進", "require_trigger": True}
