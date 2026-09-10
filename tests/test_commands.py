from __future__ import annotations

import inspect

import pytest

from lib.commands import COMMANDS, CommandSpec, RejectChannel, spec_for
from lib.match_state import (
    PHASES_ANY,
    PHASES_DURING_MATCH,
    PHASES_OUTSIDE_MATCH,
    Phase,
)
from lib.server import RobotServer


def phase_deny_reason(command: str, phase: Phase) -> str | None:
    spec = spec_for(command)
    return None if spec is None else spec.phase_deny_reason(phase)


def e_stop_deny_reason(command: str) -> str | None:
    spec = spec_for(command)
    return None if spec is None else spec.e_stop_deny_reason()


def dev_tools_deny_reason(command: str, dev_tools_enabled: bool) -> str | None:
    spec = spec_for(command)
    return None if spec is None else spec.dev_tools_deny_reason(dev_tools_enabled)


_EXPECTED_COMMANDS = {
    "trigger",
    "e_stop",
    "e_stop_release",
    "health_check",
    "sequence_start",
    "sequence_stop",
    "sequence_jump",
    "motor_check_start",
    "motor_check_abort",
    "homing_start",
    "switch_measure_start",
    "switch_distance_start",
    "reenergize_motors",
    "set_court",
    "checklist_set",
    "checklist_reset",
    "checklist_check_all",
    "match_start",
    "match_finish",
    "match_reset",
    "set_operation_mode",
    "manual_move",
    "manual_set",
    "manual_jog",
    "suction_pads_set",
}


class TestRegistryCoverage:
    def test_every_command_is_declared(self) -> None:
        assert set(COMMANDS) == _EXPECTED_COMMANDS

    def test_key_matches_spec_name(self) -> None:
        for key, spec in COMMANDS.items():
            assert key == spec.name

    def test_every_handler_exists_and_is_async(self) -> None:
        for spec in COMMANDS.values():
            handler = getattr(RobotServer, spec.handler, None)
            assert handler is not None, f"{spec.name} のハンドラ {spec.handler} が無い"
            assert inspect.iscoroutinefunction(handler)

    def test_missing_handler_aborts_startup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        broken = CommandSpec(
            name="broken_command",
            allowed_phases=PHASES_ANY,
            phase_deny_message="",
            allowed_during_e_stop=True,
            e_stop_deny_message="",
            handler="_cmd_does_not_exist",
            reject_channel=RejectChannel.COMMAND_REJECTED,
            requires_dev_tools=False,
            blocked_during_manual=False,
            manual_deny_message=None,
            blocked_during_reenergize=False,
            reenergize_deny_message=None,
            blocked_without_court=False,
            court_deny_message=None,
        )
        monkeypatch.setitem(COMMANDS, broken.name, broken)

        with pytest.raises(RuntimeError) as exc:
            RobotServer()

        assert "broken_command" in str(exc.value)
        assert "_cmd_does_not_exist" in str(exc.value)

    def test_no_handler_bypasses_the_registry(self) -> None:
        implemented = {name for name in dir(RobotServer) if name.startswith("_cmd_")}
        declared = {spec.handler for spec in COMMANDS.values()}
        assert implemented == declared

    def test_ungated_commands_declare_it_explicitly(self) -> None:
        for spec in COMMANDS.values():
            if spec.allowed_phases == PHASES_ANY:
                assert spec.phase_deny_message is None
            else:
                assert spec.phase_deny_message

    def test_e_stop_policy_is_declared_for_every_command(self) -> None:
        for spec in COMMANDS.values():
            if spec.allowed_during_e_stop:
                assert spec.e_stop_deny_message is None
            else:
                assert spec.e_stop_deny_message

    def test_manual_gate_policy_is_declared_for_every_command(self) -> None:
        for spec in COMMANDS.values():
            if spec.blocked_during_manual:
                assert spec.manual_deny_message
            else:
                assert spec.manual_deny_message is None

    def test_reenergize_gate_policy_is_declared_for_every_command(self) -> None:
        for spec in COMMANDS.values():
            if spec.blocked_during_reenergize:
                assert spec.reenergize_deny_message
            else:
                assert spec.reenergize_deny_message is None

    def test_court_gate_policy_is_declared_for_every_command(self) -> None:
        for spec in COMMANDS.values():
            if spec.blocked_without_court:
                assert spec.court_deny_message
            else:
                assert spec.court_deny_message is None

    def test_commands_that_move_the_robot_need_a_court(self) -> None:
        blocked = {name for name, spec in COMMANDS.items() if spec.blocked_without_court}
        assert blocked == {
            "sequence_start",
            "sequence_jump",
            "trigger",
            "manual_move",
            "manual_set",
            "manual_jog",
            "homing_start",
            "switch_measure_start",
            "switch_distance_start",
            "motor_check_start",
        }

    def test_sequence_commands_are_blocked_while_reenergizing(self) -> None:
        blocked = {name for name, spec in COMMANDS.items() if spec.blocked_during_reenergize}
        assert blocked == {"sequence_start", "sequence_jump", "trigger"}


