"""再励磁コマンド (`reenergize_motors`) の回帰テスト。

CommandSpec のゲート宣言 (フェーズ / 緊急停止 / 手動モード) は `tests/test_commands.py`
が固定するので、ここではハンドラの実処理を見る:

- 動作確認の実行中・緊急停止解除の再励磁の in-flight 中・同一ロボットへの
  二重投入は、CommandSpec に無い独自ゲートなのでここでしか守れない
- 対象ロボットだけを触ること (もう一方の `activate_motors` を巻き込まない)
- 有効化に失敗したモータが `safety.unenergized_motors` に載ること
- **フォルト直前の古い目標を、無励磁のモータだけに絞って剥がしてから励磁すること**
  — 剥がさないと励磁直後の再送 (最大 50ms 後) が古い値で上書きし、「現在角を
  書いてから励磁する」保証が意味を失う
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
import time
from typing import ClassVar
from unittest.mock import AsyncMock

import can
import pytest
from aiohttp.test_utils import TestClient, TestServer

from lib.axis_sync import MotorSpec, SyncGroup
from lib.can_manager import CANManager
from lib.control.sync_monitor import SyncMonitor
from lib.control.target_refresh import QueryDrivenTargetRefresher
from lib.drivers.base import ControlMode
from lib.drivers.edulite05 import Edulite05Driver
from lib.manual import ManualController
from lib.sequence.engine import Sequence, step
from lib.sequence.motors import MotorGroup, MotorHandle
from lib.sequence.positions import load_position_table
from lib.server import _ENERGIZE_GRACE_S
from tests.fake_can import direct_runner, mock_bus, mock_can_manager, mock_motor, set_motors
from tests.feedback_frames import feed_edulite
from tests.server_fixtures import RecordingClient, ServerFixture, wait_until

_ROBOT_NAMES = ("main_hand", "sub_hand")


def _target_value(msg: can.Message) -> float:
    """EDULITE 05 の SET_TARGET (WRITE_PARAM) フレームから指令値を読む。

    `feed_edulite` は 16bit 固定小数点を経由するので厳密な浮動小数点一致にならない。
    """
    _param_id, value = struct.unpack("<Hxxf", msg.data)
    return value


class _EmptySequence(Sequence):
    """ステップを 1 つも持たないシーケンス。`Sequence` を直に使うと
    `__init_subclass__` が走らず `_steps` が未定義のままになるため、
    配信 (`_build_state_message`) が `current_step` の参照で落ちる。
    """


def _count_e_stop_broadcasts(sent: list[can.Message]) -> int:
    """ワイヤ上に出たブロードキャスト停止 (仕様書 §3.5 / CAN ID 0x0FF・data 全 0) の数。

    エンコーダの戻り値と突き合わせず形をそのまま書くのは `tests/test_server_e_stop.py`
    と同じ理由 —— 突き合わせると「実装が実装と一致する」ことしか見られない。
    """
    return sum(
        1
        for msg in sent
        if msg.arbitration_id == 0x0FF and bytes(msg.data) == bytes(3) and not msg.is_extended_id
    )


def _build_fixture() -> ServerFixture:
    fx = ServerFixture.build()
    for name in _ROBOT_NAMES:
        fx.add_robot(name, _EmptySequence(name))
    return fx


def _can_manager_with_dropped_motor() -> tuple[CANManager, Edulite05Driver]:
    """「無励磁だと分かっている」EDULITE を 1 台だけ載せたモック CANManager。

    **`mock_motor` では「対象が 1 台もいない再励磁」しか作れない** —— `is_energized()`
    が MagicMock を返すので `dropped` は必ず空集合になり、ラッチ剥がしもペア展開も
    1 つも通らない。実ドライバへ無励磁のフィードバックを流して本物の対象を作る。
    """
    can_manager = mock_can_manager(bus_name="can_edulite")
    can_manager.send = AsyncMock()
    dropped = Edulite05Driver("dropped", can_id=1)
    set_motors(can_manager, {"dropped": dropped})
    feed_edulite(dropped, position=0.5, mode_state=0)
    return can_manager, dropped


def _dropped_fixture() -> tuple[ServerFixture, Edulite05Driver]:
    """main_hand が無励磁のモータを 1 台抱えている最小構成。"""
    can_manager, dropped = _can_manager_with_dropped_motor()
    fx = ServerFixture.build()
    fx.add_robot("main_hand", _EmptySequence("main_hand"), can_manager)
    fx.add_robot("sub_hand", _EmptySequence("sub_hand"))
    return fx, dropped


def _manual_fixture() -> ServerFixture:
    """main_hand に手動操縦 1 軸 (`axis` → モータ `m1`) を持たせた最小構成。

    「手動と再励磁が同じロボットで競合する」ことを見る 2 クラスが共有する。
    """
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
    # `is_energized()` を三値で返させる。MagicMock の既定戻り値のままだと
    # `is False` が成立せず `dropped` が空になり、在飛中の窓を作れない
    motor = mock_motor("m1")
    motor.is_energized.return_value = False
    can_manager = mock_can_manager({"m1": motor})
    group = MotorGroup()
    group.add(MotorHandle("m1", can_manager.motors["m1"], can_manager))
    manual = ManualController(group, table)

    fx = ServerFixture.build()
    fx.add_robot("main_hand", _EmptySequence("main_hand"), can_manager, manual=manual)
    fx.add_robot("sub_hand", _EmptySequence("sub_hand"))
    return fx


class TestUnknownOrMissingRobot:
    """`data["robot"]` が無い・未知なら黙って何もしない (拒否理由も返さない)。"""

    async def test_unknown_robot_is_ignored(self) -> None:
        fx = _build_fixture()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])
        await fx.command({"type": "reenergize_motors", "robot": "no_such_robot"})
        # タスクが立っていないことまで見る。`assert_not_called()` だけだと、未知の
        # 名前でタスクを立てても中で KeyError に落ちて即完了するので検出できない
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
        client = RecordingClient()
        fx.attach_clients(client)
        try:
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"}, requester=client)
            await asyncio.sleep(0)
            fx.can_manager("main_hand").activate_motors.assert_not_called()
            rejected = client.of_type("command_rejected")
            assert len(rejected) == 1
            assert rejected[0]["command"] == "reenergize_motors"
            assert "動作確認" in rejected[0]["reason"]
        finally:
            gate.set()

    async def test_rejected_while_e_stop_reactivation_in_flight(self) -> None:
        """緊急停止解除の再励磁 (`_reactivate_motors`) と同じバスを取り合わない。

        再励磁はロボットを順に処理するので、main_hand の `activate_motors` をゲート
        しておけば sub_hand の分はまだ呼ばれない。**sub_hand** 側で判定するのは、
        main_hand の呼び出し回数だと再励磁フロー自身の呼び出しと混ざるため。
        """
        fx = _build_fixture()
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        fx.can_manager("sub_hand").activate_motors = AsyncMock(return_value=[])
        client = RecordingClient()
        fx.attach_clients(client)
        try:
            await fx.command({"type": "e_stop"})
            await fx.command({"type": "e_stop_release"})
            await asyncio.sleep(0)  # main_hand のゲートで止まらせる
            assert fx.server._reactivating

            await fx.command({"type": "reenergize_motors", "robot": "sub_hand"}, requester=client)
            fx.can_manager("sub_hand").activate_motors.assert_not_called()
            rejected = client.of_type("command_rejected")
            assert len(rejected) == 1
            assert "進行中" in rejected[0]["reason"]
        finally:
            gate.set()
            await fx.wait_reactivation()

    async def test_second_press_is_rejected_while_first_still_running(self) -> None:
        """同一ロボットへの二重投入。ボタン連打や、is_energized() の反映待ちで
        ボタンが消えずに残っている間の 2 回目を想定する。
        """
        fx, _dropped = _dropped_fixture()
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        client = RecordingClient()
        fx.attach_clients(client)
        try:
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
            await asyncio.sleep(0)
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"}, requester=client)
            await asyncio.sleep(0)
            rejected = client.of_type("command_rejected")
            assert len(rejected) == 1
            assert "処理中" in rejected[0]["reason"]
        finally:
            gate.set()
            await fx.wait_reenergize("main_hand")


class TestTargetsRobotOnly:
    """対象ロボットの `CANManager` だけを触り、もう一方は巻き込まない。

    **`mock_motor` 構成では `dropped` が必ず空になり、`only=set()` のまま
    `assert_awaited_once()` を通ってしまう** (中身が壊れても緑)。実ドライバの無励磁
    フィードバックで本物の対象を作り、渡した `only` まで見る。
    """

    async def test_only_named_robot_is_activated(self) -> None:
        fx, _dropped = _dropped_fixture()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])
        fx.can_manager("sub_hand").activate_motors = AsyncMock(return_value=[])

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        main = fx.can_manager("main_hand")
        main.activate_motors.assert_awaited_once()
        assert main.activate_motors.await_args.kwargs["only"] == {"dropped"}
        fx.can_manager("sub_hand").activate_motors.assert_not_called()

    async def test_nothing_is_sent_when_no_motor_is_dropped(self) -> None:
        """対象が 1 台も無ければ `activate_motors` を呼ばない。

        `only=set()` で呼んでも実害は無いが、CAN へ 1 通も出さないほうが
        「押したのに何も起きていない」ことがログからも読める。
        """
        fx = _build_fixture()  # `mock_motor` なので `dropped` は空
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        fx.can_manager("main_hand").activate_motors.assert_not_called()


class TestFailureIsReported:
    async def test_motors_that_fail_to_activate_appear_in_safety(self) -> None:
        fx, _driver = _dropped_fixture()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=["dropped"])

        # `safety.unenergized_motors` は起動時 (`_on_startup`) に置く猶予の起点に
        # 依存する。素の `fx.command()` だけだとその起点が無いまま (`None`) で常に
        # 空を返すので、実際にアプリを起動させる
        app = fx.create_app()
        async with TestClient(TestServer(app)):
            # 起動直後の猶予 (_ENERGIZE_GRACE_S) をやり過ごす
            await asyncio.sleep(_ENERGIZE_GRACE_S + 0.1)
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
            await fx.wait_reenergize("main_hand")

            # **成功直後の 1 周期は何も出さない。** 再励磁も猶予の起点を置き直す
            # ので (`_reactivate_motors` と対称)、enable が次のフィードバックへ
            # 反映されるまでの窓に「直っていない」と言わない。置き直しを外すと
            # ここが ["m1"] になる
            assert fx.state_message("main_hand")["safety"]["unenergized_motors"] == []

            await asyncio.sleep(_ENERGIZE_GRACE_S + 0.1)
            assert fx.state_message("main_hand")["safety"]["unenergized_motors"] == ["dropped"]
            # 巻き込んでいない側は空のまま
            assert fx.state_message("sub_hand")["safety"]["unenergized_motors"] == []

    async def test_activate_motors_exception_reports_only_set(self) -> None:
        """`activate_motors` が例外を投げても、`only` に渡した集合がそのまま
        `unenergized_motors` に反映される (`_activate_motors_for_robot` の except 分岐)。

        **`only` に「相方拡張でしか入らないモータ」(`rotate_l`) を混ぜる。**
        `rotate_l` は健全 (`is_energized()` が True) で `_inactive_motors` にも
        載っていないので、except 分岐が走らない限り最終結果に現れない —— `only` を
        無視する変異も、except 分岐を削って例外を上位へ素通りさせる変異も拾える。
        無関係な `gripper` (健全・非ペア) の同居は「全モータ扱い」の変異を拾う。
        """
        fx = ServerFixture.build()

        rotate_r = mock_motor("rotate_r")
        rotate_r.is_energized.return_value = False  # 無励磁 (相方拡張の起点)
        rotate_l = mock_motor("rotate_l")
        rotate_l.is_energized.return_value = True  # 健全 (相方拡張でのみ対象に入る)
        gripper = mock_motor("gripper")
        gripper.is_energized.return_value = True  # 無関係 (only に混ざってはならない)

        can_manager = mock_can_manager(
            {"rotate_r": rotate_r, "rotate_l": rotate_l, "gripper": gripper}
        )
        can_manager.activate_motors = AsyncMock(side_effect=RuntimeError("CAN 送信失敗"))

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

        fx.add_robot("main_hand", _EmptySequence("main_hand"), can_manager, sync_monitors=[monitor])
        fx.add_robot("sub_hand", _EmptySequence("sub_hand"))

        app = fx.create_app()
        async with TestClient(TestServer(app)):
            await asyncio.sleep(_ENERGIZE_GRACE_S + 0.1)
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
            await fx.wait_reenergize("main_hand")
            # 再励磁は猶予の起点を置き直すので、報告が出るのはその後
            await asyncio.sleep(_ENERGIZE_GRACE_S + 0.1)

            assert fx.state_message("main_hand")["safety"]["unenergized_motors"] == [
                "rotate_l",
                "rotate_r",
            ]


class TestPreviouslyInactiveMotorsAreRetried:
    """前回 (起動時、または前回の再励磁) に有効化できなかったモータも対象に含める。

    `is_energized()` はフィードバックが届くまで None を返すので、起動直後に有効化を
    見送ったモータは「今無励磁」の判定に引っかからない。`safety.unenergized_motors` が
    見せている集合を対象へ合併しないと、その状態のまま押してもリトライされない。
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
        # activate_motors 自体の中身は drivers/test_edulite05.py・test_can_manager.py
        # が見る。ここでの関心はその前段 (何を剥がすか) だけ
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
        # フォルト前は 4.0rad へ move_to しており、無励磁の間に機構が 0.5rad へ沈んだ
        await handles["dropped"].set_target(ControlMode.POSITION, 4.0)
        feed_edulite(drivers["dropped"], position=0.5, mode_state=0)  # RESET = 無励磁
        assert drivers["dropped"].is_energized() is False

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        # 古い目標 (4.0) が生きたままだと activate_motors 後の再送がそこへ引き戻す
        assert handles["dropped"].has_target is False

    async def test_dropped_motor_loses_stale_idle_latch(self) -> None:
        fx, drivers, _handles = self._build()
        can_manager = fx.can_manager("main_hand")
        (refresher,) = fx.server._robots["main_hand"].target_refreshers

        # 目標を一度も持たないまま「今の姿勢を保て」がラッチされた状態を作る。
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
        """`activate_motors(only=...)` に渡すのは無励磁のモータだけ。

        絞らずに全モータへ渡すと、健全で移動中の `healthy` まで
        「現在角を書いてから enable」に巻き込まれ、動いている軸へ割り込む。
        絞り込みの実効果は
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
    """再励磁は無励磁だったモータのジョグ起点も捨てる。**ただし対象の軸だけ。**

    無励磁のあいだ機構が自重で下がっていた場合、目標とラッチだけ剥がしてジョグの
    起点を残すと、次のジョグが古い (フォルト前の) 起点から飛ぶ。ここまでは緊急停止
    (`activate_e_stop` → `ManualController.on_e_stop()`) と同じ。

    **違うのは範囲。** 再励磁は「機体を止めずに」が売りなので、落ちた 1 台のために
    無関係な軸の起点まで捨ててはならない (docs/invariants.md §4)。**軸を 2 本置くのは
    そのため** —— 1 本では「全体を捨てる」と「対象だけ捨てる」の区別が付かない。
    """

    _POSITIONS: ClassVar[dict] = {
        "axes": {
            "test_axis": {
                "unit": "rad",
                "command_unit": "rad",
                "manual": {"min": -12.0, "max": 12.0, "steps": [1.0]},
                "motors": {"dropped": {"scale": 1.0}},
            },
            # 無関係な軸。落ちたモータを 1 台も含まないので起点は残らねばならない
            "other_axis": {
                "unit": "rad",
                "command_unit": "rad",
                "manual": {"min": -12.0, "max": 12.0, "steps": [1.0]},
                "motors": {"healthy": {"scale": 1.0}},
            },
        },
        "positions": {},
    }

    def _build(self) -> tuple[ServerFixture, dict[str, Edulite05Driver], ManualController]:
        can_manager = mock_can_manager(bus_name="can_edulite")
        can_manager.send = AsyncMock()
        can_manager.activate_motors = AsyncMock(return_value=[])

        dropped = Edulite05Driver("dropped", can_id=1)
        healthy = Edulite05Driver("healthy", can_id=2)
        set_motors(can_manager, {"dropped": dropped, "healthy": healthy})

        handles = {
            name: MotorHandle(name, driver, can_manager)
            for name, driver in (("dropped", dropped), ("healthy", healthy))
        }
        refresher = QueryDrivenTargetRefresher(
            list(handles.values()), can_manager, is_estop_active=lambda: False
        )

        group = MotorGroup()
        for handle in handles.values():
            group.add(handle)
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
        return fx, {"dropped": dropped, "healthy": healthy}, manual

    async def test_origin_is_dropped_when_motor_was_unenergized(self) -> None:
        fx, drivers, manual = self._build()
        await manual.set_value("test_axis", 5.0)
        feed_edulite(drivers["dropped"], position=1.0, mode_state=0)  # 無励磁・自重で沈んだ
        feed_edulite(drivers["healthy"], position=0.0, mode_state=2)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        feed_edulite(drivers["dropped"], position=1.0, mode_state=2)
        # 捨てられていればフィードバック (1.0) から積む。残っていれば古い起点 (5.0)
        assert await manual.jog("test_axis", 0.5) == pytest.approx(1.5, abs=0.01)

    async def test_unrelated_axis_keeps_its_origin(self) -> None:
        """**落ちたモータを含まない軸の起点は捨てない。**

        `ManualController.reset()` (ロボット全体) へ戻すとここが落ちる。
        フィードバック位置 (2.0) を起点 (7.0) と別の値にしておくのは、揃えると
        「たまたま同じ値を測り直す」ため区別が付かないから。
        """
        fx, drivers, manual = self._build()
        await manual.set_value("test_axis", 5.0)
        await manual.set_value("other_axis", 7.0)
        feed_edulite(drivers["dropped"], position=1.0, mode_state=0)  # こちらだけ無励磁
        feed_edulite(drivers["healthy"], position=2.0, mode_state=2)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        # 起点 (7.0) が保たれていれば 7.5。全体を捨てていれば 2.5 になる
        assert await manual.jog("other_axis", 0.5) == pytest.approx(7.5, abs=0.01)

    async def test_origin_is_kept_when_nothing_was_dropped(self) -> None:
        """無励磁のモータが無ければジョグ起点も触らない (無関係な巻き添えを作らない)。

        フィードバック位置 (2.0) をあえて起点 (5.0) と別の値にする —— 揃えると、
        起点を捨てても「たまたま同じ値を測り直す」ため区別が付かない
        (実際に区別の付かないアサーションで変異を見逃しかけた)。
        """
        fx, drivers, manual = self._build()
        await manual.set_value("test_axis", 5.0)
        feed_edulite(drivers["dropped"], position=2.0, mode_state=2)  # 励磁中のまま
        feed_edulite(drivers["healthy"], position=0.0, mode_state=2)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        # 起点 (5.0) が保たれていれば 5.5。捨てられていればフィードバック (2.0) から
        # 積み直すので 2.5 になる
        assert await manual.jog("test_axis", 0.5) == pytest.approx(5.5, abs=0.01)


class TestPairedAxisIsExpandedToPartner:
    """直結ペア (`rotate` = EDULITE x2) の片側だけが無励磁になっても、対象は
    相方を含めて拡張する。

    片側だけを対象にすると、相方が移動中の場合「無励磁で連れ回されていた片側」を
    「相方に逆らって現在角を保持する片側」へ変えるだけになり、直後に
    `SyncMonitor` の偏差超過で試合が止まる (docs/invariants.md §2「ペア軸に片側だけ
    効く操作を作らない」)。
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
        """無励磁のモータが 1 台も無ければ、対象もラッチ剥がしも励磁も一切走らない。"""
        fx, drivers, _handles = self._build()
        feed_edulite(drivers["rotate_l"], position=0.0, mode_state=2)
        feed_edulite(drivers["rotate_r"], position=0.5, mode_state=2)
        feed_edulite(drivers["gripper"], position=0.0, mode_state=2)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        fx.can_manager("main_hand").activate_motors.assert_not_called()


