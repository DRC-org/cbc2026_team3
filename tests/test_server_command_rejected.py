"""command_rejected の宛先を検証する。

拒否通知は「今その操作をした人」への返答であって、全員への通知ではない。
全配信していると、Monitor が試合中に set_court を弾かれただけで両操縦者の画面にも
赤トーストが出る。自分が押していない操作の拒否は操縦者にとってノイズでしかなく、
本当に自分の操作が弾かれたときの通知と区別が付かなくなる。
"""

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


#: 指差喚呼を 1 項目ずつ持たせる。項目ゼロだと全ロールが即完了扱いになり
#: 起動直後から READY になるため、SETUP を前提にする検証が書けない
def _build_fixture() -> ServerFixture:
    fx = ServerFixture.build()
    fx.add_robot("main_hand", _DummySequence())
    return fx


class TestRejectionGoesToRequesterOnly:
    async def test_phase_denied_command_notifies_requester_only(self) -> None:
        """フェーズゲートの拒否は要求元 1 台だけに返ること。"""
        fx = _build_fixture()
        fx.enter_match()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            requester = await client.ws_connect("/ws")
            bystander = await client.ws_connect("/ws")

            # 試合中の set_court はフェーズゲートで拒否される
            await requester.send_json({"type": "set_court", "court": Court.BLUE.value})

            msg = await recv_type(requester, "command_rejected")
            assert msg is not None
            assert msg["command"] == "set_court"
            assert msg["reason"]

            await expect_no_type(bystander, "command_rejected")

            await requester.close()
            await bystander.close()

    async def test_e_stop_denied_command_notifies_requester_only(self) -> None:
        """緊急停止ゲートの拒否も要求元 1 台だけに返ること。"""
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
        """match_start の拒否 (_handle_match_start 経路) が要求元へ返ること。

        この経路は handle_command のフェーズゲートを通り抜けた後の防御的な
        二重判定なので、ゲートを迂回して直接呼び出さないと踏めない。
        """
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
        """要求元 ws を持たない経路 (内部呼び出し) では誰にも送らないこと。

        HTTP POST や内部の安全機構から来たコマンドには返す相手がいない。
        代わりに全配信すると、誰も押していない拒否通知が全画面に出る。
        """
        fx = _build_fixture()
        fx.enter_match()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            watcher = await client.ws_connect("/ws")

            await fx.command({"type": "set_court", "court": Court.BLUE.value})

            await expect_no_type(watcher, "command_rejected")

            await watcher.close()


class TestUnknownCommandsNeverReachHandlers:
    """語彙に無いコマンドはゲートを素通りしてハンドラへ届いてはならない。

    ゲート表とディスパッチが別々の表だった頃は、どの表にも載っていないコマンドが
    「拒否もされず実行される」状態になり得た。今は COMMANDS が唯一の入口なので、
    宣言されていない名前はハンドラへ到達しない。
    """

    def _record_handler_calls(self, fx: ServerFixture) -> list[str]:
        """全ハンドラを記録役へ差し替える。

        差し替え先の名前は ``CommandSpec.handler`` が持っている宣言そのもの
        (lib/commands.py)。ここでハンドラ名を書き写すと、語彙表とテストが
        別々の一覧を持つことになり、本文が防いでいる事故がテスト側で再発する。
        """
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

        # 差し替えたハンドラが実際に呼ばれる構成であることも確かめる
        # (呼ばれない仕掛けになっていると上の assert は常に通ってしまう)
        await fx.command({"type": "trigger", "robot": "main_hand"})
        assert called == [COMMANDS["trigger"].handler]

    async def test_undeclared_command_is_not_rejected(self) -> None:
        """未知のコマンドに拒否理由を返すと、語彙の有無を外から総当たりで探れてしまう。"""
        fx = _build_fixture()
        fx.enter_match()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            requester = await client.ws_connect("/ws")

            await requester.send_json({"type": "totally_unknown"})

            await expect_no_type(requester, "command_rejected")

            await requester.close()


class TestDeclaredCommandsAlwaysAnswer:
    """語彙にあるコマンドは、引数が不正でも黙って捨てない。

    未知のコマンド (語彙に無い = 存在を知らせない) とは別で、こちらは操縦者が
    実在するボタンを押した結果である。黙って捨てると「送信できた」と信じたまま
    効いていない状態が続く。
    """

    async def test_未知のコートは理由付きで拒否される(self) -> None:
        fx = _build_fixture()
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            requester = await client.ws_connect("/ws")

            await requester.send_json({"type": "set_court", "court": "green"})

            msg = await recv_type(requester, "command_rejected")
            assert msg is not None
            assert msg["command"] == "set_court"
            # コートは変わっていない
            assert fx.match.court is Court.RED

            await requester.close()


class _TwoStepSequence(Sequence):
    """ジャンプ先の違いが実行結果に現れる最小シーケンス。"""

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
    """`step_index` は整数のときだけステップ移動として扱う。

    `isinstance(True, int)` は真なので、素通しすると `True` が index 1 として
    通る。ジャンプ要求は停止中のシーケンスを叩き起こすため、操縦者が誰も
    開始していないのに 2 番目のステップから機体が動き出す。
    """

    async def test_真偽値のstep_indexではシーケンスが動き出さない(self) -> None:
        fx = ServerFixture.build()
        seq = _TwoStepSequence()
        fx.add_robot("main_hand", seq)
        fx.enter_match()

        await fx.command({"type": "sequence_jump", "robot": "main_hand", "step_index": True})

        task = asyncio.create_task(seq.run_forever())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert seq.executed == [], "誰も開始していないのにシーケンスが走り出した"

    async def test_整数のstep_indexは従来どおり通る(self) -> None:
        # 上の検証が「ジャンプ自体が効かなくなった」ことを見ているのではないと示す
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
    """**ハンドラが投げても操縦者の WS を切ってはならない。**

    `handle_command` は `_ws_handler` の受信ループから await されている。例外を
    抜けさせると `async for msg in ws` ごと降り、その操縦者は画面から何も送れなく
    なる —— 試合中なら E-STOP を押す手段まで失う。以前は `_run_manual` だけが
    自前で握っており、`set_param` → `set_pid_gains` のように投げうる経路が
    無防備なまま残っていた。握りはディスパッチ 1 箇所に置く。
    """

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

            # 接続は生きたままで、次のコマンドも処理される
            await requester.send_json({"type": "set_court", "court": "green"})
            follow_up = await recv_type(requester, "command_rejected")
            assert follow_up is not None
            assert follow_up["command"] == "set_court"

            await requester.close()

    async def test_例外は呼び出し元へ伝播しない(self) -> None:
        """内部呼び出し (HTTP POST・安全機構) の経路も同じ握りで守る。"""
        fx = _build_fixture()
        fx.break_command_handler("health_check", RuntimeError("ハンドラ内部の異常"))

        await fx.command({"type": "health_check"})

    async def test_動作確認の失敗は動作確認の状態として返る(self) -> None:
        """拒否の経路はコマンドが宣言したものを使う (専用チャネルを迂回しない)。"""
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
