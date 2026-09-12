"""初期位置への復帰 (`return_home` / `ReturnHomeController`)。

**要は 3 つ**: 宛先のロボットだけを動かすこと、零点が未確定の軸へ位置名で指令しない
こと、そして軸を握る他の点検・シーケンスと同時に走らないこと。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from lib.drivers.generic import GenericDriver
from lib.match_state import Court
from lib.sequence.engine import Sequence, step
from lib.sequence.motors import MotorGroup, MotorHandle
from lib.sequence.positions import PositionTable, load_position_table
from lib.server_homing import HomingSource
from tests.fake_can import mock_can_manager
from tests.server_fixtures import RecordingClient, ServerFixture

_CONFIG = {
    "axes": {
        "sub_lift": {
            "unit": "mm",
            "command_unit": "mm",
            "homing": {
                "sensor": "sub_switch",
                "direction": -1,
                "search_distance": 5.0,
                "step": 1.0,
            },
        },
        # 零点確定を持たない軸 (サーボ・弁・ポンプの側)。確定という状態を持たない
        "wall_r": {"unit": "deg", "command_unit": "deg"},
    },
    "positions": {"sub_lift": {"pick": -10.0, "top": 0.0}, "wall_r": {"initial": 5.0}},
}

_POSES: dict[str, tuple[Mapping[str, str], ...]] = {
    "sub_hand": ({"sub_lift": "pick"}, {"wall_r": "initial"}),
    "main_hand": ({"wall_r": "initial"},),
}


class _IdleSequence(Sequence):
    @step("何もしない")
    async def noop(self) -> None:
        return


class _HoldSequence(Sequence):
    """解放されるまでステップの中で止まる。制御権の扱いを見るための代役。"""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @step("解放されるまで待つ")
    async def hold(self) -> None:
        self.entered.set()
        await self.release.wait()


def _table() -> PositionTable:
    return load_position_table(_CONFIG, source="<test>")


class _Fixture:
    """サーバー 1 台ぶん。**寄せる口は本番と同じ `HomingSource.move_to`。**"""

    def __init__(
        self,
        *,
        origin_confirmed: bool = True,
        sequences: dict[str, Sequence] | None = None,
    ) -> None:
        self.moves: list[dict[str, str]] = []
        self.release: asyncio.Event | None = None
        self.entered = asyncio.Event()

        mgr = mock_can_manager()
        group = MotorGroup(sensor_active=lambda _name: False)
        for index, name in enumerate(("sub_lift", "wall_r"), start=1):
            driver = GenericDriver(name, can_id=index)
            if origin_confirmed:
                driver.mark_origin_confirmed()
            group.add(MotorHandle(name, driver, mgr))

        async def _move_to(targets: Mapping[str, str]) -> None:
            self.moves.append(dict(targets))
            self.entered.set()
            if self.release is not None:
                await self.release.wait()

        self.fx = ServerFixture.build()
        self.fx.freeze_broadcast()
        for name in _POSES:
            self.fx.add_robot(name, (sequences or {}).get(name) or _IdleSequence(name))
        self.fx.set_homing_source(
            HomingSource(
                runner=None,  # type: ignore[arg-type]
                table=_table(),
                motors=group,
                court=lambda: Court.RED,
                axes_by_robot={"sub_hand": ("sub_lift",)},
                move_to=_move_to,
            )
        )
        self.fx.set_return_home_poses(_POSES)


class TestRunsTheDeclaredOrder:
    async def test_宣言された順に1通ずつ投げる(self) -> None:
        env = _Fixture()

        assert await env.fx.start_return_home("sub_hand") is None
        await env.fx.wait_return_home_idle()

        assert env.moves == [{"sub_lift": "pick"}, {"wall_r": "initial"}]
        state = env.fx.return_home_state()["robots"]["sub_hand"]
        assert state["completed"] is True
        assert state["error"] is None
        assert state["current_step"] is None

    async def test_宛先のロボットの列だけを投げる(self) -> None:
        env = _Fixture()

        assert await env.fx.start_return_home("main_hand") is None
        await env.fx.wait_return_home_idle()

        assert env.moves == [{"wall_r": "initial"}]

    async def test_ロボットを省いたら拒む(self) -> None:
        env = _Fixture()

        reason = await env.fx.start_return_home(None)

        assert reason is not None
        assert env.moves == []


class TestOriginGate:
    async def test_零点が未確定なら1通も投げない(self) -> None:
        """原点の決まっていない軸へ位置名で指令すると、どこへ動くか分からない。"""
        env = _Fixture(origin_confirmed=False)

        reason = await env.fx.start_return_home("sub_hand")

        assert reason is not None and "sub_lift" in reason and "零点" in reason
        assert env.moves == []
        assert "零点" in (env.fx.return_home_state()["robots"]["sub_hand"]["blocked_reason"] or "")

    async def test_零点を持たない軸だけなら通る(self) -> None:
        """弁・ポンプ・サーボは確定という状態を持たない。ここを塞ぐと永久に戻せない。"""
        env = _Fixture(origin_confirmed=False)

        assert await env.fx.start_return_home("main_hand") is None
        await env.fx.wait_return_home_idle()

        assert env.moves == [{"wall_r": "initial"}]


class TestDenyGate:
    async def test_緊急停止中は拒む(self) -> None:
        env = _Fixture()
        await env.fx.activate_e_stop(reason="テスト")

        reason = await env.fx.start_return_home("sub_hand")

        assert reason is not None and "緊急停止" in reason
        assert env.moves == []

    async def test_緊急停止が入ったら残りの手順を投げない(self) -> None:
        env = _Fixture()
        env.release = asyncio.Event()

        assert await env.fx.start_return_home("sub_hand") is None
        await asyncio.wait_for(env.entered.wait(), timeout=2.0)
        await env.fx.activate_e_stop(reason="テスト")
        env.release.set()
        await env.fx.wait_return_home_idle()

        assert env.moves == [{"sub_lift": "pick"}]
        assert "緊急停止" in (env.fx.return_home_state()["robots"]["sub_hand"]["error"] or "")

    async def test_零点合わせの実行中は拒む(self) -> None:
        env = _Fixture()
        env.fx.set_homing_running(True)

        reason = await env.fx.start_return_home("sub_hand")

        assert reason is not None and "零点合わせ" in reason
        assert env.moves == []

    async def test_相手ロボットの零点合わせ中は通る(self) -> None:
        """点検どうしの排他は台ごと。相手の台を見て塞ぐと原点を取り戻せない。"""
        env = _Fixture()
        env.fx.set_homing_running(True, "main_hand")

        assert await env.fx.start_return_home("sub_hand") is None
        await env.fx.wait_return_home_idle()

        assert env.moves == [{"sub_lift": "pick"}, {"wall_r": "initial"}]

    @pytest.mark.parametrize("robot", ["main_hand", "sub_hand"])
    async def test_動作確認の実行中はどの台も拒む(self, robot: str) -> None:
        # 動作確認は両ハンド 1 本のシーケンスなので、ここだけ全機横断で塞ぐ
        env = _Fixture()
        pending: asyncio.Task[None] = asyncio.create_task(asyncio.Event().wait())  # type: ignore[arg-type]
        env.fx.set_motor_check_task(pending)
        try:
            reason = await env.fx.start_return_home(robot)
        finally:
            pending.cancel()

        assert reason is not None and "動作確認" in reason
        assert env.moves == []

    async def test_相手のシーケンス実行中でも通る(self) -> None:
        env = _Fixture(sequences={"main_hand": _HoldSequence("main_hand")})
        held = env.fx.sequence("main_hand")
        task = asyncio.create_task(held.run_forever())
        held.request_start()
        await asyncio.wait_for(held.entered.wait(), timeout=2.0)

        assert await env.fx.start_return_home("sub_hand") is None
        await env.fx.wait_return_home_idle()

        assert env.moves == [{"sub_lift": "pick"}, {"wall_r": "initial"}]
        assert held.is_running is True

        held.release.set()
        held.request_stop()
        task.cancel()

    async def test_原点復帰中は動作確認を拒む(self) -> None:
        env = _Fixture()
        env.fx.set_motor_check_sequence(_IdleSequence("motor_check"))
        env.fx.set_return_home_running(True)

        started = await env.fx.start_motor_check()

        assert started is False
        assert "原点復帰" in (env.fx.motor_check_error() or "")

    async def test_原点復帰中はシーケンスを開始できない(self) -> None:
        env = _Fixture()
        env.fx.enter_match()
        env.fx.set_return_home_running(True)
        client = RecordingClient()
        env.fx.attach_clients(client)

        await env.fx.command({"type": "sequence_start", "robot": "sub_hand"}, requester=client)

        assert env.fx.sequence("sub_hand").is_running is False
        assert "原点復帰" in client.of_type("command_rejected")[-1]["reason"]

    async def test_同じロボットの重ね掛けは拒む(self) -> None:
        env = _Fixture()
        env.release = asyncio.Event()

        assert await env.fx.start_return_home("sub_hand") is None
        await asyncio.wait_for(env.entered.wait(), timeout=2.0)
        reason = await env.fx.start_return_home("sub_hand")
        env.release.set()
        await env.fx.wait_return_home_idle()

        assert reason is not None and "実行中" in reason
        assert env.moves == [{"sub_lift": "pick"}, {"wall_r": "initial"}]
