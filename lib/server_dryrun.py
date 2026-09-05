"""dry-run (CAN バスなし起動) で UI を成立させるための擬似値。

`--dry-run` は python-can の virtual バスで起動するため、フィードバックが 1 通も
返らない。実機の配信経路をそのまま通すと全モータが STALE になり、机上で Web UI の
描画を確かめられない。ここは**見栄えのための値しか作らない** —— 実機モードからは
1 行も呼ばれず、判定ロジックもここには置かない。

**構成情報 (PID ゲイン・可動範囲) をここで作ってはならない。** dry-run でも実 config
から読める値なので、擬似値へ倒すと机上で確かめたい対象そのものが消える
(かつてゲインを dry-run 分岐の内側で組み立て、全モータが「調整不可」になった)。
"""

from __future__ import annotations

import math
import time

__all__ = ["motor_state", "patch_health", "sensor_state"]


def motor_state(robot_name: str, motor_name: str) -> dict:
    """UI に意味のある動きを見せるための擬似モータ状態。

    ロボット名・モータ名の文字列ハッシュをオフセットに使い、各モータが
    異なる位相で揺らぐようにしている。実値ではなく見栄え重視。
    """
    h = sum(ord(c) for c in robot_name + ":" + motor_name)
    t = time.time()
    return {
        "pos": math.sin(t * 0.6 + h * 0.3) * 1500.0,
        "vel": math.cos(t * 0.9 + h * 0.5) * 80.0,
        "torque": math.sin(t * 0.7 + h * 0.2) * 0.35,
        "temp": 30.0 + math.sin(t * 0.15 + h * 0.7) * 6.0,
    }


#: 擬似センサが接触したままでいる秒数。1 周期の半分ずつ接触・開放を繰り返す。
#: 机上で両方のチップを目視できる速さにしてあるだけで、実機の挙動とは無関係
_SENSOR_CYCLE_S = 4.0


def sensor_state(robot_name: str, sensor_name: str) -> dict:
    """UI に接触・開放の両方を見せるための擬似センサ状態。

    virtual バスは FEEDBACK を 1 通も返さないので、実機の経路をそのまま通すと
    全センサが途絶 (stale) で固まり、机上では接触チップの描画を一度も確かめられない。

    センサ名のハッシュを位相に使い、複数のセンサが同時に切り替わらないようにする。
    """
    h = sum(ord(c) for c in robot_name + ":" + sensor_name)
    phase = (time.time() + h) % _SENSOR_CYCLE_S
    return {"active": phase < _SENSOR_CYCLE_S / 2, "stale": False}


def patch_health(snapshot_dict: dict) -> dict:
    """ヘルススナップショットを「全体 OK」相当に整える。

    virtual バスはフィードバックを返さないため通常モータが STALE になるが、
    UI デモ目的で OK 表示にする。実機モードでは呼ばれない。
    """
    snapshot_dict["overall"] = "ok"
    for motor in snapshot_dict.get("motors", []):
        motor["state"] = "ok"
        motor["feedback_age_ms"] = 0
        motor["last_feedback_at"] = time.time()
        motor["detail"] = None
    for bus in snapshot_dict.get("buses", []):
        bus["state"] = "ok"
    return snapshot_dict
