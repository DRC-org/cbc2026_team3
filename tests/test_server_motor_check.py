"""統合動作確認のサーバー側 (起動ゲート・排他・中断・配信)。

**両ハンドを 1 本のシーケンスで駆動する**ので、ゲートも排他も全ロボットに対して
掛かる。片方だけ見ていると、確認中にもう一方が手動で動かされて干渉する。

シーケンスそのものが何を動かすかは `tests/test_motor_check_sequence.py` が見る。
ここで見るのは **いつ走ってよくて、いつ止まるか** だけ。

駆動の有無は代役シーケンス (`_CheckSequence`) の記録で確かめる。実モータを繋ぐと
「止まっていること」の確認が送信フレームの不在という弱い形になり、たまたま
送っていないだけの状態と区別が付かない。
"""

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

# ---------------------------------------------------------------------- #
#  テスト用ダミー実装
# ---------------------------------------------------------------------- #


class _CheckSequence(Sequence):
    """統合動作確認シーケンスの代役。

    実際に軸を動かす代わりに、どのステップまで進んだかを記録する。
    「止めたのに駆動された」を `driven` の中身で直接見られる。
    """

    def __init__(self) -> None:
        super().__init__("motor_check")
        self.driven: list[str] = []
        # 2 番目のステップで待たせるゲート。実行中の状態を観測する窓を作る
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
    """通常シーケンスの代役 (何もしない)。"""

    @step("何もしない")
    async def noop(self) -> None:
        return


class _RunningSequence(Sequence):
    """走らせたまま止められる通常シーケンスの代役。"""

    def __init__(self, name: str = "main_hand") -> None:
        super().__init__(name)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @step("解放されるまで待つ")
    async def hold(self) -> None:
        self.entered.set()
        await self.release.wait()


class _AutoClock:
    """呼ばれるたびに一定量進む時計。位置制御ループの周期を決定的にする。"""

    def __init__(self, step_s: float = 0.005) -> None:
        self._now = 0.0
        self._step = step_s

    def __call__(self) -> float:
        self._now += self._step
        return self._now


class _LoopProbe:
    """位置制御ループ + そのバスへの送信フレーム記録。"""

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
    """M3508 の軸を `move_to` で動かす動作確認の代役。

    軸を動かす経路を `move_to` だけにしてあるのは実物と同じ性質を保つため。
    M3508 は電流指令しか受け付けないので、この指令は位置制御ループを通らなければ
    1 通の CAN フレームにもならない。
    """

    def __init__(self, probe: _LoopProbe) -> None:
        super().__init__("motor_check")
        self.probe = probe
        self.moved = asyncio.Event()
        # 指令の後も実行中のまま観測するための窓
        self.gate = asyncio.Event()

    @step("y 軸へ指令する")
    async def drive(self) -> None:
        await self.move_to({"lift": "up"})
        self.moved.set()

    @step("ゲート待ち")
    async def hold(self) -> None:
        await self.gate.wait()


def _slot_current(frame: can.Message, can_id: int) -> int:
    """C620 の電流指令フレームから 1 モータ分のスロットを読む。"""
    return struct.unpack(">hhhh", frame.data)[can_id - 1]


def _build_with_axis() -> tuple[ServerFixture, _MoveCheckSequence]:
    """M3508 の軸 1 本を持つ構成。動作確認はその軸へ `move_to` する。

    到達判定の許容差を広く取ってあるのは、見たいのが到達ではなく
    **指令が電流指令として出ること** だけだから。
    """
    fx = ServerFixture.build()
    fx.freeze_broadcast()
    fx.add_robot("main_hand", _IdleSequence("main_hand"))

    mgr = fx.can_manager("main_hand")
    probe = _LoopProbe(mgr)
    fx.set_position_loops("main_hand", [probe.loop])
    # 実測位置が読めなければループは途絶として電流 0 に落とす。原点に居る機体を作る
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
            # M3508 は電流指令しか受け付けないため、目標値は PC 側 PID へ迂回する
            target_sink=probe.loop.target_sink("lift"),
        )
    )
    sequence.bind_motors(group)
    fx.set_motor_check_sequence(sequence)
    return fx, sequence


