"""シーケンス進行コマンドのサーバー側 (開始 / 停止 / 失敗の可視化)。

ここで見るのは **止めたのに動く経路が残っていないか** と、**止まった理由が
操縦者に届くか** の 2 つ。シーケンス単体の状態遷移は
`tests/test_sequence_engine.py` が持つ。

常駐ループ (`run_forever`) はサーバー起動時に立ち上がるので、開始要求が
「拾われる」かどうかを見るテストは必ずアプリを起動した状態で書く。
"""

from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from lib.sequence.engine import AxisSyncError, Sequence, step
from tests.server_fixtures import ServerFixture, wait_until

_ROBOT = "main_hand"


class _GatedSequence(Sequence):
    """1 ステップ目で外部のゲートを待つシーケンス。

    「実行中に 2 通目の開始要求が届く」状況を、実機と同じ
    *まだステップが終わっていない* 形で作れる。
    """

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
    """左右ずれで止まるシーケンス (実運用と同じ例外)。"""

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
    """**STOP を押した後、シーケンスが先頭から全工程を走り切ってはならない。**

    2 通目の START は現実に届く (操縦者 2 名 + 予備タブ / 二度押し / 詰まった
    クライアントの古い `running:false`)。保護は 2 枚 —— シーケンス側が実行中の
    開始要求を再開イベントにしないこと、サーバー側が停止のたびに未処理の要求を
    捨てること —— で、**1 枚ずつ単独で効いていることを見る**。
    """

    async def test_実行中の2通目は停止後に発火しない(self) -> None:
        """1 枚目: 実行中に届いた START が停止後の再走行にならないこと。"""
        seq = _GatedSequence()
        fx = _build(seq)
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "sequence_start", "robot": _ROBOT})
            assert await wait_until(lambda: seq.driven == ["hold"])

            # 実行中に届く 2 通目
            await ws.send_json({"type": "sequence_start", "robot": _ROBOT})
            await asyncio.sleep(0.05)

            await ws.send_json({"type": "sequence_stop", "robot": _ROBOT})
            await asyncio.sleep(0.05)
            # 走行中のステップは完了まで待つ (中断ではなく通常停止)
            seq.gate.set()
            assert await wait_until(lambda: not seq.is_running)

            # 以後、操縦者は何も押さない
            await asyncio.sleep(0.1)
            assert seq.driven == ["hold"]
            await ws.close()

    async def test_停止は未処理の開始要求も捨てる(self) -> None:
        """2 枚目: 常駐ループが拾う前の START を、停止が確実に捨てること。

        2 通を続けて流すあいだ `run_forever` は起床しないので、この層だけが
        「動き出さない」を保証している状態になる。
        """
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
        """`match_finish` の直前に届いた START で、終了後に機体が動き出さないこと。

        フェーズは `finished` なのに機体だけが動く形になる
        (`run_forever` はフェーズを見ない)。
        """
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
    """止まった理由が state に載ること。

    到達タイムアウト・左右ずれ・零点確定失敗はステップ単位の try で握られる。
    載せないと画面は「待機中 — START で開始」と描くだけで、**3 層保護の
    第 1 層 (`AxisSyncError`) が画面から無音**になる。
    """

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
    """**起動時に励磁できなかったモータも画面に出さなければならない。**

    `_inactive_motors` は緊急停止解除の経路でしか埋まらなかったため、起動時の
    励磁失敗はログの外に出ず、操縦者に見えるのは「指令しても動かない」だけだった。
    """

    async def test_起動時の励磁失敗が_safety_に載る(self) -> None:
        fx = _build(_GatedSequence())
        app = fx.create_app()

        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            fx.server.set_initial_inactive_motors(_ROBOT, ["lift"])

            # 励磁の猶予 (_ENERGIZE_GRACE_S) を跨ぐまで報告は出ない
            assert await wait_until(
                lambda: fx.state_message(_ROBOT)["safety"]["unenergized_motors"] == ["lift"]
            ), "起動時に励磁できなかったモータが safety に載っていない"
            await ws.close()
