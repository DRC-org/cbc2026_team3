from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

__all__ = [
    "Sample",
    "StepMetrics",
    "analyze_step_response",
    "settle_band_for",
    "step_span",
]


@dataclass(frozen=True)
class Sample:
    t: float
    target: float
    position: float
    output: float
    saturated: bool


@dataclass(frozen=True)
class StepMetrics:
    step_from: float
    step_to: float
    step_size: float
    rise_time_s: float | None
    overshoot_pct: float
    peak_time_s: float | None
    settling_time_s: float | None
    steady_state_error: float
    oscillation_hz: float | None
    damping_ratio: float | None
    saturation_ratio: float
    peak_output: float
    settle_band: float
    sample_count: int
    duration_s: float


def step_span(
    samples: list[Sample] | tuple[Sample, ...],
) -> tuple[float, float] | None:
    post = [s for s in samples if s.t >= 0.0]
    if len(post) < 2:
        return None
    return post[0].position, post[-1].target


def settle_band_for(step_size: float, *, ratio: float, minimum: float) -> float:
    return max(minimum, abs(step_size) * ratio)


def analyze_step_response(
    samples: list[Sample] | tuple[Sample, ...],
    *,
    settle_band: float,
) -> StepMetrics | None:
    post = [s for s in samples if s.t >= 0.0]
    if len(post) < 2:
        return None

    step_from = post[0].position
    step_to = post[-1].target
    step_size = step_to - step_from
    if abs(step_size) < 1e-9:
        return None

    duration = post[-1].t - post[0].t

    progress = [(s.t, (s.position - step_from) / step_size) for s in post]

    rise = _rise_time(progress)
    peak_time, peak_value = _peak(progress)
    overshoot = max(0.0, (peak_value - 1.0)) * 100.0

    errors = [(s.t, step_to - s.position) for s in post]
    settling = _settling_time(errors, settle_band)
    steady = _steady_state_error(errors)
    hz, damping = _oscillation(errors, settle_band)

    saturated = sum(1 for s in post if s.saturated)
    peak_output = max(abs(s.output) for s in post)

    return StepMetrics(
        step_from=step_from,
        step_to=step_to,
        step_size=step_size,
        rise_time_s=rise,
        overshoot_pct=overshoot,
        peak_time_s=peak_time,
        settling_time_s=settling,
        steady_state_error=steady,
        oscillation_hz=hz,
        damping_ratio=damping,
        saturation_ratio=saturated / len(post),
        peak_output=peak_output,
        settle_band=settle_band,
        sample_count=len(post),
        duration_s=duration,
    )


def _crossing(progress: list[tuple[float, float]], level: float) -> float | None:
    prev_t, prev_y = progress[0]
    if prev_y >= level:
        return prev_t
    for t, y in progress[1:]:
        if y >= level:
            span = y - prev_y
            if span <= 0:
                return t
            return prev_t + (t - prev_t) * (level - prev_y) / span
        prev_t, prev_y = t, y
    return None


def _rise_time(progress: list[tuple[float, float]]) -> float | None:
    lo = _crossing(progress, 0.1)
    hi = _crossing(progress, 0.9)
    if lo is None or hi is None:
        return None
    return max(0.0, hi - lo)


def _peak(progress: list[tuple[float, float]]) -> tuple[float | None, float]:
    peak_t, peak_y = progress[0]
    for t, y in progress:
        if y > peak_y:
            peak_t, peak_y = t, y
    return peak_t, peak_y


def _settling_time(errors: list[tuple[float, float]], band: float) -> float | None:
    last_violation = None
    for index, (_, err) in enumerate(errors):
        if abs(err) > band:
            last_violation = index
    if last_violation is None:
        return errors[0][0]
    if last_violation + 1 >= len(errors):
        return None
    return errors[last_violation + 1][0]


def _steady_state_error(errors: list[tuple[float, float]]) -> float:
    tail_start = max(0, len(errors) - max(1, len(errors) // 5))
    tail = errors[tail_start:]
    return sum(err for _, err in tail) / len(tail)


def _oscillation(
    errors: list[tuple[float, float]], band: float
) -> tuple[float | None, float | None]:
    extrema = _extrema(errors, band)
    if len(extrema) < 2:
        return None, None

    intervals = [b[0] - a[0] for a, b in itertools.pairwise(extrema)]
    half_period = sum(intervals) / len(intervals)
    hz = 1.0 / (2.0 * half_period) if half_period > 0 else None

    damping = None
    ratios = [
        abs(a[1]) / abs(b[1])
        for a, b in itertools.pairwise(extrema)
        if abs(b[1]) > 1e-12 and abs(a[1]) > abs(b[1])
    ]
    if ratios:
        delta = 2.0 * (sum(math.log(r) for r in ratios) / len(ratios))
        damping = delta / math.sqrt(4.0 * math.pi**2 + delta**2)
    return hz, damping


def _extrema(errors: list[tuple[float, float]], band: float) -> list[tuple[float, float]]:
    found: list[tuple[float, float]] = []
    for index in range(1, len(errors) - 1):
        t, err = errors[index]
        if abs(err) <= band:
            continue
        prev_err = errors[index - 1][1]
        next_err = errors[index + 1][1]
        is_max = err >= prev_err and err > next_err
        is_min = err <= prev_err and err < next_err
        if is_max or is_min:
            found.append((t, err))
    return found
