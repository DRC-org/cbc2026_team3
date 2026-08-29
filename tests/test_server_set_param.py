"""set_param (PID チューニング) の受理・拒否を検証する。

以前の set_param はログを出すだけで何もしておらず、/pid-tuning タブは
「送信しました」の顔をして一切効いていなかった。受け付けたように見えて何もしないのが
最悪なので、通らない要求は必ず command_rejected で理由を返す。
"""

from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from lib.axis_sync import MotorSpec, SyncGroup
from lib.control.position_loop import MAX_TUNABLE_GAIN, M3508PositionLoop, make_position_pid
from lib.drivers.m3508 import M3508Driver
from lib.sequence.engine import Sequence, step
from tests.fake_can import mock_can_manager
from tests.server_fixtures import ServerFixture, expect_no_type, recv_type, require_type

M3508_BUS = "m3508_bus"


class _DummySequence(Sequence):
    def __init__(self) -> None:
        super().__init__("test_seq")

    @step("ノーオペ")
    async def noop(self) -> None:
        return None


def _build_fixture() -> tuple[ServerFixture, M3508PositionLoop]:
    """M3508 ペア軸 (PC 側 PID あり) と generic モータ (PID なし) を持つ構成。"""
    fx = ServerFixture.build()
    mgr = mock_can_manager(["y_axis_r", "y_axis_l", "gripper"], bus_name=M3508_BUS)
    loop = M3508PositionLoop(mgr, M3508_BUS)
    loop.add_motor("y_axis_r", M3508Driver("y_axis_r", 1), make_position_pid(2.0))
    loop.add_motor("y_axis_l", M3508Driver("y_axis_l", 2), make_position_pid(2.0))
    loop.add_sync_group(
        SyncGroup(
            name="y_axis",
            members=(
                MotorSpec(name="y_axis_r", scale=1.0, offset=0.0),
                MotorSpec(name="y_axis_l", scale=-1.0, offset=0.0),
            ),
            tolerance=5.0,
        )
    )
    fx.add_robot("main_hand", _DummySequence(), mgr, position_loops=[loop])
    return fx, loop


async def _send_set_param(ws, **payload) -> None:
    await ws.send_json({"type": "set_param", **payload})


class TestSetParamApplies:
    async def test_gain_reaches_the_position_loop(self) -> None:
        fx, loop = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _send_set_param(ws, motor="y_axis_r", gains={"kp": 3.5})
            await asyncio.sleep(0.05)

            assert loop.pid("y_axis_r").kp == 3.5
            await ws.close()

    async def test_sync_pair_receives_the_same_gain(self) -> None:
        """左右直結ペアは片側だけ別ゲインにしない (押し合って機構が壊れる)。"""
        fx, loop = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _send_set_param(ws, motor="y_axis_l", gains={"ki": 0.25})
            await asyncio.sleep(0.05)

            assert loop.pid("y_axis_l").ki == 0.25
            assert loop.pid("y_axis_r").ki == 0.25
            await ws.close()

    async def test_three_gains_arrive_in_one_message(self) -> None:
        """3 値は 1 通で運ぶ。

        分けて送ると混ざった状態が 200Hz の制御周期をまたいで残り、通らないときの
        拒否も 3 通に増える。
        """
        fx, loop = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _send_set_param(ws, motor="y_axis_r", gains={"kp": 1.5, "ki": 0.1, "kd": 0.05})
            await asyncio.sleep(0.05)

            pid = loop.pid("y_axis_r")
            assert (pid.kp, pid.ki, pid.kd) == (1.5, 0.1, 0.05)
            await ws.close()

    async def test_partial_request_leaves_the_others_alone(self) -> None:
        fx, loop = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _send_set_param(ws, motor="y_axis_r", gains={"kd": 0.4})
            await asyncio.sleep(0.05)

            pid = loop.pid("y_axis_r")
            assert (pid.kp, pid.ki, pid.kd) == (2.0, 0.0, 0.4)
            await ws.close()

    async def test_accepted_request_is_not_rejected(self) -> None:
        fx, _ = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _send_set_param(ws, motor="y_axis_r", gains={"kp": 1.0})

            await expect_no_type(ws, "command_rejected")
            await ws.close()


