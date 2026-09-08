"""`INFO` (自己申告) を一度も受けていない自作モタドラを画面に出す。

`INFO` は送信バッファの都合だけで 1 通も出ないことがあり (docs/invariants.md §7)、
その間は焼き忘れ検出 (`GenericDriver.info_mismatch`) も一緒に沈黙する。

**対象は「`FEEDBACK` は届いているのに `INFO` だけ来ない」モータに限る** —— 基板が
丸ごと落ちていれば `CANManager.health()` の STALE が既に言う。
"""

from __future__ import annotations

import time

import can
from aiohttp.test_utils import TestClient, TestServer

from lib.can_manager import CANManager
from lib.config_schema import DEFAULT_HEALTH
from lib.drivers.base import ControlMode
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import M3508Driver
from lib.sequence.engine import Sequence, step
from lib.server import _FIRMWARE_INFO_GRACE_S
from tests.fake_can import deliver_frame, mark_feedback_at
from tests.feedback_frames import generic_feedback, generic_info, m3508_feedback
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


def _feed_alive(mgr: CANManager, motor: GenericDriver) -> None:
    """基板が生きていること (= `FEEDBACK` が届いていること) を作る。

    呼ばないと鮮度が未受信のままになり**STALE 除外だけで報告が消える**。各テストが
    見たい層 (猶予・ドライバ種別・dry-run) を単独で確かめるために鮮度を満たす。
    """
    deliver_frame(mgr, _BUS, generic_feedback(motor, position=0.0))


class TestFirmwareUnconfirmedMotorsAreVisible:
    async def test_猶予を過ぎても未受信なら_safety_に載る(self) -> None:
        fx = ServerFixture.build()
        mgr, bus = _build_can_manager(bus_channel="vfw0")
        motor = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
        mgr.add_motor(_BUS, motor)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            _feed_alive(mgr, motor)
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
            _feed_alive(mgr, motor)
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
            _feed_alive(mgr, motor)
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
            # 鮮度の層を満たしておく (STALE 除外で消えたのでは、この層を見たことに
            # ならない)
            deliver_frame(mgr, _BUS, m3508_feedback(motor, angle_raw=0))
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
            _feed_alive(mgr_main, motor_main)
            _feed_alive(mgr_sub, motor_sub)
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
            _feed_alive(mgr, motor)
            fx.expire_firmware_grace()

            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == []

        bus.shutdown()


class TestStaleMotorsAreExcluded:
    """**基板が落ちている場合はここで言わない。**

    残したいのは「`FEEDBACK` は 10ms で届き続けているのに `INFO` だけが 1 通も出ない」
    という壊れ方だけ。基板が丸ごと落ちていれば STALE が診断ツリーを強制展開するので、
    ここでも言うと同じ事実を 2 度描く。手当ても別物で、生きている基板に「電源・CAN
    配線を確認」と言っても必ず何も見つからない。
    """

    async def test_フィードバックが途絶えたモータは対象外(self) -> None:
        fx = ServerFixture.build()
        mgr, bus = _build_can_manager(bus_channel="vfw7")
        motor = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
        mgr.add_motor(_BUS, motor)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            _feed_alive(mgr, motor)
            # 鮮度だけを許容の外へ押し出す (`firmware_confirmed()` は False のまま)
            mark_feedback_at(
                mgr,
                "gripper",
                time.time() - (DEFAULT_HEALTH.feedback_timeout_ms / 1000.0) - 0.1,
            )
            fx.expire_firmware_grace()

            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == []

        bus.shutdown()

    async def test_一度もフィードバックが来ていないモータは対象外(self) -> None:
        """未受信は「基板がそこに居ない」で、`INFO` を送れない基板とは別の話。"""
        fx = ServerFixture.build()
        mgr, bus = _build_can_manager(bus_channel="vfw8")
        motor = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
        mgr.add_motor(_BUS, motor)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            fx.expire_firmware_grace()

            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == []

        bus.shutdown()

    async def test_鮮度が生きていれば対象(self) -> None:
        """除外の反対向き。しきい値の内側なら報告が出ることを 1 件で固定する
        (これが無いと、鮮度の条件を常に False にする変異が緑のまま通る)。
        """
        fx = ServerFixture.build()
        mgr, bus = _build_can_manager(bus_channel="vfw9")
        motor = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
        mgr.add_motor(_BUS, motor)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            _feed_alive(mgr, motor)
            mark_feedback_at(
                mgr,
                "gripper",
                time.time() - (DEFAULT_HEALTH.feedback_timeout_ms / 1000.0) + 0.2,
            )
            fx.expire_firmware_grace()

            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == [
                "gripper"
            ]

        bus.shutdown()


class TestGraceIsAnchoredToStartupOnly:
    """猶予の起点 (`_server_started_at`) は**サーバー起動 1 回だけ**。

    `_ENERGIZE_GRACE_S` と違って試合開始でも緊急停止でも置き直さない —— `INFO` は
    励磁状態と無関係に 1Hz なので、置き直すたびに報告が数秒間消え、試合中に緊急停止を
    数回踏むだけでこの機能は実質無効になる。
    """

    async def test_試合開始で猶予は置き直されない(self) -> None:
        fx = ServerFixture.build()
        mgr, bus = _build_can_manager(bus_channel="vfwa")
        motor = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
        mgr.add_motor(_BUS, motor)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            _feed_alive(mgr, motor)
            fx.expire_firmware_grace()
            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == [
                "gripper"
            ]

            fx.complete_all_checklists()
            await fx.command({"type": "match_start"})
            assert fx.match.phase.value == "match", "試合開始が通っていない"

            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == [
                "gripper"
            ], "試合開始で猶予が置き直され、報告が消えた"

        bus.shutdown()

    async def test_緊急停止で猶予は置き直されない(self) -> None:
        fx = ServerFixture.build()
        mgr, bus = _build_can_manager(bus_channel="vfwb")
        motor = GenericDriver("gripper", 0x40, control_type=ControlMode.POSITION)
        mgr.add_motor(_BUS, motor)
        fx.add_robot("main_hand", _DummySequence("main_hand"), mgr)
        app = fx.create_app()

        async with TestClient(TestServer(app)):
            _feed_alive(mgr, motor)
            fx.expire_firmware_grace()

            await fx.activate_e_stop(reason="テスト")
            assert fx.e_stop_active

            assert fx.state_message("main_hand")["safety"]["firmware_unconfirmed_motors"] == [
                "gripper"
            ], "緊急停止で猶予が置き直され、報告が消えた"

        bus.shutdown()


def test_猶予は_INFO_数周期ぶんに留める() -> None:
    """`INFO` は 1Hz (仕様書 §3.4)。数周期を超える猶予は「準備フェーズのあいだ報告が
    1 度も出ない」ことを意味し、**この機能自身が静かに無効になる**。

    定数を大きくする変異はヘルパ (`expire_firmware_grace`) が値から逆算するため他の
    どのテストでも落ちないので、値そのものをここで固定する。
    """
    assert _FIRMWARE_INFO_GRACE_S <= 10.0
