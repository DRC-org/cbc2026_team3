from __future__ import annotations

import math
import time

__all__ = ["motor_state", "patch_health", "sensor_state"]


def motor_state(robot_name: str, motor_name: str) -> dict:
    h = sum(ord(c) for c in robot_name + ":" + motor_name)
    t = time.time()
    return {
        "pos": math.sin(t * 0.6 + h * 0.3) * 1500.0,
        "vel": math.cos(t * 0.9 + h * 0.5) * 80.0,
        "torque": math.sin(t * 0.7 + h * 0.2) * 0.35,
        "temp": 30.0 + math.sin(t * 0.15 + h * 0.7) * 6.0,
    }


_SENSOR_CYCLE_S = 4.0


def sensor_state(robot_name: str, sensor_name: str) -> dict:
    h = sum(ord(c) for c in robot_name + ":" + sensor_name)
    phase = (time.time() + h) % _SENSOR_CYCLE_S
    return {"active": phase < _SENSOR_CYCLE_S / 2, "stale": False}


def patch_health(snapshot_dict: dict) -> dict:
    snapshot_dict["overall"] = "ok"
    for motor in snapshot_dict.get("motors", []):
        motor["state"] = "ok"
        motor["feedback_age_ms"] = 0
        motor["last_feedback_at"] = time.time()
        motor["detail"] = None
    for bus in snapshot_dict.get("buses", []):
        bus["state"] = "ok"
    return snapshot_dict
