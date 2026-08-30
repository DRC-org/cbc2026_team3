"""波形・指標・助言を 1 通へまとめる層。

**3 つを 1 通で運ぶ**ことと、**表示のための間引きが指標と食い違わない**ことを固定する。
間引きで行き過ぎのピークが落ちると、指標は「行き過ぎ 30%」と言うのにグラフは
行き過ぎていない、という食い違いが画面上で起きる。
"""

from __future__ import annotations

import pytest

from lib.tuning.metrics import Sample
from lib.tuning.recorder import Capture, PidSnapshot
from lib.tuning.report import summarize

DT = 0.005


def _capture(
    positions: list[float],
    *,
    target: float = 10.0,
    dead_band: float = 0.05,
    kp: float = 2.0,
    ki: float = 0.0,
    kd: float = 0.0,
    output: float = 100.0,
    saturated: bool = False,
) -> Capture:
    return Capture(
        motor="y_axis_r",
        captured_at=1234.5,
        samples=tuple(
            Sample(
                t=index * DT,
                target=target,
                position=pos,
                output=output,
                saturated=saturated,
            )
            for index, pos in enumerate(positions)
        ),
        gains=PidSnapshot(kp=kp, ki=ki, kd=kd, dead_band=dead_band),
    )


class TestSummarize:
    def test_produces_metrics_and_advice_together(self) -> None:
        report = summarize("main_hand", _capture([0.0, 5.0, 9.5, 10.0, 10.0, 10.0]))
        assert report.metrics is not None
        assert report.advice

    def test_settle_band_floors_at_the_dead_band(self) -> None:
        """不感帯より狭い帯で判定すると、正常な機構が永久に整定しなくなる。"""
        report = summarize("main_hand", _capture([0.0] + [10.0] * 5, dead_band=1.0))
        assert report.metrics is not None
        assert report.metrics.settle_band == pytest.approx(1.0)

    def test_settle_band_scales_with_step_size(self) -> None:
        positions = [0.0] + [100.0] * 5
        report = summarize("main_hand", _capture(positions, target=100.0, dead_band=0.05))
        assert report.metrics is not None
        assert report.metrics.settle_band == pytest.approx(2.0)

    def test_non_step_capture_yields_no_metrics_and_no_advice(self) -> None:
        """助言だけが残ると、根拠の無い指示が画面に出る。"""
        report = summarize("main_hand", _capture([10.0] * 6))
        assert report.metrics is None
        assert report.advice == []


class TestPayload:
    def test_carries_waveform_metrics_and_advice_in_one_message(self) -> None:
        payload = summarize("main_hand", _capture([0.0, 5.0, 9.5, 10.0, 10.0])).to_payload()
        assert payload["type"] == "tuning_capture"
        assert payload["robot"] == "main_hand"
        assert payload["motor"] == "y_axis_r"
        assert payload["metrics"] is not None
        assert payload["advice"]
        assert set(payload["samples"]) == {"t", "target", "pos", "output", "sat"}

    def test_gains_are_not_rounded(self) -> None:
        """表示された値と機体の実際のゲインが食い違うと、送った値を確認できない。"""
        capture = _capture([0.0, 5.0, 10.0, 10.0], kp=1.2345678, kd=0.000125)
        payload = summarize("main_hand", capture).to_payload()
        assert payload["gains"] == {"kp": 1.2345678, "ki": 0.0, "kd": 0.000125}

    def test_waveform_columns_have_equal_length(self) -> None:
        payload = summarize("main_hand", _capture([0.0, 5.0, 10.0, 10.0])).to_payload()
        samples = payload["samples"]
        assert len({len(column) for column in samples.values()}) == 1

    def test_metrics_is_null_when_not_a_step(self) -> None:
        payload = summarize("main_hand", _capture([10.0] * 6)).to_payload()
        assert payload["metrics"] is None
        assert payload["advice"] == []


class TestDecimation:
    def test_reduces_the_point_count(self) -> None:
        positions = [float(index) / 10.0 for index in range(600)]
        payload = summarize("main_hand", _capture(positions, target=60.0)).to_payload(
            max_points=100
        )
        assert len(payload["samples"]["t"]) <= 100

    def test_keeps_the_overshoot_peak(self) -> None:
        """等間隔に拾うだけだと、ピークが落ちた区間にあると波形から行き過ぎが消える。"""
        positions = [0.0] * 50 + [10.0] * 100
        positions[75] = 18.0
        payload = summarize("main_hand", _capture(positions)).to_payload(max_points=20)
        assert max(payload["samples"]["pos"]) == pytest.approx(18.0)

    def test_keeps_both_ends(self) -> None:
        """始点と終点が欠けると、立ち上がりの起点と定常値が読めなくなる。"""
        positions = [0.0] + [float(index) for index in range(1, 199)] + [200.0]
        payload = summarize("main_hand", _capture(positions, target=200.0)).to_payload(
            max_points=20
        )
        assert payload["samples"]["pos"][0] == pytest.approx(0.0)
        assert payload["samples"]["pos"][-1] == pytest.approx(200.0)

    def test_short_captures_are_untouched(self) -> None:
        payload = summarize("main_hand", _capture([0.0, 5.0, 10.0, 10.0])).to_payload(
            max_points=300
        )
        assert len(payload["samples"]["t"]) == 4

    def test_analysis_uses_every_sample_not_the_decimated_view(self) -> None:
        """間引きは表示だけの都合。指標まで間引いた点で計算すると精度が落ちる。"""
        positions = [0.0] * 50 + [10.0] * 100
        positions[75] = 18.0
        report = summarize("main_hand", _capture(positions))
        assert report.metrics is not None
        assert report.metrics.sample_count == 150
