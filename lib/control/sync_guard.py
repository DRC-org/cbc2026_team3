from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping

from lib.axis_sync import SyncGroup

logger = logging.getLogger(__name__)

__all__ = ["SyncGuard"]

PositionReader = Callable[[str], float]


class SyncGuard:
    def __init__(self, *, context: str = "", logger: logging.Logger = logger) -> None:
        self._context = f" ({context})" if context else ""
        self._logger = logger
        self._groups: dict[str, SyncGroup] = {}
        self._group_of: dict[str, str] = {}
        self._violations: set[str] = set()
        self._stale_groups: set[str] = set()

    def add(self, group: SyncGroup) -> None:
        if group.name in self._groups:
            raise ValueError(f"同期グループ '{group.name}' は既に登録済み")

        for member in group.members:
            existing = self._group_of.get(member.name)
            if existing is not None:
                raise ValueError(f"モータ '{member.name}' は既に同期グループ '{existing}' に所属")

        self._groups[group.name] = group
        for member in group.members:
            self._group_of[member.name] = group.name

    @property
    def group_names(self) -> tuple[str, ...]:
        return tuple(self._groups)

    def __contains__(self, group_name: object) -> bool:
        return group_name in self._groups

    def group_of(self, motor_name: str) -> str | None:
        return self._group_of.get(motor_name)

    def members_of(self, group_name: str) -> tuple[str, ...]:
        return tuple(member.name for member in self._groups[group_name].members)

    @property
    def violations(self) -> frozenset[str]:
        return frozenset(self._violations)

    def reset(self, name: str | None = None) -> None:
        if name is None:
            self._violations.clear()
            return
        if name not in self._groups:
            raise KeyError(name)
        self._violations.discard(name)

    def blocked(self, *, stale: Mapping[str, bool], position_of: PositionReader) -> frozenset[str]:
        blocked = set(self._violations)
        for group in self._groups.values():
            if any(stale.get(member.name, True) for member in group.members):
                blocked.add(group.name)
                if group.name not in self._stale_groups:
                    self._stale_groups.add(group.name)
                    self._logger.warning(
                        "同期グループのフィードバック途絶のため全員を電流 0 に落とす (axis=%s)%s",
                        group.name,
                        self._context,
                    )
                continue
            self._stale_groups.discard(group.name)
            if group.name in self._violations:
                continue
            if self._check_deviation(group, position_of):
                blocked.add(group.name)
        return frozenset(blocked)

    def corrections(
        self, *, position_of: PositionReader, skip_groups: Collection[str]
    ) -> dict[str, float]:
        corrections: dict[str, float] = {}
        for group in self._groups.values():
            if group.name in skip_groups or group.sync_kp == 0.0:
                continue
            positions = {member.name: position_of(member.name) for member in group.members}
            corrections.update(group.corrections(positions))
        return corrections

    def _check_deviation(self, group: SyncGroup, position_of: PositionReader) -> bool:
        positions = {member.name: position_of(member.name) for member in group.members}
        deviation = group.violation(positions)
        if deviation is None:
            return False

        self._violations.add(group.name)
        self._logger.error(
            "同期ずれのため電流 0 にラッチ (axis=%s, deviation=%.3f, tolerance=%.3f)%s",
            group.name,
            deviation,
            group.tolerance,
            self._context,
        )
        return True
