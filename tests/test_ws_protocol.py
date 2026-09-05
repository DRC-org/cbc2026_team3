from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from lib.can_manager import CANManager
from lib.control.target_refresh import GenericTargetRefresher
from lib.drivers.base import ControlMode, MotorState
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import M3508Driver
from lib.health import BusHealth, MotorHealth
from lib.sequence.engine import Sequence, step
from lib.sequence.motors import MotorGroup, MotorHandle
from tests.fake_can import mock_can_manager, set_motors
from tests.fake_health import ok_health_snapshot
from tests.feedback_frames import feed_generic, feed_m3508
from tests.server_fixtures import ServerFixture, recv_type


class DummySequence(Sequence):
    """テスト用の最小シーケンス。"""

    def __init__(self) -> None:
        super().__init__("test_seq")
        self.triggered = False

    @step("待機ステップ", require_trigger=True)
    async def wait_step(self) -> None:
        self.triggered = True


def _build_fixture() -> ServerFixture:
    fx = ServerFixture.build()
    fx.add_robot(
        "main_hand",
        DummySequence(),
        mock_can_manager(
            {"m3508_1": MotorState(position=1500.0, velocity=0.0, current=0.2, temperature=35.0)},
            bus_name="generic_bus",
        ),
    )
    return fx


class TestStateMessageFormat:
    async def test_state_message_format(self) -> None:
        """state メッセージの JSON 形式を検証する。"""
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            # ブロードキャストを手動でトリガー
            await fx.publish_state()

            msg = await recv_type(ws, "state")
            assert msg is not None
            assert msg["type"] == "state"
            assert msg["robot"] == "main_hand"
            assert msg["sequence"] == "test_seq"
            assert "current_step" in msg
            assert "step_index" in msg
            assert "total_steps" in msg
            assert "waiting_trigger" in msg
            assert "motors" in msg
            assert "m3508_1" in msg["motors"]

            motor_data = msg["motors"]["m3508_1"]
            assert motor_data["pos"] == 1500.0
            assert motor_data["vel"] == 0.0
            assert motor_data["torque"] == 0.2
            assert motor_data["temp"] == 35.0

            await ws.close()


class TestTriggerCommand:
    async def test_trigger_command(self) -> None:
        """trigger コマンドでシーケンスの trigger() が呼ばれることを検証する。"""
        fx = _build_fixture()
        fx.enter_match()
        seq = fx.sequence("main_hand")
        app = fx.create_app()

        # シーケンスを実行して trigger 待ち状態にする
        task = asyncio.create_task(seq.run())
        await asyncio.sleep(0.05)
        assert seq.waiting_trigger is True

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "trigger", "robot": "main_hand"})
            await asyncio.sleep(0.05)

            assert seq.waiting_trigger is False
            await ws.close()

        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


class TestEStopCommand:
    async def test_e_stop_command(self) -> None:
        """e_stop コマンドが処理されることを検証する。"""
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "e_stop"})
            await asyncio.sleep(0.05)
            await ws.close()

        # send_to_bus が呼ばれたことを確認
        fx.can_manager("main_hand").send_to_bus.assert_called()


class TestUnknownCommandIgnored:
    async def test_unknown_command_ignored(self) -> None:
        """不明なコマンドでエラーにならないことを検証する。"""
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "totally_unknown_command"})
            await asyncio.sleep(0.05)

            # 接続が維持されていることを確認
            assert not ws.closed

            # 正常にブロードキャストを受信できることを確認
            await fx.publish_state()
            msg = await recv_type(ws, "state")
            assert msg is not None

            await ws.close()


class TestEStopSetsActiveState:
    async def test_e_stop_sets_active_state(self) -> None:
        """e_stop コマンドで e_stop_state メッセージが配信されることを検証する。"""
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "e_stop"})
            await asyncio.sleep(0.05)

            assert fx.e_stop_active is True

            msg = await recv_type(ws, "e_stop_state")
            assert msg is not None
            assert msg["active"] is True

            await ws.close()