class TestSpecValidation:
    def _spec(self, **overrides: object) -> CommandSpec:
        kwargs: dict[str, object] = {
            "name": "dummy",
            "allowed_phases": PHASES_ANY,
            "phase_deny_message": None,
            "allowed_during_e_stop": True,
            "e_stop_deny_message": None,
            "requires_dev_tools": False,
            "blocked_during_manual": False,
            "manual_deny_message": None,
            "blocked_during_reenergize": False,
            "reenergize_deny_message": None,
            "blocked_without_court": False,
            "court_deny_message": None,
            "handler": "_cmd_dummy",
            "reject_channel": RejectChannel.COMMAND_REJECTED,
        }
        kwargs.update(overrides)
        return CommandSpec(**kwargs)  # type: ignore[arg-type]

    def test_gated_command_requires_a_reason(self) -> None:
        with pytest.raises(ValueError):
            self._spec(allowed_phases=PHASES_DURING_MATCH, phase_deny_message=None)

    def test_ungated_command_must_not_carry_a_reason(self) -> None:
        with pytest.raises(ValueError):
            self._spec(phase_deny_message="使われない理由")

    def test_e_stop_denied_command_requires_a_reason(self) -> None:
        with pytest.raises(ValueError):
            self._spec(allowed_during_e_stop=False, e_stop_deny_message=None)

    def test_e_stop_allowed_command_must_not_carry_a_reason(self) -> None:
        with pytest.raises(ValueError):
            self._spec(e_stop_deny_message="使われない理由")

    def test_empty_phase_set_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            self._spec(allowed_phases=frozenset(), phase_deny_message="常に不可")

    def test_manual_gated_command_requires_a_reason(self) -> None:
        with pytest.raises(ValueError):
            self._spec(blocked_during_manual=True, manual_deny_message=None)

    def test_manual_ungated_command_must_not_carry_a_reason(self) -> None:
        with pytest.raises(ValueError):
            self._spec(manual_deny_message="使われない理由")

    def test_reenergize_gated_command_requires_a_reason(self) -> None:
        with pytest.raises(ValueError):
            self._spec(blocked_during_reenergize=True, reenergize_deny_message=None)

    def test_reenergize_ungated_command_must_not_carry_a_reason(self) -> None:
        with pytest.raises(ValueError):
            self._spec(reenergize_deny_message="使われない理由")

    def test_court_gated_command_requires_a_reason(self) -> None:
        with pytest.raises(ValueError):
            self._spec(blocked_without_court=True, court_deny_message=None)

    def test_court_ungated_command_must_not_carry_a_reason(self) -> None:
        with pytest.raises(ValueError):
            self._spec(court_deny_message="使われない理由")


class TestPhaseGate:
    @pytest.mark.parametrize("command", ["sequence_start", "sequence_jump", "trigger"])
    def test_sequence_commands_only_in_match(self, command: str) -> None:
        assert phase_deny_reason(command, Phase.SETUP) is not None
        assert phase_deny_reason(command, Phase.READY) is not None
        assert phase_deny_reason(command, Phase.MATCH) is None
        assert phase_deny_reason(command, Phase.FINISHED) is not None

    def test_motor_check_start_blocked_during_match(self) -> None:
        assert phase_deny_reason("motor_check_start", Phase.SETUP) is None
        assert phase_deny_reason("motor_check_start", Phase.MATCH) is not None

    @pytest.mark.parametrize(
        "command",
        [
            "e_stop",
            "e_stop_release",
            "sequence_stop",
            "motor_check_abort",
            "match_reset",
            "health_check",
        ],
    )
    def test_stop_direction_commands_are_never_phase_gated(self, command: str) -> None:
        assert COMMANDS[command].allowed_phases == PHASES_ANY
        for phase in Phase:
            assert phase_deny_reason(command, phase) is None

    def test_unknown_command_is_not_gated(self) -> None:
        assert phase_deny_reason("totally_unknown", Phase.MATCH) is None


