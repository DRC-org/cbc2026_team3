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

import can
from aiohttp.test_utils import TestClient, TestServer

from lib.can_manager import CANManager
from lib.control.position_loop import M3508PositionLoop, make_position_pid
from lib.control.target_refresh import GenericTargetRefresher
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import M3508Driver
from lib.manual import ManualController
from lib.sequence.engine import Sequence, step
from lib.sequence.motors import MotorGroup, MotorHandle
from lib.sequence.positions import load_position_table
from tests.fake_can import mock_can_manager
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
    def _install(fx: ServerFixture, robot: str, stall: _StallingPausable) -> None:
        fx.set_position_loops(robot, [stall])  # type: ignore[list-item]

    async def test_窓の中の緊急停止で一歩も駆動しない(self) -> None:
        fx, sequence = _build()
        stall = _StallingPausable()
        self._install(fx, "main_hand", stall)
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
        self._install(fx, "main_hand", stall)
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
        self._install(fx, "main_hand", stall)

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
    async def test_実行中は位置制御ループを黙らせる(self) -> None:
        """0x200 は 1 通に 4 モータ分のスロットを持つ。動作確認中にループが送ると
        確認用の指令が 0 電流で上書きされる。"""
        fx, sequence = _build()
        mgr = fx.can_manager("main_hand")
        probe = _LoopProbe(mgr)
        fx.set_position_loops("main_hand", [probe.loop])

        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_running()

        assert probe.loop.is_paused is True
        before = len(probe.frames)
        await probe.loop.step()
        assert len(probe.frames) == before

        sequence.gate.set()
        await fx.wait_motor_check_idle()

    async def test_終了後にループを復帰させる(self) -> None:
        fx, sequence = _build()
        probe = _LoopProbe(fx.can_manager("main_hand"))
        fx.set_position_loops("main_hand", [probe.loop])
        sequence.gate.set()

        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_idle()

        # 復帰しないと昇降軸が保持電流を失って落ちる
        assert probe.loop.is_paused is False
        before = len(probe.frames)
        await probe.loop.step()
        assert len(probe.frames) == before + 1

    async def test_中断で降りても復帰させる(self) -> None:
        fx, sequence = _build()
        probe = _LoopProbe(fx.can_manager("main_hand"))
        fx.set_position_loops("main_hand", [probe.loop])

        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_running()
        fx.abort_motor_check()
        sequence.gate.set()
        await fx.wait_motor_check_idle()

        assert probe.loop.is_paused is False

    async def test_例外で降りても復帰させる(self) -> None:
        class _RaisingSequence(Sequence):
            @step("必ず失敗する")
            async def boom(self) -> None:
                raise RuntimeError("テスト用例外")

        fx, _ = _build(check=_RaisingSequence("motor_check"))
        probe = _LoopProbe(fx.can_manager("main_hand"))
        fx.set_position_loops("main_hand", [probe.loop])

        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_idle()

        assert probe.loop.is_paused is False

    async def test_両ロボットの送信経路を止める(self) -> None:
        """**片方だけ止めてはならない。** 1 本のシーケンスが両機を動かすので、
        止め損ねた側の再送が確認用の指令を上書きする。"""
        fx, sequence = _build()
        refreshers = {
            name: GenericTargetRefresher([], is_estop_active=lambda: False)
            for name in ("main_hand", "sub_hand")
        }
        for name, refresher in refreshers.items():
            fx.set_target_refreshers(name, [refresher])

        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_running()

        assert [r.is_paused for r in refreshers.values()] == [True, True]

        sequence.gate.set()
        await fx.wait_motor_check_idle()

        assert [r.is_paused for r in refreshers.values()] == [False, False]

    async def test_停止中の手動切替を拒否する(self) -> None:
        fx, sequence = _build(manual=True)
        assert await fx.start_motor_check() is True
        await fx.wait_motor_check_running()

        await fx.command({"type": "set_operation_mode", "robot": "main_hand", "mode": "manual"})
        assert fx.operation_mode("main_hand") == "sequence"

        sequence.gate.set()
        await fx.wait_motor_check_idle()


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

    async def test_起動できない理由を状態に載せる(self) -> None:
        """UI は理由を説明するだけ。可否をクライアントで導出し直させない。"""
        fx, _ = _build()
        fx.enter_match()

        assert "試合中" in (fx.motor_check_state()["blocked_reason"] or "")

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
