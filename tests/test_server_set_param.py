"""set_param (PID チューニング) の受理・拒否を検証する。

以前の set_param はログを出すだけで何もしておらず、/pid-tuning タブは
「送信しました」の顔をして一切効いていなかった。受け付けたように見えて何もしないのが
最悪なので、通らない要求は必ず command_rejected で理由を返す。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from aiohttp.test_utils import TestClient, TestServer

from lib.axis_sync import MotorSpec, SyncGroup
from lib.can_manager import CANManager
from lib.control.position_loop import M3508PositionLoop, make_position_pid
from lib.drivers.base import MotorState
from lib.drivers.m3508 import M3508Driver
from lib.sequence.engine import Sequence, step
from lib.server import RobotServer
from tests.fake_health import ok_health_snapshot

M3508_BUS = "m3508_bus"


class _DummySequence(Sequence):
    def __init__(self) -> None:
        super().__init__("test_seq")

    @step("ノーオペ")
    async def noop(self) -> None:
        return None


def _make_mock_can_manager(motor_names: list[str]) -> CANManager:
    mgr = MagicMock(spec=CANManager)
    motors = {}
    for name in motor_names:
        motor = MagicMock()
        motor.state = MotorState(position=0.0, velocity=0.0, current=0.0, temperature=30.0)
        motor.name = name
        motors[name] = motor
    # 実物では motors は _motors の読み取り専用ビュー。fake_health が _motors を
    # 見るため、モックでは両方を同じ dict に揃える
    mgr._motors = motors
    mgr.motors = motors
    mgr.send = AsyncMock()
    mgr.send_to_bus = AsyncMock()
    mgr._buses = {M3508_BUS: MagicMock()}
    mgr.bus_names = tuple(mgr._buses)
    mgr.health.side_effect = lambda **_kwargs: ok_health_snapshot(mgr)
    return mgr


def _build_server() -> tuple[RobotServer, M3508PositionLoop]:
    """M3508 ペア軸 (PC 側 PID あり) と generic モータ (PID なし) を持つ構成。"""
    server = RobotServer()
    mgr = _make_mock_can_manager(["y_axis_r", "y_axis_l", "gripper"])
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
    server.add_robot("main_hand", _DummySequence(), mgr, position_loops=[loop])
    return server, loop


async def _recv_type(ws, wanted: str, *, tries: int = 40) -> dict | None:
    for _ in range(tries):
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=0.2)
        except (TimeoutError, TypeError):
            return None
        if msg.get("type") == wanted:
            return msg
    return None


async def _send_set_param(ws, **payload) -> None:
    await ws.send_json({"type": "set_param", **payload})


class TestSetParamApplies:
    async def test_gain_reaches_the_position_loop(self) -> None:
        server, loop = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _send_set_param(ws, motor="y_axis_r", key="kp", value=3.5)
            await asyncio.sleep(0.05)

            assert loop.pid("y_axis_r").kp == 3.5
            await ws.close()

    async def test_sync_pair_receives_the_same_gain(self) -> None:
        """左右直結ペアは片側だけ別ゲインにしない (押し合って機構が壊れる)。"""
        server, loop = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _send_set_param(ws, motor="y_axis_l", key="ki", value=0.25)
            await asyncio.sleep(0.05)

            assert loop.pid("y_axis_l").ki == 0.25
            assert loop.pid("y_axis_r").ki == 0.25
            await ws.close()

    async def test_all_three_keys_are_accepted(self) -> None:
        server, loop = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            for key, value in (("kp", 1.5), ("ki", 0.1), ("kd", 0.05)):
                await _send_set_param(ws, motor="y_axis_r", key=key, value=value)
            await asyncio.sleep(0.05)

            pid = loop.pid("y_axis_r")
            assert (pid.kp, pid.ki, pid.kd) == (1.5, 0.1, 0.05)
            await ws.close()

    async def test_accepted_request_is_not_rejected(self) -> None:
        server, _ = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _send_set_param(ws, motor="y_axis_r", key="kp", value=1.0)

            for _ in range(8):
                try:
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=0.1)
                except (TimeoutError, TypeError):
                    break
                assert msg.get("type") != "command_rejected", msg
            await ws.close()


class TestSetParamRejections:
    async def _expect_rejection(self, payload: dict) -> dict:
        server, _ = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _send_set_param(ws, **payload)
            msg = await _recv_type(ws, "command_rejected")
            await ws.close()

        assert msg is not None, f"拒否理由が返らなかった: {payload}"
        assert msg["command"] == "set_param"
        assert msg["reason"]
        return msg

    async def test_unknown_motor_is_rejected(self) -> None:
        msg = await self._expect_rejection({"motor": "nope", "key": "kp", "value": 1.0})
        assert "nope" in msg["reason"]

    async def test_motor_without_pc_side_pid_is_rejected(self) -> None:
        """generic / EDULITE はドライバ側で制御しており PC 側に PID が無い。"""
        msg = await self._expect_rejection({"motor": "gripper", "key": "kp", "value": 1.0})
        assert "gripper" in msg["reason"]

    async def test_unsupported_key_is_rejected(self) -> None:
        await self._expect_rejection({"motor": "y_axis_r", "key": "dead_band", "value": 1.0})

    async def test_missing_key_is_rejected(self) -> None:
        await self._expect_rejection({"motor": "y_axis_r", "value": 1.0})

    async def test_non_numeric_value_is_rejected(self) -> None:
        await self._expect_rejection({"motor": "y_axis_r", "key": "kp", "value": "1.0"})

    async def test_boolean_value_is_rejected(self) -> None:
        """bool は Python では int だが、ゲインとしては明らかに誤送信。"""
        await self._expect_rejection({"motor": "y_axis_r", "key": "kp", "value": True})

    async def test_negative_gain_is_rejected(self) -> None:
        """負のゲインは正帰還になり発散する。"""
        await self._expect_rejection({"motor": "y_axis_r", "key": "kp", "value": -1.0})

    async def test_non_finite_value_is_rejected(self) -> None:
        server, loop = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            # JSON に NaN/Infinity のリテラルは無いので文字列経由で送る
            await ws.send_str(
                '{"type": "set_param", "motor": "y_axis_r", "key": "kp", "value": Infinity}'
            )
            msg = await _recv_type(ws, "command_rejected")
            await ws.close()

        assert msg is not None
        assert msg["command"] == "set_param"
        assert loop.pid("y_axis_r").kp == 2.0

    async def test_rejected_request_does_not_change_gains(self) -> None:
        server, loop = _build_server()
        app = server.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await _send_set_param(ws, motor="y_axis_r", key="kp", value=-3.0)
            await _recv_type(ws, "command_rejected")
            await ws.close()

        assert loop.pid("y_axis_r").kp == 2.0
