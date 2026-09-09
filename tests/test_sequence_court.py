from __future__ import annotations

import asyncio

import pytest

from lib.match_state import Court
from lib.sequence.engine import Sequence, step
from lib.sequence.positions import CourtUnresolvedError, PositionTable, load_position_table


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


class TestUnresolvedCourt:
    """未確定のまま動かすと、**コート依存軸だけ**が落ちる。非依存軸は今までどおり。"""

    def _table(self) -> PositionTable:
        return load_position_table(
            {
                "axes": {
                    "sub_lift": {
                        "unit": "mm",
                        "command_unit": "rad",
                        "scale": {"red": -1.0, "blue": 1.0},
                        "tolerance": 1.0,
                    },
                    "gripper": {"unit": "deg", "command_unit": "deg", "scale": 1.0},
                },
                "positions": {"sub_lift": {"top": 0.0}, "gripper": {"open": 5.0}},
            },
            source="<test>",
        )

    def test_コート依存軸は換算できない(self) -> None:
        table = self._table()
        with pytest.raises(CourtUnresolvedError):
            table.commands("sub_lift", "top", court=None)

    def test_コート非依存軸は換算できる(self) -> None:
        table = self._table()
        assert table.commands("gripper", "open", court=None) == {"gripper": 5.0}

    def test_コートを解決すれば通る(self) -> None:
        table = self._table()
        assert table.commands("sub_lift", "top", court=Court.BLUE) == {"sub_lift": 0.0}

    def test_for_court_は未確定を黙って赤へ倒さない(self) -> None:
        spec = self._table().axis("sub_lift")
        assert spec.for_court(None) is spec
        assert spec.for_court(Court.RED) is not spec


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
