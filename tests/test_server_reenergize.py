"""再励磁コマンド (`reenergize_motors`) の回帰テスト。

励磁が落ちたモータ (EDULITE 05 / DM3520 の fault) を機体を止めずに戻す操作。
CommandSpec のゲート宣言 (フェーズ / 緊急停止 / 手動モード) は `tests/test_commands.py`
が固定するので、ここではハンドラの実処理を見る:

- 動作確認の実行中・緊急停止解除の再励磁の in-flight 中・同一ロボットへの
  二重投入は、CommandSpec に無い独自ゲートなのでここでしか守れない
- 対象ロボットだけを触ること (もう一方の `activate_motors` を巻き込まない)
- 有効化に失敗したモータが `safety.unenergized_motors` に載ること
- **フォルト直前の古い目標 (`QueryDrivenTargetRefresher` のラッチ・
  `MotorHandle` の明示目標) を、無励磁のモータだけに絞って剥がしてから
  励磁すること** — 剥がさないと、励磁した直後の再送 (最大 50ms 後) が古い値で
  上書きし、「現在角を書いてから励磁する」保証が意味を失う
  (advisor 指摘。詳細は `lib/control/target_refresh.py` の `clear_target`)
"""

from __future__ import annotations

import asyncio
import struct
import time
from typing import ClassVar
from unittest.mock import AsyncMock

import can
import pytest
from aiohttp.test_utils import TestClient, TestServer

from lib.axis_sync import MotorSpec, SyncGroup
from lib.control.sync_monitor import SyncMonitor
from lib.control.target_refresh import QueryDrivenTargetRefresher
from lib.drivers.base import ControlMode
from lib.drivers.edulite05 import Edulite05Driver
from lib.manual import ManualController
from lib.sequence.engine import Sequence
from lib.sequence.motors import MotorGroup, MotorHandle
from lib.sequence.positions import load_position_table
from tests.fake_can import mock_can_manager, set_motors
from tests.feedback_frames import feed_edulite
from tests.server_fixtures import ServerFixture

_ROBOT_NAMES = ("main_hand", "sub_hand")


def _target_value(msg: can.Message) -> float:
    """EDULITE 05 の SET_TARGET (WRITE_PARAM) フレームから指令値を読む。

    `feed_edulite` は 16bit 固定小数点を経由するので、実測角の丸め誤差ぶん
    厳密な浮動小数点一致にならない。値そのもの (近似) を見る。
    """
    _param_id, value = struct.unpack("<Hxxf", msg.data)
    return value


class _EmptySequence(Sequence):
    """ステップを 1 つも持たないシーケンス。`Sequence` を直に使うと
    `__init_subclass__` が走らず `_steps` が未定義のままになるため、
    配信 (`_build_state_message`) が `current_step` の参照で落ちる。
    """


def _build_fixture() -> ServerFixture:
    fx = ServerFixture.build()
    for name in _ROBOT_NAMES:
        fx.add_robot(name, _EmptySequence(name))
    return fx


class TestUnknownOrMissingRobot:
    """`data["robot"]` が無い・未知なら黙って何もしない (拒否理由も返さない)。"""

    async def test_unknown_robot_is_ignored(self) -> None:
        fx = _build_fixture()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])
        await fx.command({"type": "reenergize_motors", "robot": "no_such_robot"})
        # タスクが 1 つも立っていないことまで見る。`assert_not_called()` だけだと、
        # 検証を外して未知の名前のままタスクを立てても (中で KeyError に落ちて
        # 即座に完了するだけなので) main_hand 側の呼び出しには一切現れず、
        # 検証漏れを見逃す
        assert not fx.has_pending_reenergize("no_such_robot")
        await asyncio.sleep(0)
        fx.can_manager("main_hand").activate_motors.assert_not_called()

    async def test_missing_robot_is_ignored(self) -> None:
        fx = _build_fixture()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])
        await fx.command({"type": "reenergize_motors"})
        assert not fx.has_pending_reenergize(None)  # type: ignore[arg-type]
        await asyncio.sleep(0)
        fx.can_manager("main_hand").activate_motors.assert_not_called()


