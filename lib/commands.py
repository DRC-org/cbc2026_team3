"""操縦者から届く WS コマンドの語彙を 1 箇所に集める。

以前はコマンド名が「許可フェーズ表」「フェーズ拒否文」「緊急停止拒否文」
「_handle_command の if-elif」の 4 箇所に裸の文字列で散っていた。その結果
`sequence_stop` と `motor_check_abort` はどのゲート表にも載っておらず、
「意図してゲート対象外にした」のか「単に書き忘れた」のかコードから読めなくなっていた。

ここでは 1 コマンド = 1 `CommandSpec` とし、**フィールドに既定値を一切持たせない**。
新しいコマンドを足す人は許可フェーズも緊急停止中の可否もハンドラも必ず書くことになり、
「表に無いから素通り」という暗黙の状態を作れない。全フェーズで通したいコマンドは
`PHASES_ANY` を明示的に書く (= 素通りさせると宣言する)。

判定そのものは 2 段。フェーズゲート (試合進行としての可否) と緊急停止ゲート
(今この瞬間モータを動かしてよいか) は独立で、フェーズが `match` のままでも
緊急停止中は止める必要がある。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from lib.match_state import (
    PHASES_ANY,
    PHASES_DURING_MATCH,
    PHASES_OUTSIDE_MATCH,
    PHASES_PREPARATION,
    PHASES_START_GATE,
    Phase,
)


class RejectChannel(StrEnum):
    """拒否を操縦者へ届ける経路。UI 側の表示経路がコマンドによって違う。"""

    COMMAND_REJECTED = "command_rejected"
    #: 動作確認だけは HTTP POST 経路とイベントを共有しているため専用チャネルを使う
    MOTOR_CHECK_ERROR = "motor_check_error"


@dataclass(frozen=True)
class CommandSpec:
    """1 コマンドの受理条件と実行先。既定値を持たないのは宣言漏れを防ぐため。"""

    name: str
    #: 実行を許可するフェーズ。ゲートしないなら PHASES_ANY を明示する
    allowed_phases: frozenset[Phase]
    #: フェーズ拒否の理由文。PHASES_ANY のときは None (使われないため書かせない)
    phase_deny_message: str | None
    #: 緊急停止中に通すか。停止・復帰方向の操作は必ず True
    allowed_during_e_stop: bool
    e_stop_deny_message: str | None
    #: RobotServer 側のハンドラメソッド名。ゲートと実行が別々に増えないよう同じ行に置く
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

    def phase_deny_reason(self, phase: Phase) -> str | None:
        """phase で実行できなければ理由を返す。実行できるなら None。"""
        if phase in self.allowed_phases:
            return None
        return self.phase_deny_message

    def e_stop_deny_reason(self) -> str | None:
        """緊急停止中に実行できなければ理由を返す。実行できるなら None。"""
        return None if self.allowed_during_e_stop else self.e_stop_deny_message


def _spec(
    name: str,
    *,
    allowed_phases: frozenset[Phase],
    phase_deny_message: str | None = None,
    allowed_during_e_stop: bool,
    e_stop_deny_message: str | None = None,
    handler: str,
    reject_channel: RejectChannel = RejectChannel.COMMAND_REJECTED,
) -> CommandSpec:
    return CommandSpec(
        name=name,
        allowed_phases=allowed_phases,
        phase_deny_message=phase_deny_message,
        allowed_during_e_stop=allowed_during_e_stop,
        e_stop_deny_message=e_stop_deny_message,
        handler=handler,
        reject_channel=reject_channel,
    )


_SPECS: tuple[CommandSpec, ...] = (
    # ------------------------------------------------------------------ #
    #  シーケンス進行 — 試合中のみ、かつ緊急停止中は通さない。
    #  緊急停止中に次のステップが走ると、新しいモータ目標値が停止指令を上書きする。
    # ------------------------------------------------------------------ #
    _spec(
        "sequence_start",
        allowed_phases=PHASES_DURING_MATCH,
        phase_deny_message="試合中のみシーケンスを開始できます",
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中のためシーケンスを開始できません",
        handler="_cmd_sequence_start",
    ),
    _spec(
        "sequence_jump",
        allowed_phases=PHASES_DURING_MATCH,
        phase_deny_message="試合中のみステップ移動できます",
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中のためステップ移動できません",
        handler="_cmd_sequence_jump",
    ),
    _spec(
        "trigger",
        allowed_phases=PHASES_DURING_MATCH,
        phase_deny_message="試合中のみトリガーを送れます",
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中のためトリガーを送れません",
        handler="_cmd_trigger",
    ),
    # ------------------------------------------------------------------ #
    #  停止・復帰方向 — ゲートしないことをここで宣言する。
    #  止める操作を塞ぐと「動いている機体を止められない」状態が作れてしまうため、
    #  フェーズにも緊急停止にも依存させない。
    # ------------------------------------------------------------------ #
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
        # 解除を塞ぐと緊急停止から抜け出せなくなる (解除自体は機体を動かさない)
        "e_stop_release",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=True,
        handler="_cmd_e_stop_release",
    ),
    _spec(
        # 実行中の動作確認を止める操作。緊急停止の発動も内部で同じ abort を呼ぶ
        "motor_check_abort",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=True,
        handler="_cmd_motor_check_abort",
    ),
    _spec(
        # どのフェーズからでもセッティングタイムへ戻す復帰操作
        "match_reset",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=True,
        handler="_cmd_match_reset",
    ),
    _spec(
        # 状態を読むだけで機体に触らない。塞ぐと異常時ほど状況が分からなくなる
        "health_check",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=True,
        handler="_cmd_health_check",
    ),
    # ------------------------------------------------------------------ #
    #  フェーズ遷移
    # ------------------------------------------------------------------ #
    _spec(
        # 緊急停止中に MATCH へ入ると sequence_start が解禁され、同時に動作確認と
        # コート設定が閉じる。フェーズ遷移そのものを起こさせない
        "match_start",
        allowed_phases=PHASES_START_GATE,
        phase_deny_message="チェックリスト完了後に試合を開始できます",
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
    # ------------------------------------------------------------------ #
    #  準備中の操作 — 試合中は設定を触らせない。
    # ------------------------------------------------------------------ #
    _spec(
        # コート変更は機体を動かさないので緊急停止中でも通す
        "set_court",
        allowed_phases=PHASES_OUTSIDE_MATCH,
        phase_deny_message="試合中はコートを変更できません",
        allowed_during_e_stop=True,
        handler="_cmd_set_court",
    ),
    _spec(
        "checklist_set",
        allowed_phases=PHASES_PREPARATION,
        phase_deny_message="このフェーズではチェックリストを操作できません",
        allowed_during_e_stop=True,
        handler="_cmd_checklist_set",
    ),
    _spec(
        # 緊急停止からの復旧は指差喚呼のやり直しを伴うため通す
        "checklist_reset",
        allowed_phases=PHASES_PREPARATION,
        phase_deny_message="このフェーズではチェックリストを操作できません",
        allowed_during_e_stop=True,
        handler="_cmd_checklist_reset",
    ),
    _spec(
        # モータを微小駆動するため試合中と緊急停止中は通さない。
        # HTTP POST 経路は _handle_command を通らないので _start_motor_check 側にも同じ判定がある
        "motor_check_start",
        allowed_phases=PHASES_OUTSIDE_MATCH,
        phase_deny_message="試合中は動作確認を実行できません",
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中のため動作確認を実行できません",
        handler="_cmd_motor_check_start",
        reject_channel=RejectChannel.MOTOR_CHECK_ERROR,
    ),
    _spec(
        # 試合中の PID 差し替えは、走行中の位置制御の特性をその場で変える。
        # 左右直結ペアはグループ全員に同じ値が入るため、動いている機構が同時に別特性になる。
        # 緊急停止中も同じ理由 (解除した瞬間に停止前と違う特性で動き出す) で塞ぐ
        "set_param",
        allowed_phases=PHASES_OUTSIDE_MATCH,
        phase_deny_message="試合中はパラメータを変更できません",
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中のためパラメータを変更できません",
        handler="_cmd_set_param",
    ),
)

#: コマンド名 → 仕様。_handle_command のディスパッチ表もここから作る
COMMANDS: dict[str, CommandSpec] = {spec.name: spec for spec in _SPECS}


def spec_for(command: object) -> CommandSpec | None:
    """コマンド名から仕様を引く。未知の型・未知の名前なら None。

    未知のコマンドは拒否理由を返さない。WS を直接叩かれたときに語彙の有無を
    返答から推測させないためで、ディスパッチ側で黙って捨てる。
    """
    if not isinstance(command, str):
        return None
    return COMMANDS.get(command)


def phase_deny_reason(command: str, phase: Phase) -> str | None:
    """フェーズゲートの単一判定点。許可なら None。

    UI でボタンを隠すだけでは WS 直叩きやリロード直後を防げないため、
    サーバー側にも同じ制約を置く。
    """
    spec = spec_for(command)
    return None if spec is None else spec.phase_deny_reason(phase)


def e_stop_deny_reason(command: str) -> str | None:
    """緊急停止ゲートの単一判定点。許可なら None。"""
    spec = spec_for(command)
    return None if spec is None else spec.e_stop_deny_reason()
