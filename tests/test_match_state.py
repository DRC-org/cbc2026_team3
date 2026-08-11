from __future__ import annotations

import pytest

from lib.match_state import (
    ROLE_MAIN_HAND,
    ROLE_MONITOR,
    ROLE_SUB_HAND,
    ChecklistItem,
    Court,
    MatchState,
    Mode,
    Phase,
)

_DEFS = {
    ROLE_MONITOR: [
        ChecklistItem(id="power", label="電源投入確認"),
        ChecklistItem(id="estop", label="非常停止解除確認"),
    ],
    ROLE_MAIN_HAND: [
        ChecklistItem(id="home", label="メインハンド初期位置確認"),
    ],
    ROLE_SUB_HAND: [
        ChecklistItem(id="home", label="サブハンド初期位置確認"),
    ],
}


def _make(mode: Mode = Mode.SEMI_AUTO) -> MatchState:
    return MatchState(definitions=_DEFS, mode=mode)


def _complete(state: MatchState, role: str) -> None:
    for item in state.checklists[role].items:
        state.set_checklist_item(role, item.id, True)


class TestDefaults:
    def test_initial_state(self) -> None:
        state = _make()
        assert state.mode is Mode.SEMI_AUTO
        assert state.court is Court.RED
        assert state.phase is Phase.SETUP
        assert state.can_start_match is False

    def test_definitions_are_copied_per_role(self) -> None:
        """定義を共有すると 1 ロールのチェックが他ロールに伝播してしまう。"""
        state = _make()
        state.set_checklist_item(ROLE_MAIN_HAND, "home", True)
        assert state.checklists[ROLE_SUB_HAND].items[0].checked is False


class TestRequiredRoles:
    def test_semi_auto_requires_both_operators(self) -> None:
        state = _make(Mode.SEMI_AUTO)
        assert state.required_roles == [ROLE_MAIN_HAND, ROLE_SUB_HAND]

    def test_full_auto_requires_monitor_only(self) -> None:
        state = _make(Mode.FULL_AUTO)
        assert state.required_roles == [ROLE_MONITOR]


class TestChecklistCompletion:
    def test_semi_auto_needs_both_roles_complete(self) -> None:
        state = _make(Mode.SEMI_AUTO)
        _complete(state, ROLE_MAIN_HAND)
        assert state.checklists[ROLE_MAIN_HAND].completed is True
        # 片方だけでは試合に入れない
        assert state.can_start_match is False
        assert state.phase is Phase.SETUP

        _complete(state, ROLE_SUB_HAND)
        assert state.can_start_match is True
        assert state.phase is Phase.READY

    def test_monitor_checklist_does_not_gate_semi_auto(self) -> None:
        state = _make(Mode.SEMI_AUTO)
        _complete(state, ROLE_MONITOR)
        assert state.can_start_match is False

    def test_full_auto_needs_monitor_only(self) -> None:
        state = _make(Mode.FULL_AUTO)
        _complete(state, ROLE_MONITOR)
        assert state.phase is Phase.READY

    def test_unchecking_returns_to_setup(self) -> None:
        state = _make(Mode.FULL_AUTO)
        _complete(state, ROLE_MONITOR)
        assert state.phase is Phase.READY

        state.set_checklist_item(ROLE_MONITOR, "power", False)
        assert state.phase is Phase.SETUP
        assert state.can_start_match is False

    def test_unknown_role_or_item_is_rejected(self) -> None:
        state = _make()
        assert state.set_checklist_item("nobody", "power", True) is False
        assert state.set_checklist_item(ROLE_MONITOR, "no_such_item", True) is False

    def test_empty_checklist_counts_as_complete(self) -> None:
        """項目未定義のロールで永久にゲートが開かなくなるのを防ぐ。"""
        state = MatchState(definitions={ROLE_MONITOR: []}, mode=Mode.FULL_AUTO)
        assert state.phase is Phase.READY


class TestModeAndCourtChange:
    def test_mode_change_resets_checklists(self) -> None:
        state = _make(Mode.SEMI_AUTO)
        _complete(state, ROLE_MAIN_HAND)

        assert state.set_mode(Mode.FULL_AUTO) is True
        assert state.mode is Mode.FULL_AUTO
        assert state.checklists[ROLE_MAIN_HAND].completed is False
        assert state.phase is Phase.SETUP

    def test_court_change_resets_checklists(self) -> None:
        state = _make(Mode.FULL_AUTO)
        _complete(state, ROLE_MONITOR)
        assert state.phase is Phase.READY

        assert state.set_court(Court.BLUE) is True
        assert state.court is Court.BLUE
        assert state.phase is Phase.SETUP

    def test_same_value_is_noop(self) -> None:
        """同値の再設定でチェックリストを消さない (誤操作での作業やり直しを防ぐ)。"""
        state = _make(Mode.FULL_AUTO)
        _complete(state, ROLE_MONITOR)

        assert state.set_mode(Mode.FULL_AUTO) is True
        assert state.set_court(Court.RED) is True
        assert state.phase is Phase.READY

    def test_mode_change_denied_during_match(self) -> None:
        state = _make(Mode.FULL_AUTO)
        _complete(state, ROLE_MONITOR)
        state.match_start()

        assert state.set_mode(Mode.SEMI_AUTO) is False
        assert state.set_court(Court.BLUE) is False
        assert state.mode is Mode.FULL_AUTO
        assert state.court is Court.RED


