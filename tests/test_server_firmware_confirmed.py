"""`INFO` (自己申告) を一度も受けていない自作モタドラを画面に出す。

CLAUDE.md「送信バッファの本数は 3 枚で違う」節が書くとおり、`INFO` は送信バッファの
都合だけで 1 通も出ないことがある。PC 側は未受信を FAULT にしないのが正しいが、
その間は焼き忘れ検出 (`GenericDriver.info_mismatch`) も一緒に沈黙する。ここが
黙って無効になったままだと、実際に焼き忘れがあっても誰も気付けない。
"""

from __future__ import annotations

import can
from aiohttp.test_utils import TestClient, TestServer

from lib.can_manager import CANManager
from lib.drivers.base import ControlMode
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import M3508Driver
from lib.sequence.engine import Sequence, step
from tests.fake_can import deliver_frame
from tests.feedback_frames import generic_info
from tests.server_fixtures import ServerFixture, wait_until

_BUS = "can_generic"


class _DummySequence(Sequence):
    def __init__(self, name: str) -> None:
        super().__init__(name)

    @step("ノーオペ")
    async def noop(self) -> None:
        return None


def _build_can_manager(*, bus_channel: str) -> tuple[CANManager, can.Bus]:
    mgr = CANManager()
    bus = can.Bus(interface="virtual", channel=bus_channel, receive_own_messages=False)
    mgr.add_bus(_BUS, bus, channel=bus_channel)
    return mgr, bus


class TestFirmwareUnconfirmedMotorsAreVisible:
    async def test_猶予を過ぎても未受信なら_safety_に載る(self) -> None:
        fx = ServerFixture.build()
        mgr, bus = _build_can_manager(bus_channel="vfw0")
        motor = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
        mgr.add_motor(_BUS, motor)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            fx.expire_firmware_grace()
            reported = await wait_until(
                lambda: (
                    fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"]
                    == ["gripper"]
                )
            )
            assert reported, "INFO 未受信のモータが safety に載っていない"

        bus.shutdown()

    async def test_猶予の間は報告しない(self) -> None:
        """`INFO` は 1Hz。起動直後の空白を焼き忘れと誤認してはならない。"""
        fx = ServerFixture.build()
        mgr, bus = _build_can_manager(bus_channel="vfw1")
        motor = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
        mgr.add_motor(_BUS, motor)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            # 猶予を過ぎさせずに読む (`expire_firmware_grace` を呼ばない)
            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == []

        bus.shutdown()

    async def test_info_を受けたモータは載らない(self) -> None:
        fx = ServerFixture.build()
        mgr, bus = _build_can_manager(bus_channel="vfw2")
        motor = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
        mgr.add_motor(_BUS, motor)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            deliver_frame(mgr, _BUS, generic_info(motor, firmware_version=1))
            fx.expire_firmware_grace()

            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == []

        bus.shutdown()

    async def test_info_を送らないドライバは対象外(self) -> None:
        """M3508 / EDULITE 05 / DM3520 を混ぜると全モータが常時この状態になる。"""
        fx = ServerFixture.build()
        mgr, bus = _build_can_manager(bus_channel="vfw3")
        motor = M3508Driver("y_axis_r", can_id=1)
        mgr.add_motor(_BUS, motor)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            fx.expire_firmware_grace()

            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == []

        bus.shutdown()

    async def test_他ロボットへ巻き添えを出さない(self) -> None:
        fx = ServerFixture.build()
        mgr_main, bus_main = _build_can_manager(bus_channel="vfw4")
        mgr_sub, bus_sub = _build_can_manager(bus_channel="vfw5")
        motor_main = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
        motor_sub = GenericDriver("wall", 0x41, control_type=ControlMode.POSITION)
        mgr_main.add_motor(_BUS, motor_main)
        mgr_sub.add_motor(_BUS, motor_sub)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr_main)
        fx.add_robot("sub_hand", _DummySequence("sub_hand"), mgr_sub)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            deliver_frame(mgr_sub, _BUS, generic_info(motor_sub, firmware_version=1))
            fx.expire_firmware_grace()

            reported = await wait_until(
                lambda: (
                    fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"]
                    == ["gripper"]
                )
            )
            assert reported, "main_hand 側の未受信が safety に載っていない"
            assert fx.state_message("sub_hand")["safety"]["firmware_unconfirmed_motors"] == []

        bus_main.shutdown()
        bus_sub.shutdown()

    async def test_dry_run_では出さない(self) -> None:
        """virtual バスは INFO を 1 通も返さないので、猶予を過ぎれば全自作モタドラが
        恒久的に「未確認」になる。dry-run は机上で画面を確かめる用途なので黙らせる。
        """
        fx = ServerFixture.build(dry_run=True)
        mgr, bus = _build_can_manager(bus_channel="vfw6")
        motor = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
        mgr.add_motor(_BUS, motor)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            fx.expire_firmware_grace()

            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == []

        bus.shutdown()
