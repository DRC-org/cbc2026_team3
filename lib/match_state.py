from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from lib.config_schema import DEFAULT_MATCH, MatchSettings

ROLE_PRE_MATCH = "pre_match"

ALL_ROLES: tuple[str, ...] = (ROLE_PRE_MATCH,)


class Court(StrEnum):
    RED = "red"
    BLUE = "blue"


class Phase(StrEnum):
    SETUP = "setup"
    READY = "ready"
    MATCH = "match"
    FINISHED = "finished"


PHASES_ANY: frozenset[Phase] = frozenset(Phase)

PHASES_DURING_MATCH: frozenset[Phase] = frozenset({Phase.MATCH})

PHASES_OUTSIDE_MATCH: frozenset[Phase] = frozenset({Phase.SETUP, Phase.READY, Phase.FINISHED})

PHASES_PREPARATION: frozenset[Phase] = frozenset({Phase.SETUP, Phase.READY})

PHASES_START_GATE: frozenset[Phase] = frozenset({Phase.READY})


@dataclass
class ChecklistItem:
    id: str
    label: str
    checked: bool = False
    group: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "checked": self.checked,
            "group": self.group,
        }


@dataclass
class ChecklistState:
    role: str
    items: list[ChecklistItem] = field(default_factory=list)

    @property
    def completed(self) -> bool:
        return all(item.checked for item in self.items)

    def reset(self) -> None:
        for item in self.items:
            item.checked = False

    def check_all(self) -> None:
        for item in self.items:
            item.checked = True

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "items": [item.to_dict() for item in self.items],
            "completed": self.completed,
        }


def load_checklist_definitions(config: dict) -> dict[str, list[ChecklistItem]]:
    raw = (config or {}).get("checklists") or {}
    definitions: dict[str, list[ChecklistItem]] = {role: [] for role in ALL_ROLES}

    for role, entries in raw.items():
        if role not in definitions:
            raise ValueError(
                f"未知の指差喚呼ロール '{role}' が checklists に書かれています"
                f" (使えるロール: {', '.join(ALL_ROLES)})。"
                "このまま起動すると、その項目は組み立ての段で丸ごと落ち、"
                "指差喚呼を 1 つも読み上げないまま試合を開始できてしまいます"
            )
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
            group = entry.get("group")
            items.append(
                ChecklistItem(
                    id=str(item_id),
                    label=str(label),
                    group=str(group) if group else None,
                )
            )
        definitions[role] = items

    return definitions


class MatchState:
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
        self._clock = clock
        self._started_at: float | None = None
        self._frozen_elapsed_s: float | None = None
        self._phase = Phase.SETUP
        self.checklists: dict[str, ChecklistState] = {}
        self._rebuild_checklists()

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
        return self._phase in phases

    @property
    def timer_running(self) -> bool:
        return self._started_at is not None and self._frozen_elapsed_s is None

    @property
    def elapsed_s(self) -> float:
        if self._frozen_elapsed_s is not None:
            return self._frozen_elapsed_s
        if self._started_at is None:
            return 0.0
        return self._clock() - self._started_at

    def set_court(self, court: Court) -> bool:
        if not self.allows(PHASES_OUTSIDE_MATCH):
            return False
        if court is not self._court:
            self._court = court
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

    def check_all_checklist_items(self, role: str | None = None) -> bool:
        if not self.allows(PHASES_PREPARATION):
            return False
        if role is None:
            for state in self.checklists.values():
                state.check_all()
        else:
            state = self.checklists.get(role)
            if state is None:
                return False
            state.check_all()
        self._sync_phase()
        return True

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
        self._started_at = self._clock()
        return True

    def match_finish(self) -> bool:
        if not self.allows(PHASES_DURING_MATCH):
            return False
        self._phase = Phase.FINISHED
        self._frozen_elapsed_s = self.elapsed_s
        return True

    def match_reset(self) -> bool:
        self._reset_all_checklists()
        self._phase = Phase.SETUP
        self._started_at = None
        self._frozen_elapsed_s = None
        self._sync_phase()
        return True

    def _rebuild_checklists(self) -> None:
        self.checklists = {
            role: ChecklistState(
                role=role,
                items=[
                    ChecklistItem(id=i.id, label=i.label, group=i.group)
                    for i in self._definitions.get(role, [])
                ],
            )
            for role in ALL_ROLES
        }
        self._sync_phase()

    def _reset_all_checklists(self) -> None:
        for state in self.checklists.values():
            state.reset()

    def _sync_phase(self) -> None:
        if self._phase in (Phase.MATCH, Phase.FINISHED):
            return
        self._phase = Phase.READY if self.can_start_match else Phase.SETUP

    def to_dict(self) -> dict:
        return {
            "type": "match_state",
            "court": self._court.value,
            "phase": self._phase.value,
            "can_start_match": self.can_start_match,
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
