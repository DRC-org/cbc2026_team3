from __future__ import annotations

import math

__all__ = ["TrapezoidalProfile"]


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# 連続時間の v^2/(2a) は「今の周期に進む分」を含まないため 1 周期ぶん取りこぼす。
# 実測 (dt=5ms / v=200mm/s / a=1200mm/s^2) で 1 周期 0.94mm、合計 16mm 以上行き過ぎた。
def _stoppable_velocity(distance: float, acceleration: float, dt: float) -> float:
    if distance <= 0.0:
        return 0.0

    quota = 2.0 * distance / (acceleration * dt * dt)
    steps = math.floor((math.sqrt(1.0 + 4.0 * quota) - 1.0) / 2.0)
    while steps > 0 and steps * (steps + 1) > quota:
        steps -= 1
    while (steps + 1) * (steps + 2) <= quota:
        steps += 1

    return (distance / dt + acceleration * dt * steps * (steps + 1) / 2.0) / (steps + 1)


class TrapezoidalProfile:
    def __init__(self, *, max_velocity: float, max_acceleration: float) -> None:
        if max_velocity <= 0.0:
            raise ValueError(f"max_velocity は正の値: {max_velocity}")
        if max_acceleration <= 0.0:
            raise ValueError(f"max_acceleration は正の値: {max_acceleration}")

        self._max_velocity = max_velocity
        self._max_acceleration = max_acceleration
        self._position = 0.0
        self._velocity = 0.0
        self._target = 0.0

    @property
    def position(self) -> float:
        return self._position

    @property
    def velocity(self) -> float:
        return self._velocity

    @property
    def done(self) -> bool:
        return self._position == self._target and self._velocity == 0.0

    def reset(self, position: float) -> None:
        self._position = position
        self._velocity = 0.0
        self._target = position

    def retarget(self, target: float) -> None:
        self._target = target

    def advance(self, dt: float) -> tuple[float, float]:
        if dt <= 0:
            return self._position, self._velocity

        remaining = self._target - self._position
        limit = _stoppable_velocity(abs(remaining), self._max_acceleration, dt)
        if remaining == 0.0:
            desired = 0.0
        else:
            desired = math.copysign(min(self._max_velocity, limit), remaining)

        step_limit = self._max_acceleration * dt
        velocity = _clamp(desired, self._velocity - step_limit, self._velocity + step_limit)

        step = velocity * dt
        if remaining == 0.0:
            pass
        elif step * remaining > 0.0 and abs(step) >= abs(remaining):
            self._position = self._target
        else:
            self._position += step
        self._velocity = velocity
        return self._position, self._velocity
