from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def instant_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """整定待ちを実時間で待たない。

    出荷 yaml の `settle_s` は弁の応答やポンプの負圧到達を待つ実機の値
    (最大 0.5 秒) で、通しのテストはこれを軸の数だけ実時間で待つ。
    通しが見ているのは歯止めが拒否しないことだけなので、待ち時間は結果を
    変えない。**時間そのものを見るテストには付けないこと。**
    """
    real_sleep = asyncio.sleep

    async def _no_wait(_delay: float, result: object = None) -> object:
        return await real_sleep(0, result)

    monkeypatch.setattr(asyncio, "sleep", _no_wait)


@pytest.fixture(autouse=True)
def _shrink_can_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """再励磁の応答待ちをテストでは 1/10 にする。

    `_ACTIVATION_FEEDBACK_TIMEOUT_S` は実機のドライバが応答を返すまでの猶予で、
    テストは代役の応答を即座に返す。実寸のまま待つとここだけで全体の 3 割を使う。
    """
    from lib import can_manager

    monkeypatch.setattr(can_manager, "_ACTIVATION_FEEDBACK_TIMEOUT_S", 0.05)
    monkeypatch.setattr(can_manager, "_ACTIVATION_PROBE_INTERVAL_S", 0.005)