class TestExclusionGates:
    """CommandSpec に無い、ハンドラ固有の排他。1 枚ずつ単独で確かめる。"""

    async def test_rejected_while_motor_check_running(self) -> None:
        fx = _build_fixture()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])
        gate = asyncio.Event()

        async def _never_finishes() -> None:
            await gate.wait()

        fx.set_motor_check_task(asyncio.create_task(_never_finishes()))
        fx.server._reject_command = AsyncMock()  # type: ignore[method-assign]
        try:
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
            await asyncio.sleep(0)
            fx.can_manager("main_hand").activate_motors.assert_not_called()
            fx.server._reject_command.assert_awaited_once()
            args = fx.server._reject_command.await_args.args
            assert args[1] == "reenergize_motors"
            assert "動作確認" in args[2]
        finally:
            gate.set()

    async def test_rejected_while_e_stop_reactivation_in_flight(self) -> None:
        """緊急停止解除の再励磁 (`_reactivate_motors`) と同じバスを取り合わない。

        再励磁はロボットを順に (main_hand → sub_hand) 処理するので、main_hand の
        `activate_motors` をゲートしておけば sub_hand の分はまだ 1 回も呼ばれない。
        そこで **sub_hand** への `reenergize_motors` が「進行中」で拒否され、
        `activate_motors` が (自分からは) 1 回も呼ばれないことを確かめる ——
        main_hand 側の呼び出し回数で判定すると、再励磁フロー自身の呼び出しと
        混ざって「拒否できているか」を判定できない。
        """
        fx = _build_fixture()
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        fx.can_manager("sub_hand").activate_motors = AsyncMock(return_value=[])
        fx.server._reject_command = AsyncMock()  # type: ignore[method-assign]
        try:
            await fx.command({"type": "e_stop"})
            await fx.command({"type": "e_stop_release"})
            # 再励磁タスクへ 1 度だけ実行機会を与え、main_hand のゲートで止まらせる
            await asyncio.sleep(0)
            assert fx.server._reactivating

            await fx.command({"type": "reenergize_motors", "robot": "sub_hand"})
            fx.can_manager("sub_hand").activate_motors.assert_not_called()
            fx.server._reject_command.assert_awaited_once()
            assert "進行中" in fx.server._reject_command.await_args.args[2]
        finally:
            gate.set()
            await fx.wait_reactivation()

    async def test_second_press_is_rejected_while_first_still_running(self) -> None:
        """同一ロボットへの二重投入。ボタン連打や、is_energized() の反映待ちで
        ボタンが消えずに残っている間の 2 回目を想定する。
        """
        fx = _build_fixture()
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        fx.server._reject_command = AsyncMock()  # type: ignore[method-assign]
        try:
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
            await asyncio.sleep(0)
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
            await asyncio.sleep(0)
            fx.server._reject_command.assert_awaited_once()
            assert "処理中" in fx.server._reject_command.await_args.args[2]
        finally:
            gate.set()
            await fx.wait_reenergize("main_hand")


class TestTargetsRobotOnly:
    """対象ロボットの `CANManager` だけを触り、もう一方は巻き込まない。"""

    async def test_only_named_robot_is_activated(self) -> None:
        fx = _build_fixture()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])
        fx.can_manager("sub_hand").activate_motors = AsyncMock(return_value=[])

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        fx.can_manager("main_hand").activate_motors.assert_awaited_once()
        fx.can_manager("sub_hand").activate_motors.assert_not_called()


class TestFailureIsReported:
    async def test_motors_that_fail_to_activate_appear_in_safety(self) -> None:
        fx = _build_fixture()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=["m1"])

        # `safety.unenergized_motors` は起動時 (`_on_startup`) に置く猶予の起点に
        # 依存する。素の `fx.command()` だけだとその起点が無いまま (`None`) で常に
        # 空を返すので、実際にアプリを起動させる
        app = fx.create_app()
        async with TestClient(TestServer(app)):
            # 起動直後の猶予 (_ENERGIZE_GRACE_S = 0.5s) をやり過ごす
            await asyncio.sleep(0.6)
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
            await fx.wait_reenergize("main_hand")

            assert fx.state_message("main_hand")["safety"]["unenergized_motors"] == ["m1"]
            # 巻き込んでいない側は空のまま
            assert fx.state_message("sub_hand")["safety"]["unenergized_motors"] == []


