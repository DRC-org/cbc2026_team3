"""周期タスク基盤 (lib/control/periodic.py) のテスト。

位置制御ループ・同期監視・目標値再送の 3 つが共有する土台なので、ここが崩れると
安全機構が 3 つ同時に崩れる。ライフサイクルの作法と周期の実測をここで固定する。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from lib.control.periodic import (
    LOG_THROTTLE_S,
    LogThrottle,
    PausablePeriodicTask,
    PeriodicTask,
)


class _Clock:
    """単調増加クロックのスタブ。実時間 sleep に依存せず周期を検証する。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


class _Recorder(PeriodicTask):
    """tick 時刻と sleep 要求を記録するだけの周期タスク。"""

    def __init__(
        self,
        clock: _Clock,
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
    """pause / resume の作法だけを見る周期タスク。"""

    def __init__(self, clock: _Clock, *, interval_s: float = 0.01) -> None:
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
        clock = _Clock()
        throttle = LogThrottle(logging.getLogger("test.throttle"), time_source=clock)

        with caplog.at_level(logging.ERROR, logger="test.throttle"):
            for _ in range(5):
                throttle.exception("step", "周期処理で例外")

        assert len(caplog.records) == 1

    def test_different_keys_are_independent(self, caplog: pytest.LogCaptureFixture) -> None:
        clock = _Clock()
        throttle = LogThrottle(logging.getLogger("test.throttle"), time_source=clock)

        with caplog.at_level(logging.ERROR, logger="test.throttle"):
            throttle.exception("step", "周期処理で例外")
            throttle.exception("zero", "0 電流フレームの送信に失敗")

        assert len(caplog.records) == 2

    def test_logs_again_after_interval(self, caplog: pytest.LogCaptureFixture) -> None:
        clock = _Clock()
        throttle = LogThrottle(logging.getLogger("test.throttle"), time_source=clock)

        with caplog.at_level(logging.ERROR, logger="test.throttle"):
            throttle.exception("step", "周期処理で例外")
            clock.advance(LOG_THROTTLE_S * 1.1)
            throttle.exception("step", "周期処理で例外")

        assert len(caplog.records) == 2

    def test_message_arguments_are_formatted(self, caplog: pytest.LogCaptureFixture) -> None:
        throttle = LogThrottle(logging.getLogger("test.throttle"), time_source=_Clock())

        with caplog.at_level(logging.ERROR, logger="test.throttle"):
            throttle.exception("step", "周期処理で例外 (bus=%s)", "m3508_bus")

        assert "m3508_bus" in caplog.text
        # 例外情報を落とすと、試合中に原因の分からない 1 行だけが残る
        assert caplog.records[0].exc_info is not None


class TestPeriod:
    async def test_period_excludes_processing_time(self) -> None:
        """公称周期が処理時間ぶん伸びないこと (次回起床を絶対時刻で管理する)。"""
        clock = _Clock()
        task = _Recorder(clock, interval_s=0.01, work_s=0.004, stop_after=5)

        await task.run()

        assert task.periods == pytest.approx([0.01] * 4)

    async def test_overrun_does_not_burst_catch_up(self) -> None:
        """1 周期を超えて遅れたら位相を捨てる。遅れを取り戻そうと詰めて回さない。"""
        clock = _Clock()
        task = _Recorder(clock, interval_s=0.01, work_s=0.03, stop_after=4)

        await task.run()

        # 遅れは待ち時間 0 として吸収するだけで、詰めて回して取り戻しにいかない
        assert task.delays == pytest.approx([0.0] * 4)
        assert task.periods == pytest.approx([0.03] * 3)

    async def test_first_tick_runs_before_any_sleep(self) -> None:
        """起動直後に 1 周期待たない (監視の空白を作らない)。"""
        clock = _Clock()
        task = _Recorder(clock, interval_s=0.01, stop_after=1)

        await task.run()

        assert task.tick_at == [1000.0]


class TestLifecycle:
    async def test_run_exits_on_request_stop(self) -> None:
        task = _Recorder(_Clock(), stop_after=3)
        await task.run()
        assert len(task.tick_at) == 3

    async def test_run_survives_tick_exception(self) -> None:
        task = _Recorder(_Clock(), stop_after=3, fail_ticks=1)
        await task.run()
        # 例外でループを抜けない。抜けると防護が丸ごと失われる
        assert len(task.tick_at) == 3
        assert task.errors == 1

    async def test_on_run_exit_runs_even_after_exception(self) -> None:
        class _Boom(_Recorder):
            async def _tick(self) -> None:
                raise asyncio.CancelledError

        task = _Boom(_Clock())
        with pytest.raises(asyncio.CancelledError):
            await task.run()
        assert task.exits == 1

    async def test_run_propagates_cancellation(self) -> None:
        task = _Recorder(_Clock(), interval_s=0.001)
        task.set_sleep(asyncio.sleep)
        runner = asyncio.create_task(task.run())
        await asyncio.sleep(0.005)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner

    async def test_double_start_raises(self) -> None:
        """3 タスク共通の作法。黙って無視すると「起動したつもり」に気付けない。"""
        task = _Recorder(_Clock(), interval_s=0.001)
        task.set_sleep(asyncio.sleep)
        task.start()
        try:
            with pytest.raises(RuntimeError):
                task.start()
        finally:
            await task.stop()

    async def test_start_and_stop_leaves_no_task(self) -> None:
        task = _Recorder(_Clock(), interval_s=0.001)
        task.set_sleep(asyncio.sleep)
        task.start()
        assert task.is_running is True
        await task.stop()
        assert task.is_running is False
        assert task.exits == 1

    async def test_stop_without_start_is_noop(self) -> None:
        task = _Recorder(_Clock())
        await task.stop()
        assert task.is_running is False

    async def test_restart_after_stop(self) -> None:
        """停止要求は start() でクリアする。持ち越すと再起動が即座に抜ける。"""
        task = _Recorder(_Clock(), interval_s=0.001)
        task.set_sleep(asyncio.sleep)
        task.start()
        await task.stop()
        task.start()
        assert task.is_running is True
        await task.stop()

    async def test_stop_requested_before_start_is_not_lost(self) -> None:
        """run() は停止要求をクリアしない。直接駆動時に要求を取りこぼさない。"""
        task = _Recorder(_Clock())
        task.request_stop()
        await task.run()
        assert task.tick_at == []


class TestPausable:
    async def test_paused_task_skips_work(self) -> None:
        task = _Pausable(_Clock())
        await task.step()
        await task.pause(reason="動作確認")
        await task.step()

        assert task.is_paused is True
        assert task.steps == 1

    async def test_resume_restores_work(self) -> None:
        task = _Pausable(_Clock())
        await task.pause()
        task.resume()
        await task.step()

        assert task.is_paused is False
        assert task.steps == 1
        assert task.resumed == 1

    async def test_double_pause_and_resume_are_noop(self) -> None:
        task = _Pausable(_Clock())
        await task.pause()
        await task.pause()
        task.resume()
        task.resume()
        assert task.resumed == 1

    async def test_pause_waits_for_in_flight_step(self) -> None:
        """pause() の戻り値時点で在庫の 1 周期も送信済みでないこと。"""
        released = asyncio.Event()

        class _Slow(_Pausable):
            async def _step_locked(self) -> None:
                await released.wait()
                await super()._step_locked()

        task = _Slow(_Clock())
        stepping = asyncio.create_task(task.step())
        await asyncio.sleep(0)
        pausing = asyncio.create_task(task.pause())
        await asyncio.sleep(0)

        assert pausing.done() is False
        released.set()
        await stepping
        await pausing
        assert task.steps == 1