class TestEStopRelease:
    async def test_e_stop_release(self) -> None:
        """e_stop_release コマンドで e_stop_state active=false が配信されることを検証する。"""
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            # まず緊急停止を有効化
            await ws.send_json({"type": "e_stop"})
            await asyncio.sleep(0.05)
            msg = await recv_type(ws, "e_stop_state")
            assert msg is not None
            assert msg["active"] is True

            # 緊急停止を解除
            await ws.send_json({"type": "e_stop_release"})
            await asyncio.sleep(0.05)

            assert fx.e_stop_active is False

            # ブロードキャストループの state メッセージが混在するため、
            # e_stop_state メッセージが見つかるまで読み進める
            found = False
            for _ in range(20):
                try:
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=0.1)
                except TimeoutError:
                    break
                if msg.get("type") == "e_stop_state" and msg.get("active") is False:
                    found = True
                    break
            assert found, "e_stop_state active=false メッセージが配信されなかった"

            await ws.close()


class TestStateIncludesEStopActive:
    async def test_state_includes_e_stop_active(self) -> None:
        """state メッセージに e_stop_active フィールドが含まれることを検証する。"""
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            await fx.publish_state()
            msg = await recv_type(ws, "state")
            assert msg is not None
            assert "e_stop_active" in msg
            assert msg["e_stop_active"] is False

            await ws.close()


class TestSetParamCommand:
    async def test_set_param_command(self) -> None:
        """set_param コマンドの受付を検証する。"""
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json(
                {
                    "type": "set_param",
                    "motor": "m3508_1",
                    "key": "kp",
                    "value": 1.5,
                }
            )
            await asyncio.sleep(0.05)

            # エラーなく接続が維持されていること
            assert not ws.closed
            await ws.close()


def _fault_health_snapshot(mgr: CANManager):
    """全モータ FAULT のスナップショット (health_change 差分を作るため)。"""
    snap = ok_health_snapshot(mgr)
    for motor in snap.motors:
        motor.state = MotorHealth.FAULT
    snap.overall = BusHealth.DOWN
    return snap


class TestStateIncludesRunning:
    async def test_state_includes_running(self) -> None:
        """state に running が含まれること。

        欠けていると UI が step_index/total_steps から実行中かを推測することになる。
        """
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            await fx.publish_state()
            msg = await recv_type(ws, "state")
            assert msg is not None
            assert msg["running"] is False

            await ws.close()

    async def test_state_running_true_while_sequence_runs(self) -> None:
        """シーケンス実行中は running=true になること。"""
        fx = _build_fixture()
        fx.enter_match()
        seq = fx.sequence("main_hand")
        app = fx.create_app()

        task = asyncio.create_task(seq.run())
        await asyncio.sleep(0.05)

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await fx.publish_state()
            msg = await recv_type(ws, "state")
            assert msg is not None
            assert msg["running"] is True
            await ws.close()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestHealthChangeIncludesRobot:
    async def test_health_change_includes_robot(self) -> None:
        """health_change にどの機体のイベントかが載ること。

        Monitor は 2 機分のイベントを 1 本のリストに並べるため、robot が無いと
        どちらの異常か判別できない。
        """
        fx = _build_fixture()
        can_mgr = fx.can_manager("main_hand")
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")

            # 1 回目で前回スナップショットを作り、2 回目で FAULT への遷移を出す
            await fx.publish_state()
            can_mgr.health.side_effect = lambda **_kwargs: _fault_health_snapshot(can_mgr)
            await fx.publish_state()

            msg = await recv_type(ws, "health_change")
            assert msg is not None
            assert msg["robot"] == "main_hand"
            assert msg["target"] == "motor:m3508_1"

            await ws.close()


