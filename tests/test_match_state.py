from __future__ import annotations

import inspect
import time

import pytest

from lib.config_schema import MatchSettings
from lib.match_state import (
    PHASES_ANY,
    PHASES_DURING_MATCH,
    PHASES_OUTSIDE_MATCH,
    PHASES_PREPARATION,
    PHASES_START_GATE,
    ROLE_PRE_MATCH,
    ChecklistItem,
    Court,
    MatchState,
    Phase,
)
from tests.fake_clock import FakeClock

_DEFS = {
    ROLE_PRE_MATCH: [
        ChecklistItem(id="power", label="電源投入確認"),
        ChecklistItem(id="home", label="メインハンド初期位置確認"),
    ],
}


def _make() -> MatchState:
    return MatchState(definitions=_DEFS)


def _make_with_clock(clock: FakeClock, *, duration_s: float = 180.0) -> MatchState:
    return MatchState(definitions=_DEFS, settings=MatchSettings(duration_s=duration_s), clock=clock)


def _enter_match(state: MatchState) -> None:
    _complete_all(state)
    assert state.match_start() is True


def _complete(state: MatchState, role: str) -> None:
    for item in state.checklists[role].items:
        state.set_checklist_item(role, item.id, True)


def _complete_all(state: MatchState) -> None:
    _complete(state, ROLE_PRE_MATCH)


class TestDefaults:
    def test_initial_state(self) -> None:
        state = _make()
        assert state.court is Court.RED
        assert state.phase is Phase.SETUP
        assert state.can_start_match is False

    def test_definitions_are_copied_into_each_state(self) -> None:
        """定義をそのまま持つと、ある MatchState のチェックが定義側を汚す。

        定義は yaml から 1 度だけ読んで使い回すため、複製しないと試合を
        リセットしても前回のチェックが残ったまま READY で立ち上がる。
        """
        first = _make()
        _complete(first, ROLE_PRE_MATCH)
        assert first.can_start_match is True

        second = _make()
        assert second.can_start_match is False

    def test_role_is_a_single_pre_match_list(self) -> None:
        """ロールが増減すると試合開始ゲートの対象がそのまま変わる。

        操縦者 2 名で分けていたものを 1 つへ統合した。分かれていた頃は
        「片方だけ完了」で開始できない状態に意味があったが、2 名が同じ場所で
        同じ機体を見る以上、独立した確認にはなっていなかった。
        """
        assert set(_make().checklists) == {ROLE_PRE_MATCH}


class TestChecklistCompletion:
    def test_needs_every_item_complete(self) -> None:
        state = _make()
        # 1 項目でも残っている間は試合に入れない
        state.set_checklist_item(ROLE_PRE_MATCH, "power", True)
        assert state.checklists[ROLE_PRE_MATCH].completed is False
        assert state.can_start_match is False
        assert state.phase is Phase.SETUP

        state.set_checklist_item(ROLE_PRE_MATCH, "home", True)
        assert state.checklists[ROLE_PRE_MATCH].completed is True
        assert state.can_start_match is True
        assert state.phase is Phase.READY

    def test_unchecking_returns_to_setup(self) -> None:
        state = _make()
        _complete_all(state)
        assert state.phase is Phase.READY

        state.set_checklist_item(ROLE_PRE_MATCH, "power", False)
        assert state.phase is Phase.SETUP
        assert state.can_start_match is False

    def test_unknown_role_or_item_is_rejected(self) -> None:
        state = _make()
        assert state.set_checklist_item("nobody", "power", True) is False
        assert state.set_checklist_item(ROLE_PRE_MATCH, "no_such_item", True) is False

    def test_empty_checklist_counts_as_complete(self) -> None:
        """項目未定義のロールで永久にゲートが開かなくなるのを防ぐ。"""
        state = MatchState(definitions={ROLE_PRE_MATCH: []})
        assert state.phase is Phase.READY


class TestCourtChange:
    def test_court_change_resets_checklists(self) -> None:
        state = _make()
        _complete_all(state)
        assert state.phase is Phase.READY

        assert state.set_court(Court.BLUE) is True
        assert state.court is Court.BLUE
        assert state.phase is Phase.SETUP

    def test_same_value_is_noop(self) -> None:
        """同値の再設定でチェックリストを消さない (誤操作での作業やり直しを防ぐ)。"""
        state = _make()
        _complete_all(state)

        assert state.set_court(Court.RED) is True
        assert state.phase is Phase.READY

    def test_court_change_denied_during_match(self) -> None:
        state = _make()
        _complete_all(state)
        state.match_start()

        assert state.set_court(Court.BLUE) is False
        assert state.court is Court.RED