class TestReactivationSettlesPendingReenergize:
    """逆方向の排他: 緊急停止解除の再励磁 (`_reactivate_motors`) は、同じロボットの
    `reenergize_motors` が in-flight なら**畳んでから**自分の `activate_motors` を呼ぶ。

    並走すると片方のプローブ (`feedback_probe_message()` = disable) がもう片方が
    enable したばかりのモータへ届く。DM3520 は disable で自重落下するので「戻した
    直後にもう一度落とす」形になる。

    **解除コマンドの受理そのものは待たせない** —— 待たせるのはバックグラウンドの
    再励磁タスクだけで、ハンドラ側に置くと同じ待ちが WS の直列処理を止める
    (置き場所は `TestEStopReleaseIsNotReentrant` の
    `test_the_settle_is_not_on_the_release_handlers_path` が固定する)。
    """

    async def test_reactivate_activate_motors_never_overlaps_the_pending_one(self) -> None:
        """並走しないこと。**解放を待たずに畳めることまで含めて見る。**

        `reenergize_gate` は最後まで set しない —— キャンセルが効いていれば
        `finally` を通って降りるので、それでも順序は成立する。畳む処理を
        丸ごと消すと `reactivate` が先に走り、この順序が崩れる。
        """
        fx, _dropped = _dropped_fixture()
        order: list[str] = []
        reenergize_gate = asyncio.Event()

        async def _activate(**_kwargs: object) -> list[str]:
            if not order:
                order.append("reenergize_start")
                try:
                    await reenergize_gate.wait()
                finally:
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
        # 解除の受理そのものは再励磁を待たない
        assert not fx.server._e_stop_active

        await fx.wait_reactivation()
        await fx.wait_reenergize("main_hand")

        assert order == ["reenergize_start", "reenergize_done", "reactivate"]


