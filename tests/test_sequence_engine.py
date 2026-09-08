from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from lib.sequence.engine import AxisSyncError, Sequence, StepInfo, step


class SampleSequence(Sequence):
    def __init__(self):
        super().__init__("sample")
        self.executed = []

    @step("ステップ1")
    async def step1(self):
        self.executed.append("step1")

    @step("ステップ2", require_trigger=True)
    async def step2(self):
        self.executed.append("step2")

    @step("ステップ3")
    async def step3(self):
        self.executed.append("step3")


class FailingSequence(Sequence):
    def __init__(self):
        super().__init__("failing")
        self.fail = True

    @step("先に通るステップ")
    async def ok_step(self):
        return None

    @step("必ず失敗する")
    async def bad_step(self):
        if self.fail:
            raise AxisSyncError("シーケンス 'failing': 軸内のモータ位置が左右がずれています")


@contextlib.asynccontextmanager
async def _auto_trigger(seq: Sequence) -> AsyncIterator[None]:

    async def press() -> None:
        while True:
            if seq.waiting_trigger:
                seq.trigger()
            await asyncio.sleep(0.01)

    task = asyncio.create_task(press())
    try:
        yield
    finally:
        task.cancel()


class TestStepDecorator:
    def test_step_decorator_sets_attributes(self):
        assert SampleSequence.step1._step_label == "ステップ1"
        assert SampleSequence.step1._step_require_trigger is False
        assert SampleSequence.step2._step_label == "ステップ2"
        assert SampleSequence.step2._step_require_trigger is True


class TestStepsCollection:
    def test_steps_collected_in_order(self):
        seq = SampleSequence()
        assert len(seq.steps) == 3
        assert seq.steps[0] == StepInfo(
            label="ステップ1", method_name="step1", require_trigger=False
        )
        assert seq.steps[1] == StepInfo(
            label="ステップ2", method_name="step2", require_trigger=True
        )
        assert seq.steps[2] == StepInfo(
            label="ステップ3", method_name="step3", require_trigger=False
        )


class TestRun:
    async def test_run_executes_all_steps(self):
        seq = SampleSequence()

        async with _auto_trigger(seq):
            await seq.run()

        assert seq.executed == ["step1", "step2", "step3"]

    async def test_run_waits_for_trigger(self):
        seq = SampleSequence()
        reached_step2 = False

        async def run_seq():
            nonlocal reached_step2
            await seq.run()
            reached_step2 = True

        task = asyncio.create_task(run_seq())
        await asyncio.sleep(0.05)

        assert seq.executed == ["step1"]
        assert seq.waiting_trigger is True
        assert reached_step2 is False

        seq.trigger()
        await asyncio.sleep(0.05)

        assert seq.executed == ["step1", "step2", "step3"]
        assert reached_step2 is True
        task.cancel()

    async def test_trigger_advances_step(self):
        seq = SampleSequence()

        task = asyncio.create_task(seq.run())
        await asyncio.sleep(0.05)

        assert seq.progress["step_index"] == 1
        assert seq.waiting_trigger is True

        seq.trigger()
        await asyncio.sleep(0.05)

        assert seq.executed == ["step1", "step2", "step3"]
        task.cancel()

    async def test_run_completes(self):
        seq = SampleSequence()

        async with _auto_trigger(seq):
            await seq.run()

        assert seq.is_running is False


class TestProgress:
    def test_progress_property(self):
        seq = SampleSequence()
        progress = seq.progress
        assert progress == {
            "sequence": "sample",
            "current_step": "ステップ1",
            "step_index": 0,
            "total_steps": 3,
            "waiting_trigger": False,
            "running": False,
            "steps": [
                {"index": 0, "label": "ステップ1", "require_trigger": False},
                {"index": 1, "label": "ステップ2", "require_trigger": True},
                {"index": 2, "label": "ステップ3", "require_trigger": False},
            ],
            "last_error": None,
        }

    async def test_progress_waiting_trigger(self):
        seq = SampleSequence()
        task = asyncio.create_task(seq.run())
        await asyncio.sleep(0.05)

        progress = seq.progress
        assert progress["waiting_trigger"] is True
        assert progress["running"] is True
        assert progress["current_step"] == "ステップ2"
        assert progress["step_index"] == 1

        seq.trigger()
        await task


