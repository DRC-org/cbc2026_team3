from __future__ import annotations

import asyncio
import struct
import time
from dataclasses import replace
from unittest.mock import MagicMock, patch

import can
import pytest

from lib.can_manager import CANManager
from lib.config_schema import DEFAULT_HEALTH
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import M3508Driver
from lib.health import BusHealth, HealthSnapshot, MotorHealth
from tests.fake_can import deliver_frame, mark_bus_off, mark_feedback_at
from tests.fake_clock import FakeClock
from tests.fake_drivers import HealthFlagDriver
from tests.feedback_frames import generic_feedback, generic_info, m3508_feedback
from tests.test_can_manager import _direct_runner


def _make_virtual_bus(channel: str) -> can.Bus:
    return can.Bus(interface="virtual", channel=channel, receive_own_messages=False)


@pytest.fixture
def mgr_with_motors():
    """共通 fixture: 1 バス + 1 モータの CANManager を返す。"""
    mgr = CANManager(run_blocking=_direct_runner())
    bus = _make_virtual_bus("vhealth0")
    motor = HealthFlagDriver("m1", 1)
    mgr.add_bus("bus0", bus, channel="vhealth0")
    mgr.add_motor("bus0", motor)
    yield mgr, motor
    bus.shutdown()


class TestCANManagerHealth:
    def test_initial_snapshot_all_stale(self, mgr_with_motors) -> None:
        # 受信ゼロの初期状態では全モータ STALE、バスは OK のはず
        mgr, _ = mgr_with_motors
        snap = mgr.health()
        assert isinstance(snap, HealthSnapshot)
        assert len(snap.buses) == 1
        assert len(snap.motors) == 1
        assert snap.buses[0].state is BusHealth.OK
        assert snap.motors[0].state is MotorHealth.STALE
        assert snap.motors[0].last_feedback_at is None

    def test_health_snapshot_structure(self, mgr_with_motors) -> None:
        # WS 配信で使う dataclass の基本フィールドが揃っていることを担保
        mgr, _ = mgr_with_motors
        snap = mgr.health()
        assert isinstance(snap.timestamp, float)
        assert isinstance(snap.overall, BusHealth)
        assert isinstance(snap.buses, list)
        assert isinstance(snap.motors, list)
        assert snap.buses[0].name == "bus0"
        assert snap.buses[0].channel == "vhealth0"
        assert snap.motors[0].name == "m1"
        assert snap.motors[0].bus == "bus0"

    async def test_receive_records_last_rx_and_marks_ok(self, mgr_with_motors) -> None:
        # 受信ループがフィードバック鮮度を進め、十分新しければ OK 判定
        mgr, motor = mgr_with_motors
        feedback_msg = can.Message(arbitration_id=0x200 + motor.can_id, data=bytes(8))

        call_count = 0

        def recv_side_effect(timeout: float):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return feedback_msg
            raise asyncio.CancelledError

        bus = mgr._buses["bus0"]
        with (
            patch.object(bus, "recv", side_effect=recv_side_effect),
            pytest.raises(asyncio.CancelledError),
        ):
            await mgr._receive_loop("bus0")

        snap = mgr.health(thresholds=replace(DEFAULT_HEALTH, feedback_timeout_ms=500.0))
        assert snap.motors[0].state is MotorHealth.OK
        assert snap.motors[0].last_feedback_at is not None
        assert snap.motors[0].feedback_age_ms is not None
        assert snap.motors[0].feedback_age_ms < 500.0

    def test_feedback_timeout_transitions_to_stale(self, mgr_with_motors) -> None:
        # last_rx_at が timeout を超えていると STALE
        mgr, motor = mgr_with_motors
        mark_feedback_at(mgr, motor.name, time.time() - 1.0)
        snap = mgr.health(thresholds=replace(DEFAULT_HEALTH, feedback_timeout_ms=100.0))
        assert snap.motors[0].state is MotorHealth.STALE

    def test_thermal_warning(self, mgr_with_motors) -> None:
        # 受信は新鮮 + 温度 WARNING フラグ → MotorHealth.WARNING
        mgr, motor = mgr_with_motors
        deliver_frame(mgr, "bus0", motor.feedback_message())
        motor.thermal_warning = True
        snap = mgr.health(thresholds=replace(DEFAULT_HEALTH, feedback_timeout_ms=500.0))
        assert snap.motors[0].state is MotorHealth.WARNING
        # overall は DEGRADED に正規化される (health.py の _MOTOR_TO_BUS_SEVERITY 参照)
        assert snap.overall is BusHealth.DEGRADED

    def test_thermal_fault(self, mgr_with_motors) -> None:
        # 温度 FAULT フラグは STALE/WARNING より優先される
        mgr, motor = mgr_with_motors
        deliver_frame(mgr, "bus0", motor.feedback_message())
        motor.thermal_fault = True
        snap = mgr.health(thresholds=replace(DEFAULT_HEALTH, feedback_timeout_ms=500.0))
        assert snap.motors[0].state is MotorHealth.FAULT
        assert snap.overall is BusHealth.DOWN

    def test_overcurrent_warning(self, mgr_with_motors) -> None:
        mgr, motor = mgr_with_motors
        deliver_frame(mgr, "bus0", motor.feedback_message())
        motor.overcurrent = True
        snap = mgr.health(thresholds=replace(DEFAULT_HEALTH, feedback_timeout_ms=500.0))
        assert snap.motors[0].state is MotorHealth.WARNING

    def test_is_fault_takes_priority(self, mgr_with_motors) -> None:
        # is_fault() True は最優先で FAULT
        mgr, motor = mgr_with_motors
        deliver_frame(mgr, "bus0", motor.feedback_message())
        motor.fault = True
        motor.thermal_warning = True  # 同時に warning でも FAULT 維持
        snap = mgr.health(thresholds=replace(DEFAULT_HEALTH, feedback_timeout_ms=500.0))
        assert snap.motors[0].state is MotorHealth.FAULT

    async def test_send_failure_increments_tx_error_and_degrades(self) -> None:
        # bus.send が CanError を投げると tx_error_count が増え、しきい値以上で DEGRADED
        mgr = CANManager()
        bus = MagicMock()
        bus.send.side_effect = can.CanError("simulated tx failure")
        motor = HealthFlagDriver("m1", 1)
        mgr.add_bus("bus0", bus, channel="vhealth-fail")
        mgr.add_motor("bus0", motor)

        msg = can.Message(arbitration_id=0x100, data=bytes(8))
        # 互換性維持のため例外は再 raise されるはず
        for _ in range(3):
            with pytest.raises(can.CanError):
                await mgr.send_to_bus("bus0", msg)

        assert mgr._tx_error_count["bus0"] == 3

        # しきい値 2 で DEGRADED 判定
        snap = mgr.health(thresholds=replace(DEFAULT_HEALTH, tx_error_threshold=2))
        assert snap.buses[0].state is BusHealth.DEGRADED
        assert snap.buses[0].tx_error_count == 3

    async def test_degraded_clears_once_sending_recovers(self) -> None:
        """**送信が復旧したら DEGRADED は消えなければならない。**

        判定を累計カウンタで行っていた頃は、一度しきい値を超えたバスが永久に
        DEGRADED のまま残った。実機では物理緊急停止で DM3520 の電源が数秒落ちた
        だけで 6000 件積み上がり、CAN が完全に復旧した後 (ip -s link が
        ERROR-ACTIVE・bus-off 0 回・送受信ともエラー 0) も UI が異常を出し続けた。
        操縦者には「直したのに直らない」としか見えず、本物の異常と区別が付かない。
        """
        mgr = CANManager(run_blocking=_direct_runner())
        bus = MagicMock()
        mgr.add_bus("bus0", bus)
        mgr.add_motor("bus0", HealthFlagDriver("m1", 1))
        thresholds = replace(DEFAULT_HEALTH, tx_error_threshold=16)
        msg = can.Message(arbitration_id=0x100, data=bytes(8))

        bus.send.side_effect = can.CanError("相手が電源を失って ACK が返らない")
        for _ in range(5):
            with pytest.raises(can.CanError):
                await mgr.send_to_bus("bus0", msg)
        assert mgr.health(thresholds=thresholds).buses[0].state is BusHealth.DEGRADED

        bus.send.side_effect = None
        for _ in range(64):
            await mgr.send_to_bus("bus0", msg)

        snap = mgr.health(thresholds=thresholds)
        assert snap.buses[0].state is BusHealth.OK
        # 累計は残す。「この試合で何回失敗したか」は判定とは別に記録が要る
        assert snap.buses[0].tx_error_count == 5

    async def test_error_frame_marks_bus_off_and_is_not_delivered_to_motors(self) -> None:
        """SocketCAN のエラーフレームは bus-off を立て、モータへは配らない。

        python-can は既定でエラーフレームを受信する。これを通常フレームとして
        配ると、エラー種別のビット列がそのまま arbitration_id として宛先判定に
        掛かる (DM3520 の MST_ID 0x11 は CAN_ERR_TRX|CAN_ERR_TX_TIMEOUT と同値)。
        """
        mgr = CANManager(run_blocking=_direct_runner())
        bus = MagicMock()
        motor = HealthFlagDriver("m1", 1)
        mgr.add_bus("bus0", bus)
        mgr.add_motor("bus0", motor)

        bus_off_frame = can.Message(
            arbitration_id=motor.FEEDBACK_ID_BASE + motor.can_id | 0x40,
            data=bytes(8),
            is_error_frame=True,
        )
        # 受信ループと同じ判定を通す (recv が 1 通返して次で降りる)
        bus.recv.side_effect = [bus_off_frame, asyncio.CancelledError()]
        with pytest.raises(asyncio.CancelledError):
            await mgr._receive_loop("bus0")

        snap = mgr.health()
        assert snap.buses[0].bus_off is True
        assert snap.buses[0].state is BusHealth.DOWN
        # 鮮度は 1ms も進んではならない (エラーフレームはモータの応答ではない)
        assert mgr.last_feedback_at("m1") is None

    async def test_bus_off_clears_once_traffic_returns(self) -> None:
        """bus-off ラッチは実通信が戻ったら外れる。

        `restart-ms` が 0 のインタフェースは復帰通知 (CAN_ERR_RESTARTED) を送らない。
        実通信を根拠に外す経路が無いと、一度立った DOWN が永久に残る。
        """
        mgr = CANManager(run_blocking=_direct_runner())
        bus = MagicMock()
        mgr.add_bus("bus0", bus)
        mgr.add_motor("bus0", HealthFlagDriver("m1", 1))
        mark_bus_off(mgr, "bus0")

        await mgr.send_to_bus("bus0", can.Message(arbitration_id=0x100, data=bytes(8)))

        assert mgr.health().buses[0].bus_off is False

    def test_bus_off_marks_down(self, mgr_with_motors) -> None:
        mgr, _ = mgr_with_motors
        mark_bus_off(mgr, "bus0")
        snap = mgr.health()
        assert snap.buses[0].state is BusHealth.DOWN
        assert snap.buses[0].bus_off is True
        assert snap.overall is BusHealth.DOWN

    async def test_send_success_records_last_tx_at(self) -> None:
        # 送信成功時は _last_tx_at が更新され、tx_error_count は据え置き
        mgr = CANManager(run_blocking=_direct_runner())
        bus = MagicMock()
        motor = HealthFlagDriver("m1", 1)
        mgr.add_bus("bus0", bus)
        mgr.add_motor("bus0", motor)

        before = time.time()
        msg = can.Message(arbitration_id=0x100, data=bytes(8))
        await mgr.send_to_bus("bus0", msg)

        assert mgr._last_tx_at["bus0"] >= before
        assert mgr._tx_error_count["bus0"] == 0

    async def test_frame_decode_failure_surfaces_as_rx_error_count(self) -> None:
        """握り潰したフレームは rx_error_count としてヘルスに現れなければならない。

        ヘルス配信が「全部 OK」と言い続けるなら、握り潰しはバグの隠蔽と同じになる。
        """
        mgr = CANManager(run_blocking=_direct_runner())
        bus = MagicMock()
        motor = HealthFlagDriver("m1", 1)
        motor.decode_feedback = MagicMock(  # type: ignore[method-assign]
            side_effect=struct.error("DLC 不足")
        )
        mgr.add_bus("bus0", bus, channel="vhealth-rx")
        mgr.add_motor("bus0", motor)

        feedback_msg = can.Message(arbitration_id=0x200 + motor.can_id, data=bytes(4))
        queue = [feedback_msg, feedback_msg]

        def recv_side_effect(timeout: float):
            if queue:
                return queue.pop(0)
            raise asyncio.CancelledError

        bus.recv.side_effect = recv_side_effect
        with pytest.raises(asyncio.CancelledError):
            await mgr._receive_loop("bus0")

        snap = mgr.health()
        assert snap.buses[0].rx_error_count == 2
        # デコードできなかったフレームを受信扱いしないので、モータは STALE のまま出る
        assert snap.motors[0].state is MotorHealth.STALE
        # バスの判定自体は変えない。フレームを解釈できない機器が同じバスに相乗りする
        # (メインハンドとサブハンドは can_edulite / can_generic を物理的に共有する)
        # だけで DEGRADED になると、本物の送信障害の警告まで信用されなくなる
        assert snap.buses[0].state is BusHealth.OK


