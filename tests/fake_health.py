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
    state メッセージの中身が食い違わない。参照するのは ``motors`` /
    ``bus_names`` という公開 API だけにする。ここが private (``_motors``) を
    見ていた頃は、モックを組む側にも「``motors`` と ``_motors`` を同じ dict へ
    揃える」という本番に存在しない儀式が必要だった。
    """
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
                # **測れない基板は None。** 本物の `CANManager.health` が
                # `telemetry` を見て温度を落とすので、ここが 30.0 固定だと
                # 「モックだけ温度が付いている」状態になり、null が届く形を
                # 誰も検証しないまま通る
                temperature=30.0 if _measures_temperature(driver) else None,
                detail=None,
            )
            for name, driver in motors.items()
        ],
    )


def _measures_temperature(driver: Any) -> bool:
    """ドライバが温度を測れると宣言しているか。

    宣言を持たないモック (``telemetry`` を生やしていないもの) は従来どおり
    温度ありとして扱う。ここで False へ倒すと、CAN 層に関心の無いテストが
    まとめて「温度不明」の経路を通ることになる。
    """
    telemetry = getattr(driver, "telemetry", None)
    return telemetry is None or bool(telemetry.temperature)
