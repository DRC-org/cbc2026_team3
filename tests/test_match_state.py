from __future__ import annotations

import inspect
import time

import pytest

from lib.config_schema import MatchSettings
from lib.match_state import (
    PHASES_ANY,
    PHASES_DURING_MATCH,
    PHASES_OUTSIDE_MATCH,
    PHASES_START_GATE,
    Court,
    MatchState,
    Phase,
)
from tests.fake_clock import FakeClock


def _make() -> MatchState:
    return MatchState()


def _make_with_clock(clock: FakeClock, *, duration_s: float = 180.0) -> MatchState:
    return MatchState(settings=MatchSettings(duration_s=duration_s), clock=clock)


def _enter_match(state: MatchState) -> None:
    state.set_court(Court.RED)
    assert state.match_start() is True


class TestDefaults:
    def test_initial_state(self) -> None:
        state = _make()
        assert state.court is None
        assert state.phase is Phase.SETUP
        assert state.can_start_match is False


class TestCourtChange:
    def test_court_change_keeps_ready(self) -> None:
        state = _make()
        state.set_court(Court.RED)
        assert state.phase is Phase.READY

        assert state.set_court(Court.BLUE) is True
        assert state.court is Court.BLUE
        assert state.phase is Phase.READY

    def test_same_value_is_noop(self) -> None:
        state = _make()
        state.set_court(Court.RED)

        assert state.set_court(Court.RED) is True
        assert state.phase is Phase.READY

    def test_court_change_denied_during_match(self) -> None:
        state = _make()
        _enter_match(state)

        assert state.set_court(Court.BLUE) is False
        assert state.court is Court.RED


class TestCourtUnresolved:
    """コート未確定という状態。**開始ゲートは `can_start_match` 1 つで閉じる。**"""

    def test_selecting_the_court_opens_the_gate(self) -> None:
        state = _make()
        assert state.set_court(Court.BLUE) is True

        assert state.can_start_match is True
        assert state.phase is Phase.READY


class TestPhaseTransitions:
    def test_match_start_requires_ready(self) -> None:
        state = _make()
        assert state.match_start() is False
        assert state.phase is Phase.SETUP

        state.set_court(Court.RED)
        assert state.match_start() is True
        assert state.phase is Phase.MATCH

    def test_match_finish_requires_match(self) -> None:
        state = _make()
        assert state.match_finish() is False

        _enter_match(state)
        assert state.match_finish() is True
        assert state.phase is Phase.FINISHED

    def test_match_reset_from_any_phase(self) -> None:
        state = _make()
        _enter_match(state)

        assert state.match_reset() is True
        assert state.phase is Phase.SETUP

    def test_match_reset_clears_the_court(self) -> None:
        state = _make()
        state.set_court(Court.BLUE)
        state.match_start()

        state.match_reset()
        assert state.court is None
        assert state.can_start_match is False
        assert state.match_start() is False


class TestPhaseSets:
    def test_allows_follows_current_phase(self) -> None:
        state = _make()
        assert state.allows(PHASES_OUTSIDE_MATCH) is True
        assert state.allows(PHASES_DURING_MATCH) is False

        _enter_match(state)
        assert state.allows(PHASES_DURING_MATCH) is True
        assert state.allows(PHASES_OUTSIDE_MATCH) is False

    def test_any_covers_every_phase(self) -> None:
        assert frozenset(Phase) == PHASES_ANY

    def test_outside_match_is_the_complement_of_during_match(self) -> None:
        assert PHASES_OUTSIDE_MATCH == PHASES_ANY - PHASES_DURING_MATCH

    def test_start_gate_is_ready_only(self) -> None:
        assert frozenset({Phase.READY}) == PHASES_START_GATE


class TestSerialization:
    def test_to_dict_shape(self) -> None:
        payload = _make().to_dict()

        assert payload["type"] == "match_state"
        assert payload["court"] is None
        assert payload["phase"] == "setup"
        assert payload["can_start_match"] is False


class TestMatchTimer:
    def test_default_clock_is_monotonic(self) -> None:
        default = inspect.signature(MatchState.__init__).parameters["clock"].default
        assert default is time.monotonic

    def test_not_started_reads_zero(self) -> None:
        state = _make_with_clock(FakeClock())
        assert state.timer_running is False
        assert state.elapsed_s == 0.0

    def test_elapsed_advances_with_injected_clock(self) -> None:
        clock = FakeClock()
        state = _make_with_clock(clock)
        _enter_match(state)

        clock.advance(12.5)
        assert state.timer_running is True
        assert state.elapsed_s == pytest.approx(12.5)

    def test_time_before_start_is_not_counted(self) -> None:
        clock = FakeClock()
        state = _make_with_clock(clock)
        clock.advance(300.0)
        _enter_match(state)

        assert state.elapsed_s == pytest.approx(0.0)

    def test_denied_match_start_does_not_move_the_origin(self) -> None:
        clock = FakeClock()
        state = _make_with_clock(clock)
        _enter_match(state)
        clock.advance(40.0)

        assert state.match_start() is False
        assert state.elapsed_s == pytest.approx(40.0)

    def test_finish_freezes_the_value(self) -> None:
        clock = FakeClock()
        state = _make_with_clock(clock)
        _enter_match(state)

        clock.advance(95.0)
        assert state.match_finish() is True
        frozen = state.elapsed_s

        clock.advance(60.0)
        assert state.timer_running is False
        assert state.elapsed_s == pytest.approx(frozen)
        assert frozen == pytest.approx(95.0)

    def test_reset_clears_the_timer(self) -> None:
        clock = FakeClock()
        state = _make_with_clock(clock)
        _enter_match(state)
        clock.advance(50.0)

        assert state.match_reset() is True
        clock.advance(10.0)
        assert state.timer_running is False
        assert state.elapsed_s == 0.0

    def test_second_match_starts_from_zero(self) -> None:
        clock = FakeClock()
        state = _make_with_clock(clock)
        _enter_match(state)
        clock.advance(80.0)
        state.match_finish()
        state.match_reset()

        clock.advance(30.0)
        _enter_match(state)
        clock.advance(5.0)

        assert state.timer_running is True
        assert state.elapsed_s == pytest.approx(5.0)

    def test_timer_payload_carries_elapsed_and_duration(self) -> None:
        clock = FakeClock()
        state = _make_with_clock(clock, duration_s=120.0)
        _enter_match(state)
        clock.advance(7.25)

        timer = state.to_dict()["timer"]
        assert timer == {"running": True, "elapsed_ms": 7250, "duration_ms": 120000}

    def test_duration_comes_from_settings_not_a_literal(self) -> None:
        state = _make_with_clock(FakeClock(), duration_s=90.0)
        assert state.to_dict()["timer"]["duration_ms"] == 90000