class TestInfoDoesNotRefreshFeedbackAge:
    """INFO (1Hz の自己申告) でフィードバック鮮度を更新してはならない (仕様書 §3.4)。

    **鮮度を動かすのは FEEDBACK だけ。** 100Hz の FEEDBACK が完全に途絶えても、
    1Hz の自己申告が ``_last_rx_at`` を書き換え続けると feedback_timeout_ms
    (既定 500ms) を満たし続け、そのモータは**永久に STALE にならない**。
    途絶検出そのものが効かなくなり、症状は「UI は正常なのに機体が動かない」になる。

    この層は単独で確かめる。健全性の統合経路には他の判定も混ざっているので、
    ここだけ壊しても他が拾ってしまい落ちない。
    """

    @pytest.fixture
    def mgr_with_servo(self):
        mgr = CANManager(run_blocking=_direct_runner())
        bus = _make_virtual_bus("vinfo0")
        motor = GenericDriver("gripper", 0x40, expected_angle_range_deg=270.0)
        mgr.add_bus("bus0", bus, channel="vinfo0")
        mgr.add_motor("bus0", motor)
        yield mgr, motor
        bus.shutdown()

    def test_info_is_delivered_without_touching_age(self, mgr_with_servo) -> None:
        mgr, motor = mgr_with_servo
        deliver_frame(mgr, "bus0", generic_info(motor, firmware_version=2, angle_range_deg=270.0))

        # 配られてはいる (配らないと焼き忘れも型違いも検出できない)
        assert motor.info is not None
        assert motor.info.angle_range_deg == pytest.approx(270.0)
        # 鮮度は 1 度も受信していないまま
        assert mgr.last_feedback_at("gripper") is None

    def test_info_does_not_rescue_a_stale_motor(self, mgr_with_servo) -> None:
        """FEEDBACK が途絶えたモータは、INFO が届き続けても STALE のままであること。"""
        mgr, motor = mgr_with_servo
        mark_feedback_at(mgr, "gripper", time.time() - 1.0)

        deliver_frame(mgr, "bus0", generic_info(motor, firmware_version=2, angle_range_deg=270.0))

        snap = mgr.health(thresholds=replace(DEFAULT_HEALTH, feedback_timeout_ms=100.0))
        assert snap.motors[0].state is MotorHealth.STALE

    def test_feedback_still_refreshes_age(self, mgr_with_servo) -> None:
        """対の確認。FEEDBACK 側まで止めてしまうと途絶検出が常に真になる。"""
        mgr, motor = mgr_with_servo
        deliver_frame(mgr, "bus0", generic_feedback(motor, position=1.0))

        assert mgr.last_feedback_at("gripper") is not None


