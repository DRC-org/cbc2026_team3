from __future__ import annotations

import importlib.util
import pathlib
import sys

_SCRIPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "sync_probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("sync_probe", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # @dataclass は型注釈の解決でモジュールを引くので、exec_module の前に登録する。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sync_probe = _load_module()

MoveTracker = sync_probe.MoveTracker


def _tracker(**kwargs):
    params = {"still_speed": 2.0, "still_s": 0.15, "min_travel": 0.5}
    params.update(kwargs)
    return MoveTracker(**params)


def _feed(tracker, samples: list[tuple[float, float, float | None]]) -> list:
    return [
        summary
        for now, value, deviation in samples
        if (summary := tracker.observe(now, value, deviation)) is not None
    ]


def _move(
    *,
    start: float = 0.0,
    distance: float = 10.0,
    duration: float = 0.5,
    dt: float = 0.001,
    t0: float = 0.0,
    deviation=lambda progress: 0.0,
) -> list[tuple[float, float, float | None]]:
    steps = int(duration / dt)
    samples = []
    for i in range(steps + 1):
        progress = i / steps
        samples.append((t0 + i * dt, start + distance * progress, deviation(progress)))
    return samples


def _hold(
    *, value: float, duration: float = 0.3, dt: float = 0.001, t0: float = 0.0, deviation=0.0
) -> list[tuple[float, float, float | None]]:
    steps = int(duration / dt)
    return [(t0 + i * dt, value, deviation) for i in range(steps + 1)]


class TestMoveDetection:
    def test_still_axis_produces_no_move(self) -> None:
        tracker = _tracker()

        assert _feed(tracker, _hold(value=3.0, duration=1.0)) == []

    def test_move_then_stop_is_reported_once(self) -> None:
        tracker = _tracker()
        samples = _move(distance=10.0, duration=0.5)
        samples += _hold(value=10.0, duration=0.3, t0=0.5)

        summaries = _feed(tracker, samples)

        assert len(summaries) == 1
        assert summaries[0].index == 1
        assert summaries[0].start_value == 0.0
        assert abs(summaries[0].end_value - 10.0) < 1e-6
        assert abs(summaries[0].travel - 10.0) < 1e-6

    def test_two_moves_are_separate_intervals(self) -> None:
        tracker = _tracker()
        samples = _move(distance=10.0, duration=0.4, t0=0.0)
        samples += _hold(value=10.0, duration=0.3, t0=0.4)
        samples += _move(start=10.0, distance=-10.0, duration=0.4, t0=0.7)
        samples += _hold(value=0.0, duration=0.3, t0=1.1)

        summaries = _feed(tracker, samples)

        assert [s.index for s in summaries] == [1, 2]
        assert summaries[0].travel > 0
        assert summaries[1].travel < 0

    def test_tiny_move_is_not_reported(self) -> None:
        tracker = _tracker(min_travel=0.5)
        samples = _move(distance=0.2, duration=0.05)
        samples += _hold(value=0.2, duration=0.3, t0=0.05)

        assert _feed(tracker, samples) == []

    def test_brief_pause_does_not_split_a_move(self) -> None:
        tracker = _tracker(still_s=0.15)
        samples = _move(distance=5.0, duration=0.2, t0=0.0)
        samples += _hold(value=5.0, duration=0.05, t0=0.2)
        samples += _move(start=5.0, distance=5.0, duration=0.2, t0=0.25)
        samples += _hold(value=10.0, duration=0.3, t0=0.45)

        summaries = _feed(tracker, samples)

        assert len(summaries) == 1
        assert abs(summaries[0].travel - 10.0) < 1e-6


class TestDeviationStatistics:
    def test_peak_is_the_maximum_during_the_move(self) -> None:
        tracker = _tracker()
        samples = _move(
            distance=10.0,
            duration=0.5,
            deviation=lambda p: 0.4 * (1.0 - abs(p - 0.5) * 2.0),
        )
        samples += _hold(value=10.0, duration=0.3, t0=0.5)

        summary = _feed(tracker, samples)[0]

        assert abs(summary.peak_deviation - 0.4) < 0.01
        assert abs(summary.peak_at_value - 5.0) < 0.5

    def test_peak_uses_magnitude_not_sign(self) -> None:
        tracker = _tracker()
        samples = _move(distance=10.0, duration=0.5, deviation=lambda p: -0.3)
        samples += _hold(value=10.0, duration=0.3, t0=0.5)

        summary = _feed(tracker, samples)[0]

        assert abs(summary.peak_deviation - 0.3) < 1e-6

    def test_missing_deviation_is_skipped(self) -> None:
        tracker = _tracker()
        samples = _move(distance=10.0, duration=0.5, deviation=lambda p: None)
        samples += _hold(value=10.0, duration=0.3, t0=0.5, deviation=None)

        summary = _feed(tracker, samples)[0]

        assert summary.samples == 0
        assert summary.mean_abs_deviation == 0.0
        assert summary.peak_deviation == 0.0

    def test_mean_is_over_the_move_only(self) -> None:
        tracker = _tracker()
        samples = _hold(value=0.0, duration=1.0, t0=0.0, deviation=0.0)
        samples += _move(distance=10.0, duration=0.5, t0=1.0, deviation=lambda p: 0.2)
        samples += _hold(value=10.0, duration=0.3, t0=1.5, deviation=0.2)

        summary = _feed(tracker, samples)[0]

        assert abs(summary.mean_abs_deviation - 0.2) < 0.01
