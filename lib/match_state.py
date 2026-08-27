"""試合全体の状態 (コート・フェーズ・指差喚呼チェックリスト) を管理する。

ロボット単位の状態 (lib/server.py の state メッセージ) とは別に、
「どちらのコートで、セッティング中か試合中か」という
全クライアント共通の状態をここで一元管理する。

操縦者 2 名 + Monitor が別ブラウザで接続するため、チェックリストの進捗を
クライアント側に持つと「2 人とも完了」の判定ができない。正はサーバー側に置く。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from lib.config_schema import DEFAULT_MATCH, MatchSettings

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
        settings: MatchSettings = DEFAULT_MATCH,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._definitions: dict[str, list[ChecklistItem]] = definitions or {
            role: [] for role in ALL_ROLES
        }
        self._court = court
        self._settings = settings
        # 経過時間は必ず単調時計で測る。time.time() は NTP 補正で後ろへ飛ぶことがあり、
        # 試合中に残り時間が増える (= 操縦者が残り時間を信用できなくなる)。
        self._clock = clock
        #: 試合開始時点の単調時刻。未開始は None
        self._started_at: float | None = None
        #: 試合終了時点で凍結した経過秒。結果確認中に数字が進み続けないようにする
        self._frozen_elapsed_s: float | None = None
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

    @property
    def timer_running(self) -> bool:
        """試合時間が進行中か。開始前と終了後 (凍結済み) は False。"""
        return self._started_at is not None and self._frozen_elapsed_s is None

    @property
    def elapsed_s(self) -> float:
        """試合開始からの経過秒。未開始は 0、終了後は終了時点で凍結した値。"""
        if self._frozen_elapsed_s is not None:
            return self._frozen_elapsed_s
        if self._started_at is None:
            return 0.0
        return self._clock() - self._started_at

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
        # 起点はフェーズ遷移が成立した後にだけ引く。試合中に届いた match_start は
        # ゲートで弾かれるが、その手前で起点を書き換えると機体は動いたまま
        # タイマーだけが満了時間へ巻き戻る。
        # 凍結の解除は match_reset だけが行う (READY へは match_reset を通ってしか
        # 到達できないため)。FINISHED から直接 READY へ戻す遷移を足すなら、
        # そこでも _frozen_elapsed_s を落とすこと。
        self._started_at = self._clock()
        return True

    def match_finish(self) -> bool:
        if not self.allows(PHASES_DURING_MATCH):
            return False
        self._phase = Phase.FINISHED
        # 終了時点の経過を焼き付ける。凍結を解くのは match_start だけで、
        # 解き忘れると 2 試合目が 1 試合目の残り時間から始まる
        self._frozen_elapsed_s = self.elapsed_s
        return True

    def match_reset(self) -> bool:
        """どのフェーズからでもセッティングタイムに戻す。コートは維持する。"""
        self._reset_all_checklists()
        self._phase = Phase.SETUP
        self._started_at = None
        self._frozen_elapsed_s = None
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
            # タイマーは「残り時間」ではなく**この配信瞬間の経過ミリ秒**を配る。
            # 各デバイスはこれを起点に自分の単調時計で進めるため、デバイス間のずれは
            # WS の片道遅延ぶん (数 ms) に収まり、**端末の壁時計が揃っている必要がない**。
            # 残り時間そのものを毎秒配ると (1) match_state の参照が毎秒作り直され
            # useRobotStatus を読む全画面が再描画される (2) 配信が詰まった 1 台では
            # タイマーだけが凍り、WS は「接続中」のままなので操縦者が気付けない。
            "timer": {
                "running": self.timer_running,
                "elapsed_ms": round(self.elapsed_s * 1000),
                "duration_ms": round(self._settings.duration_s * 1000),
            },
            "checklists": {
                role: {
                    "items": [item.to_dict() for item in state.items],
                    "completed": state.completed,
                }
                for role, state in self.checklists.items()
            },
        }
