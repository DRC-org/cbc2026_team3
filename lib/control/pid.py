from __future__ import annotations

import math

__all__ = ["PIDController"]


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class PIDController:
    def __init__(
        self,
        kp: float,
        ki: float = 0.0,
        kd: float = 0.0,
        *,
        output_min: float = -math.inf,
        output_max: float = math.inf,
        integral_limit: float | None = None,
        dead_band: float = 0.0,
    ) -> None:
        if output_min > output_max:
            raise ValueError(f"output_min は output_max 以下: {output_min} > {output_max}")
        if integral_limit is not None and integral_limit < 0:
            raise ValueError(f"integral_limit は 0 以上: {integral_limit}")
        if dead_band < 0:
            raise ValueError(f"dead_band は 0 以上: {dead_band}")

        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.output_min = output_min
        self.output_max = output_max
        self.integral_limit = integral_limit
        self.dead_band = dead_band

        self._integral = 0.0
        self._prev_measurement: float | None = None
        self._last_output = 0.0

    @property
    def integral(self) -> float:
        return self._integral

    @property
    def last_output(self) -> float:
        return self._last_output

    def reset(self) -> None:
        self._integral = 0.0
        self._prev_measurement = None
        self._last_output = 0.0

    def update(
        self,
        setpoint: float,
        measurement: float,
        dt: float,
        *,
        feedforward: float = 0.0,
    ) -> float:
        if dt <= 0:
            return self._last_output

        error = setpoint - measurement
        if abs(error) <= self.dead_band:
            error = 0.0

        proportional = self.kp * error

        derivative = 0.0
        if self._prev_measurement is not None:
            derivative = -self.kd * (measurement - self._prev_measurement) / dt

        candidate_integral = self._integral
        if self.ki != 0.0:
            candidate_integral += error * dt
            if self.integral_limit is not None:
                bound = self.integral_limit / abs(self.ki)
                candidate_integral = _clamp(candidate_integral, -bound, bound)

        unclamped = proportional + self.ki * candidate_integral + derivative + feedforward
        output = _clamp(unclamped, self.output_min, self.output_max)

        if unclamped != output and error * (unclamped - output) > 0:
            unclamped = proportional + self.ki * self._integral + derivative + feedforward
            output = _clamp(unclamped, self.output_min, self.output_max)
        else:
            self._integral = candidate_integral

        self._prev_measurement = measurement
        self._last_output = output
        return output
