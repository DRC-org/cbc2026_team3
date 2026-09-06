"""周期タスク基盤 (lib/control/periodic.py) のテスト。

位置制御ループ・同期監視・目標値再送の 3 つが共有する土台なので、ここが崩れると
安全機構が 3 つ同時に崩れる。ライフサイクルの作法と周期の実測をここで固定する。
"""

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
    """タスクが自力で終わる (= 例外死する) のを待つ。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while task.is_running and loop.time() < deadline:
        await asyncio.sleep(0.001)
    assert task.is_running is False, "タスクが死ななかった"


class _Recorder(PeriodicTask):
    """tick 時刻と sleep 要求を記録するだけの周期タスク。"""

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
    """pause / resume の作法だけを見る周期タスク。"""

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
        # 例外情報を落とすと、試合中に原因の分からない 1 行だけが残る
        assert caplog.records[0].exc_info is not None

    def test_warning_same_key_is_throttled_within_interval(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """周期の乱れのような「例外を伴わない」警告も間引く (200Hz なら瞬時に溢れる)。"""
        clock = FakeClock()
        throttle = LogThrottle(logging.getLogger("test.throttle"), time_source=clock)

        with caplog.at_level(logging.WARNING, logger="test.throttle"):
            for _ in range(5):
                throttle.warning("jitter", "実周期が乱れています")

        assert len(caplog.records) == 1
        # exception() と違い、正常に完了した tick が遅かっただけなのでトレースバックは無い
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
        """公称周期が処理時間ぶん伸びないこと (次回起床を絶対時刻で管理する)。"""
        clock = FakeClock()
        task = _Recorder(clock, interval_s=0.01, work_s=0.004, stop_after=5)

        await task.run()

        assert task.periods == pytest.approx([0.01] * 4)

    async def test_overrun_does_not_burst_catch_up(self) -> None:
        """1 周期を超えて遅れたら位相を捨てる。遅れを取り戻そうと詰めて回さない。"""
        clock = FakeClock()
        task = _Recorder(clock, interval_s=0.01, work_s=0.03, stop_after=4)

        await task.run()

        # 遅れは待ち時間 0 として吸収するだけで、詰めて回して取り戻しにいかない
        assert task.delays == pytest.approx([0.0] * 4)
        assert task.periods == pytest.approx([0.03] * 3)

    async def test_first_tick_runs_before_any_sleep(self) -> None:
        """起動直後に 1 周期待たない (監視の空白を作らない)。"""
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
        # 例外でループを抜けない。抜けると防護が丸ごと失われる
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
        """3 タスク共通の作法。黙って無視すると「起動したつもり」に気付けない。"""
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
        """停止要求は start() でクリアする。持ち越すと再起動が即座に抜ける。"""
        task = _Recorder(FakeClock(), interval_s=0.001)
        task.set_sleep(asyncio.sleep)
        task.start()
        await task.stop()
        task.start()
        assert task.is_running is True
        await task.stop()

    async def test_stop_requested_before_start_is_not_lost(self) -> None:
        """run() は停止要求をクリアしない。直接駆動時に要求を取りこぼさない。"""
        task = _Recorder(FakeClock())
        task.request_stop()
        await task.run()
        assert task.tick_at == []

    async def test_stop_は死んだタスクの例外を再送出しない(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """止める処理が止まってはならない。

        `main()` の finally は周期タスクの停止と CAN のシャットダウンを素の for で
        並べている。既に例外で死んでいるタスクの `stop()` がその例外を再送出すると、
        以降の後始末 (2 台目のロボットのバス停止まで含めて) が丸ごと飛ぶ。
        死因はタスク側が既にログへ残しているので、ここは記録して先へ進める。
        """

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
        """例外死したタスクも stop() 後は start() で作り直せる。"""

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
        """pause() の戻り値時点で在庫の 1 周期も送信済みでないこと。"""
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
    """実周期の実測 (docs/impl_plan.md「乱れているかどうかを知る手段が無い」の解消)。

    `y_axis` の台形速度プロファイルの停止距離も `SyncMonitor` の時間予算も、
    公称周期どおりに回っていることが前提になっている。それが崩れたことを
    サンプル列を溜めずに (件数・最大値だけで) 検知できることをここで固定する。
    """

    async def test_no_overrun_when_on_schedule(self) -> None:
        """公称通りに回っていれば乱れを 1 件も数えない (平常時は静かにする)。"""
        clock = FakeClock()
        task = _Recorder(clock, interval_s=0.01, work_s=0.002, stop_after=5)

        await task.run()

        assert task.jitter_overrun_count == 0
        assert task.worst_jitter_s == pytest.approx(0.0)

    async def test_overrun_counts_when_period_exceeds_threshold(self) -> None:
        """公称周期の (1 + JITTER_OVERRUN_MARGIN) 倍を超えたら乱れとして数える。"""
        clock = FakeClock()
        # 1 tick が 0.02s かかる = 実周期 0.02s。公称 0.01s に対し超過分 0.01s は
        # しきい値 (0.01 * 0.5 = 0.005s) を上回るので、その次の tick 開始時に検知される
        task = _Recorder(clock, interval_s=0.01, work_s=0.02, stop_after=3)

        await task.run()

        assert task.jitter_overrun_count == 2
        assert task.worst_jitter_s == pytest.approx(0.01)

    async def test_overrun_warning_is_throttled(self, caplog: pytest.LogCaptureFixture) -> None:
        """乱れの WARNING も `LogThrottle` で間引く (200Hz の乱れが続けば瞬時に溢れる)。

        2 回の乱れが FakeClock 上で 0.02s しか離れていない (`LOG_THROTTLE_S`=1.0s の
        間引き窓の内側) ので、件数は 2 でも journal に出る行は 1 のはず。
        """
        clock = FakeClock()
        task = _Recorder(clock, interval_s=0.01, work_s=0.02, stop_after=3)

        with caplog.at_level(logging.WARNING, logger="tests.test_periodic"):
            await task.run()

        assert task.jitter_overrun_count == 2
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    async def test_boundary_exactly_at_threshold_does_not_count(self) -> None:
        """しきい値ちょうど (超過ではなく到達) は乱れに数えない (`>` であって `>=` でない)。

        `interval_s` は 2 の冪 (0.125) を使う —— `work_s = interval_s + threshold_s`
        を素直な小数 (0.01 等) で作ると、浮動小数の丸めで `jitter` がしきい値と
        1 ULP ずれ、`>` と `>=` のどちらでも同じ結果になって境界を検証できない
        (0.01 では実際に `jitter == 0.004999999999999999` になり食い違った)。
        2 の冪なら加減算が丸め無しで通り、`jitter` がしきい値とビット単位で一致する。
        """
        clock = FakeClock()
        interval_s = 0.125
        threshold_s = interval_s * JITTER_OVERRUN_MARGIN
        work_s = interval_s + threshold_s
        assert work_s - interval_s == threshold_s  # 前提: 丸めが起きていないことの自己チェック
        task = _Recorder(clock, interval_s=interval_s, work_s=work_s, stop_after=2)

        await task.run()

        assert task.jitter_overrun_count == 0

    async def test_boundary_just_above_threshold_counts(self) -> None:
        """しきい値をわずかに超えたら数える (`>` の反対側の境界)。"""
        clock = FakeClock()
        interval_s = 0.125
        threshold_s = interval_s * JITTER_OVERRUN_MARGIN
        task = _Recorder(
            clock, interval_s=interval_s, work_s=interval_s + threshold_s + 1e-6, stop_after=2
        )

        await task.run()

        assert task.jitter_overrun_count == 1

    async def test_worst_jitter_keeps_max_not_last(self) -> None:
        """最悪値は最大値を保持し、後続が平常に戻っても最新値へ上書きしない。"""

        class _Variable(_Recorder):
            """tick ごとに異なる処理時間を与える (1 回だけ大きく遅れる)。"""

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
        """stop() で空いた間隔を、再起動後の 1 発目の乱れとして数えない。"""
        clock = FakeClock()
        task = _Recorder(clock, interval_s=0.001, stop_after=2)
        task.set_sleep(asyncio.sleep)

        task.start()
        await asyncio.sleep(0.01)
        await task.stop()
        assert task.jitter_overrun_count == 0

        # 停止していた間に長時間が経過した状態を作る (会場で機体を放置した間など)
        clock.advance(10.0)

        task.stop_after = 4
        task.start()
        await asyncio.sleep(0.01)
        await task.stop()

        # start() が _last_tick_at をリセットしなければ、再開後 1 発目が
        # この 10 秒の空白をそのまま「実周期」として読み、乱れの最大値へ
        # 巨大な値が入ってしまう
        assert task.jitter_overrun_count == 0

    async def test_first_tick_has_no_prior_period_to_compare(self) -> None:
        """起動直後の 1 発目は比較対象が無いので乱れとして数えない。"""
        clock = FakeClock()
        task = _Recorder(clock, interval_s=0.01, work_s=1.0, stop_after=1)

        await task.run()

        assert task.jitter_overrun_count == 0
        assert task.worst_jitter_s == pytest.approx(0.0)


class TestJitterSummary:
    """試合ぶんの実周期を journal へ 1 行残す (`log_jitter_summary`)。

    呼び出し口は `lib/server.py` の `_cmd_match_finish`。ここでは
    `PeriodicTask` 自身の約束 —— **超過 0 件でも必ず 1 行出す** / 0 件と
    1 件以上を文言で読み分けられる —— だけを固定する。
    """

    async def test_summary_is_logged_even_without_overrun(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """超過 0 件でも 1 行残し、最悪の遅れをその行に載せる。

        ここを `if 超過 > 0` で塞ぐと、公称の 1.4 倍で常時走っている機体
        (200Hz が 143Hz。停止距離も偏差監視の 40ms 予算も既に崩れている) で
        journal が無音になり、しきい値を決めるためにしきい値を超えている必要が
        ある、という循環になる。実機でしきい値を決める唯一の観測値がこの 1 行。
        """
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
        # 0 件でも最悪の遅れ (31.2ms) が読めること。ここが 0.0ms を名乗ると
        # 「乱れていない」と誤読され、この 1 行の存在価値が消える
        assert f"{threshold_s / 2 * 1000.0:.1f}ms" in message

    async def test_summary_wording_distinguishes_overrun_from_clean(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """超過 1 件以上は 0 件と違う文言で出す (1 行だけ読んで区別が付くこと)。"""
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
        """集計は読むだけ。0 に戻すのは `reset_jitter_stats` の仕事。

        2 つを 1 つのメソッドへ戻すと、集計を出さずにリセットしたい
        `match_start` (前縁リセット) が「試合の集計」を名乗る 1 行を
        毎回吐くようになる。
        """
        task = _Recorder(FakeClock(), interval_s=0.01, work_s=0.02, stop_after=3)
        await task.run()

        task.log_jitter_summary()

        assert task.jitter_overrun_count == 2
        assert task.worst_jitter_s == pytest.approx(0.01)


class TestJitterReset:
    """乱れの記録を落とす (`reset_jitter_stats`)。

    呼び出し口は `lib/server.py` の `_cmd_match_finish` (集計を残した直後) と
    `_handle_match_start` (前縁リセット)。詳しい設計判断 (なぜ試合スコープか・
    なぜ集計が match_finish 側か) はそちらのコメントに書いてあるので、ここでは
    `PeriodicTask` 自身の実装 (回数と最悪値を両方落とす / ログは出さない /
    `_last_tick_at` は触らない) だけを固定する。
    """

    async def test_reset_clears_both_count_and_worst(self) -> None:
        """回数と最悪値は必ず両方一緒に落ちる (片方だけ残す不整合を作らない)。"""
        clock = FakeClock()
        task = _Recorder(clock, interval_s=0.01, work_s=0.02, stop_after=3)
        await task.run()
        assert task.jitter_overrun_count == 2
        assert task.worst_jitter_s == pytest.approx(0.01)

        task.reset_jitter_stats()

        assert task.jitter_overrun_count == 0
        assert task.worst_jitter_s == pytest.approx(0.0)

    async def test_reset_when_no_overrun_is_a_noop(self) -> None:
        """乱れが一度も無ければ、リセットしても何も変わらない (0 のまま)。"""
        task = _Recorder(FakeClock(), interval_s=0.01, work_s=0.001, stop_after=3)
        await task.run()
        assert task.jitter_overrun_count == 0

        task.reset_jitter_stats()

        assert task.jitter_overrun_count == 0
        assert task.worst_jitter_s == pytest.approx(0.0)

    async def test_reset_alone_logs_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        """リセット単体では 1 行も出さない (`match_start` の前縁リセットが無音であること)。

        集計とリセットを 1 つのメソッドへ戻すと、準備中のぶんを洗い流すだけの
        `match_start` が「試合の集計」を名乗る 1 行を毎回吐き、試合終了時の
        本物の 1 行と見分けが付かなくなる。
        """
        task = _Recorder(FakeClock(), interval_s=0.01, work_s=0.02, stop_after=3)
        await task.run()
        assert task.jitter_overrun_count == 2
        # run() 中の超過 WARNING はここでの検証対象ではない (見るのはリセット単体)
        caplog.clear()

        with caplog.at_level(logging.INFO, logger="tests.test_periodic"):
            task.reset_jitter_stats()

        assert not caplog.records

    async def test_reset_does_not_disturb_ongoing_measurement(self) -> None:
        """リセットは統計だけを落とし、次の tick の実周期計測を巻き添えにしない。

        `_last_tick_at` を触らないことの確認。もしここを ``None`` に戻すと、
        リセット直後の 1 回だけ実周期の計測を取りこぼし、本当はリセットの
        直後に起きた乱れを見逃す (「乱れが出るからこの機能を作っている」と
        矛盾する)。この違いは、リセットを挟んだ後にも乱れを検知できるかで
        判別できる: `_last_tick_at` を消していれば直後の 1 回ぶんだけ
        検知漏れが起きるので、最終的な超過回数が 1 少なくなる。
        """
        clock = FakeClock()

        class _ResetMidRun(_Recorder):
            def __init__(self, clock: FakeClock) -> None:
                super().__init__(clock, interval_s=0.01, work_s=0.02, stop_after=4)
                self._reset_done = False

            async def _tick(self) -> None:
                await super()._tick()
                # 2 tick 目の直後、既に 1 回乱れを記録した状態でリセットする
                if len(self.tick_at) == 2 and not self._reset_done:
                    self._reset_done = True
                    self.reset_jitter_stats()

        task = _ResetMidRun(clock)
        await task.run()

        # リセットが _last_tick_at も消していれば、3 tick 目の乱れ検知が
        # 1 回分すり抜けて 1 になる。据え置いていれば 3・4 tick 目の 2 回とも拾う
        assert task.jitter_overrun_count == 2
        assert task.worst_jitter_s == pytest.approx(0.01)
