from __future__ import annotations

import time

import can
from aiohttp.test_utils import TestClient, TestServer

from lib.can_manager import CANManager
from lib.config_schema import DEFAULT_HEALTH
from lib.drivers.base import ControlMode
from lib.drivers.edulite05 import Edulite05Driver
from lib.drivers.generic import GenericDriver
from lib.sequence.engine import Sequence, step
from tests.fake_can import deliver_frame, mark_feedback_at
from tests.feedback_frames import edulite_feedback, generic_feedback
from tests.server_fixtures import ServerFixture

_ROBOT = "sub_hand"
_BUS = "can_edulite"

_MODE_RESET = 0
_MODE_MOTOR = 2


class _DummySequence(Sequence):
    @step("ノーオペ")
    async def noop(self) -> None:
        return None


def _build(*, bus_channel: str) -> tuple[ServerFixture, CANManager, can.Bus, Edulite05Driver]:
    mgr = CANManager()
    bus = can.Bus(interface="virtual", channel=bus_channel, receive_own_messages=False)
    mgr.add_bus(_BUS, bus, channel=bus_channel)
    motor = Edulite05Driver("rotate_l", can_id=1)
    mgr.add_motor(_BUS, motor)

    fx = ServerFixture.build()
    fx.add_robot(_ROBOT, _DummySequence(_ROBOT), mgr)
    return fx, mgr, bus, motor


def _safety(fx: ServerFixture) -> dict:
    return fx.state_message(_ROBOT)["safety"]


def _go_stale(mgr: CANManager, motor_name: str) -> None:
    mark_feedback_at(
        mgr, motor_name, time.time() - (DEFAULT_HEALTH.feedback_timeout_ms / 1000.0) - 0.1
    )


class TestUnresponsiveIsNotReportedAsUnenergized:
    async def test_フィードバックが1通も届かないモータは無励磁として報告されない(self) -> None:
        fx, _mgr, bus, _motor = _build(bus_channel="vun0")
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            fx.expire_energize_grace()
            fx.server.set_initial_inactive_motors(_ROBOT, ["rotate_l"])

            assert _safety(fx)["unenergized_motors"] == []

        bus.shutdown()

    async def test_フィードバックが1通も届かないモータは応答なしとして報告される(self) -> None:
        fx, _mgr, bus, _motor = _build(bus_channel="vun1")
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            fx.expire_energize_grace()
            fx.server.set_initial_inactive_motors(_ROBOT, ["rotate_l"])

            assert _safety(fx)["unresponsive_motors"] == ["rotate_l"], (
                "励磁できなかったモータが、無励磁からも応答なしからも消えている"
            )

        bus.shutdown()

    async def test_鮮度切れのラッチは応答なしへ移り両方から消えない(self) -> None:
        fx, mgr, bus, motor = _build(bus_channel="vun2")
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            deliver_frame(mgr, _BUS, edulite_feedback(motor, mode_state=_MODE_RESET))
            _go_stale(mgr, "rotate_l")
            fx.expire_energize_grace()
            fx.server.set_initial_inactive_motors(_ROBOT, ["rotate_l"])

            safety = _safety(fx)
            assert safety["unenergized_motors"] == []
            assert safety["unresponsive_motors"] == ["rotate_l"]

        bus.shutdown()

    async def test_ラッチ外の無励磁も鮮度が切れたら応答なしへ移る(self) -> None:
        # 稼働中に励磁が落ちた直後 CAN も落ちる経路。`mode_state` は復号時にしか
        # 書かれないので、古い False が張り付いたまま `_inactive_motors` を通らない。
        fx, mgr, bus, motor = _build(bus_channel="vun9")
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            deliver_frame(mgr, _BUS, edulite_feedback(motor, mode_state=_MODE_RESET))
            _go_stale(mgr, "rotate_l")
            fx.expire_energize_grace()

            safety = _safety(fx)
            assert safety["unenergized_motors"] == []
            assert safety["unresponsive_motors"] == ["rotate_l"]

        bus.shutdown()

    async def test_ラッチ外の無励磁は鮮度が切れても消えない(self) -> None:
        fx, mgr, bus, motor = _build(bus_channel="vun5")
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            deliver_frame(mgr, _BUS, edulite_feedback(motor, mode_state=_MODE_RESET))
            _go_stale(mgr, "rotate_l")
            fx.expire_energize_grace()

            safety = _safety(fx)
            reported = safety["unenergized_motors"] + safety["unresponsive_motors"]
            assert reported == ["rotate_l"], "無励磁の報告が鮮度切れで黙って消えた"

        bus.shutdown()


class TestFreshMotorsAreStillReportedAsUnenergized:
    async def test_鮮度が生きているラッチは無励磁として報告される(self) -> None:
        # 励磁の可否を答えないドライバ (`is_energized()` が常に None) なので、
        # ラッチを鮮度で仕分ける経路だけが結果を決める。
        mgr = CANManager()
        bus = can.Bus(interface="virtual", channel="vun3", receive_own_messages=False)
        mgr.add_bus(_BUS, bus, channel="vun3")
        motor = GenericDriver("wall", 0x40, control_type=ControlMode.POSITION)
        mgr.add_motor(_BUS, motor)
        fx = ServerFixture.build()
        fx.add_robot(_ROBOT, _DummySequence(_ROBOT), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            deliver_frame(mgr, _BUS, generic_feedback(motor, position=0.0))
            fx.expire_energize_grace()
            fx.server.set_initial_inactive_motors(_ROBOT, ["wall"])

            safety = _safety(fx)
            assert safety["unenergized_motors"] == ["wall"]
            assert safety["unresponsive_motors"] == []

        bus.shutdown()

    async def test_ラッチ外の無励磁は鮮度が生きていれば無励磁のまま(self) -> None:
        fx, mgr, bus, motor = _build(bus_channel="vun4")
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            deliver_frame(mgr, _BUS, edulite_feedback(motor, mode_state=_MODE_RESET))
            fx.expire_energize_grace()

            safety = _safety(fx)
            assert safety["unenergized_motors"] == ["rotate_l"]
            assert safety["unresponsive_motors"] == []

        bus.shutdown()


class TestSelfRecoveredMotorsLeaveTheLatch:
    async def test_自力で励磁されたモータはラッチに居ても報告されない(self) -> None:
        fx, mgr, bus, motor = _build(bus_channel="vun6")
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            deliver_frame(mgr, _BUS, edulite_feedback(motor, mode_state=_MODE_MOTOR))
            fx.expire_energize_grace()
            fx.server.set_initial_inactive_motors(_ROBOT, ["rotate_l"])

            safety = _safety(fx)
            assert safety["unenergized_motors"] == []
            assert safety["unresponsive_motors"] == []

        bus.shutdown()


class TestGuardsAreSharedWithUnenergized:
    async def test_緊急停止中は応答なしも報告しない(self) -> None:
        fx, _mgr, bus, _motor = _build(bus_channel="vun7")
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            fx.expire_energize_grace()
            fx.server.set_initial_inactive_motors(_ROBOT, ["rotate_l"])
            await fx.activate_e_stop(reason="テスト")

            safety = _safety(fx)
            assert safety["unenergized_motors"] == []
            assert safety["unresponsive_motors"] == []

        bus.shutdown()

    async def test_猶予の間は応答なしも報告しない(self) -> None:
        fx, _mgr, bus, _motor = _build(bus_channel="vun8")
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            fx.server.set_initial_inactive_motors(_ROBOT, ["rotate_l"])

            safety = _safety(fx)
            assert safety["unenergized_motors"] == []
            assert safety["unresponsive_motors"] == []

        bus.shutdown()
