"""指標から助言を組み立てる規則。

助言は指標の言い換えであって新しい判定ではない。**順序に意味がある** — 飽和は
必ず先に出す。出力が上限に張り付いている間はゲインを変えても応答が変わらないため、
これを後回しにすると操縦者は「上げても下げても同じ」という観察から制御以外の
原因 (機構・output_limit・ステップ幅) へ辿り着けない。
"""

from __future__ import annotations

from lib.tuning.advice import AdviceSeverity, advise
from lib.tuning.metrics import StepMetrics


def _metrics(**overrides: object) -> StepMetrics:
    """「気になる点が無い」応答を既定にして、見たい 1 項目だけを崩す。

    全項目を毎回書くと、テストが落ちたときにどの値が効いたのか読めなくなる。
    """
    base: dict[str, object] = {
        "step_from": 0.0,
        "step_to": 10.0,
        "step_size": 10.0,
        "rise_time_s": 0.08,
        "overshoot_pct": 2.0,
        "peak_time_s": 0.1,
        "settling_time_s": 0.2,
        "steady_state_error": 0.0,
        "oscillation_hz": None,
        "damping_ratio": None,
        "saturation_ratio": 0.0,
        "peak_output": 500.0,
        "settle_band": 0.2,
        "sample_count": 600,
        "duration_s": 3.0,
    }
    base.update(overrides)
    return StepMetrics(**base)  # type: ignore[arg-type]


def _codes(advices: list) -> list[str]:
    return [a.code for a in advices]


class TestSaturation:
    def test_saturation_is_reported_as_a_warning(self) -> None:
        found = advise(_metrics(saturation_ratio=0.6), kp=2.0, ki=0.0, kd=0.0)
        assert "saturated" in _codes(found)
        assert found[0].severity is AdviceSeverity.WARNING

    def test_saturation_comes_before_other_advice(self) -> None:
        """飽和が INFO の下に埋もれると、読み飛ばして無意味なゲイン調整が続く。"""
        found = advise(
            _metrics(saturation_ratio=0.6, overshoot_pct=50.0, steady_state_error=5.0),
            kp=2.0,
            ki=0.0,
            kd=0.0,
        )
        assert _codes(found)[0] == "saturated"

    def test_slow_response_is_not_blamed_on_gain_while_saturated(self) -> None:
        """飽和中の「届かない」は kp 不足ではない。両方出すと相反する指示になる。"""
        found = advise(_metrics(saturation_ratio=0.9, rise_time_s=None), kp=2.0, ki=0.0, kd=0.0)
        assert "too_slow" not in _codes(found)


class TestOscillation:
    def test_undamped_oscillation_is_a_warning(self) -> None:
        found = advise(_metrics(oscillation_hz=6.0, damping_ratio=0.05), kp=8.0, ki=0.0, kd=0.0)
        assert "oscillating" in _codes(found)
        assert found[0].severity is AdviceSeverity.WARNING

    def test_suggests_adding_kd_when_none_is_set(self) -> None:
        found = advise(_metrics(oscillation_hz=6.0, damping_ratio=0.05), kp=8.0, ki=0.0, kd=0.0)
        message = next(a.message for a in found if a.code == "oscillating")
        assert "kd を少しずつ入れて" in message

    def test_suspects_noise_amplification_when_kd_is_already_set(self) -> None:
        """同じ「振動」でも kd を足すか減らすかで正反対になる。"""
        found = advise(_metrics(oscillation_hz=6.0, damping_ratio=0.05), kp=8.0, ki=0.0, kd=0.5)
        message = next(a.message for a in found if a.code == "oscillating")
        assert "ノイズを増幅" in message

    def test_well_damped_oscillation_is_not_warned(self) -> None:
        found = advise(_metrics(oscillation_hz=6.0, damping_ratio=0.6), kp=8.0, ki=0.0, kd=0.5)
        assert "oscillating" not in _codes(found)


class TestOvershoot:
    def test_large_overshoot_is_reported(self) -> None:
        found = advise(_metrics(overshoot_pct=35.0), kp=8.0, ki=0.0, kd=0.0)
        assert "overshoot" in _codes(found)

    def test_overshoot_is_not_duplicated_with_oscillation(self) -> None:
        """振動しているなら行き過ぎは症状の一部。2 件出すと同じ事実を 2 度読む。"""
        found = advise(
            _metrics(overshoot_pct=35.0, oscillation_hz=6.0, damping_ratio=0.05),
            kp=8.0,
            ki=0.0,
            kd=0.0,
        )
        assert "overshoot" not in _codes(found)


class TestSteadyStateError:
    def test_suggests_ki_with_integral_limit_when_ki_is_zero(self) -> None:
        found = advise(_metrics(steady_state_error=1.5), kp=2.0, ki=0.0, kd=0.0)
        message = next(a.message for a in found if a.code == "steady_state_error")
        assert "integral_limit" in message

    def test_points_at_integral_limit_when_ki_is_already_set(self) -> None:
        found = advise(_metrics(steady_state_error=1.5), kp=2.0, ki=0.4, kd=0.0)
        message = next(a.message for a in found if a.code == "steady_state_error")
        assert "integral_limit で頭打ち" in message

    def test_error_inside_the_settle_band_is_not_reported(self) -> None:
        """帯の内側の残差は機構にとって無害。出すと毎回 ki を足す助言になる。"""
        found = advise(_metrics(steady_state_error=0.1, settle_band=0.2), kp=2.0, ki=0.0, kd=0.0)
        assert "steady_state_error" not in _codes(found)


class TestSpeed:
    def test_reports_when_target_is_never_reached(self) -> None:
        found = advise(_metrics(rise_time_s=None, settling_time_s=None), kp=0.2, ki=0.0, kd=0.0)
        assert "too_slow" in _codes(found)

    def test_offers_headroom_when_the_response_is_calm(self) -> None:
        found = advise(_metrics(), kp=2.0, ki=0.0, kd=0.0)
        assert "headroom" in _codes(found)

    def test_no_headroom_advice_while_oscillating(self) -> None:
        found = advise(_metrics(oscillation_hz=6.0, damping_ratio=0.05), kp=8.0, ki=0.0, kd=0.0)
        assert "headroom" not in _codes(found)


class TestNotSettled:
    def test_missing_settling_time_is_a_warning(self) -> None:
        found = advise(_metrics(settling_time_s=None), kp=2.0, ki=0.0, kd=0.0)
        assert "not_settled" in _codes(found)
        assert found[0].severity is AdviceSeverity.WARNING


class TestFallback:
    def test_always_returns_at_least_one_item(self) -> None:
        """空リストは「助言が出せなかった」のか「問題が無い」のか区別できない。"""
        found = advise(
            _metrics(rise_time_s=0.08, overshoot_pct=1.0, settling_time_s=0.15),
            kp=2.0,
            ki=0.0,
            kd=0.0,
        )
        assert found
