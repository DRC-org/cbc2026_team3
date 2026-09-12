from __future__ import annotations

import asyncio
import math
import struct
import time
from unittest.mock import AsyncMock

import can
from aiohttp.test_utils import TestClient, TestServer

from lib.can_manager import CANManager
from lib.control.position_loop import M3508PositionLoop, make_position_pid
from lib.control.target_refresh import GenericTargetRefresher, QueryDrivenTargetRefresher
from lib.drivers.base import ControlMode
from lib.drivers.edulite05 import Edulite05Driver
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import M3508Driver
from lib.manual import ManualController
from lib.sequence.engine import AxisSyncError, Sequence, step
from lib.sequence.motors import MotorGroup, MotorHandle
from lib.sequence.positions import load_position_table
from tests.fake_can import mock_can_manager, set_last_feedback
from tests.feedback_frames import feed_edulite, feed_m3508
from tests.server_fixtures import RecordingClient, ServerFixture


class _CheckSequence(Sequence):
    def __init__(self) -> None:
        super().__init__("motor_check")
        self.driven: list[str] = []
        self.gate = asyncio.Event()

    @step("1 番目")
    async def first(self) -> None:
        self.driven.append("first")

    @step("2 番目 (ゲート待ち)")
    async def second(self) -> None:
        await self.gate.wait()
        self.driven.append("second")

    @step("3 番目")
    async def third(self) -> None:
        self.driven.append("third")


class _IdleSequence(Sequence):
    @step("何もしない")
    async def noop(self) -> None:
        return


class _RunningSequence(Sequence):
    def __init__(self, name: str = "main_hand") -> None:
        super().__init__(name)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @step("解放されるまで待つ")
    async def hold(self) -> None:
        self.entered.set()
        await self.release.wait()


class _AutoClock:
    def __init__(self, step_s: float = 0.005) -> None:
        self._now = 0.0
        self._step = step_s

    def __call__(self) -> float:
        self._now += self._step
        return self._now


class _LoopProbe:
    def __init__(self, mgr: CANManager, *, bus: str = "bus0", motor_name: str = "lift") -> None:
        self.frames: list[can.Message] = []
        original = mgr.send_to_bus

        async def _counting(bus_name: str, msg: can.Message) -> None:
            self.frames.append(msg)
            await original(bus_name, msg)

        mgr.send_to_bus = _counting  # type: ignore[method-assign]

        self.driver = M3508Driver(motor_name, can_id=4)
        self.loop = M3508PositionLoop(
            mgr, bus, is_estop_active=lambda: False, time_source=_AutoClock()
        )
        self.loop.add_motor(motor_name, self.driver, make_position_pid(kp=1.0))


class _MoveCheckSequence(Sequence):
    def __init__(self, probe: _LoopProbe) -> None:
        super().__init__("motor_check")
        self.probe = probe
        self.moved = asyncio.Event()
        self.gate = asyncio.Event()

    @step("y 軸へ指令する")
    async def drive(self) -> None:
        await self.move_to({"lift": "up"})
        self.moved.set()

    @step("ゲート待ち")
    async def hold(self) -> None:
        await self.gate.wait()


def _slot_current(frame: can.Message, can_id: int) -> int:
    return struct.unpack(">hhhh", frame.data)[can_id - 1]


def _build_with_axis() -> tuple[ServerFixture, _MoveCheckSequence]:
    fx = ServerFixture.build()
    fx.freeze_broadcast()
    fx.add_robot("main_hand", _IdleSequence("main_hand"))

    mgr = fx.can_manager("main_hand")
    probe = _LoopProbe(mgr)
    fx.set_position_loops("main_hand", [probe.loop])
    feed_m3508(probe.driver, deg=0.0)
    set_last_feedback(mgr, {"lift": time.time()})

    sequence = _MoveCheckSequence(probe)
    sequence.bind_positions(
        load_position_table(
            {
                "axes": {"lift": {"unit": "deg", "command_unit": "deg", "tolerance": 1.0e6}},
                "positions": {"lift": {"up": 90.0}},
            },
            source="<test>",
        )
    )
    group = MotorGroup()
    group.add(
        MotorHandle(
            "lift",
            probe.driver,
            mgr,
            target_sink=probe.loop.target_sink("lift"),
        )
    )
    sequence.bind_motors(group)
    fx.set_motor_check_sequence(sequence)
    return fx, sequence


