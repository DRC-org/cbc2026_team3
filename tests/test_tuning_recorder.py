"""ステップ応答の窓切り出し。

この記録器は 200Hz の制御ループの中で回るので、**1 周期あたりの仕事が定数時間**
であることと、**意味の変わった記録を残さない**ことの 2 つを固定する。後者を外すと、
途中で電流 0 に落とされた波形が「行き過ぎもせず整定もしない応答」として残り、
操縦者はゲインが悪いのだと読む。
"""

from __future__ import annotations

import pytest

from lib.tuning.recorder import Capture, MotorStepRecorder, PidSnapshot

DT = 0.005
GAINS = PidSnapshot(kp=2.0, ki=0.0, kd=0.0, dead_band=1.0)


class _Harness:
    """周期を刻みながら記録させる。時刻管理をテスト本体から追い出す。"""

    def __init__(self, **overrides: object) -> None:
        self.gains = GAINS
        self.wall = 1000.0
        params: dict[str, object] = {
            "window_s": 0.05,
            "pre_trigger_s": 0.01,
            "min_step": 0.5,
            "max_samples": 10_000,
        }
        params.update(overrides)
        self.recorder = MotorStepRecorder(
            "y_axis_r",
            gains_snapshot=lambda: self.gains,
            wall_clock=lambda: self.wall,
            **params,  # type: ignore[arg-type]
        )
        self.t = 0.0

    def tick(
        self,
        *,
        target: float | None,
        position: float,
        output: float = 100.0,
        saturated: bool = False,
    ) -> Capture | None:
        captured = self.recorder.record(
            self.t,
            target=target,
            position=position,
            output=output,
            saturated=saturated,
        )
        self.t += DT
        return captured

    def run(self, count: int, *, target: float | None, position: float = 0.0) -> Capture | None:
        """同じ指令で count 周期回し、途中で閉じた窓を返す。"""
        result = None
        for _ in range(count):
            captured = self.tick(target=target, position=position)
            if captured is not None:
                result = captured
        return result


class TestTrigger:
    def test_target_change_starts_a_capture(self) -> None:
        h = _Harness()
        h.tick(target=0.0, position=0.0)
        assert h.recorder.capturing

    def test_small_target_change_does_not_trigger(self) -> None:
        """ジョグ 1 目盛より細かい揺れで窓を開くと、記録が常に埋まって使えない。"""
        h = _Harness(min_step=0.5)
        h.tick(target=0.0, position=0.0)
        h.run(20, target=0.0)
        assert not h.recorder.capturing
        h.tick(target=0.2, position=0.0)
        assert not h.recorder.capturing

    def test_first_target_after_idle_counts_as_a_step(self) -> None:
        """停止から動き出す 1 回目こそ見たい応答なので、取りこぼしてはならない。"""
        h = _Harness()
        h.tick(target=None, position=0.0)
        h.tick(target=5.0, position=0.0)
        assert h.recorder.capturing


class TestWindow:
    def test_closes_after_the_window_elapses(self) -> None:
        h = _Harness(window_s=0.05)
        capture = h.run(20, target=10.0)
        assert capture is not None
        assert capture.motor == "y_axis_r"

    def test_time_is_relative_to_the_step(self) -> None:
        """複数の応答を同じ横軸で重ねるのが調整作業そのもの。"""
        h = _Harness(window_s=0.05)
        capture = h.run(20, target=10.0)
        assert capture is not None
        assert capture.samples[0].t == pytest.approx(0.0)
        assert capture.samples[-1].t == pytest.approx(0.05, abs=DT)

    def test_pre_trigger_samples_have_negative_time(self) -> None:
        """ステップ直前に静止していたか既に動いていたかで応答の読み方が変わる。"""
        h = _Harness(window_s=0.05, pre_trigger_s=0.01)
        h.tick(target=None, position=0.0)
        h.tick(target=None, position=0.0)
        capture = h.run(20, target=10.0)
        assert capture is not None
        assert capture.samples[0].t < 0.0

    def test_older_than_pre_trigger_is_dropped(self) -> None:
        h = _Harness(window_s=0.05, pre_trigger_s=0.01)
        for _ in range(50):
            h.tick(target=None, position=0.0)
        capture = h.run(20, target=10.0)
        assert capture is not None
        # 0.01s = 2 周期ぶんより古い記録は残さない (境界の 1 点は許容)
        assert capture.samples[0].t >= -0.0155

    def test_stops_at_max_samples(self) -> None:
        """時刻が進まない異常時にメモリを食い潰さないための歯止め。"""
        h = _Harness(window_s=1000.0, max_samples=5)
        capture = h.run(10, target=10.0)
        assert capture is not None
        assert len(capture.samples) <= 5


class TestRestart:
    def test_a_new_step_discards_the_running_window(self) -> None:
        """連打されたジョグを 1 本に綴じると、階段状の動きが 1 回の大きな
        行き過ぎとして解析される。"""
        h = _Harness(window_s=0.05)
        h.run(4, target=10.0)
        h.tick(target=20.0, position=5.0)
        capture = h.run(20, target=20.0, position=5.0)
        assert capture is not None
        assert all(s.target == 20.0 for s in capture.samples if s.t >= 0.0)


class TestAbort:
    def test_abort_discards_the_window(self) -> None:
        h = _Harness(window_s=0.05)
        h.run(4, target=10.0)
        h.recorder.abort()
        assert not h.recorder.capturing

    def test_no_capture_is_emitted_after_abort(self) -> None:
        """途中で電流 0 に落とされた波形を残すと、ゲインのせいだと読まれる。"""
        h = _Harness(window_s=0.05)
        h.run(4, target=10.0)
        h.recorder.abort()
        assert h.run(4, target=10.0) is None

    def test_target_resumes_as_a_fresh_step_after_abort(self) -> None:
        """異常のあいだに変わった目標へ動く様子が 1 度も記録されなくなるのを防ぐ。"""
        h = _Harness(window_s=0.05)
        h.run(4, target=10.0)
        h.recorder.abort()
        h.tick(target=10.0, position=0.0)
        assert h.recorder.capturing

    def test_losing_the_target_aborts(self) -> None:
        h = _Harness(window_s=0.05)
        h.run(4, target=10.0)
        h.tick(target=None, position=3.0)
        assert not h.recorder.capturing


class TestGains:
    def test_gains_are_snapshotted_at_the_step(self) -> None:
        """波形とゲインの対応が崩れると、届いた記録が新旧どちらのものか分からない。"""
        h = _Harness(window_s=0.05)
        h.tick(target=10.0, position=0.0)
        h.gains = PidSnapshot(kp=99.0, ki=0.0, kd=0.0, dead_band=1.0)
        capture = h.run(20, target=10.0)
        assert capture is not None
        assert capture.gains.kp == 2.0

    def test_wall_clock_is_recorded_for_display(self) -> None:
        h = _Harness(window_s=0.05)
        h.wall = 1234.5
        capture = h.run(20, target=10.0)
        assert capture is not None
        assert capture.captured_at == 1234.5


class TestValidation:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("window_s", 0.0),
            ("pre_trigger_s", -1.0),
            ("min_step", 0.0),
            ("max_samples", 0),
        ],
    )
    def test_rejects_meaningless_settings(self, field: str, value: float) -> None:
        with pytest.raises(ValueError):
            _Harness(**{field: value})
