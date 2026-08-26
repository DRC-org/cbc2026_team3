from __future__ import annotations

import asyncio

from lib.sequence.engine import Sequence, StepInfo, step


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


class TestStepDecorator:
    def test_step_decorator_sets_attributes(self):
        assert SampleSequence.step1._step_label == "ステップ1"
        assert SampleSequence.step1._step_require_trigger is False
        assert SampleSequence.step2._step_label == "ステップ2"
        assert SampleSequence.step2._step_require_trigger is True


class TestStepsCollection:
    def test_steps_collected_in_order(self):
        seq = SampleSequence()
        assert len(seq._steps) == 3
        assert seq._steps[0] == StepInfo(
            label="ステップ1", method_name="step1", require_trigger=False
        )
        assert seq._steps[1] == StepInfo(
            label="ステップ2", method_name="step2", require_trigger=True
        )
        assert seq._steps[2] == StepInfo(
            label="ステップ3", method_name="step3", require_trigger=False
        )


class TestRun:
    async def test_run_executes_all_steps(self):
        seq = SampleSequence()

        async def auto_trigger():
            while seq._running:
                if seq._waiting_trigger:
                    seq.trigger()
                await asyncio.sleep(0.01)

        trigger_task = asyncio.create_task(auto_trigger())
        await seq.run()
        trigger_task.cancel()

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
        assert seq._waiting_trigger is True
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

        assert seq._current_index == 1
        assert seq._waiting_trigger is True

        seq.trigger()
        await asyncio.sleep(0.05)

        assert seq.executed == ["step1", "step2", "step3"]
        task.cancel()

    async def test_run_completes(self):
        seq = SampleSequence()

        async def auto_trigger():
            while seq._running:
                if seq._waiting_trigger:
                    seq.trigger()
                await asyncio.sleep(0.01)

        trigger_task = asyncio.create_task(auto_trigger())
        await seq.run()
        trigger_task.cancel()

        assert seq._running is False


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

        async def auto_trigger():
            while seq._running:
                if seq._waiting_trigger:
                    seq.trigger()
                await asyncio.sleep(0.01)

        trigger_task = asyncio.create_task(auto_trigger())
        await seq.run()
        trigger_task.cancel()

        assert seq._current_index == 3
        await seq.reset()
        assert seq._current_index == 0
        assert seq._running is False
        assert seq._waiting_trigger is False


class TestCallback:
    async def test_on_step_change_callback(self):
        seq = SampleSequence()
        callback_args: list[dict] = []
        seq.set_on_step_change(lambda progress: callback_args.append(progress))

        async def auto_trigger():
            while seq._running:
                if seq._waiting_trigger:
                    seq.trigger()
                await asyncio.sleep(0.01)

        trigger_task = asyncio.create_task(auto_trigger())
        await seq.run()
        trigger_task.cancel()

        assert len(callback_args) == 3
        assert callback_args[0]["current_step"] == "ステップ1"
        assert callback_args[1]["current_step"] == "ステップ2"
        assert callback_args[2]["current_step"] == "ステップ3"


class TestLifecycle:
    """開始要求待ち → 実行 → 停止後の巻き戻し、という常駐ループの公開 API。

    このループをサーバー側に写すと「停止後にどこへ戻るか」がシーケンスの外に置かれ、
    シーケンス単体では正しい状態へ戻れなくなる。
    """

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

        # 巻き戻った状態から再び先頭を実行できる
        seq.request_start()
        await asyncio.sleep(0.05)
        assert seq.executed == ["step1", "step1"]
        task.cancel()

    async def test_完走後は位置を保持する(self):
        seq = SampleSequence()
        task = asyncio.create_task(seq.run_forever())
        seq.request_start()

        async def auto_trigger():
            while True:
                if seq.waiting_trigger:
                    seq.trigger()
                await asyncio.sleep(0.01)

        trigger_task = asyncio.create_task(auto_trigger())
        await asyncio.sleep(0.1)
        trigger_task.cancel()

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

    async def test_未処理の開始要求を破棄できる(self):
        seq = SampleSequence()
        seq.request_start()
        seq.discard_pending_start()

        task = asyncio.create_task(seq.run_forever())
        await asyncio.sleep(0.05)

        assert seq.executed == []
        task.cancel()

    async def test_未処理のジャンプ要求も破棄される(self):
        seq = SampleSequence()
        seq.request_jump(2)
        seq.discard_pending_start()

        task = asyncio.create_task(seq.run_forever())
        seq.request_start()
        await asyncio.sleep(0.05)

        # 破棄されていなければ step3 から走り出す
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
