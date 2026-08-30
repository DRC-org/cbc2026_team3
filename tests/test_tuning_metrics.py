"""ステップ応答の指標算出。

指標は「操縦者が次に何をするか」を決める材料なので、**測れなかったことを 0 で
埋めない**ことを特に固定する。0 と None が混ざると、行き過ぎが無かった応答と
窓の中で目標へ届かなかった応答が同じ表示になり、取るべき行動が正反対になる。
"""

from __future__ import annotations

import math

import pytest

from lib.tuning.metrics import Sample, analyze_step_response, settle_band_for, step_span

DT = 0.005


def _samples(
    positions: list[float],
    *,
    target: float,
    start_t: float = 0.0,
    output: float = 100.0,
    saturated: bool = False,
) -> list[Sample]:
    return [
        Sample(
            t=start_t + index * DT,
            target=target,
            position=pos,
            output=output,
            saturated=saturated,
        )
        for index, pos in enumerate(positions)
    ]


def _first_order(target: float, tau_s: float, count: int, *, start: float = 0.0) -> list[float]:
    """1 次遅れの立ち上がり。行き過ぎも振動もしない基準波形。"""
    return [
        start + (target - start) * (1.0 - math.exp(-(index * DT) / tau_s)) for index in range(count)
    ]


class TestStepSpan:
    def test_start_is_measured_position_end_is_target(self) -> None:
        samples = _samples([0.0, 1.0, 2.0], target=10.0)
        assert step_span(samples) == (0.0, 10.0)

    def test_returns_none_when_too_few_samples(self) -> None:
        assert step_span(_samples([0.0], target=10.0)) is None

    def test_pre_trigger_samples_do_not_become_the_origin(self) -> None:
        """負の t はステップ直前の記録。起点をここに取ると立ち上がりが実力より速く出る。"""
        pre = [Sample(t=-0.01, target=0.0, position=-5.0, output=0.0, saturated=False)]
        samples = pre + _samples([0.0, 5.0, 10.0], target=10.0)
        assert step_span(samples) == (0.0, 10.0)


class TestSettleBand:
    def test_band_is_a_ratio_of_step_size(self) -> None:
        assert settle_band_for(100.0, ratio=0.02, minimum=0.5) == pytest.approx(2.0)

    def test_band_never_goes_below_dead_band(self) -> None:
        """不感帯の内側では偏差が 0 として扱われ制御が働かない。

        そこより狭い帯で整定を判定すると、機構が正常でも永久に整定しない応答になる。
        """
        assert settle_band_for(1.0, ratio=0.02, minimum=1.0) == pytest.approx(1.0)

    def test_band_is_positive_for_negative_steps(self) -> None:
        assert settle_band_for(-100.0, ratio=0.02, minimum=0.0) == pytest.approx(2.0)


class TestAnalyzeBasics:
    def test_returns_none_when_target_did_not_move(self) -> None:
        """指標を捏造せず None を返す。0 で埋めると「完璧な応答」に見えてしまう。"""
        samples = _samples([10.0] * 20, target=10.0)
        assert analyze_step_response(samples, settle_band=0.2) is None

    def test_returns_none_with_fewer_than_two_samples(self) -> None:
        assert analyze_step_response(_samples([0.0], target=10.0), settle_band=0.2) is None

    def test_rise_time_is_ten_to_ninety_percent(self) -> None:
        positions = _first_order(10.0, tau_s=0.05, count=200)
        metrics = analyze_step_response(_samples(positions, target=10.0), settle_band=0.2)
        assert metrics is not None
        # 1 次遅れの 10→90% は tau * ln(9) ≒ 2.197 * tau
        assert metrics.rise_time_s == pytest.approx(0.05 * math.log(9.0), rel=0.05)

    def test_rise_time_is_none_when_ninety_percent_not_reached(self) -> None:
        """0 を返してはならない。「速すぎる」と「届いていない」が同じ数字になる。"""
        positions = _first_order(10.0, tau_s=5.0, count=100)
        metrics = analyze_step_response(_samples(positions, target=10.0), settle_band=0.2)
        assert metrics is not None
        assert metrics.rise_time_s is None

    def test_overshoot_is_reported_as_percent(self) -> None:
        positions = [0.0, 5.0, 10.0, 12.0, 11.0, 10.2, 10.0, 10.0]
        metrics = analyze_step_response(_samples(positions, target=10.0), settle_band=0.2)
        assert metrics is not None
        assert metrics.overshoot_pct == pytest.approx(20.0)
        assert metrics.peak_time_s == pytest.approx(3 * DT)

    def test_overshoot_is_zero_when_response_never_exceeds_target(self) -> None:
        positions = _first_order(10.0, tau_s=0.02, count=200)
        metrics = analyze_step_response(_samples(positions, target=10.0), settle_band=0.2)
        assert metrics is not None
        assert metrics.overshoot_pct == pytest.approx(0.0)

    def test_negative_step_yields_the_same_indicators(self) -> None:
        """符号ごとに分岐を書くと、片方の向きだけ壊れても半分のケースは通ってしまう。"""
        positions = [0.0, -5.0, -10.0, -12.0, -11.0, -10.2, -10.0, -10.0]
        metrics = analyze_step_response(_samples(positions, target=-10.0), settle_band=0.2)
        assert metrics is not None
        assert metrics.overshoot_pct == pytest.approx(20.0)
        assert metrics.step_size == pytest.approx(-10.0)