class TestReanchorSurfacesInHealth:
    """累積角の再アンカーが操縦者に届くこと。

    detail だけを載せて状態を OK に置くと、`summarizeMotors` が「All operational」を
    出して `SubsystemStatus` は畳んだままになり、報告はどの画面にも現れない ——
    「報告した」つもりの黙殺が成立する。状態と詳細を 1 つに束ねておく。
    """

    def test_再アンカーしたモータは詳細付きのWARNINGになる(self) -> None:
        clock = FakeClock()
        mgr = CANManager(run_blocking=_direct_runner())
        bus = _make_virtual_bus("vhealth_reanchor")
        motor = M3508Driver("y_axis_r", 1, time_source=clock)
        mgr.add_bus("bus0", bus, channel="vhealth_reanchor")
        mgr.add_motor("bus0", motor)

        try:
            deliver_frame(mgr, "bus0", m3508_feedback(motor, angle_raw=8000))
            # 受信が 1 秒途切れた窓を跨ぐ (watchdog の down/up と同じ長さ)
            clock.advance(1.0)
            deliver_frame(mgr, "bus0", m3508_feedback(motor, angle_raw=4108))

            info = next(m for m in mgr.health().motors if m.name == "y_axis_r")
            assert info.state is MotorHealth.WARNING, "再アンカーが平常として扱われている"
            assert info.detail is not None, "再アンカーの理由がどこにも出ていない"
        finally:
            bus.shutdown()

    def test_平常時は詳細もWARNINGも出さない(self) -> None:
        """静かであることも同じだけ重要。常に出る警告は読まれなくなる。"""
        clock = FakeClock()
        mgr = CANManager(run_blocking=_direct_runner())
        bus = _make_virtual_bus("vhealth_quiet")
        motor = M3508Driver("y_axis_r", 1, time_source=clock)
        mgr.add_bus("bus0", bus, channel="vhealth_quiet")
        mgr.add_motor("bus0", motor)

        try:
            deliver_frame(mgr, "bus0", m3508_feedback(motor, angle_raw=8000))
            clock.advance(0.001)
            deliver_frame(mgr, "bus0", m3508_feedback(motor, angle_raw=8100))

            info = next(m for m in mgr.health().motors if m.name == "y_axis_r")
            assert info.state is MotorHealth.OK
            assert info.detail is None
        finally:
            bus.shutdown()
