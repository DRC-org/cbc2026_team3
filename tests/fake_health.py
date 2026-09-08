from __future__ import annotations

import time
from typing import Any

from lib.health import (
    BusHealth,
    BusHealthInfo,
    HealthSnapshot,
    MotorHealth,
    MotorHealthInfo,
)


def ok_health_snapshot(can_manager: Any) -> HealthSnapshot:
    now = time.time()
    buses = list(getattr(can_manager, "bus_names", ()) or ())
    motors = dict(getattr(can_manager, "motors", {}) or {})
    bus_name = buses[0] if buses else "bus0"

    return HealthSnapshot(
        timestamp=now,
        overall=BusHealth.OK,
        buses=[
            BusHealthInfo(
                name=name,
                channel=name,
                state=BusHealth.OK,
                last_tx_at=now,
                last_rx_at=now,
                tx_error_count=0,
                rx_error_count=0,
                bus_off=False,
            )
            for name in buses
        ],
        motors=[
            MotorHealthInfo(
                name=name,
                bus=bus_name,
                state=MotorHealth.OK,
                last_feedback_at=now,
                feedback_age_ms=0.0,
                temperature=30.0 if _measures_temperature(driver) else None,
                detail=None,
            )
            for name, driver in motors.items()
        ],
    )


def _measures_temperature(driver: Any) -> bool:
    telemetry = getattr(driver, "telemetry", None)
    return telemetry is None or bool(telemetry.temperature)