def _generic_refresher(mgr: CANManager) -> tuple[GenericTargetRefresher, MotorHandle]:
    driver = GenericDriver("conveyor", can_id=0x80, control_type=ControlMode.DUTY)
    handle = MotorHandle("conveyor", driver, mgr)
    return GenericTargetRefresher([handle], is_estop_active=lambda: False), handle


class _QueryDrivenMotor:
    def __init__(self, *, step_rad: float = 0.5) -> None:
        self.driver = Edulite05Driver("rotate_l", can_id=1)
        self._step = step_rad
        self.replies = 0

        self.can_manager = mock_can_manager()
        self.can_manager.send = AsyncMock(side_effect=self._reply)  # type: ignore[method-assign]
        self.handle = MotorHandle("rotate_l", self.driver, self.can_manager)
        self.refresher = QueryDrivenTargetRefresher(
            [self.handle],
            self.can_manager,
            interval_s=0.001,
            is_estop_active=lambda: False,
        )
        feed_edulite(self.driver, position=0.0)

    async def _reply(self, _name: str, _msg: can.Message) -> None:
        goal = self.handle.target
        current = self.driver.state.position
        if goal is not None:
            current += math.copysign(min(self._step, abs(goal - current)), goal - current)
        feed_edulite(self.driver, position=current)
        self.replies += 1


class _QueryDrivenCheckSequence(Sequence):
    @step("問い合わせ駆動の軸へ指令する")
    async def drive(self) -> None:
        await self.move_to({"rotate": "pick"})


def _build_with_query_driven() -> tuple[ServerFixture, _QueryDrivenMotor]:
    fx = ServerFixture.build()
    fx.freeze_broadcast()
    fx.add_robot("main_hand", _IdleSequence("main_hand"))

    motor = _QueryDrivenMotor()
    fx.set_target_refreshers("main_hand", [motor.refresher])

    sequence = _QueryDrivenCheckSequence("motor_check")
    sequence.bind_positions(
        load_position_table(
            {
                "axes": {
                    "rotate": {
                        "unit": "rad",
                        "command_unit": "rad",
                        "tolerance": 0.05,
                        "timeout_s": 0.3,
                        "motors": {"rotate_l": {"scale": 1.0, "offset": 0.0}},
                    }
                },
                "positions": {"rotate": {"pick": 2.0}},
            },
            source="<test>",
        )
    )
    group = MotorGroup()
    group.add(motor.handle)
    sequence.bind_motors(group)
    fx.set_motor_check_sequence(sequence)
    return fx, motor


def _manual_controller() -> ManualController:
    table = load_position_table(
        {
            "axes": {"gripper": {"unit": "deg", "command_unit": "deg"}},
            "positions": {"gripper": {"open": 5.0, "closed": 0.0}},
        },
        source="<test>",
    )
    mgr = mock_can_manager()
    group = MotorGroup()
    group.add(MotorHandle("gripper", GenericDriver("gripper", can_id=1), mgr))
    return ManualController(group, table)


def _build(
    *,
    check: Sequence | None = None,
    robots: tuple[str, ...] = ("main_hand", "sub_hand"),
    sequences: dict[str, Sequence] | None = None,
    manual: bool = False,
) -> tuple[ServerFixture, _CheckSequence]:
    fx = ServerFixture.build()
    fx.freeze_broadcast()
    for name in robots:
        seq = (sequences or {}).get(name) or _IdleSequence(name)
        fx.add_robot(name, seq, manual=_manual_controller() if manual else None)

    sequence = check if check is not None else _CheckSequence()
    fx.set_motor_check_sequence(sequence)
    return fx, sequence  # type: ignore[return-value]