class TestPreviouslyInactiveMotorsAreRetried:
    """前回 (起動時、または前回の再励磁) に有効化できなかったモータも対象に含める。

    `is_energized()` はフィードバックが届くまで None を返すので、起動直後に
    フィードバックが来ずに有効化を見送ったモータは「今無励磁」の判定に
    引っかからない。`safety.unenergized_motors` が操縦者に見せている集合
    (`set_initial_inactive_motors` / 前回の失敗) を対象へ合併しないと、
    その状態のまま再励磁を押してもリトライされない。
    """

    async def test_only_includes_previously_inactive_motor(self) -> None:
        fx = _build_fixture()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])
        fx.server.set_initial_inactive_motors("main_hand", ["m_startup"])

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        can_manager = fx.can_manager("main_hand")
        can_manager.activate_motors.assert_awaited_once()
        assert can_manager.activate_motors.await_args.kwargs["only"] == {"m_startup"}


class TestStaleTargetIsClearedBeforeActivation:
    """`activate_motors` を呼ぶ前に、無励磁のモータだけ目標をラッチごと剥がす。"""

    def _build(self) -> tuple[ServerFixture, dict[str, Edulite05Driver], dict[str, MotorHandle]]:
        can_manager = mock_can_manager(bus_name="can_edulite")
        can_manager.send = AsyncMock()
        # activate_motors 自体の中身 (現在角を書いて enable するところ) は
        # 別のテスト (drivers/test_edulite05.py・test_can_manager.py) が見ている。
        # ここでの関心はその前段 (何を剥がすか) だけなので、活性化そのものは
        # 成功させたことにする
        can_manager.activate_motors = AsyncMock(return_value=[])

        dropped = Edulite05Driver("dropped", can_id=1)
        healthy = Edulite05Driver("healthy", can_id=2)
        set_motors(can_manager, {"dropped": dropped, "healthy": healthy})

        dropped_handle = MotorHandle("dropped", dropped, can_manager)
        healthy_handle = MotorHandle("healthy", healthy, can_manager)
        refresher = QueryDrivenTargetRefresher(
            [dropped_handle, healthy_handle], can_manager, is_estop_active=lambda: False
        )

        fx = ServerFixture.build()
        fx.add_robot(
            "main_hand", _EmptySequence("main_hand"), can_manager, target_refreshers=[refresher]
        )
        fx.add_robot("sub_hand", _EmptySequence("sub_hand"))

        return (
            fx,
            {"dropped": dropped, "healthy": healthy},
            {
                "dropped": dropped_handle,
                "healthy": healthy_handle,
            },
        )

    async def test_dropped_motor_loses_stale_explicit_target(self) -> None:
        fx, drivers, handles = self._build()
        # フォルト前: 遠くの目標 (POS_MAX 付近の 4.0rad) へ move_to していた
        await handles["dropped"].set_target(ControlMode.POSITION, 4.0)
        # フォルトで励磁が落ち、その間に機構が沈んだ (現在角は 0.5rad まで下がった)
        feed_edulite(drivers["dropped"], position=0.5, mode_state=0)  # RESET = 無励磁
        assert drivers["dropped"].is_energized() is False

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        # 古い目標 (4.0) が生きたままだと、activate_motors 後の再送がそこへ
        # 引き戻してしまう。剥がされていることを確認する
        assert handles["dropped"].has_target is False

    async def test_dropped_motor_loses_stale_idle_latch(self) -> None:
        fx, drivers, _handles = self._build()
        can_manager = fx.can_manager("main_hand")
        (refresher,) = fx.server._robots["main_hand"].target_refreshers

        # 目標を一度も持たないまま「今の姿勢を保て」がラッチされた状態を作る
        # (問い合わせ駆動リフレッシャを公開 API で 1 周期進める)。
        # 値は POS_MIN/POS_MAX (±12.57rad) に収める
        feed_edulite(drivers["dropped"], position=3.0, mode_state=2)  # まだ励磁中
        await refresher.step()
        latched = [c.args[1] for c in can_manager.send.await_args_list if c.args[0] == "dropped"][
            -1
        ]
        assert _target_value(latched) == pytest.approx(3.0, abs=0.05)

        # フォルトで励磁が落ち、機構が沈んだ (1.0rad)。ラッチ (3.0) はまだ古いまま
        feed_edulite(drivers["dropped"], position=1.0, mode_state=0)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        # 剥がされていれば、次の再送は実測角 (1.0) を新たにラッチして送る。
        # 剥がされていなければ古いラッチ (3.0) を送り続けるはず
        can_manager.send.reset_mock()
        await refresher.step()
        resent = [c.args[1] for c in can_manager.send.await_args_list if c.args[0] == "dropped"]
        assert len(resent) == 1
        assert _target_value(resent[0]) == pytest.approx(1.0, abs=0.05)

    async def test_healthy_motor_on_same_refresher_is_untouched(self) -> None:
        """同じバスの他モータが移動中でも、その目標を巻き込んで中断させない。

        ここでの "healthy" は `dropped` と直結ペアを組んでいない前提 (この fixture の
        2 台は無関係)。ペアの場合の扱いは `TestPairedAxisIsExpandedToPartner` を見る
        —— そちらは意図的に相方の目標も剥がす (2 クラスは矛盾しない)。
        """
        fx, drivers, handles = self._build()
        feed_edulite(drivers["healthy"], position=0.0, mode_state=2)
        await handles["healthy"].set_target(ControlMode.POSITION, 7.0)

        feed_edulite(drivers["dropped"], position=0.5, mode_state=0)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        assert handles["healthy"].target == 7.0

    async def test_already_energized_motor_keeps_its_target(self) -> None:
        """無励磁でないモータは剥がす対象にならない (励磁済みへの巻き添えを作らない)。"""
        fx, drivers, handles = self._build()
        feed_edulite(drivers["dropped"], position=0.0, mode_state=2)  # 励磁中のまま
        await handles["dropped"].set_target(ControlMode.POSITION, 1.2)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        assert handles["dropped"].target == 1.2

    async def test_activation_is_scoped_to_dropped_motors_only(self) -> None:
        """`activate_motors(only=...)` に渡すのは無励磁のモータだけ (advisor 指摘)。

        絞らずに全モータへ渡すと、健全で移動中の `healthy` まで
        「現在角を書いてから enable」に巻き込まれ、動いている軸へ割り込む。
        絞り込みの実効果 (`only` が本当にフィルタになっているか) は
        `test_can_manager.py::test_activate_motors_only_filters_target_motors` が見る。
        """
        fx, drivers, _handles = self._build()
        feed_edulite(drivers["healthy"], position=0.0, mode_state=2)
        feed_edulite(drivers["dropped"], position=0.5, mode_state=0)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        can_manager = fx.can_manager("main_hand")
        can_manager.activate_motors.assert_awaited_once()
        assert can_manager.activate_motors.await_args.kwargs["only"] == {"dropped"}


