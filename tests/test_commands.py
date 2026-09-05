"""コマンド語彙の網羅性テスト。

以前はコマンド名が「許可フェーズ表」「フェーズ拒否文」「緊急停止拒否文」「if-elif の分岐」
の 4 箇所に裸の文字列で散っており、どの表にも載っていないコマンドが実在した。
ここでは「全コマンドがゲート方針を宣言していること」「宣言されていないコマンドは
ハンドラへ到達しないこと」を不変条件として固定する。
"""

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
    """ゲート判定の入口は `spec_for()` → メソッドの 1 本だけ。

    かつては同じ判定へモジュール関数からも入れ、どちらを使うかの根拠が
    どこにも無かった (本番は spec のメソッド、`match_start` と動作確認だけが
    名前引き)。テストの読みやすさのためのラッパはここに置く。
    """
    spec = spec_for(command)
    return None if spec is None else spec.phase_deny_reason(phase)


def e_stop_deny_reason(command: str) -> str | None:
    spec = spec_for(command)
    return None if spec is None else spec.e_stop_deny_reason()


def dev_tools_deny_reason(command: str, dev_tools_enabled: bool) -> str | None:
    spec = spec_for(command)
    return None if spec is None else spec.dev_tools_deny_reason(dev_tools_enabled)


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
        """名前引きのディスパッチは、押した瞬間まで欠落を教えてくれない。

        `getattr(self, spec.handler)` は文字列引きなので、メソッド名を変えても
        静的には何も検出されず、起動もする。壊れていることが分かるのは操縦者が
        そのボタンを押した瞬間で、しかも拒否通知も出ないため画面から原因が読めない。
        """
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
        )
        monkeypatch.setitem(COMMANDS, broken.name, broken)

        with pytest.raises(RuntimeError) as exc:
            RobotServer()

        assert "broken_command" in str(exc.value)
        assert "_cmd_does_not_exist" in str(exc.value)

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

    def test_manual_gate_policy_is_declared_for_every_command(self) -> None:
        for spec in COMMANDS.values():
            if spec.blocked_during_manual:
                assert spec.manual_deny_message
            else:
                assert spec.manual_deny_message is None


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

    def test_manual_gated_command_requires_a_reason(self) -> None:
        with pytest.raises(ValueError):
            self._spec(blocked_during_manual=True, manual_deny_message=None)

    def test_manual_ungated_command_must_not_carry_a_reason(self) -> None:
        with pytest.raises(ValueError):
            self._spec(manual_deny_message="使われない理由")


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
            # 手動指令は目標値を送る操作。通すと緊急停止が意味を失う
            "manual_move",
            "manual_set",
            "manual_jog",
            # 緊急停止中に励磁してはならない (緊急停止の意味が消える)
            "reenergize_motors",
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

    def test_mode_switch_passes_but_manual_commands_do_not(self) -> None:
        """モード切替は機体を動かさないので通す。指令は通さない。

        ここを揃えてしまうと、片方が「停止中に画面を手動へ寄せられない」か
        「停止中に機体が動く」のどちらかになる。
        """
        assert e_stop_deny_reason("set_operation_mode") is None
        for command in ("manual_move", "manual_set", "manual_jog"):
            assert e_stop_deny_reason(command) is not None


class TestManualModeGate:
    """手動操縦モード中に塞ぐべきコマンドは、対象ロボットの制御権を奪うもの限り。

    実際に「対象ロボットが今手動モードか」まで掛け合わせた判定は
    `RobotServer._manual_mode_deny_reason` にある (ロボットごとの `OperationMode` は
    `CommandSpec` の外、`RobotContext` が持つため)。ここで固定するのは
    「どのコマンドをゲート対象として宣言したか」という語彙側の事実だけ。
    """

    def test_blocked_commands(self) -> None:
        blocked = {name for name, spec in COMMANDS.items() if spec.blocked_during_manual}
        assert blocked == {"sequence_start", "sequence_jump", "trigger"}

    @pytest.mark.parametrize(
        "command",
        [
            # 止める側は手動中でも塞がない (退避の逃げ道を残す)
            "sequence_stop",
            "e_stop",
            "e_stop_release",
            "match_reset",
            "health_check",
            # モード切替そのものと手動指令自身は、手動モードゲートの対象外
            # (指令の可否は ctx.mode が MANUAL でないと拒否する既存の
            # `_manual_target` が別に持っており、ここで重複させない)
            "set_operation_mode",
            "manual_move",
            "manual_set",
            "manual_jog",
            # 動作確認の手動モードとの排他は `_motor_check_environment_deny()` が
            # 両ロボット横断で持つ (ここで重複させない)
            "motor_check_start",
            # 手動はシーケンスからの退避路そのものなので、手動中に落ちた励磁を
            # 手動のまま戻せないと退避路自体が詰む
            "reenergize_motors",
        ],
    )
    def test_not_gated_by_manual_mode(self, command: str) -> None:
        assert COMMANDS[command].blocked_during_manual is False


class TestReenergizeMotorsGate:
    """励磁が落ちたモータを機体を止めずに戻す操作。CommandSpec が答えるのは
    「フェーズ / 緊急停止 / 手動モード」の 3 軸だけで、動作確認との排他と
    ロボット単位の in-flight ガードはハンドラ側 (`RobotServer._cmd_reenergize_motors`)
    が持つため、ここでは対象外にする。
    """

    @pytest.mark.parametrize("phase", list(Phase))
    def test_every_phase_is_allowed(self, phase: Phase) -> None:
        assert phase_deny_reason("reenergize_motors", phase) is None

    def test_denied_during_e_stop(self) -> None:
        """緊急停止中に励磁してはならない (緊急停止の意味が消える)。"""
        assert e_stop_deny_reason("reenergize_motors") is not None


class TestManualCommandsAreNotPhaseGated:
    """手動操縦は開始前・試合中・終了後のどこでも使える (運用要件)。

    調整は準備中に、シーケンスからの退避は試合中に要る。どちらかへ閉じると
    「要るときに使えない操作」になる。可否の正はここで、UI は理由を説明するだけ。
    """

    @pytest.mark.parametrize(
        "command", ["set_operation_mode", "manual_move", "manual_set", "manual_jog"]
    )
    @pytest.mark.parametrize("phase", list(Phase))
    def test_every_phase_is_allowed(self, command: str, phase: Phase) -> None:
        assert phase_deny_reason(command, phase) is None


class TestDevToolsGate:
    """開発用コマンドは「起動オプションで解禁したときだけ存在する」ことを固定する。

    指差喚呼は試合開始ゲートそのものなので、一括チェックが本番起動で通ると
    「点検していないのに試合を開始できる」経路になる。
    """

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
        """開発用でもフェーズ条件は checklist_set と同じ (試合中は触らせない)。"""
        assert (
            COMMANDS["checklist_check_all"].allowed_phases
            == COMMANDS["checklist_set"].allowed_phases
        )


class TestPreparationOnlyCommands:
    @pytest.mark.parametrize("command", ["set_court", "motor_check_start", "set_param"])
    def test_configuration_commands_share_the_same_phase_set(self, command: str) -> None:
        """試合中に設定を触らせない、という 1 つの方針を 3 コマンドで共有する。"""
        assert COMMANDS[command].allowed_phases == PHASES_OUTSIDE_MATCH
