from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from lib.match_state import (
    PHASES_ANY,
    PHASES_DURING_MATCH,
    PHASES_OUTSIDE_MATCH,
    PHASES_START_GATE,
    Phase,
)


class RejectChannel(StrEnum):
    COMMAND_REJECTED = "command_rejected"
    MOTOR_CHECK_ERROR = "motor_check_error"


@dataclass(frozen=True)
class CommandSpec:
    name: str
    allowed_phases: frozenset[Phase]
    phase_deny_message: str | None
    allowed_during_e_stop: bool
    e_stop_deny_message: str | None
    blocked_during_manual: bool
    manual_deny_message: str | None
    blocked_during_reenergize: bool
    reenergize_deny_message: str | None
    blocked_without_court: bool
    court_deny_message: str | None
    handler: str
    reject_channel: RejectChannel

    def __post_init__(self) -> None:
        if not self.allowed_phases:
            raise ValueError(f"{self.name}: 許可フェーズが空 (永久に実行できない)")

        gated = self.allowed_phases != PHASES_ANY
        if gated and not self.phase_deny_message:
            raise ValueError(f"{self.name}: フェーズゲートに理由文が無い")
        if not gated and self.phase_deny_message:
            raise ValueError(f"{self.name}: 全フェーズ許可なのに拒否理由が書かれている")

        if not self.allowed_during_e_stop and not self.e_stop_deny_message:
            raise ValueError(f"{self.name}: 緊急停止ゲートに理由文が無い")
        if self.allowed_during_e_stop and self.e_stop_deny_message:
            raise ValueError(f"{self.name}: 緊急停止中も通すのに拒否理由が書かれている")

        if self.blocked_during_manual and not self.manual_deny_message:
            raise ValueError(f"{self.name}: 手動操縦ゲートに理由文が無い")
        if not self.blocked_during_manual and self.manual_deny_message:
            raise ValueError(f"{self.name}: 手動操縦ゲートを掛けないのに拒否理由が書かれている")

        if self.blocked_during_reenergize and not self.reenergize_deny_message:
            raise ValueError(f"{self.name}: 再励磁ゲートに理由文が無い")
        if not self.blocked_during_reenergize and self.reenergize_deny_message:
            raise ValueError(f"{self.name}: 再励磁ゲートを掛けないのに拒否理由が書かれている")

        if self.blocked_without_court and not self.court_deny_message:
            raise ValueError(f"{self.name}: コート未確定ゲートに理由文が無い")
        if not self.blocked_without_court and self.court_deny_message:
            raise ValueError(f"{self.name}: コート未確定ゲートを掛けないのに拒否理由が書かれている")

    def phase_deny_reason(self, phase: Phase) -> str | None:
        if phase in self.allowed_phases:
            return None
        return self.phase_deny_message

    def e_stop_deny_reason(self) -> str | None:
        return None if self.allowed_during_e_stop else self.e_stop_deny_message

    def manual_deny_reason(self) -> str | None:
        return self.manual_deny_message if self.blocked_during_manual else None

    def reenergize_deny_reason(self) -> str | None:
        return self.reenergize_deny_message if self.blocked_during_reenergize else None

    def court_deny_reason(self) -> str | None:
        return self.court_deny_message if self.blocked_without_court else None


def _spec(
    name: str,
    *,
    allowed_phases: frozenset[Phase],
    phase_deny_message: str | None = None,
    allowed_during_e_stop: bool,
    e_stop_deny_message: str | None = None,
    blocked_during_manual: bool = False,
    manual_deny_message: str | None = None,
    blocked_during_reenergize: bool = False,
    reenergize_deny_message: str | None = None,
    blocked_without_court: bool = False,
    court_deny_message: str | None = None,
    handler: str,
    reject_channel: RejectChannel = RejectChannel.COMMAND_REJECTED,
) -> CommandSpec:
    return CommandSpec(
        name=name,
        allowed_phases=allowed_phases,
        phase_deny_message=phase_deny_message,
        allowed_during_e_stop=allowed_during_e_stop,
        e_stop_deny_message=e_stop_deny_message,
        blocked_during_manual=blocked_during_manual,
        manual_deny_message=manual_deny_message,
        blocked_during_reenergize=blocked_during_reenergize,
        reenergize_deny_message=reenergize_deny_message,
        blocked_without_court=blocked_without_court,
        court_deny_message=court_deny_message,
        handler=handler,
        reject_channel=reject_channel,
    )