class TestStartGate:
    async def test_登録されていなければ起動できない(self) -> None:
        fx = ServerFixture.build()
        fx.add_robot("main_hand", _IdleSequence("main_hand"))

        assert await fx.start_motor_check() is False
        assert "読み込まれていません" in (fx.motor_check_error() or "")
        assert fx.motor_check_state()["available"] is False

    async def test_試合中は起動できない(self) -> None:
        fx, sequence = _build()
        fx.enter_match()

        assert await fx.start_motor_check() is False
        assert sequence.driven == []
        assert "試合中" in (fx.motor_check_error() or "")

    async def test_緊急停止中は起動できない(self) -> None:
        fx, sequence = _build()
        await fx.activate_e_stop()

        assert await fx.start_motor_check() is False
        assert sequence.driven == []
        assert "緊急停止中" in (fx.motor_check_error() or "")

    async def test_どちらかが手動なら起動できない(self) -> None:
        fx, sequence = _build(manual=True)
        await fx.command({"type": "set_operation_mode", "robot": "sub_hand", "mode": "manual"})

        assert await fx.start_motor_check() is False
        assert sequence.driven == []
        assert "sub_hand" in (fx.motor_check_error() or "")

    async def test_どちらかのシーケンス実行中は起動できない(self) -> None:
        running = _RunningSequence("sub_hand")
        fx, sequence = _build(sequences={"sub_hand": running})
        fx.enter_match()
        task = asyncio.create_task(running.run_forever())
        running.request_start()
        await asyncio.wait_for(running.entered.wait(), timeout=1.0)
        fx.match.match_reset()

        assert await fx.start_motor_check() is False
        assert sequence.driven == []
        assert "sub_hand" in (fx.motor_check_error() or "")

        running.release.set()
        running.request_stop()
        task.cancel()

    async def test_二重起動は拒否される(self) -> None:
        fx, sequence = _build()
        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_running()

        assert await fx.start_motor_check() is False
        assert "既に" in (fx.motor_check_error() or "")

        sequence.gate.set()
        await fx.wait_motor_check_idle()

    async def test_起動できるなら全ステップを流す(self) -> None:
        fx, sequence = _build()
        sequence.gate.set()

        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_idle()

        assert sequence.driven == ["first", "second", "third"]


class TestAbort:
    async def test_中断すると残りを駆動しない(self) -> None:
        fx, sequence = _build()
        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_running()

        fx.abort_motor_check()
        sequence.gate.set()
        await fx.wait_motor_check_idle()

        assert sequence.driven == ["first", "second"]

    async def test_緊急停止でも中断される(self) -> None:
        fx, sequence = _build()
        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_running()

        await fx.activate_e_stop()
        sequence.gate.set()
        await fx.wait_motor_check_idle()

        assert "third" not in sequence.driven

    async def test_中断後の再起動は先頭から流す(self) -> None:
        fx, sequence = _build()
        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_running()
        fx.abort_motor_check()
        sequence.gate.set()
        await fx.wait_motor_check_idle()

        sequence.driven.clear()
        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_idle()

        assert sequence.driven == ["first", "second", "third"]


class _StallingPausable:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.entered = asyncio.Event()
        self.resumed = False

    async def pause(self, *, reason: str = "") -> None:
        self.entered.set()
        await self.release.wait()

    def resume(self) -> None:
        self.resumed = True


class TestStartupWindow:
    @staticmethod
    def _install(fx: ServerFixture, stall: _StallingPausable) -> None:
        fx.set_motor_check_pausables([stall])

    async def test_窓の中の緊急停止で一歩も駆動しない(self) -> None:
        fx, sequence = _build()
        stall = _StallingPausable()
        self._install(fx, stall)
        sequence.gate.set()

        assert await fx.start_motor_check() is True
        await asyncio.wait_for(stall.entered.wait(), timeout=1.0)

        await fx.activate_e_stop()
        stall.release.set()
        await fx.wait_motor_check_idle()

        assert sequence.driven == []
        assert "緊急停止" in (fx.motor_check_error() or "")

    async def test_窓の中の中断で一歩も駆動しない(self) -> None:
        fx, sequence = _build()
        stall = _StallingPausable()
        self._install(fx, stall)
        sequence.gate.set()

        assert await fx.start_motor_check() is True
        await asyncio.wait_for(stall.entered.wait(), timeout=1.0)

        fx.abort_motor_check()
        stall.release.set()
        await fx.wait_motor_check_idle()

        assert sequence.driven == []

    async def test_窓の中でも復帰は必ず走る(self) -> None:
        fx, _ = _build()
        stall = _StallingPausable()
        self._install(fx, stall)

        assert await fx.start_motor_check() is True
        await asyncio.wait_for(stall.entered.wait(), timeout=1.0)
        await fx.activate_e_stop()
        stall.release.set()
        await fx.wait_motor_check_idle()

        assert stall.resumed is True