class TestReactivationDoesNotWaitForeverOnAStuckReenergize:
    """**畳めない再励磁タスクがいても、緊急停止解除は先へ進む。**

    素の `await pending` では済まない。有界なのは `_wait_fresh_feedback` の deadline
    だけで、`CANManager.send_to_bus` は `_run_blocking(bus.send, msg)` をタイムアウト
    無しで待つ。詰まった送信キューでブロックするのは `cbc-can-watchdog` が bus-off を
    疑っている状況、つまり**まさに緊急停止を押した状況**である。

    キャンセルだけでは足りないので上限も要る。**居座る旧タスクは `CancelledError` を
    握り潰して `await` へ戻り続けるコルーチンで作る。`run_in_executor` でスレッドへ
    入ったブロッキング呼び出しはこの手本にならない** —— 待っている側の Task は
    `cancel()` した瞬間に `CancelledError` を受け取るので `asyncio.wait` はタイムアウト
    へ一度も到達せず (実測 0.000s)、`timeout=None` にしても通ってしまう。
    """

    async def test_release_proceeds_when_the_old_task_refuses_to_cancel(self) -> None:
        fx, _dropped = _dropped_fixture()
        release_first_call = asyncio.Event()
        calls: list[str] = []

        async def _stubborn_activate(**_kwargs: object) -> list[str]:
            calls.append("main_hand")
            if len(calls) == 1:
                while not release_first_call.is_set():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.sleep(0.01)
            return []

        fx.can_manager("main_hand").activate_motors = _stubborn_activate
        fx.can_manager("sub_hand").activate_motors = AsyncMock(return_value=[])

        try:
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
            await asyncio.sleep(0)
            assert calls == ["main_hand"]

            await fx.command({"type": "e_stop"})
            await fx.command({"type": "e_stop_release"})
            # 上限が効いていれば、畳めなくても解除の励磁が先へ進み main_hand への
            # 2 回目の activate_motors が現れる。上限を無くすと 1 本目を待ち続ける
            await fx.wait_reactivation(timeout=3.0)

            assert calls == ["main_hand", "main_hand"], "解除の励磁が先へ進んでいない"
            fx.can_manager("sub_hand").activate_motors.assert_awaited_once()
        finally:
            release_first_call.set()
            await fx.wait_reenergize("main_hand")