class TestManualJogOriginIsReset:
    """再励磁は無励磁だったモータのジョグ起点も捨てる (advisor 指摘)。

    無励磁のあいだ機構が自重で下がっていた場合、目標とラッチだけ剥がして
    ジョグの起点をそのままにすると、次のジョグが古い (フォルト前の) 起点から
    飛ぶ。緊急停止解除 (`activate_e_stop` → `ManualController.on_e_stop()`) と
    対称に扱う。軸単位に絞る API が無いのでロボット全体の起点を捨てる。
    """

    _POSITIONS: ClassVar[dict] = {
        "axes": {
            "test_axis": {
                "unit": "rad",
                "command_unit": "rad",
                "manual": {"min": -12.0, "max": 12.0, "steps": [1.0]},
                "motors": {"dropped": {"scale": 1.0}},
            },
        },
        "positions": {},
    }

    def _build(self) -> tuple[ServerFixture, dict[str, Edulite05Driver], ManualController]:
        can_manager = mock_can_manager(bus_name="can_edulite")
        can_manager.send = AsyncMock()
        can_manager.activate_motors = AsyncMock(return_value=[])

        dropped = Edulite05Driver("dropped", can_id=1)
        set_motors(can_manager, {"dropped": dropped})

        dropped_handle = MotorHandle("dropped", dropped, can_manager)
        refresher = QueryDrivenTargetRefresher(
            [dropped_handle], can_manager, is_estop_active=lambda: False
        )

        group = MotorGroup()
        group.add(dropped_handle)
        table = load_position_table(self._POSITIONS, source="<test>")
        manual = ManualController(group, table)

        fx = ServerFixture.build()
        fx.add_robot(
            "main_hand",
            _EmptySequence("main_hand"),
            can_manager,
            target_refreshers=[refresher],
            manual=manual,
        )
        fx.add_robot("sub_hand", _EmptySequence("sub_hand"))
        return fx, {"dropped": dropped}, manual

    async def test_origin_is_dropped_when_motor_was_unenergized(self) -> None:
        fx, drivers, manual = self._build()
        await manual.set_value("test_axis", 5.0)
        feed_edulite(drivers["dropped"], position=1.0, mode_state=0)  # 無励磁・自重で沈んだ

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        feed_edulite(drivers["dropped"], position=1.0, mode_state=2)
        # 起点が捨てられていれば、次のジョグはフィードバック (1.0) から積む。
        # 捨てられていなければ古い起点 (5.0) から積んでしまう
        assert await manual.jog("test_axis", 0.5) == pytest.approx(1.5, abs=0.01)

    async def test_origin_is_kept_when_nothing_was_dropped(self) -> None:
        """無励磁のモータが無ければジョグ起点も触らない (無関係な巻き添えを作らない)。

        フィードバック位置 (2.0) をあえて起点 (5.0) と別の値にする —— 揃えると、
        起点を捨てても「たまたま同じ値を測り直す」ため区別が付かない
        (実際に区別の付かないアサーションで変異を見逃しかけた)。
        """
        fx, drivers, manual = self._build()
        await manual.set_value("test_axis", 5.0)
        feed_edulite(drivers["dropped"], position=2.0, mode_state=2)  # 励磁中のまま

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        # 起点 (5.0) が保たれていれば 5.5。捨てられていればフィードバック (2.0) から
        # 積み直すので 2.5 になる
        assert await manual.jog("test_axis", 0.5) == pytest.approx(5.5, abs=0.01)