class TestExclusion:
    async def test_実行中も位置制御ループは回り続ける(self) -> None:
        fx, sequence = _build()
        probe = _LoopProbe(fx.can_manager("main_hand"))
        fx.set_position_loops("main_hand", [probe.loop])

        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_running()

        assert probe.loop.is_paused is False
        before = len(probe.frames)
        await probe.loop.step()
        assert len(probe.frames) == before + 1

        sequence.gate.set()
        await fx.wait_motor_check_idle()

    async def test_実行中の指令が電流指令になる(self) -> None:
        fx, sequence = _build_with_axis()
        probe = sequence.probe

        assert await fx.start_motor_check() is True
        await asyncio.wait_for(sequence.moved.wait(), timeout=1.0)

        before = len(probe.frames)
        await probe.loop.step()
        assert len(probe.frames) == before + 1
        assert _slot_current(probe.frames[-1], probe.driver.can_id) != 0

        sequence.gate.set()
        await fx.wait_motor_check_idle()

    async def test_実行中も目標値再送は止まらない(self) -> None:
        fx, sequence = _build()
        mgr = fx.can_manager("main_hand")
        refresher, handle = _generic_refresher(mgr)
        fx.set_target_refreshers("main_hand", [refresher])
        await handle.set_target(ControlMode.DUTY, 0.5)

        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_running()

        assert refresher.is_paused is False
        before = mgr.send.await_count
        await refresher.step()
        assert mgr.send.await_count == before + 1

        sequence.gate.set()
        await fx.wait_motor_check_idle()

    async def test_問い合わせ駆動のモータは実行中もフィードバックを更新し続ける(self) -> None:
        fx, motor = _build_with_query_driven()
        motor.refresher.start()
        try:
            assert await fx.start_motor_check() is True
            await fx.wait_motor_check_idle()
        finally:
            await motor.refresher.stop()

        assert motor.replies > 1
        assert fx.motor_check_error() is None
        assert fx.motor_check_state()["last_error"] is None

    async def test_両ロボットの周期タスクを止めない(self) -> None:
        fx, sequence = _build()
        refreshers = {}
        for name in ("main_hand", "sub_hand"):
            refresher, _ = _generic_refresher(fx.can_manager(name))
            refreshers[name] = refresher
            fx.set_target_refreshers(name, [refresher])

        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_running()

        assert [r.is_paused for r in refreshers.values()] == [False, False]

        sequence.gate.set()
        await fx.wait_motor_check_idle()

        assert [r.is_paused for r in refreshers.values()] == [False, False]

    async def test_本番の一覧は空である(self) -> None:
        fx, _ = _build()
        probe = _LoopProbe(fx.can_manager("main_hand"))
        fx.set_position_loops("main_hand", [probe.loop])
        refresher, _handle = _generic_refresher(fx.can_manager("main_hand"))
        fx.set_target_refreshers("main_hand", [refresher])

        assert fx.motor_check_pausables() == []

    async def test_停止中の手動切替を拒否する(self) -> None:
        fx, sequence = _build(manual=True)
        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_running()

        await fx.command({"type": "set_operation_mode", "robot": "main_hand", "mode": "manual"})
        assert fx.operation_mode("main_hand") == "sequence"

        sequence.gate.set()
        await fx.wait_motor_check_idle()


class _RecordingPausable:
    def __init__(self) -> None:
        self.paused = False
        self.resumed = False

    async def pause(self, *, reason: str = "") -> None:
        self.paused = True

    def resume(self) -> None:
        self.resumed = True


class TestPauseContract:
    async def test_渡された対象は起動時に止める(self) -> None:
        fx, sequence = _build()
        pausable = _RecordingPausable()
        fx.set_motor_check_pausables([pausable])

        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_running()

        assert pausable.paused is True

        sequence.gate.set()
        await fx.wait_motor_check_idle()

    async def test_終了後に復帰させる(self) -> None:
        fx, sequence = _build()
        pausable = _RecordingPausable()
        fx.set_motor_check_pausables([pausable])
        sequence.gate.set()

        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_idle()

        assert pausable.resumed is True

    async def test_中断で降りても復帰させる(self) -> None:
        fx, sequence = _build()
        pausable = _RecordingPausable()
        fx.set_motor_check_pausables([pausable])

        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_running()
        fx.abort_motor_check()
        sequence.gate.set()
        await fx.wait_motor_check_idle()

        assert pausable.resumed is True

    async def test_例外で降りても復帰させる(self) -> None:
        class _RaisingSequence(Sequence):
            @step("必ず失敗する")
            async def boom(self) -> None:
                raise RuntimeError("テスト用例外")

        fx, _ = _build(check=_RaisingSequence("motor_check"))
        pausable = _RecordingPausable()
        fx.set_motor_check_pausables([pausable])

        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_idle()

        assert pausable.resumed is True