def _telemetry_fixture(*, dry_run: bool = False) -> ServerFixture:
    """測れる項目が違う 3 台を 1 台のロボットへ載せる。

    M3508 (4 値とも測れる) / サーボ基板 (位置だけ) / DC 基板 (1 つも測れない)。
    実ドライバを挿すのは、測定可否の宣言がドライバ側にしか無いため。状態は実機と
    同じフィードバックフレームで作る (``driver._state`` への直接代入はデコード層を
    丸ごと迂回する)。
    """
    fx = ServerFixture.build(dry_run=dry_run)
    mgr = mock_can_manager(["y_axis_r"], bus_name="can_m3508")

    y_axis_r = M3508Driver("y_axis_r", 1)
    gripper = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
    conveyor = GenericDriver("conveyor", 0x80, control_type=ControlMode.DUTY)
    feed_m3508(y_axis_r, deg=90.0, rpm=120, current=200, temp=42)
    feed_generic(gripper, position=5.0, reached=True)
    # DC 基板は状態フラグ 1 バイトだけ (DLC=1)
    feed_generic(conveyor)

    set_motors(mgr, {"y_axis_r": y_axis_r, "gripper": gripper, "conveyor": conveyor})
    fx.add_robot("main_hand", DummySequence(), mgr)
    return fx


class TestUnmeasuredTelemetryIsNull:
    """測る手段の無い項目は 0 ではなく null で配ること。

    自作モタドラの DC 基板・電磁弁基板は電流も温度も測れず、位置も持たない
    (仕様書 §3.2)。0.0 を配ると UI には「測ったように見える 0」が出て、操縦者は
    「本当に 0 なのか、フィードバックが無いのか」を区別できない。
    """

    async def _motors(self, fx: ServerFixture) -> dict:
        app = fx.create_app()
        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await fx.publish_state()
            msg = await recv_type(ws, "state")
            assert msg is not None
            await ws.close()
            return msg["motors"]

    async def test_dc_board_carries_no_numbers_at_all(self) -> None:
        motors = await self._motors(_telemetry_fixture())

        assert motors["conveyor"] == {
            "pos": None,
            "vel": None,
            "torque": None,
            "temp": None,
            "pid": None,
            "target": None,
            "saturated": False,
            # 指令もまだ出していない (出した後の形は TestCommandValue が見る)
            "command": None,
            "command_mode": None,
        }

    async def test_servo_board_carries_position_only(self) -> None:
        motors = await self._motors(_telemetry_fixture())

        assert motors["gripper"]["pos"] == pytest.approx(5.0)
        assert motors["gripper"]["vel"] is None
        assert motors["gripper"]["torque"] is None
        assert motors["gripper"]["temp"] is None

    async def test_m3508_carries_all_four(self) -> None:
        """対の確認。測れる側まで落とすと、過熱も追従も画面から読めなくなる。"""
        motors = await self._motors(_telemetry_fixture())

        # 位置はエンコーダの刻み (8192 カウント/回転) ぶんだけずれる
        assert motors["y_axis_r"]["pos"] == pytest.approx(90.0, abs=0.1)
        assert motors["y_axis_r"]["vel"] == pytest.approx(120.0)
        assert motors["y_axis_r"]["torque"] is not None
        assert motors["y_axis_r"]["temp"] == pytest.approx(42.0)

    async def test_dry_run_follows_the_same_rule(self) -> None:
        """dry-run の擬似値も同じ関門を通す。

        見栄えの値を作ってよいのは実機が測れる項目だけで、DC 基板に温度や速度を
        作ると、机上で確かめている画面が実機と別物になる。
        """
        motors = await self._motors(_telemetry_fixture(dry_run=True))

        assert motors["conveyor"]["pos"] is None
        assert motors["conveyor"]["vel"] is None
        assert motors["conveyor"]["torque"] is None
        assert motors["conveyor"]["temp"] is None
        assert motors["gripper"]["pos"] is not None, "机上で位置の描画を確かめられない"
        assert motors["gripper"]["temp"] is None
        # 実機が 4 値とも測れるモータには擬似値が入る (dry-run の目的そのもの)
        assert motors["y_axis_r"]["temp"] is not None


