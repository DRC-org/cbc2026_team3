"""操縦者から届く WS コマンドの語彙を 1 箇所に集める。

以前はコマンド名が「許可フェーズ表」「フェーズ拒否文」「緊急停止拒否文」
「_handle_command の if-elif」の 4 箇所に裸の文字列で散っていた。その結果
`sequence_stop` と `motor_check_abort` はどのゲート表にも載っておらず、
「意図してゲート対象外にした」のか「単に書き忘れた」のかコードから読めなくなっていた。

ここでは 1 コマンド = 1 `CommandSpec` とし、**フィールドに既定値を一切持たせない**。
新しいコマンドを足す人は許可フェーズも緊急停止中の可否もハンドラも必ず書くことになり、
「表に無いから素通り」という暗黙の状態を作れない。全フェーズで通したいコマンドは
`PHASES_ANY` を明示的に書く (= 素通りさせると宣言する)。

判定そのものは 5 段で、どれも独立している。開発用ゲート (この起動でそのコマンドが
存在するか)、フェーズゲート (試合進行としての可否)、緊急停止ゲート (今この瞬間モータを
動かしてよいか)、手動操縦ゲート (対象ロボットの制御権を誰が握っているか)、再励磁ゲート
(対象ロボットの励磁を今書き換えている最中か)。フェーズが `match` のままでも緊急停止中は
止める必要があるように、1 つが通っても他は通らないことがある。後ろ 2 つはロボットごとの
状態と掛け合わせて初めて決まるので、`CommandSpec` は「ゲート対象にしたか」と理由文だけを
持ち、掛け合わせは `RobotServer` が行う。
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
    #: 対象ロボット (data["robot"]) が手動操縦モードのとき塞ぐか。
    #: 手動操縦は制御権をシーケンスから奪う操作で、同時に 2 つは立たない
    #: (CLAUDE.md)。塞ぐのは「シーケンスの制御権を使う」コマンドだけで、
    #: モード切替そのもの・停止方向の操作・ロボットを指定しないコマンドは
    #: 対象外 (False)。True にする側にだけ理由文を書かせる
    blocked_during_manual: bool
    manual_deny_message: str | None
    #: モータを今励磁し直しているあいだ塞ぐか。**片方向だけのゲートである。**
    #:
    #: 在飛と見なすのは 2 つ —— 対象ロボット (data["robot"]) の単発再励磁
    #: (`reenergize_motors`) と、**ロボット名に依らない緊急停止解除の再励磁**
    #: (`RobotServer._reactivate_motors`)。後者は全ロボットぶんをまとめて処理する
    #: ので、対象ロボットが誰であっても塞ぐ。判定は
    #: `RobotServer._is_reenergizing` の 1 箇所だけが持つ。
    #:
    #: 再励磁は「フォルト前の現在角」を目標として書いてから enable する。その
    #: 100ms〜1.5 秒のあいだにシーケンスが `move_to` で書いた目標が上書きされると、
    #: `AxisHandle.wait_reached` は動かない位置を見続けて `SequenceTimeoutError` で
    #: 止まる (症状は「NEXT を押したのに動かない」だけ)。
    #:
    #: **逆方向 (シーケンス実行中の再励磁) は塞がない。** 塞ぐと「試合中に機体を
    #: 止めずに励磁を戻す」というこの機能の主目的そのものが消える —— 励磁が落ちる
    #: のはたいていシーケンスを走らせている最中で、そこで使えなければ直したい状況が
    #: 直せない。CLAUDE.md「制御権の奪い合いは両方向を塞ぐ」に対する意図的な例外で、
    #: 塞ぐのは在飛中の 100ms〜1.5 秒だけなので待てば必ず通る。
    #:
    #: 手動側の同じ排他はハンドラが持つ (`RobotServer._apply_operation_mode` /
    #: `_manual_target`)。`set_operation_mode` は方向で可否が変わる ——
    #: 手動へ「入る」のは塞ぐが、手動から「出る」のは塞いではならない (退避路から
    #: 戻れなくなる) —— ので、コマンド単位で一律に塞ぐこのゲートでは表せない。
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
    #  塞ぐ。** 手動 (`lib/manual.py`) とシーケンス (`lib/sequence/engine.py`) は
    #  同じ `AxisHandle.set_target_value` を通るので、手動でジョグ中の軸へ
    #  シーケンスが別の目標値を書きに来ると衝突する。手動へ入る側の防御
    #  (`_apply_operation_mode` が `_stop_sequence` で制御権を奪う) は既にあったが、
    #  逆方向 (手動中に飛んできた `sequence_start` 等を弾く) が無かった。
    #
    #  **同じ 3 つが `blocked_during_reenergize=True` でもある。** 再励磁は
    #  `reenergize_motors` が `PHASES_ANY` なので試合中のシーケンス実行中にも
    #  押せる。在飛中に NEXT が押されると、シーケンスの `move_to` が書いた目標を
    #  再励磁の「フォルト前の現在角」が上書きし、`wait_reached` は動かない位置を
    #  見続けて `SequenceTimeoutError` で止まる。**逆方向は塞がない**理由は
    #  `CommandSpec.blocked_during_reenergize` の宣言に書いてある。
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
        # モータを微小駆動するため試合中と緊急停止中は通さない。
        # HTTP POST 経路は _handle_command を通らないので _start_motor_check 側にも同じ判定がある。
        # 手動操縦モードとの排他は `blocked_during_manual` ではなくここでは
        # `_motor_check_environment_deny()` が持つ (両ハンド横断で「どれか 1 台でも
        # 手動なら拒否」を見る必要があり、単一ロボットの data["robot"] では表せない)
        "motor_check_start",
        allowed_phases=PHASES_OUTSIDE_MATCH,
        phase_deny_message="試合中は動作確認を実行できません",
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中のため動作確認を実行できません",
        handler="_cmd_motor_check_start",
        reject_channel=RejectChannel.MOTOR_CHECK_ERROR,
    ),
    _spec(
        # 励磁が落ちたモータ (EDULITE 05 / DM3520 の fault) を、機体を止めずに
        # 戻す明示操作 (docs/checks_and_health.md「復帰には…が要る」)。**自動再励磁には
        # しない** — fault が再発するモータへ無限に励磁し直す経路を作らないため、
        # 操縦者がこのコマンドを送ったときにしか実行しない。
        #
        # 試合中に使えないと直したい状況そのものが直せないので PHASES_ANY。
        # 一方 **緊急停止中は励磁してはならない** — 緊急停止の意味が消える。
        # 手動操縦中は塞がない — 手動はシーケンスからの退避路そのものなので、
        # 手動中に落ちた励磁を手動のまま戻せないと退避路自体が詰む。**シーケンス
        # 実行中も塞がない** — 励磁が落ちるのはたいていシーケンスを走らせている
        # 最中で、そこで使えなければ直したい状況が直せない (逆向きだけを
        # `blocked_during_reenergize` で塞ぐ理由はそちらの宣言に書いてある)。動作確認との
        # 排他は `MotorCheckController.running` をハンドラ側で見る (両ハンド横断の
        # 排他で、単一ロボットの data["robot"] では表せないため motor_check_start と
        # 同じ理由で CommandSpec の外に置く)。
        "reenergize_motors",
        allowed_phases=PHASES_ANY,
        allowed_during_e_stop=False,
        e_stop_deny_message="緊急停止中は再励磁できません",
        handler="_cmd_reenergize_motors",
    ),
    # ------------------------------------------------------------------ #
    #  手動操縦 — 調整時と、シーケンスからの退避に使う補助操縦。
    #
    #  **フェーズではゲートしない。** 機構の調整は準備中に、シーケンスが想定外の
    #  状態で止まったときの退避は試合中に要る。どちらか一方に閉じると、要るときに
    #  使えない操作になる。
    #
    #  **緊急停止ゲートは別軸で、指令だけを塞ぐ。** モード切替そのものは機体を
    #  動かさないので停止中も通す (停止中に画面を手動へ寄せて、解除と同時に動かす
    #  という手順を塞ぐ理由が無い)。一方 manual_* は目標値を送る操作なので、
    #  通すと緊急停止が意味を失う。sequence_start / trigger と同じ扱いにする。
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
