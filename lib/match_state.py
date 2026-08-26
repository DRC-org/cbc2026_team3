"""試合全体の状態 (コート・フェーズ・指差喚呼チェックリスト) を管理する。

ロボット単位の状態 (lib/server.py の state メッセージ) とは別に、
「どちらのコートで、セッティング中か試合中か」という
全クライアント共通の状態をここで一元管理する。

操縦者 2 名 + Monitor が別ブラウザで接続するため、チェックリストの進捗を
クライアント側に持つと「2 人とも完了」の判定ができない。正はサーバー側に置く。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

ROLE_MAIN_HAND = "main_hand"
ROLE_SUB_HAND = "sub_hand"

#: チェックリストを持ちうる全ロール。UI が KeyError にならないよう常にこの順で埋める。
#: 操縦者 2 名分が揃うまで試合に入れない (= 全ロールが試合開始のゲートを兼ねる)。
ALL_ROLES: tuple[str, ...] = (ROLE_MAIN_HAND, ROLE_SUB_HAND)


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


# 以下はこの状態機械が「どのフェーズで何を受け付けるか」の唯一の定義。
# lib/commands.py のコマンドゲートも同じ定数を参照する。名前付きの集合にしておかないと、
# MatchState 自身の遷移条件とコマンドゲートに同じ列挙が二重に書かれ、片方だけ直されて
# 「サーバーは受け付けるのに状態機械が拒む」ずれが生まれる。

#: ゲートしない (全フェーズで受け付ける) ことを明示するための集合。
PHASES_ANY: frozenset[Phase] = frozenset(Phase)

#: 試合中のみ。シーケンスの進行操作と試合終了はここ。
PHASES_DURING_MATCH: frozenset[Phase] = frozenset({Phase.MATCH})

#: 試合中以外。モータを微小駆動する動作確認や設定変更は試合進行を乱すため試合中に通さない。
PHASES_OUTSIDE_MATCH: frozenset[Phase] = frozenset({Phase.SETUP, Phase.READY, Phase.FINISHED})

#: 準備中のみ。指差喚呼は試合が終わるまでやり直させない (結果確認の前に消させない)。
PHASES_PREPARATION: frozenset[Phase] = frozenset({Phase.SETUP, Phase.READY})

#: 試合開始ゲート。READY = 2 名の指差喚呼が揃った状態でしか試合へ入れない。
PHASES_START_GATE: frozenset[Phase] = frozenset({Phase.READY})


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
        court: Court = Court.RED,
    ) -> None:
        self._definitions: dict[str, list[ChecklistItem]] = definitions or {
            role: [] for role in ALL_ROLES
        }
        self._court = court
        self._phase = Phase.SETUP
        self.checklists: dict[str, ChecklistState] = {}
        self._rebuild_checklists()

    # ------------------------------------------------------------------ #
    #  読み取り
    # ------------------------------------------------------------------ #

    @property
    def court(self) -> Court:
        return self._court

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def can_start_match(self) -> bool:
        return all(state.completed for state in self.checklists.values())

    def allows(self, phases: frozenset[Phase]) -> bool:
        """現フェーズが phases に含まれるか。コマンドゲートと遷移条件の共通判定。"""
        return self._phase in phases

    # ------------------------------------------------------------------ #
    #  更新
    # ------------------------------------------------------------------ #

    def set_court(self, court: Court) -> bool:
        if not self.allows(PHASES_OUTSIDE_MATCH):
            return False
        if court is not self._court:
            self._court = court
            # コートが変われば機体配置も変わるので指差喚呼はやり直し
            self._reset_all_checklists()
        self._sync_phase()
        return True

    def set_checklist_item(self, role: str, item_id: str, checked: bool) -> bool:
        if not self.allows(PHASES_PREPARATION):
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
        if not self.allows(PHASES_PREPARATION):
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
        if not self.allows(PHASES_START_GATE):
            return False
        self._phase = Phase.MATCH
        return True

    def match_finish(self) -> bool:
        if not self.allows(PHASES_DURING_MATCH):
            return False
        self._phase = Phase.FINISHED
        return True

    def match_reset(self) -> bool:
        """どのフェーズからでもセッティングタイムに戻す。コートは維持する。"""
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
            "court": self._court.value,
            "phase": self._phase.value,
            "can_start_match": self.can_start_match,
            "checklists": {
                role: {
                    "items": [item.to_dict() for item in state.items],
                    "completed": state.completed,
                }
                for role, state in self.checklists.items()
            },
        }
