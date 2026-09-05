from __future__ import annotations

from collections.abc import Iterable
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
    # 受信の口そのものが読めない状態。**`bus_off` とは原因が別**なので相乗りさせない ——
    # bus-off はコントローラがバスから切り離された状態、こちらはインタフェースが
    # down している (あるいは socket が読めない) 状態で、復旧の手当ても別になる。
    # 1 つにまとめると、どちらが起きているのか画面からもログからも区別できなくなる。
    rx_down: bool = False
    # `rx_down` は生の bool で、途絶が 1 秒で復帰すれば画面には 1 秒しか出ず、
    # 機体を見ている操縦者はまず見落とす (`cbc-can-watchdog.service` の bus-off
    # 復旧は journal に `[ WD ]` として残るだけで UI には何も出ない)。
    # 途絶の「立ち上がり」を数えたエピソード数をここに持たせ、試合中ずっと
    # 残す (リセットは `CANManager.reset_rx_down_episodes()` が持ち、呼び出しの
    # タイミングはサーバー側が決める)。**`rx_down` の判定そのもの (DOWN/DEGRADED
    # の出し方) はここでは動かさない** —— 回数はあくまで付随情報。
    rx_down_episodes: int = 0
    # このバスの途絶がワーク落下に繋がりうるか。電磁弁基板はコマンド
    # ウォッチドッグ (既定 500ms) が満了すると通電を落とす一手しかなく、
    # CAN が 1 秒弱止まればまず満了する。**バスに乗っているモータの
    # `control_type: on_off` の有無で決まる**ので、UI にバス名やドライバ種別を
    # 書き写させないよう判定はサーバー (`CANManager.health`) だけが持つ。
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


def worst_bus_health(states: Iterable[BusHealth]) -> BusHealth:
    """最悪値への集約。**機体の健全性判定はここ 1 箇所だけが行う。**

    ランク表を呼び出し側へ写すと、判定材料が 2 箇所に分かれて片方だけ直せる
    状態になり、「Monitor は READY と言うのに操縦者の画面は異常と言う」という
    最も信用を失う壊れ方になる。空なら OK (判定材料が無いことを異常には倒さない
    —— 判定不能の扱いは `HealthSnapshot.detail` が別に持つ)。
    """
    return max(states, key=_BUS_SEVERITY_RANK.__getitem__, default=BusHealth.OK)


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
        return worst_bus_health(
            [b.state for b in buses] + [_MOTOR_TO_BUS_SEVERITY[m.state] for m in motors]
        )
