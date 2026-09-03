from __future__ import annotations

import math

import pytest

from lib.control.pid import PIDController

# M3508 (C620 ESC) の電流指令レンジ。PID 出力をそのまま電流指令に使う想定の検証に用いる
M3508_CURRENT_MIN = -16384.0
M3508_CURRENT_MAX = 16384.0


class TestProportional:
    def test_output_is_proportional_to_error(self) -> None:
        pid = PIDController(kp=2.0)
        assert pid.update(setpoint=10.0, measurement=4.0, dt=0.01) == pytest.approx(12.0)

    def test_negative_error_gives_negative_output(self) -> None:
        pid = PIDController(kp=2.0)
        assert pid.update(setpoint=0.0, measurement=5.0, dt=0.01) == pytest.approx(-10.0)

    def test_zero_error_gives_zero_output(self) -> None:
        pid = PIDController(kp=2.0)
        assert pid.update(setpoint=3.0, measurement=3.0, dt=0.01) == pytest.approx(0.0)


class TestIntegral:
    def test_integral_accumulates_error_over_time(self) -> None:
        pid = PIDController(kp=0.0, ki=10.0)
        first = pid.update(setpoint=1.0, measurement=0.0, dt=0.1)
        second = pid.update(setpoint=1.0, measurement=0.0, dt=0.1)
        assert first == pytest.approx(1.0)
        assert second == pytest.approx(2.0)

    def test_integral_respects_variable_dt(self) -> None:
        pid = PIDController(kp=0.0, ki=10.0)
        pid.update(setpoint=1.0, measurement=0.0, dt=0.1)
        # dt が倍になれば積分の伸びも倍
        assert pid.update(setpoint=1.0, measurement=0.0, dt=0.2) == pytest.approx(3.0)

    def test_integral_removes_steady_state_error(self) -> None:
        """P のみでは残る定常偏差が I 項で解消されることを一次遅れ系で確認する。"""

        def simulate(pid: PIDController) -> float:
            position = 0.0
            for _ in range(2000):
                # 出力に比例して動くが、常に一定の外乱 (重力相当) に引き戻される系
                output = pid.update(setpoint=1.0, measurement=position, dt=0.01)
                position += (output - 0.4) * 0.01
            return position

        assert abs(simulate(PIDController(kp=0.5)) - 1.0) > 0.1
        assert simulate(PIDController(kp=0.5, ki=2.0)) == pytest.approx(1.0, abs=0.01)

    def test_integral_not_accumulated_when_ki_is_zero(self) -> None:
        """ki=0 で積分を回すと、後からゲインを上げた瞬間に出力が飛ぶため蓄積しない。"""
        pid = PIDController(kp=1.0, ki=0.0)
        for _ in range(100):
            pid.update(setpoint=1.0, measurement=0.0, dt=0.01)
        assert pid.integral == pytest.approx(0.0)


class TestDerivative:
    def test_derivative_opposes_measurement_change(self) -> None:
        pid = PIDController(kp=0.0, kd=1.0)
        pid.update(setpoint=0.0, measurement=0.0, dt=0.1)
        # 測定値が +1.0/0.1s で動く → D 項は -kd * 10.0
        assert pid.update(setpoint=0.0, measurement=1.0, dt=0.1) == pytest.approx(-10.0)

    def test_no_derivative_on_first_update(self) -> None:
        pid = PIDController(kp=0.0, kd=1.0)
        assert pid.update(setpoint=0.0, measurement=5.0, dt=0.1) == pytest.approx(0.0)

    def test_derivative_damps_oscillation(self) -> None:
        def simulate(kd: float) -> float:
            pid = PIDController(kp=20.0, kd=kd)
            position = 0.0
            velocity = 0.0
            overshoot = 0.0
            for _ in range(500):
                output = pid.update(setpoint=1.0, measurement=position, dt=0.01)
                velocity += output * 0.01
                position += velocity * 0.01
                overshoot = max(overshoot, position - 1.0)
            return overshoot

        assert simulate(kd=2.0) < simulate(kd=0.0)