def _generic_refresher(mgr: CANManager) -> tuple[GenericTargetRefresher, MotorHandle]:
    """自作モタドラ 1 台ぶんの目標値再送と、その指令口。

    空のハンドル一覧で組むと「送っていないこと」と「送る相手が居ないこと」が
    区別できず、pause されていても緑になる。
    """
    driver = GenericDriver("conveyor", can_id=0x80, control_type=ControlMode.DUTY)
    handle = MotorHandle("conveyor", driver, mgr)
    return GenericTargetRefresher([handle], is_estop_active=lambda: False), handle


class _QueryDrivenMotor:
    """問い合わせ駆動のモータ (EDULITE 05) の模型。

    **自分の CAN ID 宛のフレームを受けたときにしか状態を返さない。** 1 通につき
    1 歩だけ目標へ近づいて応答するので、`move_to` の指令 1 通では到達しない ——
    目標値再送が回り続けて初めて到達を観測できる、という実機と同じ関係になる。
    """

    def __init__(self, *, step_rad: float = 0.5) -> None:
        self.driver = Edulite05Driver("rotate_l", can_id=1)
        self._step = step_rad
        #: 返したフィードバックの通数。指令 1 通ぶん (=1) で止まれば到達しない
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
    """問い合わせ駆動の軸へ `move_to` する動作確認の代役。"""

    @step("問い合わせ駆動の軸へ指令する")
    async def drive(self) -> None:
        await self.move_to({"rotate": "pick"})


def _build_with_query_driven() -> tuple[ServerFixture, _QueryDrivenMotor]:
    """問い合わせ駆動の軸 1 本を持つ構成。動作確認はその軸へ `move_to` する。"""
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
                        # 止めた場合に「到達しない」が短時間で確定する値。
                        # 長くしても症状は同じで、テストの実行時間だけが伸びる
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
    """サーバー + 登録済みロボット + 統合動作確認シーケンス。"""
    fx = ServerFixture.build()
    fx.freeze_broadcast()
    for name in robots:
        seq = (sequences or {}).get(name) or _IdleSequence(name)
        fx.add_robot(name, seq, manual=_manual_controller() if manual else None)

    sequence = check if check is not None else _CheckSequence()
    fx.set_motor_check_sequence(sequence)
    return fx, sequence  # type: ignore[return-value]


# ---------------------------------------------------------------------- #
#  起動ゲート
# ---------------------------------------------------------------------- #


class TestStartGate:
    async def test_登録されていなければ起動できない(self) -> None:
        """位置定数を読めていない構成 (机上ベンチ) では登録そのものをしない。

        理由を出さずに黙って何も起きないと、操縦者は押し直し続けることになる。
        """
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
        """**片方だけ見てはならない。** 1 本のシーケンスが両機を動かすので、
        もう一方が手動のままだと確認の途中で干渉する。"""
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
        fx.match.match_reset()  # 試合中ゲートを外して、シーケンス実行中だけを残す

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


# ---------------------------------------------------------------------- #
#  中断
# ---------------------------------------------------------------------- #


class TestAbort:
    async def test_中断すると残りを駆動しない(self) -> None:
        fx, sequence = _build()
        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_running()

        fx.abort_motor_check()
        sequence.gate.set()
        await fx.wait_motor_check_idle()

        # 走行中のステップは完了まで待つ。次へは進まない
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
        """途中から再開すると、そこまでの姿勢を前提にしたステップを飛ばして動かす。"""
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


# ---------------------------------------------------------------------- #
#  起動の窓
#
#  タスクを作ってから run() が駆動を始めるまでのあいだに停止が届きうる。
#  `Sequence.run()` は冒頭で停止イベントを clear するので、**サーバー側が
#  自前のフラグで覚えていないと、その 1 通が消えて全ステップが駆動される**。
# ---------------------------------------------------------------------- #


