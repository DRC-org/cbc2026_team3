"""試合全体の状態 (モード・コート・フェーズ・指差喚呼チェックリスト) を管理する。

ロボット単位の状態 (lib/server.py の state メッセージ) とは別に、
「今どのモードで、どちらのコートで、セッティング中か試合中か」という
全クライアント共通の状態をここで一元管理する。

操縦者 2 名 + Monitor が別ブラウザで接続するため、チェックリストの進捗を
クライアント側に持つと「2 人とも完了」の判定ができない。正はサーバー側に置く。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

ROLE_MONITOR = "monitor"
ROLE_MAIN_HAND = "main_hand"
ROLE_SUB_HAND = "sub_hand"

#: チェックリストを持ちうる全ロール。UI が KeyError にならないよう常にこの順で埋める
ALL_ROLES: tuple[str, ...] = (ROLE_MONITOR, ROLE_MAIN_HAND, ROLE_SUB_HAND)


class Mode(StrEnum):
    """操作モード。半自動は操縦者 2 名、全自動は Monitor 1 名で運用する。"""

    SEMI_AUTO = "semi_auto"
    FULL_AUTO = "full_auto"


class Court(StrEnum):
    """自陣コート。赤青で配置が左右反転する。"""

    RED = "red"
    BLUE = "blue"


class Phase(StrEnum):
    """試合進行フェーズ。

    SETUP    — セッティングタイム。チェックリスト実施中
    READY    — チェックリスト完了。試合開始待ち
    MATCH    — 試合中。シーケンス操作が解禁される唯一のフェーズ
    FINISHED — 試合終了。結果確認後 match_reset で SETUP へ戻る
    """

    SETUP = "setup"
    READY = "ready"
    MATCH = "match"
    FINISHED = "finished"


#: モードごとに完了が必須となるロール。半自動は操縦者 2 名分が揃うまで試合に入れない
_REQUIRED_ROLES: dict[Mode, list[str]] = {
    Mode.SEMI_AUTO: [ROLE_MAIN_HAND, ROLE_SUB_HAND],
    Mode.FULL_AUTO: [ROLE_MONITOR],
}

#: 実行を許可するフェーズをコマンド別に列挙する。ここに無いコマンドはゲート対象外。
#: UI でボタンを隠すだけでは WS を直接叩かれた場合やリロード直後を防げないため、
#: サーバー側でも同じ制約を二重に掛ける。
_ALLOWED_PHASES: dict[str, frozenset[Phase]] = {
    # 試合中のみ: シーケンスの進行操作
    "sequence_start": frozenset({Phase.MATCH}),
    "sequence_jump": frozenset({Phase.MATCH}),
    "trigger": frozenset({Phase.MATCH}),
    # 試合中以外: モータを微小駆動する動作確認は試合中に走らせてはならない
    "motor_check_start": frozenset({Phase.SETUP, Phase.READY, Phase.FINISHED}),
    # 準備中のみ: 設定変更とチェックリスト操作
    "set_mode": frozenset({Phase.SETUP, Phase.READY, Phase.FINISHED}),
    "set_court": frozenset({Phase.SETUP, Phase.READY, Phase.FINISHED}),
    "checklist_set": frozenset({Phase.SETUP, Phase.READY}),
    "checklist_reset": frozenset({Phase.SETUP, Phase.READY}),
    # フェーズ遷移そのもの
    "match_start": frozenset({Phase.READY}),
    "match_finish": frozenset({Phase.MATCH}),
}

#: 拒否理由の文言。操縦者がなぜ弾かれたか分かるようにする
_DENY_MESSAGES: dict[str, str] = {
    "sequence_start": "試合中のみシーケンスを開始できます",
    "sequence_jump": "試合中のみステップ移動できます",
    "trigger": "試合中のみトリガーを送れます",
    "motor_check_start": "試合中は動作確認を実行できません",
    "set_mode": "試合中はモードを変更できません",
    "set_court": "試合中はコートを変更できません",
    "checklist_set": "このフェーズではチェックリストを操作できません",
    "checklist_reset": "このフェーズではチェックリストを操作できません",
    "match_start": "チェックリスト完了後に試合を開始できます",
    "match_finish": "試合中ではありません",
}


@dataclass
class ChecklistItem:
    """指差喚呼 1 項目。"""

    id: str
    label: str
    checked: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "checked": self.checked}


@dataclass
class ChecklistState:
    """1 ロール分のチェックリスト。"""

    role: str
    items: list[ChecklistItem] = field(default_factory=list)

    @property
    def completed(self) -> bool:
        # 項目未定義のロールを未完了扱いにするとゲートが永久に開かないため完了とみなす
        return all(item.checked for item in self.items)

    def reset(self) -> None:
        for item in self.items:
            item.checked = False

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "items": [item.to_dict() for item in self.items],
            "completed": self.completed,
        }


def load_checklist_definitions(config: dict) -> dict[str, list[ChecklistItem]]:
    """config (checklist.yaml 相当) の dict からチェックリスト定義を組み立てる。

    id / label を持たないエントリは無視する。yaml の記述ミスで起動が落ちるより、
    項目が欠けた状態で起動して UI 上で気付ける方が競技当日の運用に適する。
    """
    raw = (config or {}).get("checklists") or {}
    definitions: dict[str, list[ChecklistItem]] = {role: [] for role in ALL_ROLES}

    for role, entries in raw.items():
        if not isinstance(entries, list):
            continue
        items: list[ChecklistItem] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            item_id = entry.get("id")
            label = entry.get("label")
            if not item_id or not label:
                continue
            items.append(ChecklistItem(id=str(item_id), label=str(label)))
        definitions[role] = items

    return definitions


class MatchState:
    """試合全体の状態機械。

    フェーズ遷移は SETUP ⇄ READY → MATCH → FINISHED → SETUP。
    SETUP ⇄ READY はチェックリストの完了状況から自動で決まり、
    MATCH への遷移だけが明示的な操作 (match_start) を要する。
    """

    def __init__(
        self,
        definitions: dict[str, list[ChecklistItem]] | None = None,
        *,
        mode: Mode = Mode.SEMI_AUTO,
        court: Court = Court.RED,
    ) -> None:
        self._definitions: dict[str, list[ChecklistItem]] = definitions or {
            role: [] for role in ALL_ROLES
        }
        self._mode = mode
        self._court = court
        self._phase = Phase.SETUP
        self.checklists: dict[str, ChecklistState] = {}
        self._rebuild_checklists()

    # ------------------------------------------------------------------ #
    #  読み取り
    # ------------------------------------------------------------------ #

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def court(self) -> Court:
        return self._court

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def required_roles(self) -> list[str]:
        return list(_REQUIRED_ROLES[self._mode])

    @property
    def can_start_match(self) -> bool:
        return all(self.checklists[role].completed for role in self.required_roles)

    def deny_reason(self, command: str) -> str | None:
        """現フェーズで command が許可されなければ理由文字列を返す。許可なら None。"""
        allowed = _ALLOWED_PHASES.get(command)
        if allowed is None or self._phase in allowed:
            return None
        return _DENY_MESSAGES.get(
            command, f"現在のフェーズ ({self._phase.value}) では実行できません"
        )

    # ------------------------------------------------------------------ #
    #  更新
    # ------------------------------------------------------------------ #

    def set_mode(self, mode: Mode) -> bool:
        if self.deny_reason("set_mode") is not None:
            return False
        if mode is not self._mode:
            self._mode = mode
            # 別モードのチェック結果を引き継ぐと未確認のまま試合に入れてしまう
            self._reset_all_checklists()
        self._sync_phase()
        return True

    def set_court(self, court: Court) -> bool:
        if self.deny_reason("set_court") is not None:
            return False
        if court is not self._court:
            self._court = court
            # コートが変われば機体配置も変わるので指差喚呼はやり直し
            self._reset_all_checklists()
        self._sync_phase()
        return True

    def set_checklist_item(self, role: str, item_id: str, checked: bool) -> bool:
        if self.deny_reason("checklist_set") is not None:
            return False
        state = self.checklists.get(role)
        if state is None:
            return False
        for item in state.items:
            if item.id == item_id:
                item.checked = bool(checked)
                self._sync_phase()
                return True
        return False

    def reset_checklist(self, role: str | None = None) -> bool:
        if self.deny_reason("checklist_reset") is not None:
            return False
        if role is None:
            self._reset_all_checklists()
        else:
            state = self.checklists.get(role)
            if state is None:
                return False
            state.reset()
        self._sync_phase()
        return True

    def match_start(self) -> bool:
        if self.deny_reason("match_start") is not None:
            return False
        self._phase = Phase.MATCH
        return True

    def match_finish(self) -> bool:
        if self.deny_reason("match_finish") is not None:
            return False
        self._phase = Phase.FINISHED
        return True

    def match_reset(self) -> bool:
        """どのフェーズからでもセッティングタイムに戻す。モード・コートは維持する。"""
        self._reset_all_checklists()
        self._phase = Phase.SETUP
        self._sync_phase()
        return True

    # ------------------------------------------------------------------ #
    #  内部
    # ------------------------------------------------------------------ #

    def _rebuild_checklists(self) -> None:
        # 定義を共有すると 1 ロールのチェックが他ロールへ伝播するため必ず複製する
        self.checklists = {
            role: ChecklistState(
                role=role,
                items=[
                    ChecklistItem(id=i.id, label=i.label) for i in self._definitions.get(role, [])
                ],
            )
            for role in ALL_ROLES
        }
        self._sync_phase()

    def _reset_all_checklists(self) -> None:
        for state in self.checklists.values():
            state.reset()

    def _sync_phase(self) -> None:
        """SETUP ⇄ READY をチェックリストの完了状況に追従させる。

        MATCH / FINISHED 中は追従しない (試合中にチェックが外れて
        フェーズが巻き戻ると進行中のシーケンスが止まってしまう)。
        """
        if self._phase in (Phase.MATCH, Phase.FINISHED):
            return
        self._phase = Phase.READY if self.can_start_match else Phase.SETUP

    # ------------------------------------------------------------------ #
    #  配信
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict:
        return {
            "type": "match_state",
            "mode": self._mode.value,
            "court": self._court.value,
            "phase": self._phase.value,
            "required_roles": self.required_roles,
            "can_start_match": self.can_start_match,
            "checklists": {
                role: {
                    "items": [item.to_dict() for item in state.items],
                    "completed": state.completed,
                }
                for role, state in self.checklists.items()
            },
        }