class TestEStopGate:
    def test_denied_commands(self) -> None:
        denied = {name for name, spec in COMMANDS.items() if not spec.allowed_during_e_stop}
        assert denied == {
            "sequence_start",
            "sequence_jump",
            "trigger",
            "match_start",
            "motor_check_start",
            "homing_start",
            "switch_measure_start",
            "switch_distance_start",
            "manual_move",
            "manual_set",
            "manual_jog",
            "reenergize_motors",
        }

    @pytest.mark.parametrize(
        "command",
        ["sequence_stop", "e_stop", "e_stop_release", "match_reset", "match_finish"],
    )
    def test_stop_direction_commands_pass_during_e_stop(self, command: str) -> None:
        assert e_stop_deny_reason(command) is None

    def test_motor_check_start_is_rejected_on_its_own_channel(self) -> None:
        assert COMMANDS["motor_check_start"].reject_channel is RejectChannel.MOTOR_CHECK_ERROR
        assert e_stop_deny_reason("motor_check_start") is not None

    def test_unknown_command_has_no_e_stop_reason(self) -> None:
        assert e_stop_deny_reason("totally_unknown") is None

    def test_mode_switch_passes_but_manual_commands_do_not(self) -> None:
        assert e_stop_deny_reason("set_operation_mode") is None
        for command in ("manual_move", "manual_set", "manual_jog"):
            assert e_stop_deny_reason(command) is not None


class TestManualModeGate:
    def test_blocked_commands(self) -> None:
        blocked = {name for name, spec in COMMANDS.items() if spec.blocked_during_manual}
        assert blocked == {"sequence_start", "sequence_jump", "trigger"}

    @pytest.mark.parametrize(
        "command",
        [
            "sequence_stop",
            "e_stop",
            "e_stop_release",
            "match_reset",
            "health_check",
            "set_operation_mode",
            "manual_move",
            "manual_set",
            "manual_jog",
            "motor_check_start",
            "reenergize_motors",
        ],
    )
    def test_not_gated_by_manual_mode(self, command: str) -> None:
        assert COMMANDS[command].blocked_during_manual is False


class TestReenergizeMotorsGate:
    @pytest.mark.parametrize("phase", list(Phase))
    def test_every_phase_is_allowed(self, phase: Phase) -> None:
        assert phase_deny_reason("reenergize_motors", phase) is None

    def test_denied_during_e_stop(self) -> None:
        assert e_stop_deny_reason("reenergize_motors") is not None


class TestManualCommandsAreNotPhaseGated:
    @pytest.mark.parametrize(
        "command", ["set_operation_mode", "manual_move", "manual_set", "manual_jog"]
    )
    @pytest.mark.parametrize("phase", list(Phase))
    def test_every_phase_is_allowed(self, command: str, phase: Phase) -> None:
        assert phase_deny_reason(command, phase) is None


class TestDevToolsGate:
    def test_only_declared_dev_commands_require_the_flag(self) -> None:
        dev_only = {name for name, spec in COMMANDS.items() if spec.requires_dev_tools}
        assert dev_only == {"checklist_check_all"}

    def test_dev_command_is_denied_without_the_flag(self) -> None:
        assert dev_tools_deny_reason("checklist_check_all", False) is not None
        assert dev_tools_deny_reason("checklist_check_all", True) is None

    @pytest.mark.parametrize("command", ["checklist_set", "e_stop", "match_start"])
    def test_normal_commands_are_unaffected_by_the_flag(self, command: str) -> None:
        assert dev_tools_deny_reason(command, False) is None
        assert dev_tools_deny_reason(command, True) is None

    def test_unknown_command_has_no_dev_reason(self) -> None:
        assert dev_tools_deny_reason("totally_unknown", False) is None

    def test_dev_command_shares_the_checklist_phase_gate(self) -> None:
        assert (
            COMMANDS["checklist_check_all"].allowed_phases
            == COMMANDS["checklist_set"].allowed_phases
        )


class TestPreparationOnlyCommands:
    @pytest.mark.parametrize("command", ["set_court", "motor_check_start"])
    def test_configuration_commands_share_the_same_phase_set(self, command: str) -> None:
        assert COMMANDS[command].allowed_phases == PHASES_OUTSIDE_MATCH