class TestPhaseTransitions:
    def test_match_start_requires_ready(self) -> None:
        state = _make()
        assert state.match_start() is False
        assert state.phase is Phase.SETUP

        _complete_all(state)
        assert state.match_start() is True
        assert state.phase is Phase.MATCH

    def test_match_finish_requires_match(self) -> None:
        state = _make()
        assert state.match_finish() is False

        _complete_all(state)
        state.match_start()
        assert state.match_finish() is True
        assert state.phase is Phase.FINISHED

    def test_match_reset_from_any_phase(self) -> None:
        state = _make()
        state.set_court(Court.BLUE)
        _complete_all(state)
        state.match_start()

        assert state.match_reset() is True
        assert state.phase is Phase.SETUP
        assert state.checklists[ROLE_PRE_MATCH].completed is False
        # コートは試合後もそのまま維持する (次の試合も同条件が普通)
        assert state.court is Court.BLUE

    def test_checklist_locked_during_match(self) -> None:
        state = _make()
        _complete_all(state)
        state.match_start()
        assert state.set_checklist_item(ROLE_PRE_MATCH, "power", False) is False
        assert state.phase is Phase.MATCH


class TestPhaseSets:
    """遷移条件の名前付き集合。lib/commands.py のコマンドゲートも同じ定数を参照する。"""

    def test_allows_follows_current_phase(self) -> None:
        state = _make()
        assert state.allows(PHASES_PREPARATION) is True
        assert state.allows(PHASES_DURING_MATCH) is False

        _complete_all(state)
        state.match_start()
        assert state.allows(PHASES_DURING_MATCH) is True
        assert state.allows(PHASES_PREPARATION) is False
        assert state.allows(PHASES_OUTSIDE_MATCH) is False

    def test_any_covers_every_phase(self) -> None:
        """全フェーズ許可の宣言が 1 つでもフェーズを取りこぼすと、そのコマンドが死ぬ。"""
        assert frozenset(Phase) == PHASES_ANY

    def test_outside_match_is_the_complement_of_during_match(self) -> None:
        assert PHASES_OUTSIDE_MATCH == PHASES_ANY - PHASES_DURING_MATCH

    def test_start_gate_is_ready_only(self) -> None:
        """指差喚呼が揃った READY 以外から試合へ入れてはならない。"""
        assert frozenset({Phase.READY}) == PHASES_START_GATE


class TestSerialization:
    def test_to_dict_shape(self) -> None:
        state = _make()
        state.set_checklist_item(ROLE_PRE_MATCH, "home", True)
        payload = state.to_dict()

        assert payload["type"] == "match_state"
        assert payload["court"] == "red"
        assert payload["phase"] == "setup"
        assert payload["can_start_match"] is False
        assert set(payload["checklists"]) == {ROLE_PRE_MATCH}

        checklist = payload["checklists"][ROLE_PRE_MATCH]
        assert checklist["completed"] is False
        assert checklist["items"] == [
            {"id": "power", "label": "電源投入確認", "checked": False, "group": None},
            {"id": "home", "label": "メインハンド初期位置確認", "checked": True, "group": None},
        ]