class TestDerivativeKick:
    def test_setpoint_step_does_not_spike_output(self) -> None:
        """偏差微分だと目標値ステップで D 項がスパイクする。測定値微分ならしない。"""
        pid = PIDController(kp=1.0, kd=100.0)
        pid.update(setpoint=0.0, measurement=0.0, dt=0.01)
        output = pid.update(setpoint=1.0, measurement=0.0, dt=0.01)
        # D 項が偏差微分なら 100 * (1.0 / 0.01) = 10000 が乗る
        assert output == pytest.approx(1.0)


class TestOutputClamp:
    def test_output_clamped_to_limits(self) -> None:
        pid = PIDController(kp=1000.0, output_min=M3508_CURRENT_MIN, output_max=M3508_CURRENT_MAX)
        assert pid.update(setpoint=100.0, measurement=0.0, dt=0.01) == pytest.approx(
            M3508_CURRENT_MAX
        )
        assert pid.update(setpoint=-100.0, measurement=0.0, dt=0.01) == pytest.approx(
            M3508_CURRENT_MIN
        )

    def test_asymmetric_limits(self) -> None:
        pid = PIDController(kp=1.0, output_min=-2.0, output_max=5.0)
        assert pid.update(setpoint=100.0, measurement=0.0, dt=0.01) == pytest.approx(5.0)
        assert pid.update(setpoint=-100.0, measurement=0.0, dt=0.01) == pytest.approx(-2.0)

    def test_default_limits_are_unbounded(self) -> None:
        pid = PIDController(kp=1.0)
        assert pid.update(setpoint=1e9, measurement=0.0, dt=0.01) == pytest.approx(1e9)

    def test_invalid_limits_raise(self) -> None:
        with pytest.raises(ValueError, match="output_min"):
            PIDController(kp=1.0, output_min=5.0, output_max=-5.0)


class TestAntiWindup:
    def test_integral_frozen_while_saturated(self) -> None:
        pid = PIDController(kp=1.0, ki=100.0, output_min=-10.0, output_max=10.0)
        for _ in range(100):
            pid.update(setpoint=100.0, measurement=0.0, dt=0.01)
        assert pid.integral == pytest.approx(0.0)

    def test_recovers_immediately_after_constraint_released(self) -> None:
        """機構端に当たって飽和し続けた後、偏差が反転したら即座に逆方向へ出力する。"""
        pid = PIDController(kp=1.0, ki=100.0, output_min=-10.0, output_max=10.0)
        for _ in range(200):
            pid.update(setpoint=100.0, measurement=0.0, dt=0.01)
        assert pid.update(setpoint=100.0, measurement=200.0, dt=0.01) == pytest.approx(-10.0)

    def test_integral_still_grows_while_unsaturated(self) -> None:
        pid = PIDController(kp=1.0, ki=1.0, output_min=-100.0, output_max=100.0)
        pid.update(setpoint=1.0, measurement=0.0, dt=0.1)
        assert pid.integral == pytest.approx(0.1)

    def test_integral_can_unwind_out_of_saturation(self) -> None:
        """飽和中でも、飽和を浅くする向きの積分は許可する。"""
        pid = PIDController(kp=0.0, ki=1.0, output_min=-10.0, output_max=10.0)
        for _ in range(200):
            pid.update(setpoint=100.0, measurement=0.0, dt=0.1)
        saturated_integral = pid.integral
        pid.update(setpoint=0.0, measurement=100.0, dt=0.1)
        assert pid.integral < saturated_integral

    def test_integral_limit_caps_integral_contribution(self) -> None:
        pid = PIDController(kp=0.0, ki=2.0, integral_limit=5.0)
        for _ in range(1000):
            output = pid.update(setpoint=10.0, measurement=0.0, dt=0.01)
        assert output == pytest.approx(5.0)
        assert pid.integral == pytest.approx(2.5)

    def test_negative_integral_limit_raises(self) -> None:
        with pytest.raises(ValueError, match="integral_limit"):
            PIDController(kp=1.0, integral_limit=-1.0)