class _StallingPausable:
    """pause() が解放されるまで返らない疑似 pausable。起動の窓を作る。"""

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
        """窓を確実に開くための代役を挿す。

        **本番の pause 対象は空**なので、窓は「タスク生成から `run()` が
        スケジュールされるまで」の一瞬になり、テストからは掴めない。窓そのものは
        実在するので、`pause()` で待たせる代役を挿して固定する。
        """
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
        """`Sequence.run()` の停止イベントに任せると、run() 冒頭の clear で消える。"""
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
        """1 台も駆動せずに降りても、止めた送信経路は戻さなければならない。"""
        fx, _ = _build()
        stall = _StallingPausable()
        self._install(fx, stall)

        assert await fx.start_motor_check() is True
        await asyncio.wait_for(stall.entered.wait(), timeout=1.0)
        await fx.activate_e_stop()
        stall.release.set()
        await fx.wait_motor_check_idle()

        assert stall.resumed is True


# ---------------------------------------------------------------------- #
#  送信経路の排他
# ---------------------------------------------------------------------- #


class TestExclusion:
    async def test_実行中も位置制御ループは回り続ける(self) -> None:
        """**動作確認は M3508 をこのループ経由でしか動かせない。**

        止めると `move_to` の目標だけが設定されて電流が 1 通も出ず、偏差が
        残ったまま到達待ちがタイムアウトする。復帰した瞬間に残った目標へ
        向かって機体が動き出すので、操縦者は失敗表示の直後に動く機体を見る。
        """
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
        """目標が設定されただけでは M3508 は動かない。

        実機では目標 520mm に対して実測 2mm・飽和なしのまま到達待ちが
        タイムアウトした。ループが 1 周期も回っていなければ、偏差がいくら
        あっても電流指令は 0 のままになる。
        """
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
        """**止めると自作モタドラのウォッチドッグが確認したい当のものを消す。**

        3 枚とも `command_timeout_ms` 500ms で出力を落とす。`conveyor` と
        ポンプの `settle_s` は 0.5s なので、目視・聴音で確認している最中に
        出力が切れる (「回っていない」を目で見ることになる)。
        """
        fx, sequence = _build()
        mgr = fx.can_manager("main_hand")
        refresher, handle = _generic_refresher(mgr)
        fx.set_target_refreshers("main_hand", [refresher])
        # 目標を持たないハンドルへは 1 通も送らない (起動直後の暴発防止) ので、
        # 「送り続けている」を見るにはまず目標を持たせる必要がある
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
        """**EDULITE 05 / DM3520 は PC が黙ると 1 通も状態を返さない。**

        `AxisHandle.wait_reached` はドライバのキャッシュを polling するだけで
        再送しないので、目標値再送を止めるとそのモータ宛へ飛ぶのは `move_to` の
        指令 1 通だけになる。返るフィードバックも**動き出す前の位置 1 通**で
        以後は更新されず、実際に動ききっても到達判定を通らない。

        実機で `rotate` の零点確定が通ったのは `HomingRunner` が 1 歩ごとに
        指令を出して毎回応答を得ていたからで、`move_to` は同じ形にならない。
        """
        fx, motor = _build_with_query_driven()
        motor.refresher.start()
        try:
            assert await fx.start_motor_check() is True
            await fx.wait_motor_check_idle()
        finally:
            await motor.refresher.stop()

        # 指令 1 通ぶんしか応答が無ければ、位置は 1 歩目で止まったまま到達しない
        assert motor.replies > 1
        assert fx.motor_check_error() is None
        assert fx.motor_check_state()["last_error"] is None

    async def test_両ロボットの周期タスクを止めない(self) -> None:
        """**片方だけ見てはならない。** 1 本のシーケンスが両機を動かすので、
        片方を止めればそのハンドの軸だけが確認できないまま失敗する。"""
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
        """**止める対象は 1 つも無い。**

        位置制御ループも目標値再送も、動作確認が軸を動かす経路そのものである
        (`RobotServer._motor_check_pausables`)。以降のテストが挿す代役は、
        本番に存在しない窓を再現するためのもの。
        """
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