class TestSetParamRejections:
    async def _expect_rejection(self, payload: dict) -> dict:
        fx, _ = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _send_set_param(ws, **payload)
            msg = await recv_type(ws, "command_rejected")
            await ws.close()

        assert msg is not None, f"拒否理由が返らなかった: {payload}"
        assert msg["command"] == "set_param"
        assert msg["reason"]
        return msg

    async def test_unknown_motor_is_rejected(self) -> None:
        msg = await self._expect_rejection({"motor": "nope", "gains": {"kp": 1.0}})
        assert "nope" in msg["reason"]

    async def test_motor_without_pc_side_pid_is_rejected(self) -> None:
        """generic / EDULITE はドライバ側で制御しており PC 側に PID が無い。"""
        msg = await self._expect_rejection({"motor": "gripper", "gains": {"kp": 1.0}})
        assert "gripper" in msg["reason"]

    async def test_unsupported_key_is_rejected(self) -> None:
        await self._expect_rejection({"motor": "y_axis_r", "gains": {"dead_band": 1.0}})

    async def test_missing_gains_is_rejected(self) -> None:
        await self._expect_rejection({"motor": "y_axis_r"})

    async def test_empty_gains_is_rejected(self) -> None:
        """1 つも指定しない差し替えは誤送信。受理すると送ったつもりで効かない。"""
        await self._expect_rejection({"motor": "y_axis_r", "gains": {}})

    async def test_non_object_gains_is_rejected(self) -> None:
        await self._expect_rejection({"motor": "y_axis_r", "gains": 1.0})

    async def test_non_numeric_value_is_rejected(self) -> None:
        await self._expect_rejection({"motor": "y_axis_r", "gains": {"kp": "1.0"}})

    async def test_boolean_value_is_rejected(self) -> None:
        """bool は Python では int だが、ゲインとしては明らかに誤送信。"""
        await self._expect_rejection({"motor": "y_axis_r", "gains": {"kp": True}})

    async def test_negative_gain_is_rejected(self) -> None:
        """負のゲインは正帰還になり発散する。"""
        await self._expect_rejection({"motor": "y_axis_r", "gains": {"kp": -1.0}})

    async def test_過大なゲインは拒否される(self) -> None:
        """出力レンジを超えるゲインは調整ではなく打ち間違い。

        `kp=1e6` は不感帯を出た瞬間に必ず出力上限へ張り付くので、位置制御が
        ゲイン調整のできないバンバン制御になる。緊急停止を解除した瞬間や
        目標を入れた瞬間にフルスケール電流が出る形なので、下限と同じく上限も要る。
        """
        msg = await self._expect_rejection(
            {"motor": "y_axis_r", "gains": {"kp": MAX_TUNABLE_GAIN * 10}}
        )
        assert str(int(MAX_TUNABLE_GAIN)) in msg["reason"]

    async def test_上限ちょうどは通る(self) -> None:
        # 「上限を入れたら全部弾かれる」実装になっていないことを示す
        fx, loop = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _send_set_param(ws, motor="y_axis_r", gains={"kp": MAX_TUNABLE_GAIN})
            await expect_no_type(ws, "command_rejected")
            await ws.close()

        assert loop.pid("y_axis_r").kp == MAX_TUNABLE_GAIN

    async def test_non_finite_value_is_rejected(self) -> None:
        fx, loop = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            # JSON に NaN/Infinity のリテラルは無いので文字列経由で送る
            await ws.send_str(
                '{"type": "set_param", "motor": "y_axis_r", "gains": {"kp": Infinity}}'
            )
            msg = await recv_type(ws, "command_rejected")
            await ws.close()

        assert msg is not None
        assert msg["command"] == "set_param"
        assert loop.pid("y_axis_r").kp == 2.0

    async def test_rejected_request_does_not_change_gains(self) -> None:
        fx, loop = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _send_set_param(ws, motor="y_axis_r", gains={"kp": -3.0})
            await recv_type(ws, "command_rejected")
            await ws.close()

        assert loop.pid("y_axis_r").kp == 2.0


class TestPidGainsAreBroadcast:
    """現在ゲインを state に載せる。

    載せていなかった頃、/pid-tuning は開いた瞬間に Kp/Ki/Kd を 0.00 と表示し、
    そのまま送ると全ゲインが 0 になって位置制御ループが無効化された。
    画面が「自分の持っていない値」を送れてしまう形そのものを塞ぐ。
    """

    async def test_state_carries_the_gains_in_effect(self) -> None:
        fx, _ = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            msg = await require_type(ws, "state")

            pid = msg["motors"]["y_axis_r"]["pid"]
            assert (pid["kp"], pid["ki"], pid["kd"]) == (2.0, 0.0, 0.0)
            await ws.close()

    async def test_pair_member_reports_both_sides_as_targets(self) -> None:
        """「送ると誰に効くか」まで配る。UI に名前から推測させない。"""
        fx, _ = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            msg = await require_type(ws, "state")

            assert msg["motors"]["y_axis_l"]["pid"]["applies_to"] == ["y_axis_r", "y_axis_l"]
            await ws.close()

    async def test_motor_without_pc_side_pid_reports_null(self) -> None:
        """PC 側 PID を持たないモータは null。UI はこれで調整対象から外す。"""
        fx, _ = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            msg = await require_type(ws, "state")

            assert msg["motors"]["gripper"]["pid"] is None
            await ws.close()

    async def test_applied_gain_appears_in_the_next_state(self) -> None:
        """送った値が次の配信で返ってくる。画面の表示と実際の値が食い違わない。"""
        fx, _ = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await require_type(ws, "state")
            await _send_set_param(ws, motor="y_axis_r", gains={"kp": 3.5})
            await asyncio.sleep(0.05)

            assert await _wait_for_gain(ws, "y_axis_r", "kp", 3.5)
            await ws.close()

    async def test_dry_run_carries_the_real_gains(self) -> None:
        """dry-run でもゲインは実物の位置制御ループから取る。

        擬似モータ状態を作る分岐の中へ入れると、dry-run では全モータが
        「調整不可」になり、机上で UI を確かめられなくなる。
        """
        fx = ServerFixture.build(dry_run=True)
        mgr = mock_can_manager(["y_axis_r"], bus_name=M3508_BUS)
        loop = M3508PositionLoop(mgr, M3508_BUS)
        loop.add_motor("y_axis_r", M3508Driver("y_axis_r", 1), make_position_pid(2.0))
        fx.add_robot("main_hand", _DummySequence(), mgr, position_loops=[loop])
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            msg = await require_type(ws, "state")

            assert msg["motors"]["y_axis_r"]["pid"]["kp"] == 2.0
            await ws.close()


async def _wait_for_gain(ws, motor: str, key: str, wanted: float, *, tries: int = 40) -> bool:
    """指定ゲインが配信されるまで state を読み進める。"""
    for _ in range(tries):
        msg = await recv_type(ws, "state")
        if msg is None:
            return False
        pid = msg["motors"][motor]["pid"]
        if pid is not None and pid[key] == wanted:
            return True
    return False
