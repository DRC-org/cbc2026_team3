"""scripts/sync_probe.py の移動区間の切り出し。

チューニングで読む値は「**移動中の**左右ずれの最大」なので、区間の切り出しが
狂うとゲインの良し悪しを取り違える。静止中のサンプルまで含めれば平均が薄まり、
移動を取りこぼせばピークがそもそも出ない。CAN は要らない —— 区間の判定は
位置の系列に対する純粋なロジックとして切り出してある。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_SCRIPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "sync_probe.py"


def _load_module():
    """scripts/ はパッケージではないのでファイルパスから直接読み込む。

    tests/test_edulite_set_id_tool.py と同じ理由と同じ作法 (exec_module の前に
    sys.modules へ入れる。@dataclass が型注釈の解決でモジュールを引くため)。
    """
    spec = importlib.util.spec_from_file_location("sync_probe", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sync_probe = _load_module()

MoveTracker = sync_probe.MoveTracker


def _tracker(**kwargs) -> "sync_probe.MoveTracker":
    params = {"still_speed": 2.0, "still_s": 0.15, "min_travel": 0.5}
    params.update(kwargs)
    return MoveTracker(**params)


def _feed(tracker, samples: list[tuple[float, float, float | None]]) -> list:
    """(時刻, 位置, ずれ) の列を流し、閉じた区間の要約を集める。"""
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
    """等速移動のサンプル列を作る。``deviation`` は 0.0〜1.0 の進捗を受ける。"""
    steps = int(duration / dt)
    samples = []
    for i in range(steps + 1):
        progress = i / steps
        samples.append((t0 + i * dt, start + distance * progress, deviation(progress)))
    return samples


def _hold(
    *, value: float, duration: float = 0.3, dt: float = 0.001, t0: float = 0.0, deviation=0.0
) -> list[tuple[float, float, float | None]]:
    """静止しているサンプル列。"""
    steps = int(duration / dt)
    return [(t0 + i * dt, value, deviation) for i in range(steps + 1)]


class TestMoveDetection:
    def test_still_axis_produces_no_move(self) -> None:
        """静止しているだけの機体から移動を捏造しない。"""
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
        """連続した 2 回の移動が 1 区間に融合しない (ピークが混ざる)。"""
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
        """min_travel 未満の区間は報告しない。

        振動やノイズを移動として数え上げると、要約が流れて肝心の移動が読めなくなる。
        """
        tracker = _tracker(min_travel=0.5)
        samples = _move(distance=0.2, duration=0.05)
        samples += _hold(value=0.2, duration=0.3, t0=0.05)

        assert _feed(tracker, samples) == []

    def test_brief_pause_does_not_split_a_move(self) -> None:
        """still_s に満たない一瞬の停滞では区間を閉じない。

        加減速の途中で速さが閾値を下回るのは普通に起きる。そこで切ると 1 回の
        移動が複数の区間に割れ、どれが本当のピークか分からなくなる。
        """
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
        """移動中の最大ずれ。**これがチューニングで読む唯一の値。**"""
        tracker = _tracker()
        # 進捗 0.5 で 0.4 のピークを作る三角波
        samples = _move(
            distance=10.0,
            duration=0.5,
            deviation=lambda p: 0.4 * (1.0 - abs(p - 0.5) * 2.0),
        )
        samples += _hold(value=10.0, duration=0.3, t0=0.5)

        summary = _feed(tracker, samples)[0]

        assert abs(summary.peak_deviation - 0.4) < 0.01
        # ピークが出た位置も記録する (どのあたりで開くかが飽和の切り分けになる)
        assert abs(summary.peak_at_value - 5.0) < 0.5

    def test_peak_uses_magnitude_not_sign(self) -> None:
        """負のずれも同じ大きさとして扱う (どちらが先行したかは別の話)。"""
        tracker = _tracker()
        samples = _move(distance=10.0, duration=0.5, deviation=lambda p: -0.3)
        samples += _hold(value=10.0, duration=0.3, t0=0.5)

        summary = _feed(tracker, samples)[0]

        assert abs(summary.peak_deviation - 0.3) < 1e-6

    def test_missing_deviation_is_skipped(self) -> None:
        """比較対象が揃わなかったサンプルを 0 として平均に混ぜない。

        混ぜると、途絶していた区間ほど「ずれが小さい」という誤った要約になる。
        """
        tracker = _tracker()
        samples = _move(distance=10.0, duration=0.5, deviation=lambda p: None)
        samples += _hold(value=10.0, duration=0.3, t0=0.5, deviation=None)

        summary = _feed(tracker, samples)[0]

        assert summary.samples == 0
        assert summary.mean_abs_deviation == 0.0
        assert summary.peak_deviation == 0.0

    def test_mean_is_over_the_move_only(self) -> None:
        """静止したまま待っている時間の 0 で平均を薄めない。"""
        tracker = _tracker()
        samples = _hold(value=0.0, duration=1.0, t0=0.0, deviation=0.0)
        samples += _move(distance=10.0, duration=0.5, t0=1.0, deviation=lambda p: 0.2)
        samples += _hold(value=10.0, duration=0.3, t0=1.5, deviation=0.2)

        summary = _feed(tracker, samples)[0]

        # 静止 1 秒 (1000 サンプル) が混ざれば平均は 0.2 を大きく下回る
        assert abs(summary.mean_abs_deviation - 0.2) < 0.01