def _command_fixture() -> tuple[ServerFixture, MotorGroup, GenericTargetRefresher]:
    """指令値を追える最小構成。**本番と同じく `MotorGroup` を 1 つだけ作って共有する。**

    シーケンス・手動・目標値再送が別々のハンドルを持つと、緊急停止で捨てられる
    目標と画面に出る指令が別物になる (`main._wire_one_robot` は 1 つを共有する)。
    """
    fx = ServerFixture.build()
    mgr = mock_can_manager(["conveyor"], bus_name="can_generic")

    conveyor = GenericDriver("conveyor", 0x80, control_type=ControlMode.DUTY)
    gripper = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
    set_motors(mgr, {"conveyor": conveyor, "gripper": gripper})

    group = MotorGroup()
    for driver in (conveyor, gripper):
        group.add(MotorHandle(driver.name, driver, mgr, is_estop_active=lambda: fx.e_stop_active))

    sequence = DummySequence()
    sequence.bind_motors(group)
    # 目標値再送は緊急停止で目標を捨てる側でもある。登録しないと、停止しても
    # 指令が残り続ける構成 (本番には存在しない) をテストしてしまう
    refresher = GenericTargetRefresher(list(group.handles))
    fx.add_robot("main_hand", sequence, mgr, target_refreshers=[refresher])
    return fx, group, refresher


class TestCommandValue:
    """PC が最後に送った指令値を配ること。

    **フィードバックを持たない基板 (DC・電磁弁) では、これが画面に出せる唯一の
    「今どうなっているか」である。** 実測 4 値はすべて null になるので、指令まで
    落とすと画面はそのモータについて何も言えなくなる。
    """

    async def test_duty_command_appears_with_its_mode(self) -> None:
        fx, group, _ = _command_fixture()
        await group["conveyor"].set_target(ControlMode.DUTY, 0.3)

        motor = fx.state_message("main_hand")["motors"]["conveyor"]
        assert motor["command"] == pytest.approx(0.3)
        assert motor["command_mode"] == "duty"
        # 実測値は測れないので null のまま。**指令が実測へ化けてはならない**
        assert motor["pos"] is None

    async def test_never_commanded_motor_is_null(self) -> None:
        """起動直後に 0 を出さない。0 は「duty 0 を出している」と読める。"""
        fx, _group, _ = _command_fixture()

        motor = fx.state_message("main_hand")["motors"]["gripper"]
        assert motor["command"] is None
        assert motor["command_mode"] is None

    async def test_e_stop_clears_the_command(self) -> None:
        """**緊急停止中は指令も消える。**

        停止中に `→0.30` と出ていると、操縦者は「まだコンベアへ 0.3 を出し続けて
        いる」と読む。実際には停止時に `GenericTargetRefresher.clear_targets()` が
        ハンドルの目標ごと捨てており (捨てないと解除した瞬間に再送が走って
        操縦者の操作なしにコンベアが回り出す)、再送は 1 通も出ていない。
        """
        fx, group, _ = _command_fixture()
        await group["conveyor"].set_target(ControlMode.DUTY, 0.3)

        await fx.activate_e_stop(reason="テスト")

        motor = fx.state_message("main_hand")["motors"]["conveyor"]
        assert motor["command"] is None, "停止中なのに指令が出続けているように見える"
        assert motor["command_mode"] is None

    async def test_motor_outside_the_group_is_null(self) -> None:
        """``MotorGroup`` に居ないモータでも配信を落とさない。"""
        fx, _group, _ = _command_fixture()
        mgr = fx.can_manager("main_hand")
        # 電磁弁を 1 枚だけ増設し、MotorGroup へは登録しない
        spare = GenericDriver("spare_valve", 0x81, control_type=ControlMode.ON_OFF)
        set_motors(mgr, {**mgr.motors, "spare_valve": spare})

        motor = fx.state_message("main_hand")["motors"]["spare_valve"]
        assert motor["command"] is None
        assert motor["command_mode"] is None

    async def test_robot_without_bound_motors_does_not_raise(self) -> None:
        """位置定数を読めていないロボット (`has_motors` が False) でも配信は続く。

        ここで例外にすると state 配信ごと落ち、そのロボットの画面が全部止まる。
        """
        fx = ServerFixture.build()
        mgr = mock_can_manager(["conveyor"], bus_name="can_generic")
        set_motors(
            mgr, {"conveyor": GenericDriver("conveyor", 0x80, control_type=ControlMode.DUTY)}
        )
        fx.add_robot("main_hand", DummySequence(), mgr)

        motor = fx.state_message("main_hand")["motors"]["conveyor"]
        assert motor["command"] is None
        assert motor["command_mode"] is None