class TestDeadBand:
    def test_no_proportional_output_inside_dead_band(self) -> None:
        pid = PIDController(kp=10.0, dead_band=0.5)
        assert pid.update(setpoint=1.0, measurement=0.6, dt=0.01) == pytest.approx(0.0)

    def test_output_resumes_outside_dead_band(self) -> None:
        pid = PIDController(kp=10.0, dead_band=0.5)
        assert pid.update(setpoint=1.0, measurement=0.0, dt=0.01) == pytest.approx(10.0)

    def test_integral_frozen_inside_dead_band(self) -> None:
        pid = PIDController(kp=0.0, ki=10.0, dead_band=0.5)
        pid.update(setpoint=1.0, measurement=0.0, dt=0.1)
        held = pid.integral
        for _ in range(10):
            pid.update(setpoint=1.0, measurement=0.7, dt=0.1)
        assert pid.integral == pytest.approx(held)

    def test_integral_term_still_holds_load_inside_dead_band(self) -> None:
        """昇降軸の保持電流を失わないよう、不感帯でも積分項の出力は残す。"""
        pid = PIDController(kp=0.0, ki=10.0, dead_band=0.5)
        held = pid.update(setpoint=1.0, measurement=0.0, dt=0.1)
        assert held == pytest.approx(1.0)
        assert pid.update(setpoint=1.0, measurement=0.7, dt=0.1) == pytest.approx(held)

    def test_negative_dead_band_raises(self) -> None:
        with pytest.raises(ValueError, match="dead_band"):
            PIDController(kp=1.0, dead_band=-0.1)


class TestNonPositiveDt:
    def test_zero_dt_returns_previous_output(self) -> None:
        pid = PIDController(kp=1.0, ki=10.0, kd=1.0)
        previous = pid.update(setpoint=1.0, measurement=0.0, dt=0.1)
        assert pid.update(setpoint=5.0, measurement=3.0, dt=0.0) == pytest.approx(previous)

    def test_zero_dt_does_not_change_state(self) -> None:
        pid = PIDController(kp=1.0, ki=10.0, kd=1.0)
        pid.update(setpoint=1.0, measurement=0.0, dt=0.1)
        integral = pid.integral
        pid.update(setpoint=5.0, measurement=3.0, dt=0.0)
        assert pid.integral == pytest.approx(integral)
        # 直前測定値も汚さない (汚れていれば D 項が -1 * (0-3)/0.1 = +30 跳ねる)
        assert pid.update(setpoint=1.0, measurement=0.0, dt=0.1) == pytest.approx(3.0)

    def test_negative_dt_returns_previous_output(self) -> None:
        pid = PIDController(kp=1.0)
        previous = pid.update(setpoint=1.0, measurement=0.0, dt=0.1)
        assert pid.update(setpoint=9.0, measurement=0.0, dt=-0.05) == pytest.approx(previous)

    def test_zero_dt_before_first_update_returns_zero(self) -> None:
        pid = PIDController(kp=1.0)
        assert pid.update(setpoint=1.0, measurement=0.0, dt=0.0) == pytest.approx(0.0)


class TestReset:
    def test_reset_clears_integral(self) -> None:
        pid = PIDController(kp=0.0, ki=10.0)
        for _ in range(10):
            pid.update(setpoint=1.0, measurement=0.0, dt=0.1)
        pid.reset()
        assert pid.integral == pytest.approx(0.0)
        assert pid.update(setpoint=1.0, measurement=0.0, dt=0.1) == pytest.approx(1.0)

    def test_reset_clears_derivative_history(self) -> None:
        pid = PIDController(kp=0.0, kd=1.0)
        pid.update(setpoint=0.0, measurement=0.0, dt=0.1)
        pid.reset()
        # 前回測定値が消えているので D 項は 0 (再開時のキックを防ぐ)
        assert pid.update(setpoint=0.0, measurement=100.0, dt=0.1) == pytest.approx(0.0)

    def test_reset_clears_last_output(self) -> None:
        pid = PIDController(kp=1.0)
        pid.update(setpoint=1.0, measurement=0.0, dt=0.1)
        pid.reset()
        assert pid.last_output == pytest.approx(0.0)
        assert pid.update(setpoint=1.0, measurement=0.0, dt=0.0) == pytest.approx(0.0)


