"""自作基板のセンサ入力 (原点スイッチ) が state 配信に載ることを固定する。

`config/checklist.yaml` の `origin_sensor_react` は「原点センサに 1 本ずつ触れて
反応することを確認する」項目だが、配信が無ければ操縦者は `candump` を打つしか
確かめる手段が無い。しかも**未配線・極性違いのセンサは STALE にならない**
(基板は配線の有無に関わらず FEEDBACK を送り、`INPUT_PULLUP` の負論理で
「接触なし」を報告し続ける) ので、押してみる以外に検出手段が無い。
"""

from __future__ import annotations

import time

import can

from lib.can_manager import CANManager
from lib.config_schema import HealthThresholds
from lib.drivers.base import ControlMode
from lib.drivers.generic import GenericDriver
from lib.sequence.engine import Sequence, step
from tests.fake_can import deliver_frame, mark_feedback_at
from tests.feedback_frames import generic_feedback
from tests.server_fixtures import ServerFixture

_BUS = "can_generic"
_TIMEOUT_MS = 500.0


class _DummySequence(Sequence):
    def __init__(self) -> None:
        super().__init__("sensor_seq")

    @step("ノーオペ")
    async def noop(self) -> None:
        return None


def _build(
    *, channel: str, dry_run: bool = False
) -> tuple[ServerFixture, CANManager, GenericDriver, can.Bus]:
    """RobotServer + 実 CANManager + virtual バス + センサ 1 本。

    センサは ``add_sensor`` から登録する。``add_motor`` へ入れると本番と別物の
    構成になり (モータ一覧・動作確認・目標値再送に「常に 0 のモータ」が並ぶ)、
    「センサはモータ一覧に混ざらない」という検証そのものが成立しない。
    """
    fx = ServerFixture.build(
        health=HealthThresholds(feedback_timeout_ms=_TIMEOUT_MS), dry_run=dry_run
    )
    mgr = CANManager()
    bus = can.Bus(interface="virtual", channel=channel, receive_own_messages=False)
    sensor = GenericDriver("origin_sensor", can_id=0x44, control_type=ControlMode.POSITION)
    mgr.add_bus(_BUS, bus, channel=channel)
    mgr.add_sensor(_BUS, sensor)

    fx.add_robot("main_hand", _DummySequence(), mgr)
    return fx, mgr, sensor, bus


def _sensors(fx: ServerFixture) -> dict:
    return fx.state_message("main_hand")["sensors"]


class TestSensorStateInBroadcast:
    async def test_contact_is_reported_as_active(self) -> None:
        """接触が `active` として載ること。**判定は素通しで、異常扱いにしない。**"""
        fx, mgr, sensor, bus = _build(channel="vsensor_active")
        try:
            deliver_frame(mgr, _BUS, generic_feedback(sensor, sensor=True))
            assert _sensors(fx) == {"origin_sensor": {"active": True, "stale": False}}
        finally:
            bus.shutdown()

    async def test_release_is_reported_as_inactive(self) -> None:
        """離した状態も届く。接触したままになる不具合はこの形でしか見えない。"""
        fx, mgr, sensor, bus = _build(channel="vsensor_release")
        try:
            deliver_frame(mgr, _BUS, generic_feedback(sensor, sensor=True))
            deliver_frame(mgr, _BUS, generic_feedback(sensor, sensor=False))
            assert _sensors(fx) == {"origin_sensor": {"active": False, "stale": False}}
        finally:
            bus.shutdown()

    async def test_never_received_is_stale(self) -> None:
        """1 通も受けていないセンサは途絶。**「触れていない」に化けさせない。**"""
        fx, _mgr, _sensor, bus = _build(channel="vsensor_unheard")
        try:
            assert _sensors(fx)["origin_sensor"]["stale"] is True
        finally:
            bus.shutdown()

    async def test_stale_uses_configured_threshold(self) -> None:
        """鮮度の境界は config の `feedback_timeout_ms` だけが決める。

        サーバーに別の既定値があると、config を直しても画面の判定だけが古い境界の
        まま残る。接触状態は最後に受けた値のまま残ることも同時に見る (途絶と
        接触は別の軸で、片方が片方を塗り潰してはならない)。
        """
        fx, mgr, sensor, bus = _build(channel="vsensor_threshold")
        try:
            deliver_frame(mgr, _BUS, generic_feedback(sensor, sensor=True))
            # しきい値の 2 倍だけ過去へ置く。実時間を待たずに境界の外側を作る
            mark_feedback_at(mgr, "origin_sensor", time.time() - _TIMEOUT_MS / 1000.0 * 2)
            assert _sensors(fx)["origin_sensor"] == {"active": True, "stale": True}
        finally:
            bus.shutdown()

    async def test_sensors_are_not_listed_as_motors(self) -> None:
        """センサはモータ一覧に混ざらない (仕様書 §5.2 / `CANManager.add_sensor`)。

        混ざると動作確認・目標値再送・UI のモータ一覧に「常に 0 のモータ」が並ぶ。
        """
        fx, mgr, sensor, bus = _build(channel="vsensor_notmotor")
        try:
            deliver_frame(mgr, _BUS, generic_feedback(sensor, sensor=True))
            message = fx.state_message("main_hand")
            assert message["motors"] == {}
            assert "origin_sensor" in message["sensors"]
        finally:
            bus.shutdown()

    async def test_dry_run_reports_sensors_without_feedback(self) -> None:
        """dry-run でも接触チップを机上で確かめられること。

        virtual バスは FEEDBACK を 1 通も返さないので、実機の経路をそのまま通すと
        全センサが途絶で固まり、UI の描画を確かめる対象そのものが消える。
        """
        fx, _mgr, _sensor, bus = _build(channel="vsensor_dryrun", dry_run=True)
        try:
            state = _sensors(fx)["origin_sensor"]
            assert state["stale"] is False
            assert isinstance(state["active"], bool)
        finally:
            bus.shutdown()