class TestPairedAxisIsExpandedToPartner:
    """直結ペア (`rotate` = EDULITE x2) の片側だけが無励磁になっても、対象は
    相方を含めて拡張する (advisor 指摘)。

    片側だけを対象にすると、相方が移動中の場合「無励磁で連れ回されていた片側」を
    「相方に逆らって現在角を保持する片側」へ変えるだけになり、直後に
    `SyncMonitor` の偏差超過で試合が止まる。CLAUDE.md「ペア軸に片側だけ効く
    操作を作らない」をここにも適用する。
    """

    def _build(
        self,
    ) -> tuple[ServerFixture, dict[str, Edulite05Driver], dict[str, MotorHandle]]:
        can_manager = mock_can_manager(bus_name="can_edulite")
        can_manager.send = AsyncMock()
        can_manager.activate_motors = AsyncMock(return_value=[])
        can_manager.last_feedback_at = lambda _name: time.time()

        rotate_r = Edulite05Driver("rotate_r", can_id=1)
        rotate_l = Edulite05Driver("rotate_l", can_id=2)
        # ペアに属さないモータ。相方探索が「グループ全体」ではなく「無励磁のモータが
        # 属するグループだけ」を見ていることを、こちらを落として確かめる
        gripper = Edulite05Driver("gripper", can_id=3)
        set_motors(can_manager, {"rotate_r": rotate_r, "rotate_l": rotate_l, "gripper": gripper})

        handle_r = MotorHandle("rotate_r", rotate_r, can_manager)
        handle_l = MotorHandle("rotate_l", rotate_l, can_manager)
        handle_g = MotorHandle("gripper", gripper, can_manager)
        refresher = QueryDrivenTargetRefresher(
            [handle_r, handle_l, handle_g], can_manager, is_estop_active=lambda: False
        )

        group = SyncGroup(
            name="rotate",
            members=(
                MotorSpec(name="rotate_r", scale=1.0, offset=0.0),
                MotorSpec(name="rotate_l", scale=-1.0, offset=0.0),
            ),
            tolerance=5.0,
        )
        monitor = SyncMonitor(
            [group],
            {"rotate_r": rotate_r, "rotate_l": rotate_l},  # type: ignore[arg-type]
            last_feedback_at=lambda _name: time.time(),
        )

        fx = ServerFixture.build()
        fx.add_robot(
            "main_hand",
            _EmptySequence("main_hand"),
            can_manager,
            target_refreshers=[refresher],
            sync_monitors=[monitor],
        )
        fx.add_robot("sub_hand", _EmptySequence("sub_hand"))

        return (
            fx,
            {"rotate_r": rotate_r, "rotate_l": rotate_l, "gripper": gripper},
            {"rotate_r": handle_r, "rotate_l": handle_l, "gripper": handle_g},
        )

    async def test_activation_includes_healthy_partner(self) -> None:
        fx, drivers, _handles = self._build()
        feed_edulite(drivers["rotate_l"], position=0.0, mode_state=2)  # 健全・励磁中
        feed_edulite(drivers["rotate_r"], position=0.5, mode_state=0)  # 無励磁
        feed_edulite(drivers["gripper"], position=0.0, mode_state=2)  # 無関係・健全

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        can_manager = fx.can_manager("main_hand")
        can_manager.activate_motors.assert_awaited_once()
        assert can_manager.activate_motors.await_args.kwargs["only"] == {"rotate_r", "rotate_l"}

    async def test_healthy_partners_target_is_also_cleared(self) -> None:
        """相方が移動中でも、押し合いを避けるため目標を剥がす (割り込みは許容する)。"""
        fx, drivers, handles = self._build()
        feed_edulite(drivers["rotate_l"], position=0.0, mode_state=2)
        await handles["rotate_l"].set_target(ControlMode.POSITION, 7.0)
        feed_edulite(drivers["rotate_r"], position=0.5, mode_state=0)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        assert handles["rotate_l"].has_target is False

    async def test_unpaired_motor_is_not_expanded(self) -> None:
        """ペアに属さないモータ (`gripper`) が単独で無励磁になっても、
        ペアの `rotate_r` / `rotate_l` は対象に巻き込まれない (両方とも健全なまま)。
        """
        fx, drivers, _handles = self._build()
        feed_edulite(drivers["rotate_l"], position=0.0, mode_state=2)
        feed_edulite(drivers["rotate_r"], position=0.5, mode_state=2)  # 両方とも健全
        feed_edulite(drivers["gripper"], position=0.0, mode_state=0)  # こちらだけ無励磁

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        can_manager = fx.can_manager("main_hand")
        can_manager.activate_motors.assert_awaited_once()
        assert can_manager.activate_motors.await_args.kwargs["only"] == {"gripper"}

    async def test_nothing_dropped_yields_empty_target(self) -> None:
        """無励磁のモータが 1 台も無ければ、対象もラッチ剥がしも一切走らない。"""
        fx, drivers, _handles = self._build()
        feed_edulite(drivers["rotate_l"], position=0.0, mode_state=2)
        feed_edulite(drivers["rotate_r"], position=0.5, mode_state=2)
        feed_edulite(drivers["gripper"], position=0.0, mode_state=2)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        can_manager = fx.can_manager("main_hand")
        can_manager.activate_motors.assert_awaited_once()
        assert can_manager.activate_motors.await_args.kwargs["only"] == set()


