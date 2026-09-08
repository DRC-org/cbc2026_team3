"""操縦者から届く WS コマンドの語彙を 1 箇所に集める。

語彙が「許可フェーズ表」「拒否文」「if-elif」へ分かれていると、どのゲート表にも
載らないコマンドが生まれ、「意図してゲート対象外にした」のか「書き忘れた」のかが
コードから読めなくなる。ここでは 1 コマンド = 1 `CommandSpec` とし、**フィールドに
既定値を一切持たせない** —— 新しいコマンドを足す人はゲート方針を必ず宣言することに
なる。全フェーズで通すなら `PHASES_ANY` を明示的に書く (= 素通りさせると宣言する)。

判定は 5 段で、どれも独立している: 開発用 / フェーズ / 緊急停止 / 手動操縦 / 再励磁。
後ろ 2 つはロボットごとの状態と掛け合わせて初めて決まるので、`CommandSpec` は
「ゲート対象にしたか」と理由文だけを持ち、掛け合わせは `RobotServer` が行う。
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

#: 開発用コマンドを本番起動 (--dev-tools なし) で受けたときの拒否文。
#: 「操作を間違えた」ではなく「この起動には無い機能」だと分かる文言にする。
DEV_TOOLS_DENY_MESSAGE = "開発用コマンドです (--dev-tools / CBC_DEV_TOOLS=1 で起動したときのみ有効)"


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
    #: 開発用フラグ (--dev-tools) を立てた起動でしか受け付けないか。
    #: 試合運用の手順を飛ばすコマンドはここを True にして、本番起動では語彙ごと閉じる
    requires_dev_tools: bool
    #: 対象ロボット (data["robot"]) が手動操縦モードのとき塞ぐか
    #: (docs/invariants.md「制御権の奪い合いは両方向を塞ぐ」)。塞ぐのは
    #: 「シーケンスの制御権を使う」コマンドだけで、モード切替そのもの・停止方向の
    #: 操作・ロボットを指定しないコマンドは対象外 (False)。
    blocked_during_manual: bool
    manual_deny_message: str | None
    #: モータを今励磁し直しているあいだ塞ぐか。**片方向だけのゲートである**
    #: (docs/invariants.md「再励磁は片方向しか塞がない」)。在飛と見なすのは
    #: 単発再励磁と**ロボット名に依らない緊急停止解除の再励磁**の 2 つで、判定は
    #: `RobotServer._is_reenergizing` の 1 箇所だけが持つ。
    #:
    #: 塞ぐ理由は、再励磁が書く「フォルト前の現在角」がシーケンスの `move_to` の
    #: 目標を上書きし、`wait_reached` が動かない位置を見続けて
    #: `SequenceTimeoutError` になるため (症状は「NEXT を押したのに動かない」だけ)。
    #:
    #: 手動側の同じ排他はハンドラが持つ (`RobotServer._apply_operation_mode` /
    #: `_manual_target`)。`set_operation_mode` は方向で可否が変わる ——
    #: 手動へ「入る」のは塞ぐが、手動から「出る」のは塞いではならない —— ので、
    #: コマンド単位で一律に塞ぐこのゲートでは表せない。
    blocked_during_reenergize: bool
    reenergize_deny_message: str | None
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

        if self.blocked_during_manual and not self.manual_deny_message:
            raise ValueError(f"{self.name}: 手動操縦ゲートに理由文が無い")
        if not self.blocked_during_manual and self.manual_deny_message:
            raise ValueError(f"{self.name}: 手動操縦ゲートを掛けないのに拒否理由が書かれている")

        if self.blocked_during_reenergize and not self.reenergize_deny_message:
            raise ValueError(f"{self.name}: 再励磁ゲートに理由文が無い")
        if not self.blocked_during_reenergize and self.reenergize_deny_message:
            raise ValueError(f"{self.name}: 再励磁ゲートを掛けないのに拒否理由が書かれている")

    def phase_deny_reason(self, phase: Phase) -> str | None:
        """phase で実行できなければ理由を返す。実行できるなら None。"""
        if phase in self.allowed_phases:
            return None
        return self.phase_deny_message

    def e_stop_deny_reason(self) -> str | None:
        """緊急停止中に実行できなければ理由を返す。実行できるなら None。"""
        return None if self.allowed_during_e_stop else self.e_stop_deny_message

    def manual_deny_reason(self) -> str | None:
        """このコマンドが手動操縦ゲートの対象でなければ None、対象なら理由文を返す。

        実際に塞ぐかどうか (対象ロボットが今手動モードか) の判定は呼び出し側が持つ
        (`RobotServer` はロボットごとの `OperationMode` を data["robot"] から引く必要が
        あり、`CommandSpec` はロボット状態を知らない)。
        """
        return self.manual_deny_message if self.blocked_during_manual else None

    def reenergize_deny_reason(self) -> str | None:
        """このコマンドが再励磁ゲートの対象でなければ None、対象なら理由文を返す。

        `manual_deny_reason()` と同じく、実際に塞ぐか (対象ロボットの再励磁が今
        in-flight か) の判定は呼び出し側が持つ —— `CommandSpec` はサーバーの
        タスク表を知らない。
        """
        return self.reenergize_deny_message if self.blocked_during_reenergize else None

    def dev_tools_deny_reason(self, dev_tools_enabled: bool) -> str | None:
        """この起動で実行できなければ理由を返す。実行できるなら None。"""
        if not self.requires_dev_tools or dev_tools_enabled:
            return None
        return DEV_TOOLS_DENY_MESSAGE


def _spec(
    name: str,
    *,
    allowed_phases: frozenset[Phase],
    phase_deny_message: str | None = None,
    allowed_during_e_stop: bool,
    e_stop_deny_message: str | None = None,
    requires_dev_tools: bool = False,
    blocked_during_manual: bool = False,
    manual_deny_message: str | None = None,
    blocked_during_reenergize: bool = False,
    reenergize_deny_message: str | None = None,
    handler: str,
    reject_channel: RejectChannel = RejectChannel.COMMAND_REJECTED,
) -> CommandSpec:
    return CommandSpec(
        name=name,
        allowed_phases=allowed_phases,
        phase_deny_message=phase_deny_message,
        allowed_during_e_stop=allowed_during_e_stop,
        e_stop_deny_message=e_stop_deny_message,
        requires_dev_tools=requires_dev_tools,
        blocked_during_manual=blocked_during_manual,
        manual_deny_message=manual_deny_message,
        blocked_during_reenergize=blocked_during_reenergize,
        reenergize_deny_message=reenergize_deny_message,
        handler=handler,
        reject_channel=reject_channel,
    )


_SPECS: tuple[CommandSpec, ...] = (
    # ------------------------------------------------------------------ #
    #  シーケンス進行 — 試合中のみ、かつ緊急停止中は通さない。
    #  緊急停止中に次のステップが走ると、新しいモータ目標値が停止指令を上書きする。
    #
    #  **`blocked_during_manual=True` の 3 つは、対象ロボットが手動操縦モードなら
    #  塞ぐ。** 手動とシーケンスは同じ `AxisHandle.set_target_value` を通るので、
    #  ジョグ中の軸へシーケンスが別の目標値を書きに来ると衝突する。
    #
    #  **同じ 3 つが `blocked_during_reenergize=True` でもある** (再励磁が書く
    #  「フォルト前の現在角」が `move_to` の目標を上書きする)。**逆方向は塞がない**
    #  理由は `CommandSpec.blocked_during_reenergize` の宣言に書いてある。
    # ------------------------------------------------------------------ #
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
        handler="_cmd_trigger",
    ),
    # ------------------------------------------------------------------ #
    #  停止・復帰方向 — ゲートしないことをここで宣言する。
    #  止める操作を塞ぐと「動いている機体を止められない」状態が作れてしまうため、
    #  フェーズにも緊急停止にも依存させない。
    # ------------------------------------------------------------------ #
    _spec(
        # 手動操縦モード中でも再励磁の在飛中でも塞がない (`blocked_during_manual` /
        # `blocked_during_reenergize` を書かず既定 False のまま)。実行中のシーケンスは
        # 無いはずなので実害は無いうえ、塞ぐと「手動中は sequence_stop も送れない」
        # という止める側の操作を減らすだけになる
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
        # 開発用。指差喚呼を 1 操作で全部埋める。**試合開始ゲートを飛ばす操作**なので
        # 本番起動 (--dev-tools 無し) では語彙ごと閉じる。フェーズと緊急停止の扱いは
        # checklist_set と揃える (機体を動かさない = 緊急停止中も通す)
        "checklist_check_all",
        allowed_phases=PHASES_PREPARATION,
        phase_deny_message="このフェーズではチェックリストを操作できません",
        allowed_during_e_stop=True,
        requires_dev_tools=True,
        handler="_cmd_checklist_check_all",
    ),
    _spec(
        # アクチュエータを駆動するため試合中と緊急停止中は通さない
        # (HTTP POST 経路は handle_command を通らないので同じ判定がそちらにもある)。
        # 手動操縦モードとの排他は `_motor_check_environment_deny()` が持つ ——
        # 両ハンド横断の判定なので単一ロボットの data["robot"] では表せない
        "motor_check_start",
        allowed_phases=PHASES_OUTSIDE_MATCH,
        phase_deny_message="試合中は動作確認を実行できません",
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中のため動作確認を実行できません",
        handler="_cmd_motor_check_start",
        reject_channel=RejectChannel.MOTOR_CHECK_ERROR,
    ),
    _spec(
        # 励磁が落ちたモータを機体を止めずに戻す明示操作。**自動再励磁にはしない**
        # — fault が再発するモータへ無限に励磁し直す経路を作らないため。
        #
        # 試合中に使えないと直したい状況そのものが直せないので PHASES_ANY。一方
        # **緊急停止中は励磁してはならない**。手動操縦中もシーケンス実行中も塞がない
        # (理由は `CommandSpec.blocked_during_reenergize` の宣言)。動作確認との排他は
        # 両ハンド横断なのでハンドラ側が `MotorCheckController.running` を見る。
        "reenergize_motors",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中は再励磁できません",
        handler="_cmd_reenergize_motors",
    ),
    # ------------------------------------------------------------------ #
    #  手動操縦 — 調整時と、シーケンスからの退避に使う補助操縦。
    #
    #  **フェーズではゲートしない** (調整は準備中に、退避は試合中に要る)。
    #  **緊急停止ゲートは別軸で、指令だけを塞ぐ** —— モード切替そのものは機体を
    #  動かさないので停止中も通し、目標値を送る manual_* は通さない
    #  (docs/invariants.md「手動はフェーズでゲートしないが…」)。
    # ------------------------------------------------------------------ #
    _spec(
        "set_operation_mode",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=True,
        handler="_cmd_set_operation_mode",
    ),
    _spec(
        # 位置名によるプリセット指令。既定義の点しか送らないので全軸で使える
        "manual_move",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中のため手動操縦できません",
        handler="_cmd_manual_move",
    ),
    _spec(
        # 人間の単位の絶対値指定。可動範囲 (axes.<軸>.manual) を持つ軸のみ
        "manual_set",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中のため手動操縦できません",
        handler="_cmd_manual_set",
    ),
    _spec(
        # 直前の手動目標からの相対移動
        "manual_jog",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中のため手動操縦できません",
        handler="_cmd_manual_jog",
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