class TestPhaseTransitions:
    def test_match_start_requires_ready(self) -> None:
        state = _make(Mode.FULL_AUTO)
        assert state.match_start() is False
        assert state.phase is Phase.SETUP

        _complete(state, ROLE_MONITOR)
        assert state.match_start() is True
        assert state.phase is Phase.MATCH

    def test_match_finish_requires_match(self) -> None:
        state = _make(Mode.FULL_AUTO)
        assert state.match_finish() is False

        _complete(state, ROLE_MONITOR)
        state.match_start()
        assert state.match_finish() is True
        assert state.phase is Phase.FINISHED

    def test_match_reset_from_any_phase(self) -> None:
        state = _make(Mode.FULL_AUTO)
        _complete(state, ROLE_MONITOR)
        state.match_start()

        assert state.match_reset() is True
        assert state.phase is Phase.SETUP
        assert state.checklists[ROLE_MONITOR].completed is False
        # モード・コートは試合後もそのまま維持する (次の試合も同条件が普通)
        assert state.mode is Mode.FULL_AUTO

    def test_checklist_locked_during_match(self) -> None:
        state = _make(Mode.FULL_AUTO)
        _complete(state, ROLE_MONITOR)
        state.match_start()
        assert state.set_checklist_item(ROLE_MONITOR, "power", False) is False
        assert state.phase is Phase.MATCH


class TestCommandGate:
    @pytest.mark.parametrize(
        "command",
        ["sequence_start", "sequence_jump", "trigger"],
    )
    def test_sequence_commands_only_in_match(self, command: str) -> None:
        state = _make(Mode.FULL_AUTO)
        assert state.deny_reason(command) is not None

        _complete(state, ROLE_MONITOR)
        assert state.deny_reason(command) is not None  # READY でもまだ不可

        state.match_start()
        assert state.deny_reason(command) is None

        state.match_finish()
        assert state.deny_reason(command) is not None

    def test_motor_check_blocked_during_match(self) -> None:
        state = _make(Mode.FULL_AUTO)
        assert state.deny_reason("motor_check_start") is None

        _complete(state, ROLE_MONITOR)
        state.match_start()
        assert state.deny_reason("motor_check_start") is not None

    @pytest.mark.parametrize(
        "command",
        ["e_stop", "e_stop_release", "sequence_stop", "match_reset", "health_check"],
    )
    def test_always_allowed_commands(self, command: str) -> None:
        state = _make(Mode.FULL_AUTO)
        assert state.deny_reason(command) is None

        _complete(state, ROLE_MONITOR)
        state.match_start()
        assert state.deny_reason(command) is None

    def test_unknown_command_is_not_gated(self) -> None:
        """ゲート表に無いコマンドは従来どおりの扱い (拒否しない)。"""
        state = _make()
        assert state.deny_reason("totally_unknown") is None


class TestSerialization:
    def test_to_dict_shape(self) -> None:
        state = _make(Mode.SEMI_AUTO)
        state.set_checklist_item(ROLE_MAIN_HAND, "home", True)
        payload = state.to_dict()

        assert payload["type"] == "match_state"
        assert payload["mode"] == "semi_auto"
        assert payload["court"] == "red"
        assert payload["phase"] == "setup"
        assert payload["required_roles"] == [ROLE_MAIN_HAND, ROLE_SUB_HAND]
        assert payload["can_start_match"] is False

        main = payload["checklists"][ROLE_MAIN_HAND]
        assert main["completed"] is True
        assert main["items"] == [
            {"id": "home", "label": "メインハンド初期位置確認", "checked": True}
        ]


class TestLoadDefinitions:
    def test_load_from_mapping(self) -> None:
        from lib.match_state import load_checklist_definitions

        defs = load_checklist_definitions(
            {
                "checklists": {
                    ROLE_MONITOR: [{"id": "power", "label": "電源投入確認"}],
                }
            }
        )
        assert defs[ROLE_MONITOR] == [ChecklistItem(id="power", label="電源投入確認")]
        # 未定義ロールも空リストで必ず存在させる (KeyError を UI 側に出さない)
        assert defs[ROLE_MAIN_HAND] == []
        assert defs[ROLE_SUB_HAND] == []

    def test_load_skips_malformed_entries(self) -> None:
        from lib.match_state import load_checklist_definitions

        defs = load_checklist_definitions(
            {
                "checklists": {
                    ROLE_MONITOR: [
                        {"id": "ok", "label": "有効"},
                        {"label": "id なし"},
                        "文字列",
                    ],
                }
            }
        )
        assert defs[ROLE_MONITOR] == [ChecklistItem(id="ok", label="有効")]