class TestReactivationWaitsForPendingReenergize:
    """逆方向の排他: 緊急停止解除の再励磁 (`_reactivate_motors`) は、同じロボットの
    `reenergize_motors` が in-flight ならその完了を待ってから自分の
    `activate_motors` を呼ぶ (敵対的レビュー指摘)。

    両者が同じロボットへ並走すると、片方の `_wait_fresh_feedback` が送る
    プローブ (`feedback_probe_message()` = disable) が、もう片方が enable した
    ばかりのモータへ届く。DM3520 は disable で自重落下するので、「戻した直後に
    もう一度落とす」形で `reenergize_motors` 自身の存在意義を壊す。

    **解除コマンドの受理そのものは待たせない** —— 待たせる先はバックグラウンドの
    再励磁タスクのほうで、解除の受理・ラッチ解除・状態配信は即座に進む
    (`_cmd_e_stop_release` に手を入れていないことは他のテストが守る)。
    """

    async def test_reactivate_activate_motors_waits_for_pending_reenergize(self) -> None:
        fx = _build_fixture()
        order: list[str] = []
        reenergize_gate = asyncio.Event()

        async def _activate(**_kwargs: object) -> list[str]:
            if not order:
                order.append("reenergize_start")
                await reenergize_gate.wait()
                order.append("reenergize_done")
            else:
                order.append("reactivate")
            return []

        fx.can_manager("main_hand").activate_motors = _activate
        fx.can_manager("sub_hand").activate_motors = AsyncMock(return_value=[])

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await asyncio.sleep(0)
        assert order == ["reenergize_start"]

        await fx.command({"type": "e_stop"})
        await fx.command({"type": "e_stop_release"})
        await asyncio.sleep(0)
        # 解除は受理されているが、main_hand の再励磁がまだ処理中なので
        # reactivate 側の activate_motors はまだ呼ばれていない
        assert not fx.server._e_stop_active
        assert order == ["reenergize_start"]

        reenergize_gate.set()
        await fx.wait_reenergize("main_hand")
        await fx.wait_reactivation()

        assert order == ["reenergize_start", "reenergize_done", "reactivate"]