class TestReset:
    async def test_reset(self):
        seq = SampleSequence()

        async with _auto_trigger(seq):
            await seq.run()

        assert seq.progress["step_index"] == 3
        await seq.reset()
        assert seq.progress["step_index"] == 0
        assert seq.is_running is False
        assert seq.waiting_trigger is False


class TestLastError:
    async def test_平常時はNone(self):
        seq = SampleSequence()

        async with _auto_trigger(seq):
            await seq.run()

        assert seq.last_error is None
        assert seq.progress["last_error"] is None

    async def test_失敗したステップと理由を保持する(self):
        seq = FailingSequence()

        await seq.run()

        failure = seq.last_error
        assert failure is not None
        assert failure.step_index == 1
        assert failure.label == "必ず失敗する"
        assert "左右がずれています" in failure.message
        assert seq.progress["last_error"] == {
            "step_index": 1,
            "step": "必ず失敗する",
            "message": failure.message,
        }

    async def test_再実行で消える(self):
        seq = FailingSequence()
        await seq.run()
        assert seq.last_error is not None

        seq.fail = False
        await seq.reset()
        await seq.run()

        assert seq.last_error is None


class TestLifecycle:
    async def test_開始要求があるまで一歩も動かない(self):
        seq = SampleSequence()
        task = asyncio.create_task(seq.run_forever())
        await asyncio.sleep(0.05)

        assert seq.executed == []
        assert seq.is_running is False

        seq.request_start()
        await asyncio.sleep(0.05)

        assert seq.executed == ["step1"]
        assert seq.is_running is True
        task.cancel()

    async def test_通常停止でステップが先頭へ巻き戻る(self):
        seq = SampleSequence()
        task = asyncio.create_task(seq.run_forever())
        seq.request_start()
        await asyncio.sleep(0.05)
        assert seq.progress["step_index"] == 1

        seq.request_stop()
        await asyncio.sleep(0.05)

        assert seq.is_running is False
        assert seq.progress["step_index"] == 0

        seq.request_start()
        await asyncio.sleep(0.05)
        assert seq.executed == ["step1", "step1"]
        task.cancel()

    async def test_完走後は位置を保持する(self):
        seq = SampleSequence()
        task = asyncio.create_task(seq.run_forever())
        seq.request_start()

        async with _auto_trigger(seq):
            await asyncio.sleep(0.1)

        assert seq.executed == ["step1", "step2", "step3"]
        assert seq.is_running is False
        assert seq.progress["step_index"] == 3
        task.cancel()

    async def test_例外が出ても常駐ループは次の開始要求を受け付ける(self, monkeypatch):
        seq = SampleSequence()
        calls = {"n": 0}

        async def boom() -> None:
            calls["n"] += 1
            raise RuntimeError("シーケンス内部の異常")

        monkeypatch.setattr(seq, "run", boom)
        task = asyncio.create_task(seq.run_forever())

        seq.request_start()
        await asyncio.sleep(0.03)
        seq.request_start()
        await asyncio.sleep(0.03)

        assert calls["n"] == 2
        task.cancel()

    async def test_実行中に届いた2通目の開始要求は停止後に発火しない(self):
        seq = SampleSequence()
        task = asyncio.create_task(seq.run_forever())
        seq.request_start()
        await asyncio.sleep(0.05)
        assert seq.is_running is True

        seq.request_start()
        await asyncio.sleep(0.05)
        executed_at_stop = list(seq.executed)

        seq.request_stop()
        await asyncio.sleep(0.1)

        assert seq.is_running is False
        assert seq.executed == executed_at_stop
        assert seq.progress["step_index"] == 0
        task.cancel()

    async def test_未処理の開始要求を破棄できる(self):
        seq = SampleSequence()
        seq.request_start()
        seq.discard_pending_start()

        task = asyncio.create_task(seq.run_forever())
        await asyncio.sleep(0.05)

        assert seq.executed == []
        task.cancel()

    # asyncio.Event.set() は待機中の future をその場で解決するので、直後の clear() では
    # 「起きることが決まった 1 回」を取り消せない。
    async def test_常駐ループが待っている最中の破棄も効く(self):
        seq = SampleSequence()
        task = asyncio.create_task(seq.run_forever())
        await asyncio.sleep(0.02)

        seq.request_start()
        seq.discard_pending_start()
        await asyncio.sleep(0.05)

        assert seq.executed == []
        assert seq.is_running is False
        task.cancel()

    async def test_未処理のジャンプ要求も破棄される(self):
        seq = SampleSequence()
        seq.request_jump(2)
        seq.discard_pending_start()

        task = asyncio.create_task(seq.run_forever())
        seq.request_start()
        await asyncio.sleep(0.05)

        assert seq.executed == ["step1"]
        task.cancel()

    async def test_is_running_は_run_中だけ真(self):
        seq = SampleSequence()
        assert seq.is_running is False

        task = asyncio.create_task(seq.run())
        await asyncio.sleep(0.05)
        assert seq.is_running is True

        seq.request_stop()
        await task
        assert seq.is_running is False


