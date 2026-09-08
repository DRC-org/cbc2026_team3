from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BusHealth(Enum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


class MotorHealth(Enum):
    OK = "ok"
    STALE = "stale"
    WARNING = "warning"
    FAULT = "fault"


@dataclass
class BusHealthInfo:
    name: str
    channel: str
    state: BusHealth
    last_tx_at: float | None
    last_rx_at: float | None
    tx_error_count: int
    rx_error_count: int
    bus_off: bool
    rx_down: bool = False
    rx_down_episodes: int = 0
    may_affect_workpiece: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "channel": self.channel,
            "state": self.state.value,
            "last_tx_at": self.last_tx_at,
            "last_rx_at": self.last_rx_at,
            "tx_error_count": self.tx_error_count,
            "rx_error_count": self.rx_error_count,
            "bus_off": self.bus_off,
            "rx_down": self.rx_down,
            "rx_down_episodes": self.rx_down_episodes,
            "may_affect_workpiece": self.may_affect_workpiece,
        }


@dataclass
class MotorHealthInfo:
    name: str
    bus: str
    state: MotorHealth
    last_feedback_at: float | None
    feedback_age_ms: float | None
    temperature: float | None
    detail: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "bus": self.bus,
            "state": self.state.value,
            "last_feedback_at": self.last_feedback_at,
            "feedback_age_ms": self.feedback_age_ms,
            "temperature": self.temperature,
            "detail": self.detail,
        }


_MOTOR_TO_BUS_SEVERITY: dict[MotorHealth, BusHealth] = {
    MotorHealth.OK: BusHealth.OK,
    MotorHealth.STALE: BusHealth.DEGRADED,
    MotorHealth.WARNING: BusHealth.DEGRADED,
    MotorHealth.FAULT: BusHealth.DOWN,
}

_BUS_SEVERITY_RANK: dict[BusHealth, int] = {
    BusHealth.OK: 0,
    BusHealth.DEGRADED: 1,
    BusHealth.DOWN: 2,
}


def worst_bus_health(states: Iterable[BusHealth]) -> BusHealth:
    return max(states, key=_BUS_SEVERITY_RANK.__getitem__, default=BusHealth.OK)


@dataclass
class HealthSnapshot:
    timestamp: float
    overall: BusHealth
    buses: list[BusHealthInfo] = field(default_factory=list)
    motors: list[MotorHealthInfo] = field(default_factory=list)
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "overall": self.overall.value,
            "buses": [b.to_dict() for b in self.buses],
            "motors": [m.to_dict() for m in self.motors],
            "detail": self.detail,
        }

    @staticmethod
    def compute_overall(buses: list[BusHealthInfo], motors: list[MotorHealthInfo]) -> BusHealth:
        return worst_bus_health(
            [b.state for b in buses] + [_MOTOR_TO_BUS_SEVERITY[m.state] for m in motors]
        )
