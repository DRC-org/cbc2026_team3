"""位置定数 yaml を走らせたまま読み直す口。

控えた値を貼ってから試すまでに、全サービス再起動 (UI 切断 + CAN down/up) を
挟まずに済ませるためのもの。守るのは **動いている最中に行き先を入れ替えないこと** と、
**拒んだ理由が操縦者へ届くこと**。読み直しそのものの安全 (軸の定義が変わったら拒む・
読めなければ今の値を残す) は `tests/test_main_positions_config.py` が見る。
"""

from __future__ import annotations

import pytest

from lib.sequence.engine import Sequence, step
from lib.sequence.positions import PositionReloadError
from tests.server_fixtures import RecordingClient, ServerFixture

_ROBOT = "main_hand"


class _IdleSequence(Sequence):
    def __init__(self) -> None:
        super().__init__(_ROBOT)

    @step("待機")
    async def idle(self) -> None:
        return None


class _Reloader:
    def __init__(self, changed: tuple[str, ...] = (), error: str | None = None) -> None:
        self.changed = changed
        self.error = error
        self.calls = 0

    def __call__(self) -> tuple[str, ...]:
        self.calls += 1
        if self.error is not None:
            raise PositionReloadError(self.error)
        return self.changed


def _fixture(reloader: _Reloader | None) -> ServerFixture:
    fx = ServerFixture.build()
    fx.freeze_broadcast()
    fx.add_robot(_ROBOT, _IdleSequence(), reload_positions=reloader)
    return fx


def _reason(client: RecordingClient) -> str:
    rejected = client.of_type("command_rejected")
    assert rejected, "拒否が操縦者へ届いていない"
    return rejected[-1]["reason"]


@pytest.mark.asyncio
async def test_reload_is_reported_to_the_operator() -> None:
    reloader = _Reloader(changed=("y_axis.place", "rotate.pick"))
    fx = _fixture(reloader)

    await fx.command({"type": "positions_reload", "robot": _ROBOT})

    assert reloader.calls == 1
    state = fx.state_message(_ROBOT)["positions_reload"]
    assert state["changed"] == ["y_axis.place", "rotate.pick"]
    assert state["reloaded_at"] is not None


@pytest.mark.asyncio
async def test_running_sequence_blocks_the_reload() -> None:
    reloader = _Reloader()
    fx = _fixture(reloader)
    fx.sequence(_ROBOT)._running = True
    client = RecordingClient()

    await fx.command({"type": "positions_reload", "robot": _ROBOT}, requester=client)

    assert reloader.calls == 0, "走っている足元で行き先が入れ替わった"
    assert "シーケンス" in _reason(client)


@pytest.mark.asyncio
async def test_refusal_reason_reaches_the_operator() -> None:
    fx = _fixture(_Reloader(error="軸の定義 (axes:) が変わっているので読み直せません"))
    client = RecordingClient()

    await fx.command({"type": "positions_reload", "robot": _ROBOT}, requester=client)

    assert "axes" in _reason(client)
    assert fx.state_message(_ROBOT)["positions_reload"]["reloaded_at"] is None


@pytest.mark.asyncio
async def test_robot_without_positions_has_no_reload() -> None:
    fx = _fixture(None)
    client = RecordingClient()

    await fx.command({"type": "positions_reload", "robot": _ROBOT}, requester=client)

    assert fx.state_message(_ROBOT)["positions_reload"] is None
    assert "位置定数" in _reason(client)