_SPECS: tuple[CommandSpec, ...] = (
    _spec(
        "sequence_start",
        allowed_phases=PHASES_DURING_MATCH,
        phase_deny_message="試合中のみシーケンスを開始できます",
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中のためシーケンスを開始できません",
        blocked_during_manual=True,
        manual_deny_message="手動操縦中のためシーケンスを開始できません",
        blocked_during_reenergize=True,
        reenergize_deny_message="再励磁の処理中のためシーケンスを開始できません",
        blocked_without_court=True,
        court_deny_message=(
            "コートが未設定のためシーケンスを開始できません (コート設定でコートを選んでください)"
        ),
        handler="_cmd_sequence_start",
    ),
    _spec(
        "sequence_jump",
        allowed_phases=PHASES_DURING_MATCH,
        phase_deny_message="試合中のみステップ移動できます",
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中のためステップ移動できません",
        blocked_during_manual=True,
        manual_deny_message="手動操縦中のためステップ移動できません",
        blocked_during_reenergize=True,
        reenergize_deny_message="再励磁の処理中のためステップ移動できません",
        blocked_without_court=True,
        court_deny_message=(
            "コートが未設定のためステップ移動できません (コート設定でコートを選んでください)"
        ),
        handler="_cmd_sequence_jump",
    ),
    _spec(
        "trigger",
        allowed_phases=PHASES_DURING_MATCH,
        phase_deny_message="試合中のみトリガーを送れます",
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中のためトリガーを送れません",
        blocked_during_manual=True,
        manual_deny_message="手動操縦中のためトリガーを送れません",
        blocked_during_reenergize=True,
        reenergize_deny_message="再励磁の処理中のためトリガーを送れません",
        blocked_without_court=True,
        court_deny_message=(
            "コートが未設定のためトリガーを送れません (コート設定でコートを選んでください)"
        ),
        handler="_cmd_trigger",
    ),
    _spec(
        "sequence_stop",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=True,
        handler="_cmd_sequence_stop",
    ),
    _spec(
        "e_stop",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=True,
        handler="_cmd_e_stop",
    ),
    _spec(
        "e_stop_release",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=True,
        handler="_cmd_e_stop_release",
    ),
    _spec(
        "motor_check_abort",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=True,
        handler="_cmd_motor_check_abort",
    ),
    _spec(
        "match_reset",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=True,
        handler="_cmd_match_reset",
    ),
    _spec(
        "health_check",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=True,
        handler="_cmd_health_check",
    ),
    _spec(
        "match_start",
        allowed_phases=PHASES_START_GATE,
        phase_deny_message="コート設定でコートを選んでから試合を開始できます",
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中のため試合を開始できません",
        handler="_cmd_match_start",
    ),
    _spec(
        "match_finish",
        allowed_phases=PHASES_DURING_MATCH,
        phase_deny_message="試合中ではありません",
        allowed_during_e_stop=True,
        handler="_cmd_match_finish",
    ),
    _spec(
        "set_court",
        allowed_phases=PHASES_OUTSIDE_MATCH,
        phase_deny_message="試合中はコートを変更できません",
        allowed_during_e_stop=True,
        handler="_cmd_set_court",
    ),
    _spec(
        "motor_check_start",
        allowed_phases=PHASES_OUTSIDE_MATCH,
        phase_deny_message="試合中は動作確認を実行できません",
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中のため動作確認を実行できません",
        blocked_without_court=True,
        court_deny_message=(
            "コートが未設定のため動作確認を実行できません (コート設定でコートを選んでください)"
        ),
        handler="_cmd_motor_check_start",
        reject_channel=RejectChannel.MOTOR_CHECK_ERROR,
    ),
    _spec(
        "homing_start",
        allowed_phases=PHASES_OUTSIDE_MATCH,
        phase_deny_message="試合中は零点合わせを実行できません",
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中のため零点合わせを実行できません",
        blocked_without_court=True,
        court_deny_message=(
            "コートが未設定のため零点合わせを実行できません (コート設定でコートを選んでください)"
        ),
        handler="_cmd_homing_start",
    ),
    _spec(
        "switch_measure_start",
        allowed_phases=PHASES_OUTSIDE_MATCH,
        phase_deny_message="試合中は作動点測定を実行できません",
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中のため作動点測定を実行できません",
        blocked_without_court=True,
        court_deny_message=(
            "コートが未設定のため作動点測定を実行できません (コート設定でコートを選んでください)"
        ),
        handler="_cmd_switch_measure_start",
    ),
    _spec(
        "switch_distance_start",
        allowed_phases=PHASES_OUTSIDE_MATCH,
        phase_deny_message="試合中は距離測定を実行できません",
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中のため距離測定を実行できません",
        blocked_without_court=True,
        court_deny_message=(
            "コートが未設定のため距離測定を実行できません (コート設定でコートを選んでください)"
        ),
        handler="_cmd_switch_distance_start",
    ),
    _spec(
        "reenergize_motors",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中は再励磁できません",
        handler="_cmd_reenergize_motors",
    ),
    _spec(
        "set_operation_mode",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=True,
        handler="_cmd_set_operation_mode",
    ),
    _spec(
        "manual_move",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中のため手動操縦できません",
        blocked_without_court=True,
        court_deny_message=(
            "コートが未設定のため手動操縦できません (コート設定でコートを選んでください)"
        ),
        handler="_cmd_manual_move",
    ),
    _spec(
        "manual_set",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中のため手動操縦できません",
        blocked_without_court=True,
        court_deny_message=(
            "コートが未設定のため手動操縦できません (コート設定でコートを選んでください)"
        ),
        handler="_cmd_manual_set",
    ),
    _spec(
        "manual_jog",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中のため手動操縦できません",
        blocked_without_court=True,
        court_deny_message=(
            "コートが未設定のため手動操縦できません (コート設定でコートを選んでください)"
        ),
        handler="_cmd_manual_jog",
    ),
    # 機体は動かさないが、控える値は実測そのもの。緊急停止中に通すと、無励磁で自重落下
    # した位置を「その位置名の正しい値」として控えてしまう
    _spec(
        "position_capture",
        allowed_phases=PHASES_OUTSIDE_MATCH,
        phase_deny_message="試合中は位置を控えられません",
        allowed_during_e_stop=False,
        e_stop_deny_message=("緊急停止中は位置を控えられません (無励磁で下がった位置が残ります)"),
        blocked_without_court=True,
        court_deny_message=(
            "コートが未設定のため位置を控えられません (コート設定でコートを選んでください)"
        ),
        handler="_cmd_position_capture",
    ),
    # yaml を読み直すだけで機体は動かない。緊急停止で止めて直し、そのまま試す経路を残す。
    # 試合中に通すと、走っている足元で次のステップの行き先だけが入れ替わる
    _spec(
        "positions_reload",
        allowed_phases=PHASES_OUTSIDE_MATCH,
        phase_deny_message="試合中は位置定数を読み直せません",
        allowed_during_e_stop=True,
        # 手動操縦中も通す。手で合わせて控え、yaml へ貼って読み直し、その位置へ
        # 動かして確かめる —— 位置定数を決める手順はモードを跨がない
        handler="_cmd_positions_reload",
    ),
    # 選択を変えるだけで機体は動かない。次の吸着ステップから効くので、緊急停止中の準備にも通す
    _spec(
        "suction_pads_set",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=True,
        handler="_cmd_suction_pads_set",
    ),
    # 往復時間の実測。機体には触らないので全フェーズ素通し (WiFi が細いときに
    # 「指令が遅れている」と操縦者が読めるのは、この 1 本だけ)
    _spec(
        "ping",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=True,
        handler="_cmd_ping",
    ),
)

COMMANDS: dict[str, CommandSpec] = {spec.name: spec for spec in _SPECS}


def spec_for(command: object) -> CommandSpec | None:
    if not isinstance(command, str):
        return None
    return COMMANDS.get(command)