class TestGainAccess:
    def test_gains_are_mutable_for_runtime_tuning(self) -> None:
        pid = PIDController(kp=1.0)
        pid.kp = 3.0
        assert pid.update(setpoint=1.0, measurement=0.0, dt=0.01) == pytest.approx(3.0)

    def test_last_output_tracks_latest_result(self) -> None:
        pid = PIDController(kp=1.0, output_min=-2.0, output_max=2.0)
        output = pid.update(setpoint=100.0, measurement=0.0, dt=0.01)
        assert pid.last_output == pytest.approx(output)
        assert pid.last_output == pytest.approx(2.0)


class TestDefaults:
    def test_defaults_are_p_only_and_unbounded(self) -> None:
        pid = PIDController(kp=1.0)
        assert pid.ki == pytest.approx(0.0)
        assert pid.kd == pytest.approx(0.0)
        assert pid.output_min == -math.inf
        assert pid.output_max == math.inf


class TestFeedforward:
    """偏差以外の根拠で加える操作量 (左右直結ペアの同期補正がこれを使う)。

    **呼び出し側で足して後からクランプする実装との違いを固定する。** 外で足すと
    クランプが二重になるだけでなく、下の conditional integration が補正を知らない
    まま積分を進める。「補正込みでは飽和していて機構が動けないのに、積分だけが
    育ち続ける」状態は、拘束が外れた瞬間の暴走として現れる。
    """

    def test_default_is_zero_and_changes_nothing(self) -> None:
        """既定では従来と 1 counts も変わらない (既存の全構成がそのまま動く)。"""
        plain = PIDController(kp=2.0)
        explicit = PIDController(kp=2.0)

        assert plain.update(setpoint=10.0, measurement=4.0, dt=0.01) == pytest.approx(
            explicit.update(setpoint=10.0, measurement=4.0, dt=0.01, feedforward=0.0)
        )

    def test_feedforward_is_added_to_output(self) -> None:
        pid = PIDController(kp=2.0)

        assert pid.update(
            setpoint=10.0, measurement=4.0, dt=0.01, feedforward=5.0
        ) == pytest.approx(17.0)

    def test_output_is_clamped_including_feedforward(self) -> None:
        """補正込みで出力レンジに収まる。外で足すと上限を超えた指令が出る。"""
        pid = PIDController(kp=2.0, output_min=-100.0, output_max=100.0)

        output = pid.update(setpoint=10.0, measurement=0.0, dt=0.01, feedforward=500.0)

        assert output == pytest.approx(100.0)

    def test_feedforward_survives_dead_band(self) -> None:
        """不感帯の中でも補正は残る。

        不感帯は「自分が目標に十分近い」ことを言うだけで、左右が揃っているかとは
        無関係である。ここで補正まで消すと、目標付近で静止した状態のずれを
        縮める手段が無くなる。
        """
        pid = PIDController(kp=2.0, dead_band=5.0)

        output = pid.update(setpoint=1.0, measurement=0.0, dt=0.01, feedforward=30.0)

        assert output == pytest.approx(30.0)

    def test_integral_does_not_grow_while_saturated_by_feedforward(self) -> None:
        """補正だけで飽和している間は積分を進めない (アンチワインドアップが補正込み)。

        補正を PID の外側で足す実装では、PID 自身は飽和していないと判断して積分を
        育て続ける。ここが落ちる実装は、機構が動けない間に溜めた積分を拘束が
        外れた瞬間に吐き出す。
        """
        pid = PIDController(kp=1.0, ki=1.0, output_min=-1000.0, output_max=1000.0)

        for _ in range(5):
            pid.update(setpoint=10.0, measurement=0.0, dt=0.01, feedforward=1000.0)

        assert pid.integral == pytest.approx(0.0)

    def test_integral_grows_without_saturation(self) -> None:
        """対照: 飽和していなければ同じ条件で積分は育つ (上のテストの前提を固定)。"""
        pid = PIDController(kp=1.0, ki=1.0, output_min=-1000.0, output_max=1000.0)

        for _ in range(5):
            pid.update(setpoint=10.0, measurement=0.0, dt=0.01, feedforward=0.0)

        assert pid.integral > 0.0