# ---------------------------------------------------------------------- #
#  pause / resume の契約
#
#  本番の pause 対象は空 (`TestExclusion.test_本番の一覧は空である`) だが、
#  `Pausable` の口そのものは残る —— `safety.*.paused` が WS 契約に載っており、
#  送信経路を一時的に別の主が握る用途は今後も起こりうる。**止めたものを必ず
#  戻す**という保証は、対象が空になっても失ってはならない。代役を挿して見る。
# ---------------------------------------------------------------------- #


class _RecordingPausable:
    """pause / resume の呼ばれ方だけを記録する代役。"""

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

        # 戻さないと、止めた送信経路がそのまま止まったまま試合へ入る
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


# ---------------------------------------------------------------------- #
#  配信
# ---------------------------------------------------------------------- #


class TestBroadcast:
    async def test_進捗と結果を_1_通で運ぶ(self) -> None:
        """4 種類に分けると、途中の 1 通を落とした画面が復旧しない。"""
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
        """**動作確認が失敗したことが画面に出なければ、確認そのものが意味を失う。**

        到達タイムアウトも左右ずれもシーケンスのステップ単位 try で握られるため、
        載せない限り `error:None` / `step_index:0` のまま「一度も実行していない」と
        同じ表示に戻る。`config/checklist.yaml` の「アクチュエータ動作確認 完了」は、
        その誤表示のままチェックが付く経路になる。
        """

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
        }
        # 表示 1 行 (error) にも必ず出す。既存 UI はここしか読んでいない
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
        """UI は理由を説明するだけ。可否をクライアントで導出し直させない。"""
        fx, _ = _build()
        fx.enter_match()

        assert "試合中" in (fx.motor_check_state()["blocked_reason"] or "")

    async def test_除外したステップを状態に載せる(self) -> None:
        """**除外を黙って行うと、動作確認そのものが意味を失う。**

        構成に無い軸のステップが配信から消えるだけだと、サブハンド不在で減って
        いるのか、本番構成なのに config の書き忘れで減っているのかを操縦者が
        区別できない。どちらも「全ステップ成功」として同じに見え、症状は
        「動作確認は通ったのに試合でその軸だけ動かない」になる。
        """

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
        # ステップ表からは「減っていること」を読めない。欠けている軸まで載せる
        assert [s["label"] for s in state["steps"]] == ["メインハンド y 軸"]
        assert state["excluded_steps"] == [
            {"step": "サブハンド 昇降", "missing_axes": ["sub_lift"]}
        ]

    async def test_除外が無ければ空欄として載せる(self) -> None:
        """欄そのものを落とすと、UI は「除外なし」と「読めていない」を混同する。"""
        fx, _ = _build()

        assert fx.motor_check_state()["excluded_steps"] == []

    async def test_変化が無ければ配信しない(self) -> None:
        """停止中は何も変わらない。毎ティック流すと UI 側の再描画抑制が効かなくなる。"""
        fx, _ = _build()
        client = RecordingClient()
        fx.attach_clients(client)

        await fx.publish_motor_check_state()
        first = len(client.sent)
        assert first == 1

        await fx.publish_motor_check_state()
        assert len(client.sent) == first


# ---------------------------------------------------------------------- #
#  HTTP 経路
#
#  WS が使えない環境向けの代替。**`handle_command` を通らない**ので、
#  ゲートは `_start_motor_check` 側にも要る。
# ---------------------------------------------------------------------- #


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


# ---------------------------------------------------------------------- #
#  手動操縦との排他 (逆方向)
# ---------------------------------------------------------------------- #


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