class TestMotorCheckDeniedWhileReenergizeInFlight:
    """逆方向の排他: 動作確認は、どちらかのロボットの `reenergize_motors` が
    in-flight なら起動できない (敵対的レビュー指摘)。

    零点確定 (`rotate`) は disable → SET_ZERO → enable を伴う。同じモータへ
    再励磁の `activate_motors` が並走すると、そちらのプローブ (disable) が
    動作確認側の enable と競合しうる。
    """

    async def test_denied_while_pending(self) -> None:
        fx = _build_fixture()
        fx.set_motor_check_sequence(_EmptySequence("motor_check"))
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        try:
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
            await asyncio.sleep(0)

            assert await fx.start_motor_check() is False
            assert "main_hand" in (fx.motor_check_error() or "")
        finally:
            gate.set()
            await fx.wait_reenergize("main_hand")

    async def test_allowed_once_reenergize_finishes(self) -> None:
        fx = _build_fixture()
        fx.set_motor_check_sequence(_EmptySequence("motor_check"))
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        assert await fx.start_motor_check() is True


class TestSetOperationModeDeniedWhileReenergizeInFlight:
    """逆方向の排他: 手動操縦への切替は、そのロボットの `reenergize_motors` が
    in-flight なら拒否する (敵対的レビュー指摘)。

    手動へ入った直後に送るジョグは、再励磁の `activate_motors` が書く
    「フォルト前の現在角」目標と同じモータへ競合しうる。
    """

    def _fixture(self) -> ServerFixture:
        table = load_position_table(
            {
                "axes": {
                    "axis": {
                        "unit": "rad",
                        "command_unit": "rad",
                        "manual": {"min": -1.0, "max": 1.0, "steps": [0.1]},
                        "motors": {"m1": {"scale": 1.0}},
                    },
                },
                "positions": {},
            },
            source="<test>",
        )
        can_manager = mock_can_manager()
        group = MotorGroup()
        group.add(MotorHandle("m1", can_manager.motors["m1"], can_manager))
        manual = ManualController(group, table)

        fx = ServerFixture.build()
        fx.add_robot("main_hand", _EmptySequence("main_hand"), can_manager, manual=manual)
        fx.add_robot("sub_hand", _EmptySequence("sub_hand"))
        return fx

    async def test_denied_while_pending(self) -> None:
        fx = self._fixture()
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        fx.server._reject_command = AsyncMock()  # type: ignore[method-assign]
        try:
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
            await asyncio.sleep(0)

            await fx.command({"type": "set_operation_mode", "robot": "main_hand", "mode": "manual"})
            assert fx.operation_mode("main_hand") == "sequence"
            fx.server._reject_command.assert_awaited_once()
            assert "再励磁" in fx.server._reject_command.await_args.args[2]
        finally:
            gate.set()
            await fx.wait_reenergize("main_hand")

    async def test_allowed_once_reenergize_finishes(self) -> None:
        fx = self._fixture()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        await fx.command({"type": "set_operation_mode", "robot": "main_hand", "mode": "manual"})
        assert fx.operation_mode("main_hand") == "manual"
