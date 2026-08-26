from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BusHealth(Enum):
    """CAN バス全体の健全性。受動監視 (送信失敗例外 + 受信タイムスタンプ) で判定する。"""

    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


class MotorHealth(Enum):
    """個別モータの健全性。フィードバック鮮度 + ドライバ固有の警告/異常フラグから判定する。"""

    OK = "ok"
    STALE = "stale"
    WARNING = "warning"
    FAULT = "fault"


class MotorCheckResult(Enum):
    """能動アクチュエータ動作確認の結果コード。"""

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


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


# モータ状態を BusHealth 表現に正規化する: STALE/WARNING は DEGRADED 相当、FAULT は DOWN 相当
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


@dataclass
class HealthSnapshot:
    timestamp: float
    overall: BusHealth
    buses: list[BusHealthInfo] = field(default_factory=list)
    motors: list[MotorHealthInfo] = field(default_factory=list)
    #: 判定そのものが行えなかった理由。バス・モータの一覧が空の DOWN は
    #: 「全部壊れている」のか「健全性を計算できなかった」のか区別が付かないため、
    #: 後者だけがここに理由を持つ
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
        worst_rank = 0
        for b in buses:
            worst_rank = max(worst_rank, _BUS_SEVERITY_RANK[b.state])
        for m in motors:
            worst_rank = max(worst_rank, _BUS_SEVERITY_RANK[_MOTOR_TO_BUS_SEVERITY[m.state]])
        for state, rank in _BUS_SEVERITY_RANK.items():
            if rank == worst_rank:
                return state
        return BusHealth.OK


@dataclass
class MotorCheckRecord:
    motor: str
    bus: str
    started_at: float
    finished_at: float | None
    result: MotorCheckResult
    expected: float | None
    observed: float | None
    detail: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "motor": self.motor,
            "bus": self.bus,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result.value,
            "expected": self.expected,
            "observed": self.observed,
            "detail": self.detail,
        }


@dataclass
class CheckRunSnapshot:
    robot: str
    started_at: float
    finished_at: float | None
    overall: str
    records: list[MotorCheckRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "robot": self.robot,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "overall": self.overall,
            "records": [r.to_dict() for r in self.records],
        }

    @staticmethod
    def compute_overall(records: list[MotorCheckRecord]) -> str:
        # RUNNING/PENDING が含まれる間は確定しない
        if any(r.result in (MotorCheckResult.RUNNING, MotorCheckResult.PENDING) for r in records):
            return "running"

        # SKIPPED は判定対象から除外する (動作確認していないため)
        evaluated = [r for r in records if r.result is not MotorCheckResult.SKIPPED]
        if not evaluated:
            return "ok"

        passed = sum(1 for r in evaluated if r.result is MotorCheckResult.PASSED)
        failed = sum(
            1 for r in evaluated if r.result in (MotorCheckResult.FAILED, MotorCheckResult.TIMEOUT)
        )

        if passed == len(evaluated):
            return "ok"
        if failed == len(evaluated):
            return "failed"
        return "partial"