class TestStepLog:
    @staticmethod
    def _step_lines(caplog) -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.name == "lib.sequence.engine" and "完走" not in r.getMessage()
        ]

    async def test_ステップごとに1行出る(self, caplog):
        seq = SampleSequence()

        with caplog.at_level(logging.INFO, logger="lib.sequence.engine"):
            async with _auto_trigger(seq):
                await seq.run()

        lines = self._step_lines(caplog)
        assert len(lines) == 3
        assert lines[0] == "[sample] 1/3 ステップ1"
        assert lines[1] == "[sample] 2/3 ステップ2"
        assert lines[2] == "[sample] 3/3 ステップ3"

    async def test_トリガー待ちの間はまだ出ない(self, caplog):
        seq = SampleSequence()

        with caplog.at_level(logging.INFO, logger="lib.sequence.engine"):
            task = asyncio.create_task(seq.run())
            await asyncio.sleep(0.05)

            assert seq.waiting_trigger is True
            assert self._step_lines(caplog) == ["[sample] 1/3 ステップ1"]

            seq.trigger()
            await asyncio.sleep(0.05)

            assert self._step_lines(caplog)[1] == "[sample] 2/3 ステップ2"
            task.cancel()

    async def test_完走で1行出る(self, caplog):
        seq = SampleSequence()

        with caplog.at_level(logging.INFO, logger="lib.sequence.engine"):
            async with _auto_trigger(seq):
                await seq.run()

        completed = [r.getMessage() for r in caplog.records if "完走" in r.getMessage()]
        assert completed == ["[sample] 完走 (3 ステップ)"]

    async def test_途中で停止したら完走の行は出ない(self, caplog):
        seq = SampleSequence()

        with caplog.at_level(logging.INFO, logger="lib.sequence.engine"):
            task = asyncio.create_task(seq.run())
            await asyncio.sleep(0.05)
            seq.request_stop()
            await task

        assert seq.executed == ["step1"]
        assert [r.getMessage() for r in caplog.records if "完走" in r.getMessage()] == []

    async def test_失敗したら完走の行は出ない(self, caplog):
        seq = FailingSequence()

        with caplog.at_level(logging.INFO, logger="lib.sequence.engine"):
            await seq.run()

        assert seq.last_error is not None
        assert [r.getMessage() for r in caplog.records if "完走" in r.getMessage()] == []
