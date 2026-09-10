from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from lib.sequence.engine import AxisSyncError, Sequence, step
from tests.fake_can import keep_feedback_fresh
from tests.server_fixtures import ServerFixture, wait_until

_ROBOT = "main_hand"


class _GatedSequence(Sequence):
    def __init__(self, name: str = _ROBOT) -> None:
        super().__init__(name)
        self.driven: list[str] = []
        self.gate = asyncio.Event()

    @step("ゲート待ち")
    async def hold(self) -> None:
        self.driven.append("hold")
        await self.gate.wait()

    @step("後続ステップ")
    async def after(self) -> None:
        self.driven.append("after")


class _FailingSequence(Sequence):
    def __init__(self, name: str = _ROBOT) -> None:
        super().__init__(name)

    @step("Y 軸を投入位置へ")
    async def move(self) -> None:
        raise AxisSyncError("シーケンス 'main_hand': 軸内のモータ位置がずれています (y_axis)")


def _build(sequence: Sequence) -> ServerFixture:
    fx = ServerFixture.build()
    fx.freeze_broadcast()
    fx.add_robot(_ROBOT, sequence)
    fx.enter_match()
    return fx


class TestPendingStartIsNeverReplayed:
    async def test_実行中の2通目は停止後に発火しない(self) -> None:
        seq = _GatedSequence()
        fx = _build(seq)
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "sequence_start", "robot": _ROBOT})
            assert await wait_until(lambda: seq.driven == ["hold"])

            await ws.send_json({"type": "sequence_start", "robot": _ROBOT})
            await asyncio.sleep(0.05)

            await ws.send_json({"type": "sequence_stop", "robot": _ROBOT})
            await asyncio.sleep(0.05)
            seq.gate.set()
            assert await wait_until(lambda: not seq.is_running)

            await asyncio.sleep(0.1)
            assert seq.driven == ["hold"]
            await ws.close()

    async def test_停止は未処理の開始要求も捨てる(self) -> None:
        seq = _GatedSequence()
        fx = _build(seq)
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await fx.command({"type": "sequence_start", "robot": _ROBOT})
            await fx.command({"type": "sequence_stop", "robot": _ROBOT})
            await asyncio.sleep(0.1)

            assert seq.driven == []
            await ws.close()

    async def test_試合終了も未処理の開始要求を捨てる(self) -> None:
        seq = _GatedSequence()
        fx = _build(seq)
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await fx.command({"type": "sequence_start", "robot": _ROBOT})
            await fx.command({"type": "match_finish"})
            await asyncio.sleep(0.1)

            assert seq.driven == []
            await ws.close()


class TestFailureReachesTheOperator:
    async def test_平常時はnull(self) -> None:
        fx = _build(_GatedSequence())
        assert fx.state_message(_ROBOT)["last_error"] is None

    async def test_失敗したステップと理由が_state_に載る(self) -> None:
        fx = _build(_FailingSequence())
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "sequence_start", "robot": _ROBOT})
            assert await wait_until(lambda: fx.state_message(_ROBOT)["last_error"] is not None), (
                "失敗が state に載っていない"
            )

            failure = fx.state_message(_ROBOT)["last_error"]
            assert failure["step"] == "Y 軸を投入位置へ"
            assert "ずれています" in failure["message"]
            await ws.close()


class TestInitialInactiveMotors:
    async def test_起動時の励磁失敗が_safety_に載る(self) -> None:
        fx = _build(_GatedSequence())
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            keep_feedback_fresh(fx.can_manager(_ROBOT))
            fx.server.set_initial_inactive_motors(_ROBOT, ["lift"])

            assert await wait_until(
                lambda: fx.state_message(_ROBOT)["safety"]["unenergized_motors"] == ["lift"]
            ), "起動時に励磁できなかったモータが safety に載っていない"
            await ws.close()
