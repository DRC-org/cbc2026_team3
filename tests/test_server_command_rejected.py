from __future__ import annotations

import asyncio
import contextlib

from aiohttp.test_utils import TestClient, TestServer

from lib.commands import COMMANDS
from lib.match_state import Court, Phase
from lib.sequence.engine import Sequence, step
from tests.server_fixtures import ServerFixture, expect_no_type, recv_type


class _DummySequence(Sequence):
    def __init__(self) -> None:
        super().__init__("test_seq")

    @step("ノーオペ")
    async def noop(self) -> None:
        return None


def _build_fixture() -> ServerFixture:
    fx = ServerFixture.build()
    fx.add_robot("main_hand", _DummySequence())
    return fx


class TestRejectionGoesToRequesterOnly:
    async def test_phase_denied_command_notifies_requester_only(self) -> None:
        fx = _build_fixture()
        fx.enter_match()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            requester = await client.ws_connect("/ws")
            bystander = await client.ws_connect("/ws")

            await requester.send_json({"type": "set_court", "court": Court.BLUE.value})

            msg = await recv_type(requester, "command_rejected")
            assert msg is not None
            assert msg["command"] == "set_court"
            assert msg["reason"]

            await expect_no_type(bystander, "command_rejected")

            await requester.close()
            await bystander.close()

    async def test_e_stop_denied_command_notifies_requester_only(self) -> None:
        fx = _build_fixture()
        fx.enter_match()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            requester = await client.ws_connect("/ws")
            bystander = await client.ws_connect("/ws")

            await fx.activate_e_stop()
            await requester.send_json({"type": "sequence_start", "robot": "main_hand"})

            msg = await recv_type(requester, "command_rejected")
            assert msg is not None
            assert msg["command"] == "sequence_start"

            await expect_no_type(bystander, "command_rejected")

            await requester.close()
            await bystander.close()

    async def test_match_start_rejection_uses_requester_ws(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            requester = await client.ws_connect("/ws")
            await asyncio.sleep(0.05)

            assert fx.match.phase is Phase.SETUP
            await fx.handle_match_start(fx.only_client())

            msg = await recv_type(requester, "command_rejected")
            assert msg is not None
            assert msg["command"] == "match_start"

            await requester.close()

    async def test_internal_command_without_requester_is_not_broadcast(self) -> None:
        fx = _build_fixture()
        fx.enter_match()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            watcher = await client.ws_connect("/ws")

            await fx.command({"type": "set_court", "court": Court.BLUE.value})

            await expect_no_type(watcher, "command_rejected")

            await watcher.close()


class TestUnknownCommandsNeverReachHandlers:
    def _record_handler_calls(self, fx: ServerFixture) -> list[str]:
        called: list[str] = []

        def _make(name: str):
            async def _recorder(_data: dict, _requester=None) -> None:
                called.append(name)

            return _recorder

        for handler_name in {spec.handler for spec in COMMANDS.values()}:
            setattr(fx.server, handler_name, _make(handler_name))
        return called

    async def test_undeclared_command_is_dropped(self) -> None:
        fx = _build_fixture()
        fx.enter_match()
        called = self._record_handler_calls(fx)

        await fx.command({"type": "totally_unknown", "robot": "main_hand"})
        await fx.command({"type": None})

        assert called == []

        await fx.command({"type": "trigger", "robot": "main_hand"})
        assert called == [COMMANDS["trigger"].handler]

    async def test_undeclared_command_is_not_rejected(self) -> None:
        fx = _build_fixture()
        fx.enter_match()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            requester = await client.ws_connect("/ws")

            await requester.send_json({"type": "totally_unknown"})

            await expect_no_type(requester, "command_rejected")

            await requester.close()


class TestDeclaredCommandsAlwaysAnswer:
    async def test_未知のコートは理由付きで拒否される(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            requester = await client.ws_connect("/ws")

            await requester.send_json({"type": "set_court", "court": "red"})
            await requester.send_json({"type": "set_court", "court": "green"})

            msg = await recv_type(requester, "command_rejected")
            assert msg is not None
            assert msg["command"] == "set_court"
            assert fx.match.court is Court.RED

            await requester.close()


class _TwoStepSequence(Sequence):
    def __init__(self) -> None:
        super().__init__("two_step")
        self.executed: list[str] = []

    @step("最初")
    async def first(self) -> None:
        self.executed.append("first")

    @step("次")
    async def second(self) -> None:
        self.executed.append("second")


class TestSequenceJumpArgumentValidation:
    async def test_真偽値のstep_indexではシーケンスが動き出さない(self) -> None:
        fx = ServerFixture.build()
        seq = _TwoStepSequence()
        fx.add_robot("main_hand", seq)
        fx.enter_match()

        # isinstance(True, int) は真なので、素通しすると True が index 1 として通る。
        await fx.command({"type": "sequence_jump", "robot": "main_hand", "step_index": True})

        task = asyncio.create_task(seq.run_forever())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert seq.executed == [], "誰も開始していないのにシーケンスが走り出した"

    async def test_整数のstep_indexは従来どおり通る(self) -> None:
        fx = ServerFixture.build()
        seq = _TwoStepSequence()
        fx.add_robot("main_hand", seq)
        fx.enter_match()

        await fx.command({"type": "sequence_jump", "robot": "main_hand", "step_index": 1})

        task = asyncio.create_task(seq.run_forever())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert seq.executed == ["second"]


class TestHandlerExceptionsNeverKillTheConnection:
    async def test_ハンドラの例外は理由付きで返る(self) -> None:
        fx = _build_fixture()
        fx.break_command_handler("health_check", RuntimeError("ハンドラ内部の異常"))
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            requester = await client.ws_connect("/ws")

            await requester.send_json({"type": "health_check"})
            msg = await recv_type(requester, "command_rejected")

            assert msg is not None
            assert msg["command"] == "health_check"
            assert "ハンドラ内部の異常" in msg["reason"]

            await requester.send_json({"type": "set_court", "court": "green"})
            follow_up = await recv_type(requester, "command_rejected")
            assert follow_up is not None
            assert follow_up["command"] == "set_court"

            await requester.close()

    async def test_例外は呼び出し元へ伝播しない(self) -> None:
        fx = _build_fixture()
        fx.break_command_handler("health_check", RuntimeError("ハンドラ内部の異常"))

        await fx.command({"type": "health_check"})

    async def test_動作確認の失敗は動作確認の状態として返る(self) -> None:
        fx = _build_fixture()
        fx.break_command_handler("motor_check_start", RuntimeError("起動処理の異常"))
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            requester = await client.ws_connect("/ws")
            await recv_type(requester, "motor_check_state")

            await requester.send_json({"type": "motor_check_start"})
            msg = await recv_type(requester, "motor_check_state")

            assert msg is not None
            assert "起動処理の異常" in (msg["error"] or "")

            await requester.close()
