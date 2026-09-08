from __future__ import annotations

import asyncio
import logging

import pytest

from lib.control.periodic import (
    JITTER_OVERRUN_MARGIN,
    LOG_THROTTLE_S,
    LogThrottle,
    PausablePeriodicTask,
    PeriodicTask,
)
from tests.fake_clock import FakeClock


async def wait_for_death(task: PeriodicTask, *, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while task.is_running and loop.time() < deadline:
        await asyncio.sleep(0.001)
    assert task.is_running is False, "タスクが死ななかった"


class _Recorder(PeriodicTask):
    def __init__(
        self,
        clock: FakeClock,
        *,
        interval_s: float = 0.01,
        work_s: float = 0.0,
        stop_after: int | None = None,
        fail_ticks: int = 0,
    ) -> None:
        super().__init__(interval_s=interval_s, time_source=clock)
        self.clock = clock
        self.work_s = work_s
        self.stop_after = stop_after
        self.fail_ticks = fail_ticks
        self.tick_at: list[float] = []
        self.delays: list[float] = []
        self.exits = 0
        self.errors = 0
        self.set_sleep(self._fake_sleep)

    async def _fake_sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.clock.advance(delay)

    async def _tick(self) -> None:
        self.tick_at.append(self.clock.now)
        self.clock.advance(self.work_s)
        if self.stop_after is not None and len(self.tick_at) >= self.stop_after:
            self.request_stop()
        if self.fail_ticks > 0:
            self.fail_ticks -= 1
            raise RuntimeError("tick 内部エラー (テスト)")

    async def _on_tick_error(self) -> None:
        self.errors += 1
        await super()._on_tick_error()

    async def _on_run_exit(self) -> None:
        self.exits += 1

    def _label(self) -> str:
        return "テスト用周期タスク"

    @property
    def periods(self) -> list[float]:
        return [b - a for a, b in zip(self.tick_at, self.tick_at[1:], strict=False)]


class _Pausable(PausablePeriodicTask):
    def __init__(self, clock: FakeClock, *, interval_s: float = 0.01) -> None:
        super().__init__(interval_s=interval_s, time_source=clock)
        self.steps = 0
        self.resumed = 0

    async def _step_locked(self) -> None:
        if self.is_paused:
            return
        self.steps += 1

    def _on_resume(self) -> None:
        self.resumed += 1

    def _label(self) -> str:
        return "テスト用一時停止可能タスク"


class TestLogThrottle:
    def test_same_key_is_throttled_within_interval(self, caplog: pytest.LogCaptureFixture) -> None:
        clock = FakeClock()
        throttle = LogThrottle(logging.getLogger("test.throttle"), time_source=clock)

        with caplog.at_level(logging.ERROR, logger="test.throttle"):
            for _ in range(5):
                throttle.exception("step", "周期処理で例外")

        assert len(caplog.records) == 1

    def test_different_keys_are_independent(self, caplog: pytest.LogCaptureFixture) -> None:
        clock = FakeClock()
        throttle = LogThrottle(logging.getLogger("test.throttle"), time_source=clock)

        with caplog.at_level(logging.ERROR, logger="test.throttle"):
            throttle.exception("step", "周期処理で例外")
            throttle.exception("zero", "0 電流フレームの送信に失敗")

        assert len(caplog.records) == 2

    def test_logs_again_after_interval(self, caplog: pytest.LogCaptureFixture) -> None:
        clock = FakeClock()
        throttle = LogThrottle(logging.getLogger("test.throttle"), time_source=clock)

        with caplog.at_level(logging.ERROR, logger="test.throttle"):
            throttle.exception("step", "周期処理で例外")
            clock.advance(LOG_THROTTLE_S * 1.1)
            throttle.exception("step", "周期処理で例外")

        assert len(caplog.records) == 2

    def test_message_arguments_are_formatted(self, caplog: pytest.LogCaptureFixture) -> None:
        throttle = LogThrottle(logging.getLogger("test.throttle"), time_source=FakeClock())

        with caplog.at_level(logging.ERROR, logger="test.throttle"):
            throttle.exception("step", "周期処理で例外 (bus=%s)", "m3508_bus")

        assert "m3508_bus" in caplog.text
        assert caplog.records[0].exc_info is not None

    def test_warning_same_key_is_throttled_within_interval(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        clock = FakeClock()
        throttle = LogThrottle(logging.getLogger("test.throttle"), time_source=clock)

        with caplog.at_level(logging.WARNING, logger="test.throttle"):
            for _ in range(5):
                throttle.warning("jitter", "実周期が乱れています")

        assert len(caplog.records) == 1
        assert caplog.records[0].exc_info is None

    def test_warning_logs_again_after_interval(self, caplog: pytest.LogCaptureFixture) -> None:
        clock = FakeClock()
        throttle = LogThrottle(logging.getLogger("test.throttle"), time_source=clock)

        with caplog.at_level(logging.WARNING, logger="test.throttle"):
            throttle.warning("jitter", "実周期が乱れています")
            clock.advance(LOG_THROTTLE_S * 1.1)
            throttle.warning("jitter", "実周期が乱れています")

        assert len(caplog.records) == 2


class TestPeriod:
    async def test_period_excludes_processing_time(self) -> None:
        clock = FakeClock()
        task = _Recorder(clock, interval_s=0.01, work_s=0.004, stop_after=5)

        await task.run()

        assert task.periods == pytest.approx([0.01] * 4)

    async def test_overrun_does_not_burst_catch_up(self) -> None:
        clock = FakeClock()
        task = _Recorder(clock, interval_s=0.01, work_s=0.03, stop_after=4)

        await task.run()

        assert task.delays == pytest.approx([0.0] * 4)
        assert task.periods == pytest.approx([0.03] * 3)

    async def test_first_tick_runs_before_any_sleep(self) -> None:
        clock = FakeClock()
        task = _Recorder(clock, interval_s=0.01, stop_after=1)

        await task.run()

        assert task.tick_at == [1000.0]


class TestLifecycle:
    async def test_run_exits_on_request_stop(self) -> None:
        task = _Recorder(FakeClock(), stop_after=3)
        await task.run()
        assert len(task.tick_at) == 3

    async def test_run_survives_tick_exception(self) -> None:
        task = _Recorder(FakeClock(), stop_after=3, fail_ticks=1)
        await task.run()
        assert len(task.tick_at) == 3
        assert task.errors == 1

    async def test_on_run_exit_runs_even_after_exception(self) -> None:
        class _Boom(_Recorder):
            async def _tick(self) -> None:
                raise asyncio.CancelledError

        task = _Boom(FakeClock())
        with pytest.raises(asyncio.CancelledError):
            await task.run()
        assert task.exits == 1

    async def test_run_propagates_cancellation(self) -> None:
        task = _Recorder(FakeClock(), interval_s=0.001)
        task.set_sleep(asyncio.sleep)
        runner = asyncio.create_task(task.run())
        await asyncio.sleep(0.005)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner

    async def test_double_start_raises(self) -> None:
        task = _Recorder(FakeClock(), interval_s=0.001)
        task.set_sleep(asyncio.sleep)
        task.start()
        try:
            with pytest.raises(RuntimeError):
                task.start()
        finally:
            await task.stop()

    async def test_start_and_stop_leaves_no_task(self) -> None:
        task = _Recorder(FakeClock(), interval_s=0.001)
        task.set_sleep(asyncio.sleep)
        task.start()
        assert task.is_running is True
        await task.stop()
        assert task.is_running is False
        assert task.exits == 1

    async def test_stop_without_start_is_noop(self) -> None:
        task = _Recorder(FakeClock())
        await task.stop()
        assert task.is_running is False

    async def test_restart_after_stop(self) -> None:
        task = _Recorder(FakeClock(), interval_s=0.001)
        task.set_sleep(asyncio.sleep)
        task.start()
        await task.stop()
        task.start()
        assert task.is_running is True
        await task.stop()

    async def test_stop_requested_before_start_is_not_lost(self) -> None:
        task = _Recorder(FakeClock())
        task.request_stop()
        await task.run()
        assert task.tick_at == []

    async def test_stop_は死んだタスクの例外を再送出しない(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:

        async def _broken_sleep(_delay: float) -> None:
            raise RuntimeError("周期待ちが壊れた")

        task = _Recorder(FakeClock(), interval_s=0.001)
        task.set_sleep(_broken_sleep)
        task.start()
        await wait_for_death(task)

        with caplog.at_level(logging.ERROR, logger="lib.control.periodic"):
            await task.stop()

        assert task.is_running is False
        assert any(r.levelno >= logging.ERROR for r in caplog.records), (
            "死因が記録されないと、後始末を続けたことでタスクの死が無痕跡になる"
        )

    async def test_stop_後は同じタスクを再起動できる(self) -> None:

        async def _broken_sleep(_delay: float) -> None:
            raise RuntimeError("周期待ちが壊れた")

        task = _Recorder(FakeClock(), interval_s=0.001)
        task.set_sleep(_broken_sleep)
        task.start()
        await wait_for_death(task)
        await task.stop()

        task.set_sleep(asyncio.sleep)
        task.start()
        assert task.is_running is True
        await task.stop()


class TestPausable:
    async def test_paused_task_skips_work(self) -> None:
        task = _Pausable(FakeClock())
        await task.step()
        await task.pause(reason="動作確認")
        await task.step()

        assert task.is_paused is True
        assert task.steps == 1

    async def test_resume_restores_work(self) -> None:
        task = _Pausable(FakeClock())
        await task.pause()
        task.resume()
        await task.step()

        assert task.is_paused is False
        assert task.steps == 1
        assert task.resumed == 1

    async def test_double_pause_and_resume_are_noop(self) -> None:
        task = _Pausable(FakeClock())
        await task.pause()
        await task.pause()
        task.resume()
        task.resume()
        assert task.resumed == 1

    async def test_pause_waits_for_in_flight_step(self) -> None:
        released = asyncio.Event()

        class _Slow(_Pausable):
            async def _step_locked(self) -> None:
                await released.wait()
                await super()._step_locked()

        task = _Slow(FakeClock())
        stepping = asyncio.create_task(task.step())
        await asyncio.sleep(0)
        pausing = asyncio.create_task(task.pause())
        await asyncio.sleep(0)

        assert pausing.done() is False
        released.set()
        await stepping
        await pausing
        assert task.steps == 1


class TestJitter:
    async def test_no_overrun_when_on_schedule(self, caplog: pytest.LogCaptureFixture) -> None:
        clock = FakeClock()
        task = _Recorder(clock, interval_s=0.01, work_s=0.002, stop_after=5)

        with caplog.at_level(logging.WARNING, logger="tests.test_periodic"):
            await task.run()

        assert task.jitter_overrun_count == 0
        assert task.worst_jitter_s == pytest.approx(0.0)
        assert not caplog.records

    async def test_overrun_counts_when_period_exceeds_threshold(self) -> None:
        clock = FakeClock()
        task = _Recorder(clock, interval_s=0.01, work_s=0.02, stop_after=3)

        await task.run()

        assert task.jitter_overrun_count == 2
        assert task.worst_jitter_s == pytest.approx(0.01)

    async def test_overrun_warning_is_throttled(self, caplog: pytest.LogCaptureFixture) -> None:
        clock = FakeClock()
        task = _Recorder(clock, interval_s=0.01, work_s=0.02, stop_after=3)

        with caplog.at_level(logging.WARNING, logger="tests.test_periodic"):
            await task.run()

        assert task.jitter_overrun_count == 2
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    async def test_boundary_exactly_at_threshold_does_not_count(self) -> None:
        clock = FakeClock()
        # 素直な小数 (0.01) では丸めで jitter == 0.004999999999999999 になり境界が作れない。
        # 2 の冪なら加減算が丸め無しで通り、jitter がしきい値とビット単位で一致する。
        interval_s = 0.125
        threshold_s = interval_s * JITTER_OVERRUN_MARGIN
        work_s = interval_s + threshold_s
        assert work_s - interval_s == threshold_s
        task = _Recorder(clock, interval_s=interval_s, work_s=work_s, stop_after=2)

        await task.run()

        assert task.jitter_overrun_count == 0

    async def test_boundary_just_above_threshold_counts(self) -> None:
        clock = FakeClock()
        interval_s = 0.125
        threshold_s = interval_s * JITTER_OVERRUN_MARGIN
        task = _Recorder(
            clock, interval_s=interval_s, work_s=interval_s + threshold_s + 1e-6, stop_after=2
        )

        await task.run()

        assert task.jitter_overrun_count == 1

    async def test_sub_threshold_jitter_updates_worst_without_counting(self) -> None:
        interval_s = 0.125
        threshold_s = interval_s * JITTER_OVERRUN_MARGIN
        task = _Recorder(
            FakeClock(),
            interval_s=interval_s,
            work_s=interval_s + threshold_s / 2,
            stop_after=3,
        )

        await task.run()

        assert task.jitter_overrun_count == 0
        assert task.worst_jitter_s == pytest.approx(threshold_s / 2)

    async def test_worst_jitter_keeps_max_not_last(self) -> None:

        class _Variable(_Recorder):
            def __init__(self, clock: FakeClock) -> None:
                super().__init__(clock, interval_s=0.01, stop_after=4)
                self._work_schedule = [0.03, 0.0, 0.0]

            async def _tick(self) -> None:
                self.tick_at.append(self.clock.now)
                if self._work_schedule:
                    self.clock.advance(self._work_schedule.pop(0))
                if self.stop_after is not None and len(self.tick_at) >= self.stop_after:
                    self.request_stop()

        task = _Variable(FakeClock())
        await task.run()

        assert task.jitter_overrun_count == 1
        assert task.worst_jitter_s == pytest.approx(0.02)

    async def test_restart_does_not_count_the_stopped_gap(self) -> None:
        clock = FakeClock()
        task = _Recorder(clock, interval_s=0.001, stop_after=2)
        task.set_sleep(asyncio.sleep)

        task.start()
        await asyncio.sleep(0.01)
        await task.stop()
        assert task.jitter_overrun_count == 0

        clock.advance(10.0)

        task.stop_after = 4
        task.start()
        await asyncio.sleep(0.01)
        await task.stop()

        assert task.jitter_overrun_count == 0

    async def test_first_tick_has_no_prior_period_to_compare(self) -> None:
        clock = FakeClock()
        task = _Recorder(clock, interval_s=0.01, work_s=1.0, stop_after=1)

        await task.run()

        assert task.jitter_overrun_count == 0
        assert task.worst_jitter_s == pytest.approx(0.0)


class TestJitterSummary:
    async def test_summary_is_logged_even_without_overrun(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        interval_s = 0.125
        threshold_s = interval_s * JITTER_OVERRUN_MARGIN
        task = _Recorder(
            FakeClock(),
            interval_s=interval_s,
            work_s=interval_s + threshold_s / 2,
            stop_after=3,
        )
        await task.run()
        assert task.jitter_overrun_count == 0

        with caplog.at_level(logging.INFO, logger="tests.test_periodic"):
            task.log_jitter_summary()

        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 1
        message = infos[0].getMessage()
        assert "超過なし" in message
        assert f"{threshold_s / 2 * 1000.0:.1f}ms" in message

    async def test_summary_wording_distinguishes_overrun_from_clean(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        task = _Recorder(FakeClock(), interval_s=0.01, work_s=0.02, stop_after=3)
        await task.run()
        assert task.jitter_overrun_count == 2

        with caplog.at_level(logging.INFO, logger="tests.test_periodic"):
            task.log_jitter_summary()

        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 1
        message = infos[0].getMessage()
        assert "超過 2 回" in message
        assert "超過なし" not in message

    async def test_summary_does_not_reset(self) -> None:
        task = _Recorder(FakeClock(), interval_s=0.01, work_s=0.02, stop_after=3)
        await task.run()

        task.log_jitter_summary()

        assert task.jitter_overrun_count == 2
        assert task.worst_jitter_s == pytest.approx(0.01)


class TestJitterReset:
    async def test_reset_clears_both_count_and_worst(self) -> None:
        clock = FakeClock()
        task = _Recorder(clock, interval_s=0.01, work_s=0.02, stop_after=3)
        await task.run()
        assert task.jitter_overrun_count == 2
        assert task.worst_jitter_s == pytest.approx(0.01)

        task.reset_jitter_stats()

        assert task.jitter_overrun_count == 0
        assert task.worst_jitter_s == pytest.approx(0.0)

    async def test_reset_when_no_overrun_is_a_noop(self) -> None:
        task = _Recorder(FakeClock(), interval_s=0.01, work_s=0.001, stop_after=3)
        await task.run()
        assert task.jitter_overrun_count == 0

        task.reset_jitter_stats()

        assert task.jitter_overrun_count == 0
        assert task.worst_jitter_s == pytest.approx(0.0)

    async def test_reset_alone_logs_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        task = _Recorder(FakeClock(), interval_s=0.01, work_s=0.02, stop_after=3)
        await task.run()
        assert task.jitter_overrun_count == 2
        caplog.clear()

        with caplog.at_level(logging.INFO, logger="tests.test_periodic"):
            task.reset_jitter_stats()

        assert not caplog.records

    async def test_reset_does_not_disturb_ongoing_measurement(self) -> None:
        clock = FakeClock()

        class _ResetMidRun(_Recorder):
            def __init__(self, clock: FakeClock) -> None:
                super().__init__(clock, interval_s=0.01, work_s=0.02, stop_after=4)
                self._reset_done = False

            async def _tick(self) -> None:
                await super()._tick()
                if len(self.tick_at) == 2 and not self._reset_done:
                    self._reset_done = True
                    self.reset_jitter_stats()

        task = _ResetMidRun(clock)
        await task.run()

        assert task.jitter_overrun_count == 2
        assert task.worst_jitter_s == pytest.approx(0.01)
