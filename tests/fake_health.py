"""モック CANManager 向けの「全て正常」ヘルススナップショット生成。

``RobotServer._compute_health`` は健全性を判定できなかった場合に必ず DOWN へ倒す。
``MagicMock(spec=CANManager)`` の ``health()`` は HealthSnapshot を返さないため、
そのままではテストが常にその異常経路を通り、本番と別物の状態を検証してしまう。
モック側にまともなスナップショットを返させるためのヘルパをここに置く。
"""

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
    """モックに登録されたバス・モータ構成そのままの OK スナップショットを返す。

    構成を実体から引くことで、テスト側でモータを差し替えてもヘルスの中身と
    state メッセージの中身が食い違わない。
    """
    now = time.time()
    buses = list(getattr(can_manager, "_buses", {}) or {})
    motors = list(getattr(can_manager, "_motors", {}) or {})
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
                temperature=30.0,
                detail=None,
            )
            for name in motors
        ],
    )