class TestMatchTimer:
    """試合時間タイマー。全デバイスの表示はこの経過時間だけを起点にする。

    サーバーは残り時間ではなく「配信瞬間の経過ミリ秒」を配り、各デバイスが
    自分の単調時計で進める。したがってここが誤ると、ずれは 1 台ではなく
    **全デバイスで同じだけ**ずれる (画面同士を見比べても気付けない)。
    """

    def test_default_clock_is_monotonic(self) -> None:
        """既定の時刻源は単調時計であること。

        time.time() は NTP 補正で後ろへ飛ぶことがあり、試合中に残り時間が
        増える。全デバイスがこの値を起点にするため、ずれは 1 台ではなく
        **全画面で同じだけ**現れ、見比べても気付けない。
        テストは必ず clock を注入するので、既定値はここでしか踏まれない。
        """
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
        """セッティングタイムに費やした時間が試合時間に混ざってはならない。"""
        clock = FakeClock()
        state = _make_with_clock(clock)
        clock.advance(300.0)
        _enter_match(state)

        assert state.elapsed_s == pytest.approx(0.0)

    def test_denied_match_start_does_not_move_the_origin(self) -> None:
        """試合中に届いた match_start はフェーズゲートで弾かれる。そこで起点を
        引き直すと、機体は動いたままタイマーだけが満了時間へ巻き戻る。

        操縦者の押し間違いや Monitor の二重送信 1 回で成立し、しかも
        「弾かれた」ことは画面に出るので**タイマーの巻き戻りだけが残る**。
        """
        clock = FakeClock()
        state = _make_with_clock(clock)
        _enter_match(state)
        clock.advance(40.0)

        assert state.match_start() is False
        assert state.elapsed_s == pytest.approx(40.0)

    def test_finish_freezes_the_value(self) -> None:
        """結果確認中に数字が進み続けると、何秒で終えたのかが読めなくなる。"""
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
        """1 試合目の凍結値が残ると、2 試合目が途中から始まる。"""
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
        """試合時間は config/system.yaml の値。ここに数値を焼き付けると
        当日ルールが変わったときに yaml を直しても画面が追従しない。"""
        state = _make_with_clock(FakeClock(), duration_s=90.0)
        assert state.to_dict()["timer"]["duration_ms"] == 90000


class TestLoadDefinitions:
    def test_load_from_mapping(self) -> None:
        from lib.match_state import load_checklist_definitions

        defs = load_checklist_definitions(
            {
                "checklists": {
                    ROLE_PRE_MATCH: [{"id": "power", "label": "電源投入確認"}],
                }
            }
        )
        assert defs[ROLE_PRE_MATCH] == [ChecklistItem(id="power", label="電源投入確認")]

    def test_load_always_defines_every_role(self) -> None:
        """定義が空でもロールは必ず存在させる (KeyError を UI 側に出さない)。"""
        from lib.match_state import ALL_ROLES, load_checklist_definitions

        defs = load_checklist_definitions({})
        assert set(defs) == set(ALL_ROLES)
        assert defs[ROLE_PRE_MATCH] == []

    def test_load_skips_malformed_entries(self) -> None:
        from lib.match_state import load_checklist_definitions

        defs = load_checklist_definitions(
            {
                "checklists": {
                    ROLE_PRE_MATCH: [
                        {"id": "ok", "label": "有効"},
                        {"label": "id なし"},
                        "文字列",
                    ],
                }
            }
        )
        assert defs[ROLE_PRE_MATCH] == [ChecklistItem(id="ok", label="有効")]

    def test_load_carries_group(self) -> None:
        """group は「その項目をどのコントロールの隣に置くか」の唯一の宣言。

        サーバーは語彙を検証せず素通しする。未知の名前を弾くと、UI が知らない
        group を書いた瞬間に起動しなくなり、区分を持たないベンチ設定 6 セットも
        通らない。項目が画面から消えないことは UI 側 (未知は「その他」へ描く) が守る。
        """
        from lib.match_state import load_checklist_definitions

        defs = load_checklist_definitions(
            {
                "checklists": {
                    ROLE_PRE_MATCH: [
                        {"id": "court", "label": "コート一致", "group": "court"},
                        {"id": "power", "label": "電源投入"},
                    ],
                }
            }
        )
        assert defs[ROLE_PRE_MATCH] == [
            ChecklistItem(id="court", label="コート一致", group="court"),
            ChecklistItem(id="power", label="電源投入", group=None),
        ]

    def test_group_survives_rebuild_and_reaches_the_wire(self) -> None:
        """定義の複製 (_rebuild_checklists) で group を落とすと、画面では全項目が
        「その他」へ落ちる。症状は「配置だけが効かない」で、config からもログからも
        理由が読めない。"""
        state = MatchState({ROLE_PRE_MATCH: [ChecklistItem(id="court", label="C", group="court")]})

        items = state.to_dict()["checklists"][ROLE_PRE_MATCH]["items"]
        assert items == [{"id": "court", "label": "C", "checked": False, "group": "court"}]