class TestMotorCheckDeniedWhileReenergizeInFlight:
    """逆方向の排他: 動作確認は、どちらかのロボットの `reenergize_motors` が
    in-flight なら起動できない。

    零点確定 (`rotate`) は disable → SET_ZERO → enable を伴う。同じモータへ
    再励磁の `activate_motors` が並走すると、そちらのプローブ (disable) が
    動作確認側の enable と競合しうる。
    """

    async def test_denied_while_pending(self) -> None:
        fx, _dropped = _dropped_fixture()
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
    in-flight なら拒否する。

    手動へ入った直後に送るジョグは、再励磁の `activate_motors` が書く
    「フォルト前の現在角」目標と同じモータへ競合しうる。
    """

    async def test_denied_while_pending(self) -> None:
        fx = _manual_fixture()
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        client = RecordingClient()
        fx.attach_clients(client)
        try:
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
            await asyncio.sleep(0)

            await fx.command(
                {"type": "set_operation_mode", "robot": "main_hand", "mode": "manual"},
                requester=client,
            )
            assert fx.operation_mode("main_hand") == "sequence"
            rejected = client.of_type("command_rejected")
            assert len(rejected) == 1
            assert "再励磁" in rejected[0]["reason"]
        finally:
            gate.set()
            await fx.wait_reenergize("main_hand")

    async def test_allowed_once_reenergize_finishes(self) -> None:
        fx = _manual_fixture()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        await fx.command({"type": "set_operation_mode", "robot": "main_hand", "mode": "manual"})
        assert fx.operation_mode("main_hand") == "manual"


class TestManualTargetDeniedWhileReenergizeInFlight:
    """逆方向の排他その 2: 既に手動中のロボットへ再励磁をかけた最中に届く
    `manual_jog` / `manual_move` / `manual_set` を拒否する。

    `reenergize_motors` は手動操縦中も意図的に塞がない設計 (`lib/commands.py`)
    なので、手動へ「入る」ときのガード (`TestSetOperationModeDeniedWhileReenergizeInFlight`)
    だけでは、既に手動中のロボットへのジョグを塞げない。3 コマンドとも
    `_manual_target` という共通の関門を通る。
    """

    async def _enter_manual(self, fx: ServerFixture) -> None:
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])
        await fx.command({"type": "set_operation_mode", "robot": "main_hand", "mode": "manual"})
        assert fx.operation_mode("main_hand") == "manual"

    async def _start_slow_reenergize(self, fx: ServerFixture) -> asyncio.Event:
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await asyncio.sleep(0)
        return gate

    async def test_manual_jog_denied_while_pending(self) -> None:
        fx = _manual_fixture()
        await self._enter_manual(fx)
        gate = await self._start_slow_reenergize(fx)
        client = RecordingClient()
        fx.attach_clients(client)
        try:
            await fx.command(
                {"type": "manual_jog", "robot": "main_hand", "axis": "axis", "delta": 0.1},
                requester=client,
            )
            rejected = client.of_type("command_rejected")
            assert len(rejected) == 1
            assert rejected[0]["command"] == "manual_jog"
            assert "再励磁" in rejected[0]["reason"]
        finally:
            gate.set()
            await fx.wait_reenergize("main_hand")

    async def test_manual_move_denied_while_pending(self) -> None:
        fx = _manual_fixture()
        await self._enter_manual(fx)
        gate = await self._start_slow_reenergize(fx)
        client = RecordingClient()
        fx.attach_clients(client)
        try:
            await fx.command(
                {"type": "manual_move", "robot": "main_hand", "axis": "axis", "position": "home"},
                requester=client,
            )
            rejected = client.of_type("command_rejected")
            assert len(rejected) == 1
            assert rejected[0]["command"] == "manual_move"
            assert "再励磁" in rejected[0]["reason"]
        finally:
            gate.set()
            await fx.wait_reenergize("main_hand")

    async def test_manual_set_denied_while_pending(self) -> None:
        fx = _manual_fixture()
        await self._enter_manual(fx)
        gate = await self._start_slow_reenergize(fx)
        client = RecordingClient()
        fx.attach_clients(client)
        try:
            await fx.command(
                {"type": "manual_set", "robot": "main_hand", "axis": "axis", "value": 0.2},
                requester=client,
            )
            rejected = client.of_type("command_rejected")
            assert len(rejected) == 1
            assert rejected[0]["command"] == "manual_set"
            assert "再励磁" in rejected[0]["reason"]
        finally:
            gate.set()
            await fx.wait_reenergize("main_hand")

    async def test_manual_jog_allowed_once_reenergize_finishes(self) -> None:
        fx = _manual_fixture()
        await self._enter_manual(fx)
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        client = RecordingClient()
        fx.attach_clients(client)
        await fx.command(
            {"type": "manual_jog", "robot": "main_hand", "axis": "axis", "delta": 0.1},
            requester=client,
        )
        assert client.of_type("command_rejected") == []


class TestEStopDuringReenergize:
    """**再励磁と緊急停止が重なったときの 2 枚を、1 枚ずつ単独で確かめる。**

    `_send_steps` の途中 (`encode_target` → 0.05s → `encode_enable` → 0.1s) に停止が
    入りうる。守っているのは 2 つ:

    1. `activate_motors` へ渡す `should_abort` —— 残りのモータへ enable を送らない中断口
    2. 励磁を終えた後の停止フレーム再送 —— 中断判定をすり抜けた enable が停止フレーム
       より後に届き、**約 0.1 秒励磁されたまま残る**のを潰す

    **まとめて 1 ケースにしてはならない** (docs/invariants.md §9)。既存の
    `tests/test_server_e_stop.py` は解除経路しか見ておらず、**再励磁経路だけ
    `should_abort` を落とす変異も、再送だけを消す変異も 1 件も落ちなかった。**
    """

    async def test_activation_gets_a_live_e_stop_abort_hook(self) -> None:
        """1 枚目だけを見る。停止フレームの再送は観測しない。"""
        fx, _dropped = _dropped_fixture()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")

        activate = fx.can_manager("main_hand").activate_motors
        activate.assert_awaited_once()
        should_abort = activate.await_args.kwargs["should_abort"]
        # 常に False を返す中断口 (= `None` を渡したのと同じ) では意味を成さない
        assert should_abort() is False
        # 操縦者の e_stop と同じ経路で作る (フラグを直接立てると本番に無い状態を作る)
        await fx.activate_e_stop(reason="再励磁の直後に機体が異常な動きを始めた")
        assert should_abort() is True

    async def test_stop_broadcast_is_resent_after_activation(self) -> None:
        """2 枚目だけを見る。**中断口が効いたかどうかには依存させない。**

        在飛中に緊急停止が入ったら、励磁を終えた後に停止のブロードキャスト
        (`0x0FF`) がもう一度バスへ出ること。実 `CANManager` を挿してワイヤ上の
        フレームで見る (`_send_e_stop_frames` を差し替えて呼び出し回数で見ると、
        送り先も中身も変わってしまった実装を検出できない)。
        """
        sent: list[can.Message] = []
        mgr = CANManager(run_blocking=direct_runner())
        bus = mock_bus()
        bus.send.side_effect = lambda msg: sent.append(msg)
        mgr.add_bus("can_edulite", bus)
        dropped = Edulite05Driver("dropped", can_id=1)
        mgr.add_motor("can_edulite", dropped)
        feed_edulite(dropped, position=0.5, mode_state=0)

        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        mgr.activate_motors = _slow_activate  # type: ignore[method-assign]

        fx = ServerFixture.build()
        fx.add_robot("main_hand", _EmptySequence("main_hand"), mgr)
        fx.add_robot("sub_hand", _EmptySequence("sub_hand"))

        try:
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
            await asyncio.sleep(0)

            await fx.activate_e_stop(reason="再励磁の在飛中に押した")
            before = _count_e_stop_broadcasts(sent)
            assert before >= 1, "緊急停止そのものが停止フレームを出していない"
        finally:
            gate.set()
            await fx.wait_reenergize("main_hand")

        assert _count_e_stop_broadcasts(sent) > before, (
            "再励磁の完了後に停止フレームが送り直されていない"
            " (中断判定をすり抜けた enable が停止より後に届いたまま残る)"
        )


class _HoldingSequence(Sequence):
    """1 ステップ目でテストの解放を待つシーケンス (実行中の状態を作るため)。"""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @step("解放されるまで待つ")
    async def hold(self) -> None:
        self.entered.set()
        await self.release.wait()


class TestSequenceCommandsDeniedWhileReenergizeInFlight:
    """**逆方向の排他: 在飛中のシーケンス系コマンドを拒否する。片方向だけ。**

    在飛中に NEXT が押されると、`move_to` が書いた目標を「フォルト前の現在角」が
    上書きし、`wait_reached` が動かない位置を見続けて `SequenceTimeoutError` になる
    (症状は「NEXT を押したのに動かない」だけ)。

    **逆 (シーケンス実行中の再励磁) は塞がない** —— docs/invariants.md「制御権の
    奪い合いは両方向を塞ぐ」に対する意図的な例外なので、通ることも
    `test_sequence_start_does_not_block_reenergize` で固定しておく。

    ゲートの宣言は `tests/test_commands.py` が別に固定する。ここで見るのは
    掛け合わせ (対象ロボットが今在飛中か) の側。
    """

    async def _start_slow_reenergize(self, fx: ServerFixture) -> asyncio.Event:
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await asyncio.sleep(0)
        return gate

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"type": "sequence_start"}, "シーケンスを開始"),
            ({"type": "sequence_jump", "step_index": 0}, "ステップ移動"),
            ({"type": "trigger"}, "トリガー"),
        ],
    )
    async def test_denied_while_pending(self, payload: dict, expected: str) -> None:
        fx, _dropped = _dropped_fixture()
        fx.enter_match()
        gate = await self._start_slow_reenergize(fx)
        client = RecordingClient()
        fx.attach_clients(client)
        try:
            await fx.command({**payload, "robot": "main_hand"}, requester=client)

            rejected = client.of_type("command_rejected")
            assert len(rejected) == 1
            assert "再励磁の処理中" in rejected[0]["reason"]
            assert expected in rejected[0]["reason"]
        finally:
            gate.set()
            await fx.wait_reenergize("main_hand")

    async def test_other_robot_is_not_blocked(self) -> None:
        """塞ぐのは対象ロボットだけ。もう 1 台のシーケンスは巻き込まない。"""
        fx, _dropped = _dropped_fixture()
        fx.enter_match()
        gate = await self._start_slow_reenergize(fx)
        client = RecordingClient()
        fx.attach_clients(client)
        try:
            await fx.command({"type": "sequence_start", "robot": "sub_hand"}, requester=client)
            assert client.of_type("command_rejected") == []
        finally:
            gate.set()
            await fx.wait_reenergize("main_hand")

    async def test_allowed_once_reenergize_finishes(self) -> None:
        """在飛が終われば通る。塞ぐのは 100ms〜1.5 秒だけ。"""
        fx, _dropped = _dropped_fixture()
        fx.enter_match()
        fx.can_manager("main_hand").activate_motors = AsyncMock(return_value=[])
        client = RecordingClient()
        fx.attach_clients(client)

        await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
        await fx.wait_reenergize("main_hand")
        await fx.command({"type": "sequence_start", "robot": "main_hand"}, requester=client)

        assert client.of_type("command_rejected") == []

    async def test_sequence_start_does_not_block_reenergize(self) -> None:
        """**逆方向は塞がない (意図的な例外)。**

        励磁が落ちるのはたいていシーケンスを走らせている最中で、そこで
        再励磁が使えなければ直したい状況そのものが直せない。
        """
        can_manager, _dropped = _can_manager_with_dropped_motor()
        can_manager.activate_motors = AsyncMock(return_value=[])
        sequence = _HoldingSequence("main_hand")
        fx = ServerFixture.build()
        fx.add_robot("main_hand", sequence, can_manager)
        fx.add_robot("sub_hand", _EmptySequence("sub_hand"))
        fx.enter_match()
        client = RecordingClient()
        fx.attach_clients(client)

        runner = asyncio.create_task(sequence.run_forever())
        try:
            await fx.command({"type": "sequence_start", "robot": "main_hand"})
            await asyncio.wait_for(sequence.entered.wait(), timeout=1.0)
            assert sequence.is_running

            await fx.command({"type": "reenergize_motors", "robot": "main_hand"}, requester=client)
            await fx.wait_reenergize("main_hand")

            assert client.of_type("command_rejected") == []
            can_manager.activate_motors.assert_awaited_once()
        finally:
            sequence.release.set()
            runner.cancel()


class TestInFlightIsBroadcast:
    """**在飛中を `safety.reenergizing` として配る。**

    押した後の 0.1〜1.5 秒は `unenergized_motors` が消えない (励磁が次のフィードバック
    へ反映されるまで分からない) ので、これが無いとボタンは押せるまま残り、操縦者は
    2 回目を押して拒否トーストを受け取ることになる。
    """

    async def test_flag_follows_the_task_lifetime(self) -> None:
        fx, _dropped = _dropped_fixture()
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate

        assert fx.state_message("main_hand")["safety"]["reenergizing"] is False

        try:
            await fx.command({"type": "reenergize_motors", "robot": "main_hand"})
            await asyncio.sleep(0)
            assert fx.state_message("main_hand")["safety"]["reenergizing"] is True
            # 巻き込んでいない側は False のまま (ロボットごとに独立している)
            assert fx.state_message("sub_hand")["safety"]["reenergizing"] is False
        finally:
            gate.set()
            await fx.wait_reenergize("main_hand")

        assert fx.state_message("main_hand")["safety"]["reenergizing"] is False


class TestEStopReactivationSharesTheReenergizeGate:
    """**緊急停止解除の再励磁も、単発再励磁とまったく同じゲートに掛かる。**

    どちらも `activate_motors` の「現在角を書いてから enable」を打つので、
    `blocked_during_reenergize` が防ぎたい害は両者で同型である。在飛判定が 2 系統
    (`_reactivate_tasks` / `_reenergize_tasks`) に分かれているのは実装の都合で、
    `_is_reenergizing` が片方しか見ないと**塞いだつもりの経路だけが素通りする** ——
    窓が開くのは応答の無いモータを 1 台 0.5 秒待っている間、つまり CAN が不調な
    ときほど広い (まさに緊急停止を押した状況)。
    """

    @staticmethod
    async def _hold_reactivation(fx: ServerFixture) -> asyncio.Event:
        """緊急停止 → 解除を踏み、main_hand の `activate_motors` で止めて返す。

        呼び出し側は返された Event を必ず `set()` すること (`finally` で
        `fx.wait_reactivation()` まで行う)。
        """
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        await fx.command({"type": "e_stop"})
        await fx.command({"type": "e_stop_release"})
        await asyncio.sleep(0)  # main_hand のゲートで止まらせる
        assert fx.server._reactivating
        return gate

    async def test_sequence_start_is_rejected(self) -> None:
        """試合中に E-STOP → RESET → 即 START。

        素通りすると `move_to` の目標が再励磁の現在角で上書きされ、操縦者には
        「RESET したのに START が効かず、しばらくして赤いエラーだけ出る」と見える。
        """
        fx = _build_fixture()
        fx.enter_match()
        client = RecordingClient()
        fx.attach_clients(client)
        gate = await self._hold_reactivation(fx)
        try:
            await fx.command({"type": "sequence_start", "robot": "main_hand"}, requester=client)
            rejected = client.of_type("command_rejected")
            assert len(rejected) == 1
            assert rejected[0]["command"] == "sequence_start"
            assert not fx.sequence("main_hand").is_running
        finally:
            gate.set()
            await fx.wait_reactivation()

    async def test_motor_check_start_is_rejected(self) -> None:
        """準備中に E-STOP → RESET → 即「動作確認」。

        零点確定の disable / SET_ZERO と再励磁のプローブ (EDULITE 05 / DM3520 とも
        disable) が同じモータへ並走する。`sub_lift` は disable で自重落下する。
        """
        fx = _build_fixture()
        fx.set_motor_check_sequence(_EmptySequence("motor_check"))
        gate = await self._hold_reactivation(fx)
        try:
            # 拒否理由は `motor_check_state` 1 通で運ぶ (`command_rejected` ではない)
            assert await fx.start_motor_check() is False
            assert "再励磁" in (fx.motor_check_error() or "")
        finally:
            gate.set()
            await fx.wait_reactivation()

    async def test_switching_to_manual_is_rejected(self) -> None:
        """手動へ入ってジョグすると、同じく現在角で上書きされる。

        **手動から出る方向は塞がない** (退避路から戻れなくなる) ので、ここで見るのは
        `mode: manual` への切替だけ。
        """
        fx = _manual_fixture()
        client = RecordingClient()
        fx.attach_clients(client)
        gate = await self._hold_reactivation(fx)
        try:
            await fx.command(
                {"type": "set_operation_mode", "robot": "main_hand", "mode": "manual"},
                requester=client,
            )
            rejected = client.of_type("command_rejected")
            assert len(rejected) == 1
            assert rejected[0]["command"] == "set_operation_mode"
            assert fx.operation_mode("main_hand") == "sequence"
        finally:
            gate.set()
            await fx.wait_reactivation()

    async def test_switching_back_to_sequence_is_allowed(self) -> None:
        """**手動から出る方向は在飛中も通す。** 退避路から戻れなくなるため。

        `_apply_operation_mode` のガードは `mode is OperationMode.MANUAL` の `if` の
        中にある。外へ出すと「手動へ入れないが出られもしない」機体になり、症状は
        緊急停止解除の直後の数秒にしか出ない。
        """
        fx = _manual_fixture()
        await fx.command({"type": "set_operation_mode", "robot": "main_hand", "mode": "manual"})
        assert fx.operation_mode("main_hand") == "manual"
        client = RecordingClient()
        fx.attach_clients(client)
        gate = await self._hold_reactivation(fx)
        try:
            await fx.command(
                {"type": "set_operation_mode", "robot": "main_hand", "mode": "sequence"},
                requester=client,
            )
            assert client.of_type("command_rejected") == []
            assert fx.operation_mode("main_hand") == "sequence"
        finally:
            gate.set()
            await fx.wait_reactivation()

    async def test_unknown_robot_is_not_denied(self) -> None:
        """在飛中でも、未知のロボット名は再励磁ゲートで拒否しない。

        `_reenergize_deny_reason` は「未知のロボットという別の失敗を別の理由文で覆い
        隠さない」ために素通しする。解除の再励磁はロボット名に依らず True を返すので、
        未知の名前を先に落とさないと**在飛中だけ**この性質が消える
        (`_manual_mode_deny_reason` は `self._robots.get()` で同じ性質を守っている)。
        """
        fx = _build_fixture()
        fx.enter_match()
        client = RecordingClient()
        fx.attach_clients(client)
        gate = await self._hold_reactivation(fx)
        try:
            await fx.command({"type": "sequence_start", "robot": "bogus"}, requester=client)
            assert client.of_type("command_rejected") == []
        finally:
            gate.set()
            await fx.wait_reactivation()

    async def test_safety_reports_reenergizing(self) -> None:
        """在飛中は `safety.reenergizing` として配る。

        配らないと、この数秒だけ画面はまったく平常のまま —— 操縦者は拒否トーストの
        理由を画面のどこからも確認できない。
        """
        fx = _build_fixture()
        gate = await self._hold_reactivation(fx)
        try:
            assert fx.state_message("main_hand")["safety"]["reenergizing"] is True
            # 解除の再励磁は全ロボットぶんまとめて走るので、両方が在飛である
            assert fx.state_message("sub_hand")["safety"]["reenergizing"] is True
        finally:
            gate.set()
            await fx.wait_reactivation()

        assert fx.state_message("main_hand")["safety"]["reenergizing"] is False


class TestEStopReleaseIsNotReentrant:
    """**解除を 2 回踏んでも再励磁は 1 本しか走らない。**

    並走すると、片方の鮮度確認プローブ (= disable) がもう片方の enable 直後のモータへ
    届く (`sub_lift` は disable で自重落下する)。加えて `_activate_motors_for_robot` は
    結果を無条件に `_inactive_motors[robot]` へ置き換えるので、完了順によっては
    **実際は励磁できているのに画面だけ「無励磁」が残り続ける**。窓が広がるのは CAN が
    詰まっているとき、つまりまさに緊急停止を押した状況である。
    """

    @staticmethod
    def _alive_reactivations(fx: ServerFixture) -> int:
        return sum(1 for task in fx.server._reactivate_tasks if not task.done())

    async def test_second_release_settles_the_first(self) -> None:
        """**踏むのは 2 連打ではない。** `_cmd_e_stop_release` は
        `not self._e_stop_active` を拒否するので、素の 2 連打では 2 本目が立たない。
        実際に踏むのは E-STOP → RESET →(同期ずれ検出や操縦者の再押下で)
        E-STOP → RESET で、1 本目がまだ `activate_motors` の中にいるあいだに
        2 本目が立つ。
        """
        fx = _build_fixture()
        gate = asyncio.Event()
        started = 0

        async def _slow_activate(**_kwargs: object) -> list[str]:
            nonlocal started
            started += 1
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        fx.can_manager("sub_hand").activate_motors = AsyncMock(return_value=[])

        await fx.command({"type": "e_stop"})
        await fx.command({"type": "e_stop_release"})
        await asyncio.sleep(0)
        assert fx.server._reactivating

        # 1 本目が main_hand のゲートで止まっている間に、もう一度停止 → 解除
        await fx.command({"type": "e_stop"})
        try:
            await fx.command({"type": "e_stop_release"})

            # 畳み込みは**新しいタスクの冒頭**が行うので、ハンドラが返った直後は
            # まだ 2 本ある (それがハンドラを止めていないことの裏返しでもある)。
            # 2 本目が走り出せば 1 本目は畳まれ、以後 1 本しか残らない
            assert await wait_until(lambda: self._alive_reactivations(fx) == 1), (
                "1 本目が畳まれていない (2 本の activate_motors が並走する)"
            )
        finally:
            gate.set()
            await fx.wait_reactivation()

        # **拒否はしない。** 2 回目の解除でも再励磁は必ず立ち上がる
        # (解除のたびに取り残されるロボットが出る形を作らない)
        assert started >= 2

    async def test_the_settle_is_not_on_the_release_handlers_path(self) -> None:
        """**畳み込みは解除ハンドラの経路に無い。**

        `async for msg in ws` は 1 接続あたり完全に直列なので、ハンドラで待つと次の
        1 通が処理されず **E-STOP の押し直しすら効かなくなる**。
        `_settle_pending_reactivation` は畳めない相手を最悪
        `_PENDING_TASK_CANCEL_TIMEOUT_S` 待つので、ハンドラに置けばその時間を自分で
        作ることになる。

        **所要時間では見られない** —— `run_in_executor` の待ちは `Task.cancel()` で
        即座に畳めるので、置き場所に関わらず一瞬で終わる。代わりに「ハンドラが返った
        時点で前回のぶんはまだ生きている」を見る (ハンドラが畳んでいたなら成立し得ず、
        実時間にも依存しない)。
        """
        fx = _build_fixture()
        gate = asyncio.Event()

        async def _slow_activate(**_kwargs: object) -> list[str]:
            await gate.wait()
            return []

        fx.can_manager("main_hand").activate_motors = _slow_activate
        fx.can_manager("sub_hand").activate_motors = AsyncMock(return_value=[])

        try:
            await fx.command({"type": "e_stop"})
            await fx.command({"type": "e_stop_release"})
            await asyncio.sleep(0)
            first = next(iter(fx.server._reactivate_tasks))
            assert not first.done()

            await fx.command({"type": "e_stop"})
            await fx.command({"type": "e_stop_release"})
            # ハンドラは畳み込みへ 1 度も入らずに返っている
            assert not first.done(), "解除の受理が前回の畳み込みを待っている"
            assert self._alive_reactivations(fx) == 2

            # 畳むのは新しいタスクの側。イベントループへ制御を返せば必ず畳まれる
            assert await wait_until(first.done), "1 本目が畳まれていない"
            assert self._alive_reactivations(fx) == 1
        finally:
            gate.set()
            await fx.wait_reactivation()

    async def test_release_proceeds_when_the_old_reactivation_refuses_to_cancel(self) -> None:
        """**畳めない旧タスクがいても、新しい解除の励磁は永久に待たされない。**

        `_settle_pending_reactivation` の上限を無くすと、キャンセルへ応じない旧タスクを
        無期限に待ち、解除そのものが返らなくなる。**上限は単発側
        (`_settle_pending_reenergize`) と同じ定数を共有しているが、層としては別なので
        1 枚ずつ確かめる** —— 片方だけ上限を外しても、もう一方のテストは緑のままである。

        居座る旧タスクを `CancelledError` を握り潰すコルーチンで作る理由は
        `TestReactivationDoesNotWaitForeverOnAStuckReenergize` と同じ。
        """
        fx = _build_fixture()
        release_first_call = asyncio.Event()
        calls: list[str] = []

        async def _stubborn_activate(**_kwargs: object) -> list[str]:
            calls.append("main_hand")
            if len(calls) == 1:
                while not release_first_call.is_set():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.sleep(0.01)
            return []

        fx.can_manager("main_hand").activate_motors = _stubborn_activate
        fx.can_manager("sub_hand").activate_motors = AsyncMock(return_value=[])

        try:
            await fx.command({"type": "e_stop"})
            await fx.command({"type": "e_stop_release"})
            assert await wait_until(lambda: calls == ["main_hand"])

            # 2 本目は 1 本目を畳もうとするが、1 本目はキャンセルへ応じない
            await fx.command({"type": "e_stop"})
            await fx.command({"type": "e_stop_release"})

            # 上限が効いていれば、畳めなくても 2 本目は先へ進む。
            # 上限を無くすと 1 本目を待ち続けてここに到達しない
            assert await wait_until(lambda: calls == ["main_hand", "main_hand"]), (
                "畳めない旧タスクを待って解除の励磁が進んでいない"
            )
        finally:
            release_first_call.set()
            with contextlib.suppress(TimeoutError):
                await fx.wait_reactivation(timeout=2.0)
