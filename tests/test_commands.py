"""コマンド語彙の網羅性テスト。

以前はコマンド名が「許可フェーズ表」「フェーズ拒否文」「緊急停止拒否文」「if-elif の分岐」
の 4 箇所に裸の文字列で散っており、どの表にも載っていないコマンドが実在した。
ここでは「全コマンドがゲート方針を宣言していること」「宣言されていないコマンドは
ハンドラへ到達しないこと」を不変条件として固定する。
"""

from __future__ import annotations

import inspect

import pytest
from lib.commands import (
    COMMANDS,
    CommandSpec,
    RejectChannel,
    e_stop_deny_reason,
    phase_deny_reason,
)

from lib.match_state import (
    PHASES_ANY,
    PHASES_DURING_MATCH,
    PHASES_OUTSIDE_MATCH,
    Phase,
)
from lib.server import RobotServer

#: 操縦者 UI と Web ソケット越しに実際にやり取りする全コマンド。
#: 増減させるときはここも直す (テストが落ちることで宣言漏れに気付ける)。
_EXPECTED_COMMANDS = {
    "trigger",
    "e_stop",
    "e_stop_release",
    "health_check",
    "set_param",
    "sequence_start",
    "sequence_stop",
    "sequence_jump",
    "motor_check_start",
    "motor_check_abort",
    "set_court",
    "checklist_set",
    "checklist_reset",
    "match_start",
    "match_finish",
    "match_reset",
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

    def test_no_handler_bypasses_the_registry(self) -> None:
        """登録されていない _cmd_* を書いても呼ばれない (= 宣言漏れが残らない)。"""
        implemented = {name for name in dir(RobotServer) if name.startswith("_cmd_")}
        declared = {spec.handler for spec in COMMANDS.values()}
        assert implemented == declared

    def test_ungated_commands_declare_it_explicitly(self) -> None:
        """「表に無いから素通り」ではなく「全フェーズ許可」と書かせる。"""
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


class TestSpecValidation:
    def _spec(self, **overrides: object) -> CommandSpec:
        kwargs: dict[str, object] = {
            "name": "dummy",
            "allowed_phases": PHASES_ANY,
            "phase_deny_message": None,
            "allowed_during_e_stop": True,
            "e_stop_deny_message": None,
            "handler": "_cmd_dummy",
            "reject_channel": RejectChannel.COMMAND_REJECTED,
        }
        kwargs.update(overrides)
        return CommandSpec(**kwargs)  # type: ignore[arg-type]

    def test_gated_command_requires_a_reason(self) -> None:
        """理由の無い拒否は操縦者に「なぜ弾かれたか」を伝えられない。"""
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
        """空集合は「どのフェーズでも実行できない」= 事実上の死んだコマンド。"""
        with pytest.raises(ValueError):
            self._spec(allowed_phases=frozenset(), phase_deny_message="常に不可")


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

    def test_set_param_blocked_during_match(self) -> None:
        """試合中に PID を差し替えると、走行中の位置制御の特性が突然変わる。"""
        assert phase_deny_reason("set_param", Phase.READY) is None
        assert phase_deny_reason("set_param", Phase.MATCH) is not None

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
        """未知のコマンドはゲートではなくディスパッチで捨てる (拒否理由を作らない)。"""
        assert phase_deny_reason("totally_unknown", Phase.MATCH) is None


class TestEStopGate:
    def test_denied_commands(self) -> None:
        denied = {name for name, spec in COMMANDS.items() if not spec.allowed_during_e_stop}
        assert denied == {
            "sequence_start",
            "sequence_jump",
            "trigger",
            "match_start",
            "set_param",
            "motor_check_start",
        }

    @pytest.mark.parametrize(
        "command",
        ["sequence_stop", "e_stop", "e_stop_release", "match_reset", "match_finish"],
    )
    def test_stop_direction_commands_pass_during_e_stop(self, command: str) -> None:
        """止める・戻す操作を塞ぐと、緊急停止から抜け出せない機体になる。"""
        assert e_stop_deny_reason(command) is None

    def test_motor_check_start_is_rejected_on_its_own_channel(self) -> None:
        """動作確認の拒否だけは UI の表示経路が別 (motor_check_error)。"""
        assert COMMANDS["motor_check_start"].reject_channel is RejectChannel.MOTOR_CHECK_ERROR
        assert e_stop_deny_reason("motor_check_start") is not None

    def test_unknown_command_has_no_e_stop_reason(self) -> None:
        assert e_stop_deny_reason("totally_unknown") is None


class TestPreparationOnlyCommands:
    @pytest.mark.parametrize("command", ["set_court", "motor_check_start", "set_param"])
    def test_configuration_commands_share_the_same_phase_set(self, command: str) -> None:
        """試合中に設定を触らせない、という 1 つの方針を 3 コマンドで共有する。"""
        assert COMMANDS[command].allowed_phases == PHASES_OUTSIDE_MATCH
