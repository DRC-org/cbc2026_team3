from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["MotorSpec", "SyncGroup"]

_TARGET_ALIGN_EPSILON = 1e-6


@dataclass(frozen=True)
class MotorSpec:
    name: str
    scale: float
    offset: float

    def to_command(self, value: float) -> float:
        return value * self.scale + self.offset

    def to_value(self, command: float) -> float:
        return (command - self.offset) / self.scale

    def to_tolerance(self, tolerance: float) -> float:
        return abs(tolerance * self.scale)


@dataclass(frozen=True)
class SyncGroup:
    name: str
    members: tuple[MotorSpec, ...]
    tolerance: float
    sync_kp: float = 0.0
    sync_limit: float | None = None

    def __post_init__(self) -> None:
        if self.sync_kp < 0.0:
            raise ValueError(
                f"同期グループ '{self.name}' の sync_kp が負です ({self.sync_kp}): "
                "正帰還になりずれが発散します"
            )
        if self.sync_limit is not None and self.sync_limit < 0.0:
            raise ValueError(
                f"同期グループ '{self.name}' の sync_limit が負です ({self.sync_limit})"
            )
        if self.sync_kp != 0.0 and self.sync_limit is None:
            raise ValueError(
                f"同期グループ '{self.name}' に sync_kp があるのに sync_limit がありません "
                "(押し合いの歯止めが無くなります)"
            )

    def deviation(self, positions: Mapping[str, float]) -> float | None:
        values = [
            member.to_value(positions[member.name])
            for member in self.members
            if member.name in positions
        ]
        if len(values) < 2:
            return None
        return max(values) - min(values)

    def violation(self, positions: Mapping[str, float]) -> float | None:
        deviation = self.deviation(positions)
        if deviation is None or deviation <= self.tolerance:
            return None
        return deviation

    def targets_share_axis_value(self, targets: Mapping[str, float]) -> bool:
        values = [
            member.to_value(targets[member.name])
            for member in self.members
            if member.name in targets
        ]
        if len(values) != len(self.members) or not values:
            return False
        return max(values) - min(values) <= _TARGET_ALIGN_EPSILON

    def corrections(self, positions: Mapping[str, float]) -> dict[str, float]:
        if self.sync_kp == 0.0:
            return {}
        values = {
            member.name: member.to_value(positions[member.name])
            for member in self.members
            if member.name in positions
        }
        if len(values) != len(self.members) or not values:
            return {}

        mean = sum(values.values()) / len(values)
        corrections: dict[str, float] = {}
        for member in self.members:
            command_error = (mean - values[member.name]) * member.scale
            correction = self.sync_kp * command_error
            if self.sync_limit is not None:
                correction = max(-self.sync_limit, min(self.sync_limit, correction))
            corrections[member.name] = correction
        return corrections
