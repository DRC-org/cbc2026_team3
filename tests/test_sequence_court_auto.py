from __future__ import annotations

import asyncio

from lib.match_state import Court
from lib.sequence.engine import Sequence, step


class CourtAwareSequence(Sequence):
    """コート依存の動作と、全自動でも停止したいステップを持つテスト用シーケンス。"""

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

    @step("把持", require_trigger=True, auto_stop=True)
    async def grip(self) -> None:
        self.executed.append("grip")

    @step("復帰")
    async def back(self) -> None:
        self.executed.append("back")


class TestCourt:
    def test_default_court_is_red(self) -> None:
        seq = CourtAwareSequence()
        assert seq.court is Court.RED

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


class TestAutoAdvance:
    def test_default_is_manual(self) -> None:
        seq = CourtAwareSequence()
        assert seq.auto_advance is False

    async def test_auto_advance_skips_trigger_wait(self) -> None:
        """全自動では require_trigger のステップもトリガー待ちなしで通過する。"""
        seq = CourtAwareSequence()
        seq.set_auto_advance(True)

        task = asyncio.create_task(seq.run())
        await asyncio.sleep(0.05)

        # auto_stop=True の grip 直前で止まっているはず
        assert seq.executed == ["home", "advance"]
        assert seq.waiting_trigger is True
        assert seq.current_step is not None
        assert seq.current_step.label == "把持"

        seq.trigger()
        await asyncio.sleep(0.05)
        assert seq.executed == ["home", "advance", "grip", "back"]
        task.cancel()

    async def test_manual_mode_stops_at_first_trigger_step(self) -> None:
        seq = CourtAwareSequence()
        task = asyncio.create_task(seq.run())
        await asyncio.sleep(0.05)

        assert seq.executed == ["home"]
        assert seq.waiting_trigger is True
        task.cancel()

    async def test_auto_advance_toggle_takes_effect_on_next_run(self) -> None:
        seq = CourtAwareSequence()
        seq.set_auto_advance(True)
        task = asyncio.create_task(seq.run())
        await asyncio.sleep(0.05)
        seq.trigger()
        await asyncio.sleep(0.05)
        assert seq.executed == ["home", "advance", "grip", "back"]
        task.cancel()

        seq.executed.clear()
        seq.set_auto_advance(False)
        await seq.reset()
        task2 = asyncio.create_task(seq.run())
        await asyncio.sleep(0.05)
        assert seq.executed == ["home"]
        task2.cancel()


class TestStepMetadata:
    def test_auto_stop_flag_exposed(self) -> None:
        seq = CourtAwareSequence()
        infos = seq.steps_info
        assert infos[1] == {
            "index": 1,
            "label": "前進",
            "require_trigger": True,
            "auto_stop": False,
        }
        assert infos[2]["auto_stop"] is True