class TestSettlingTime:
    def test_returns_time_after_which_it_stays_inside_the_band(self) -> None:
        positions = [0.0, 10.0, 10.05, 11.0, 10.0, 10.0, 10.0]
        metrics = analyze_step_response(_samples(positions, target=10.0), settle_band=0.2)
        assert metrics is not None
        # index 3 で帯を出ているので、整定は index 4 以降
        assert metrics.settling_time_s == pytest.approx(4 * DT)

    def test_does_not_answer_with_the_first_band_crossing(self) -> None:
        """行き過ぎて戻る途中の横切りを整定と読むと、振動する機体ほど良い数字が出る。"""
        positions = [0.0, 10.0, 13.0, 7.0, 11.5, 9.0, 10.0, 10.0]
        metrics = analyze_step_response(_samples(positions, target=10.0), settle_band=0.2)
        assert metrics is not None
        assert metrics.settling_time_s == pytest.approx(6 * DT)

    def test_returns_none_when_still_outside_the_band_at_the_end(self) -> None:
        """窓長を返してはならない。「まだ分からない」と「窓ぴったりで整定」が混ざる。"""
        positions = [0.0, 5.0, 8.0, 8.5, 8.7, 8.8]
        metrics = analyze_step_response(_samples(positions, target=10.0), settle_band=0.2)
        assert metrics is not None
        assert metrics.settling_time_s is None


class TestSteadyStateError:
    def test_uses_the_average_over_the_tail(self) -> None:
        positions = [0.0] * 5 + [9.0] * 15
        metrics = analyze_step_response(_samples(positions, target=10.0), settle_band=0.05)
        assert metrics is not None
        assert metrics.steady_state_error == pytest.approx(1.0)

    def test_sign_is_target_minus_measured(self) -> None:
        """ki を入れる向きの判断に使うので、符号が反転しては使えない。"""
        positions = [0.0] * 5 + [11.0] * 15
        metrics = analyze_step_response(_samples(positions, target=10.0), settle_band=0.05)
        assert metrics is not None
        assert metrics.steady_state_error == pytest.approx(-1.0)


class TestOscillation:
    def _decaying(self, hz: float, count: int, zeta: float = 0.02) -> list[float]:
        omega = 2.0 * math.pi * hz
        return [
            10.0 - 10.0 * math.exp(-zeta * omega * index * DT) * math.cos(omega * index * DT)
            for index in range(count)
        ]

    def test_estimates_oscillation_frequency(self) -> None:
        positions = self._decaying(hz=5.0, count=400)
        metrics = analyze_step_response(_samples(positions, target=10.0), settle_band=0.2)
        assert metrics is not None
        assert metrics.oscillation_hz == pytest.approx(5.0, rel=0.1)

    def test_estimates_damping_ratio(self) -> None:
        positions = self._decaying(hz=5.0, count=400, zeta=0.1)
        metrics = analyze_step_response(_samples(positions, target=10.0), settle_band=0.2)
        assert metrics is not None
        assert metrics.damping_ratio == pytest.approx(0.1, abs=0.05)

    def test_ignores_ripple_inside_the_settle_band(self) -> None:
        """量子化ノイズを拾うと、静止している機体が高い周波数で振動していると出る。"""
        samples = [
            Sample(
                t=index * DT,
                target=10.0,
                position=10.0 + 0.01 * (-1) ** index,
                output=0.0,
                saturated=False,
            )
            for index in range(200)
        ]
        # 始点と終点が同じだとステップとして成立しないので、起点だけ離す
        samples[0] = Sample(t=0.0, target=10.0, position=0.0, output=0.0, saturated=False)
        metrics = analyze_step_response(samples, settle_band=0.2)
        assert metrics is not None
        assert metrics.oscillation_hz is None

    def test_returns_none_for_a_non_oscillating_response(self) -> None:
        positions = _first_order(10.0, tau_s=0.05, count=300)
        metrics = analyze_step_response(_samples(positions, target=10.0), settle_band=0.2)
        assert metrics is not None
        assert metrics.oscillation_hz is None
        assert metrics.damping_ratio is None


class TestSaturation:
    def test_reports_the_fraction_of_saturated_cycles(self) -> None:
        half = _samples([0.0, 2.0, 4.0, 6.0], target=10.0, saturated=True)
        rest = _samples([8.0, 9.0, 10.0, 10.0], target=10.0, start_t=4 * DT, saturated=False)
        metrics = analyze_step_response(half + rest, settle_band=0.2)
        assert metrics is not None
        assert metrics.saturation_ratio == pytest.approx(0.5)

    def test_reports_peak_output_magnitude(self) -> None:
        samples = [
            Sample(t=index * DT, target=10.0, position=float(index), output=out, saturated=False)
            for index, out in enumerate([100.0, -2000.0, 500.0])
        ]
        metrics = analyze_step_response(samples, settle_band=0.2)
        assert metrics is not None
        assert metrics.peak_output == pytest.approx(2000.0)