class TestBroadcast:
    async def test_進捗と結果を_1_通で運ぶ(self) -> None:
        fx, sequence = _build()
        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_running()

        state = fx.motor_check_state()
        assert state["type"] == "motor_check_state"
        assert state["running"] is True
        assert state["total_steps"] == 3
        assert state["current_step"] == "2 番目 (ゲート待ち)"
        assert [s["label"] for s in state["steps"]] == ["1 番目", "2 番目 (ゲート待ち)", "3 番目"]

        sequence.gate.set()
        await fx.wait_motor_check_idle()
        assert fx.motor_check_state()["running"] is False

    async def test_失敗したステップと理由を状態に載せる(self) -> None:

        class _FailingCheck(Sequence):
            @step("グリッパ 開閉")
            async def grip(self) -> None:
                raise AxisSyncError("軸内のモータ位置がずれています (y_axis: 偏差 3.0 > 許容 2.0)")

        fx, _ = _build(check=_FailingCheck("motor_check"))

        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_idle()

        state = fx.motor_check_state()
        assert state["last_error"] == {
            "step_index": 0,
            "step": "グリッパ 開閉",
            "message": "軸内のモータ位置がずれています (y_axis: 偏差 3.0 > 許容 2.0)",
            "limit_related": False,
        }
        assert "グリッパ 開閉" in (state["error"] or "")
        assert "ずれています" in (state["error"] or "")

    async def test_成功した動作確認は理由を残さない(self) -> None:
        fx, sequence = _build()

        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_running()
        sequence.gate.set()
        await fx.wait_motor_check_idle()

        state = fx.motor_check_state()
        assert state["last_error"] is None
        assert state["error"] is None

    async def test_起動できない理由を状態に載せる(self) -> None:
        fx, _ = _build()
        fx.enter_match()

        assert "試合中" in (fx.motor_check_state()["blocked_reason"] or "")

    async def test_除外したステップを状態に載せる(self) -> None:

        class _PartialCheck(Sequence):
            @step("メインハンド y 軸", axes={"y_axis"})
            async def main_y(self) -> None:
                return

            @step("サブハンド 昇降", axes={"sub_lift"})
            async def sub_lift(self) -> None:
                return

        sequence = _PartialCheck("motor_check")
        sequence.restrict_to_axes({"y_axis"})
        fx, _ = _build(check=sequence)

        state = fx.motor_check_state()
        assert [s["label"] for s in state["steps"]] == ["メインハンド y 軸"]
        assert state["excluded_steps"] == [
            {"step": "サブハンド 昇降", "missing_axes": ["sub_lift"]}
        ]

    async def test_除外が無ければ空欄として載せる(self) -> None:
        fx, _ = _build()

        assert fx.motor_check_state()["excluded_steps"] == []

    async def test_変化が無ければ配信しない(self) -> None:
        fx, _ = _build()
        client = RecordingClient()
        fx.attach_clients(client)

        await fx.publish_motor_check_state()
        first = len(client.sent)
        assert first == 1

        await fx.publish_motor_check_state()
        assert len(client.sent) == first


class TestHttpEndpoints:
    async def test_post_で起動できる(self) -> None:
        fx, sequence = _build()
        sequence.gate.set()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/motor_check")
            assert resp.status == 200
            assert (await resp.json())["started"] is True

        await fx.wait_motor_check_idle()
        assert sequence.driven == ["first", "second", "third"]

    async def test_拒否は_409_と理由を返す(self) -> None:
        fx, _ = _build()
        fx.enter_match()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/motor_check")
            assert resp.status == 409
            body = await resp.json()
            assert "試合中" in (body["reason"] or "")

    async def test_get_で現在状態を読める(self) -> None:
        fx, _ = _build()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/motor_check")
            assert resp.status == 200
            body = await resp.json()
            assert body["available"] is True
            assert body["running"] is False


class TestManualExclusion:
    async def test_手動中は起動を拒否し理由を返す(self) -> None:
        fx, sequence = _build(manual=True)
        await fx.command({"type": "set_operation_mode", "robot": "main_hand", "mode": "manual"})

        assert await fx.start_motor_check() is False
        assert sequence.driven == []
        assert "手動" in (fx.motor_check_error() or "")

    async def test_手動を抜ければ起動できる(self) -> None:
        fx, sequence = _build(manual=True)
        await fx.command({"type": "set_operation_mode", "robot": "main_hand", "mode": "manual"})
        await fx.command({"type": "set_operation_mode", "robot": "main_hand", "mode": "sequence"})
        sequence.gate.set()

        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_idle()
        assert sequence.driven == ["first", "second", "third"]
