from __future__ import annotations

import asyncio
import struct

import can
import pytest

from lib.axis_sync import MotorSpec, SyncGroup
from lib.control.pid import PIDController
from lib.control.position_loop import (
    DEFAULT_INTERVAL_S,
    DEFAULT_MAX_DT_S,
    M3508PositionLoop,
    make_position_pid,
)
from lib.drivers.base import ControlMode
from lib.drivers.m3508 import CURRENT_MAX, CURRENT_MIN, M3508Driver

BUS = "m3508_bus"


class _FakeClock:
    """単調増加クロックのスタブ。実時間 sleep に依存せず dt を制御する。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


class _StubCANManager:
    """M3508PositionLoop が触る API だけを実装したスタブ。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, can.Message]] = []
        self.feedback_at: dict[str, float] = {}
        self.fail_sends = 0

    async def send_to_bus(self, bus_name: str, msg: can.Message) -> None:
        if self.fail_sends > 0:
            self.fail_sends -= 1
            raise can.CanError("送信失敗 (テスト)")
        self.sent.append((bus_name, msg))

    def last_feedback_at(self, motor_name: str) -> float | None:
        return self.feedback_at.get(motor_name)

    # ---- テスト補助 ----

    @property
    def last_currents(self) -> tuple[int, int, int, int]:
        assert self.sent, "CAN フレームが 1 つも送信されていない"
        return struct.unpack(">hhhh", self.sent[-1][1].data)


def _feed(driver: M3508Driver, angle_raw: int, *, rpm: int = 0) -> None:
    data = struct.pack(">HhhBB", angle_raw, rpm, 0, 25, 0)
    driver.update_state(can.Message(arbitration_id=0x200 + driver.can_id, data=data))


def _counts_for_deg(deg: float) -> int:
    return round(deg / 360.0 * 8192)


class _Fixture:
    """ループ + スタブ一式。各テストで使い回す。"""

    def __init__(
        self,
        *,
        kp: float = 100.0,
        ki: float = 0.0,
        estop: bool = False,
        feedback_timeout_ms: float = 500.0,
    ) -> None:
        self.mono = _FakeClock()
        self.wall = _FakeClock(start=5000.0)
        self.manager = _StubCANManager()
        self.estop = estop
        self.loop = M3508PositionLoop(
            self.manager,
            BUS,
            feedback_timeout_ms=feedback_timeout_ms,
            is_estop_active=lambda: self.estop,
            time_source=self.mono,
            feedback_clock=self.wall,
        )
        self.lift = M3508Driver("lift", can_id=1)
        self.tilt = M3508Driver("tilt", can_id=2)
        self.loop.add_motor("lift", self.lift, make_position_pid(kp=kp, ki=ki))
        self.loop.add_motor("tilt", self.tilt, make_position_pid(kp=kp, ki=ki))
        # 原点確定 (初回フィードバック) と鮮度マークを済ませておく
        self.feed("lift", 0.0)
        self.feed("tilt", 0.0)

    def feed(self, name: str, deg: float, *, rpm: int = 0) -> None:
        driver = self.lift if name == "lift" else self.tilt
        _feed(driver, _counts_for_deg(deg) % 8192, rpm=rpm)
        self.manager.feedback_at[name] = self.wall.now

    async def tick(self, dt: float = DEFAULT_INTERVAL_S) -> None:
        self.mono.advance(dt)
        self.wall.advance(dt)
        await self.loop.step()


class TestFrameAggregation:
    async def test_two_motors_share_one_frame(self) -> None:
        fx = _Fixture(kp=100.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        await fx.loop.set_target("tilt", ControlMode.POSITION, -5.0)

        await fx.tick()

        # C620 は 1 フレームに 4 モータ分を載せるため、送信は必ずバスあたり 1 通
        assert len(fx.manager.sent) == 1
        bus_name, msg = fx.manager.sent[0]
        assert bus_name == BUS
        assert msg.arbitration_id == 0x200
        assert msg.is_extended_id is False
        assert fx.manager.last_currents == (1000, -500, 0, 0)

    async def test_zero_current_without_target(self) -> None:
        fx = _Fixture()
        await fx.tick()
        assert fx.manager.last_currents == (0, 0, 0, 0)

    async def test_output_clamped_to_current_limits(self) -> None:
        fx = _Fixture(kp=100000.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        await fx.tick()
        assert fx.manager.last_currents[0] == CURRENT_MAX

        await fx.loop.set_target("lift", ControlMode.POSITION, -10.0)
        await fx.tick()
        assert fx.manager.last_currents[0] == CURRENT_MIN

    async def test_duplicate_can_id_rejected(self) -> None:
        fx = _Fixture()
        with pytest.raises(ValueError, match="can_id"):
            fx.loop.add_motor("dup", M3508Driver("dup", can_id=1), make_position_pid(kp=1.0))

    async def test_duplicate_name_rejected(self) -> None:
        fx = _Fixture()
        with pytest.raises(ValueError, match="lift"):
            fx.loop.add_motor("lift", M3508Driver("lift", can_id=3), make_position_pid(kp=1.0))


class TestMultiTurnFeedback:
    async def test_uses_multi_turn_position_not_wrapped_angle(self) -> None:
        fx = _Fixture(kp=10.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 720.0)
        # 2 回転して目標到達 → 偏差 0 (単回転角なら 0 deg 付近で偏差 720 のまま)
        for step in range(1, 9):
            fx.feed("lift", 90.0 * step)
        await fx.tick()
        assert fx.manager.last_currents[0] == 0

    async def test_origin_reset_clears_target_and_output(self) -> None:
        fx = _Fixture(kp=10.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 100.0)
        await fx.tick()
        assert fx.manager.last_currents[0] != 0

        # ホーミング後の原点設定。目標を持ち越すと原点変更分だけ暴れるので解除する
        fx.loop.set_origin_here("lift")
        await fx.tick()
        assert fx.manager.last_currents[0] == 0
        assert fx.lift.multi_turn_position == pytest.approx(0.0)


class TestEmergencyStop:
    async def test_zero_current_while_estop_active(self) -> None:
        fx = _Fixture(kp=100.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        fx.estop = True

        await fx.tick()
        assert fx.manager.last_currents == (0, 0, 0, 0)

    async def test_pid_integral_reset_while_estop_active(self) -> None:
        fx = _Fixture(kp=0.0, ki=10.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        await fx.tick()
        await fx.tick()
        assert fx.loop.pid("lift").integral != pytest.approx(0.0)

        fx.estop = True
        await fx.tick()
        # 解除直後に溜まった積分が一気に出ないよう積分ごとクリアする
        assert fx.loop.pid("lift").integral == pytest.approx(0.0)

    async def test_no_output_after_release_until_retargeted(self) -> None:
        fx = _Fixture(kp=100.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        fx.estop = True
        await fx.tick()

        fx.estop = False
        await fx.tick()
        # 停止中に姿勢が崩れている可能性があるため、解除だけでは動き出さない
        assert fx.manager.last_currents[0] == 0

        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        await fx.tick()
        assert fx.manager.last_currents[0] == 1000


class TestSendStopFrame:
    """緊急停止の停止指令がループの生存に依存しないこと。"""

    async def test_sends_all_zero_slots(self) -> None:
        fx = _Fixture(kp=100.0)
        await fx.loop.set_target("lift", ControlMode.CURRENT, 3000.0)
        await fx.loop.set_target("tilt", ControlMode.CURRENT, -3000.0)

        await fx.loop.send_stop_frame()

        assert fx.manager.last_currents == (0, 0, 0, 0)

    async def test_works_without_running_loop(self) -> None:
        fx = _Fixture(kp=100.0)
        assert fx.loop.is_running is False

        await fx.loop.send_stop_frame()

        assert fx.manager.last_currents == (0, 0, 0, 0)

    async def test_sends_even_while_paused(self) -> None:
        """動作確認中でも緊急停止の 0 電流は通す (むしろ上書きさせたい)。"""
        fx = _Fixture(kp=100.0)
        await fx.loop.pause(reason="動作確認")

        await fx.loop.send_stop_frame()

        assert fx.manager.last_currents == (0, 0, 0, 0)

    async def test_clears_targets(self) -> None:
        """目標が残ると、ループが動き出した瞬間に再び電流が出る。"""
        fx = _Fixture(kp=100.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)

        await fx.loop.send_stop_frame()

        assert fx.loop.target("lift") is None
        assert fx.loop.mode("lift") is None

    async def test_send_failure_propagates(self) -> None:
        """送信できなかったことは呼び出し側 (サーバー) が知る必要がある。"""
        fx = _Fixture(kp=100.0)
        fx.manager.fail_sends = 1

        with pytest.raises(can.CanError):
            await fx.loop.send_stop_frame()


class TestFeedbackTimeout:
    async def test_zero_current_when_feedback_stale(self) -> None:
        fx = _Fixture(kp=100.0, feedback_timeout_ms=500.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        await fx.tick()
        assert fx.manager.last_currents[0] == 1000

        # 実測値が古いまま PID を回すと暴走するため 0 に落とす
        await fx.tick(dt=0.6)
        assert fx.manager.last_currents[0] == 0

    async def test_pid_reset_when_feedback_stale(self) -> None:
        fx = _Fixture(kp=0.0, ki=10.0, feedback_timeout_ms=500.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        await fx.tick()
        await fx.tick()
        assert fx.loop.pid("lift").integral != pytest.approx(0.0)

        await fx.tick(dt=0.6)
        assert fx.loop.pid("lift").integral == pytest.approx(0.0)

    async def test_zero_current_when_feedback_never_received(self) -> None:
        fx = _Fixture(kp=100.0)
        fx.manager.feedback_at.clear()
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        await fx.tick()
        assert fx.manager.last_currents == (0, 0, 0, 0)

    async def test_recovers_after_feedback_returns(self) -> None:
        fx = _Fixture(kp=100.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        await fx.tick(dt=0.6)
        assert fx.manager.last_currents[0] == 0

        fx.feed("lift", 0.0)
        await fx.tick()
        assert fx.manager.last_currents[0] == 1000


class TestTargetSink:
    async def test_sink_accepts_position(self) -> None:
        fx = _Fixture(kp=100.0)
        sink = fx.loop.target_sink("lift")
        await sink(ControlMode.POSITION, 10.0)

        assert fx.loop.target("lift") == pytest.approx(10.0)
        await fx.tick()
        assert fx.manager.last_currents[0] == 1000

    async def test_sink_accepts_current_as_open_loop(self) -> None:
        fx = _Fixture(kp=100.0)
        sink = fx.loop.target_sink("lift")
        # ホーミングで機構端に押し当てる用途。PID を通さず素通しする
        await sink(ControlMode.CURRENT, 300.0)
        await fx.tick()
        assert fx.manager.last_currents[0] == 300

    async def test_sink_rejects_velocity(self) -> None:
        fx = _Fixture()
        sink = fx.loop.target_sink("lift")
        with pytest.raises(ValueError, match="VELOCITY"):
            await sink(ControlMode.VELOCITY, 100.0)

    async def test_target_sinks_covers_all_motors(self) -> None:
        fx = _Fixture()
        sinks = fx.loop.target_sinks()
        assert set(sinks) == {"lift", "tilt"}

    async def test_sink_for_unknown_motor_raises(self) -> None:
        fx = _Fixture()
        with pytest.raises(KeyError):
            fx.loop.target_sink("unknown")


class TestTiming:
    async def test_dt_comes_from_measured_elapsed_time(self) -> None:
        fx = _Fixture(kp=0.0, ki=1.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 100.0)

        await fx.tick(dt=0.01)
        assert fx.loop.pid("lift").integral == pytest.approx(1.0)

        await fx.tick(dt=0.02)
        assert fx.loop.pid("lift").integral == pytest.approx(3.0)

    async def test_long_stall_dt_is_clamped(self) -> None:
        # asyncio が詰まって周期が飛んだとき、巨大な dt で積分が跳ねないこと
        fx = _Fixture(kp=0.0, ki=1.0, feedback_timeout_ms=100000.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 100.0)
        await fx.tick(dt=10.0)
        # dt は DEFAULT_MAX_DT_S に制限される
        assert fx.loop.pid("lift").integral == pytest.approx(100.0 * DEFAULT_MAX_DT_S)


class TestRunLifecycle:
    async def test_run_stops_on_request_and_sends_zero(self) -> None:
        fx = _Fixture(kp=100.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)

        ticks = 0

        async def fake_sleep(delay: float) -> None:
            nonlocal ticks
            ticks += 1
            fx.mono.advance(delay)
            fx.wall.advance(delay)
            fx.manager.feedback_at["lift"] = fx.wall.now
            fx.manager.feedback_at["tilt"] = fx.wall.now
            if ticks >= 3:
                fx.loop.request_stop()

        fx.loop.set_sleep(fake_sleep)
        await fx.loop.run()

        assert ticks == 3
        # 制御を降りるときは必ず 0 電流で終える
        assert fx.manager.last_currents == (0, 0, 0, 0)

    async def test_run_survives_send_error(self) -> None:
        fx = _Fixture(kp=100.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        fx.manager.fail_sends = 2

        ticks = 0

        async def fake_sleep(delay: float) -> None:
            nonlocal ticks
            ticks += 1
            fx.mono.advance(delay)
            fx.wall.advance(delay)
            fx.manager.feedback_at["lift"] = fx.wall.now
            fx.manager.feedback_at["tilt"] = fx.wall.now
            if ticks >= 4:
                fx.loop.request_stop()

        fx.loop.set_sleep(fake_sleep)
        await fx.loop.run()

        # 送信失敗 2 回でループが死なず、その後の周期は送れている
        assert ticks == 4
        assert len(fx.manager.sent) >= 2

    async def test_run_survives_driver_error(self) -> None:
        fx = _Fixture(kp=100.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)

        broken = fx.loop.pid("lift")
        calls = 0
        original_update = broken.update

        def flaky_update(setpoint: float, measurement: float, dt: float) -> float:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("PID 内部エラー (テスト)")
            return original_update(setpoint, measurement, dt)

        broken.update = flaky_update  # type: ignore[method-assign]

        ticks = 0

        async def fake_sleep(delay: float) -> None:
            nonlocal ticks
            ticks += 1
            fx.mono.advance(delay)
            fx.wall.advance(delay)
            fx.manager.feedback_at["lift"] = fx.wall.now
            fx.manager.feedback_at["tilt"] = fx.wall.now
            if ticks >= 3:
                fx.loop.request_stop()

        fx.loop.set_sleep(fake_sleep)
        await fx.loop.run()

        assert ticks == 3
        assert calls >= 2

    async def test_start_and_stop(self) -> None:
        fx = _Fixture(kp=100.0)

        async def fake_sleep(delay: float) -> None:
            fx.mono.advance(delay)
            fx.wall.advance(delay)
            await asyncio.sleep(0)

        fx.loop.set_sleep(fake_sleep)
        fx.loop.start()
        assert fx.loop.is_running is True
        await fx.loop.stop()
        assert fx.loop.is_running is False
        assert fx.manager.last_currents == (0, 0, 0, 0)


class TestPauseForMotorCheck:
    """アクチュエータ動作確認との 0x200 排他 (lib/server.py の _start_motor_check)。"""

    async def test_paused_loop_sends_no_frame(self) -> None:
        fx = _Fixture(kp=100.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        await fx.tick()
        assert len(fx.manager.sent) == 1

        await fx.loop.pause()
        assert fx.loop.is_paused is True

        await fx.tick()
        await fx.tick()
        # 動作確認が 0x200 を占有している間は 0 電流フレームすら送ってはならない
        assert len(fx.manager.sent) == 1

    async def test_resume_restarts_sending(self) -> None:
        fx = _Fixture(kp=100.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        await fx.loop.pause()
        await fx.tick()

        fx.loop.resume()
        assert fx.loop.is_paused is False

        await fx.tick()
        assert len(fx.manager.sent) == 1
        assert fx.manager.last_currents[0] == 1000

    async def test_pause_keeps_targets(self) -> None:
        fx = _Fixture(kp=100.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)

        await fx.loop.pause()
        fx.loop.resume()

        # 保持目標を失うと復帰時に昇降軸が落ちるため、目標そのものは残す
        assert fx.loop.target("lift") == 10.0
        assert fx.loop.mode("lift") is ControlMode.POSITION

    async def test_resume_clears_pid_integral(self) -> None:
        fx = _Fixture(kp=0.0, ki=10.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        await fx.tick()
        await fx.tick()
        assert fx.loop.pid("lift").integral != pytest.approx(0.0)

        await fx.loop.pause()
        fx.loop.resume()

        # 動作確認でモータが動かされているため、古い積分を持ち越すと復帰時に暴れる
        assert fx.loop.pid("lift").integral == pytest.approx(0.0)

    async def test_resume_does_not_charge_paused_duration_to_dt(self) -> None:
        fx = _Fixture(kp=0.0, ki=10.0)
        await fx.loop.pause()
        fx.mono.advance(30.0)
        fx.wall.advance(30.0)
        fx.feed("lift", 0.0)
        fx.feed("tilt", 0.0)
        fx.loop.resume()
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)

        await fx.tick(dt=0.005)

        # 停止していた 30s 分が dt に化けると積分が一気に育つ
        assert fx.loop.pid("lift").integral == pytest.approx(10.0 * 0.005)

    async def test_estop_while_paused_drops_targets_without_sending(self) -> None:
        fx = _Fixture(kp=100.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        await fx.loop.pause()

        fx.estop = True
        await fx.tick()

        assert fx.manager.sent == []
        # 停止中に目標が残ると、動作確認終了後の復帰で動き出してしまう
        assert fx.loop.target("lift") is None

    async def test_resume_without_pause_is_noop(self) -> None:
        fx = _Fixture(kp=100.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)

        fx.loop.resume()

        await fx.tick()
        assert fx.manager.last_currents[0] == 1000

    async def test_running_loop_stops_sending_while_paused(self) -> None:
        fx = _Fixture(kp=100.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)

        async def fake_sleep(delay: float) -> None:
            fx.mono.advance(delay)
            fx.wall.advance(delay)
            fx.manager.feedback_at["lift"] = fx.wall.now
            fx.manager.feedback_at["tilt"] = fx.wall.now
            await asyncio.sleep(0)

        fx.loop.set_sleep(fake_sleep)
        fx.loop.start()
        for _ in range(5):
            await asyncio.sleep(0)

        await fx.loop.pause()
        sent_at_pause = len(fx.manager.sent)
        for _ in range(20):
            await asyncio.sleep(0)

        # pause() の完了後は在庫の 1 周期分すら送られない (送信中の周期を待ち合わせる)
        assert len(fx.manager.sent) == sent_at_pause

        fx.loop.resume()
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(fx.manager.sent) > sent_at_pause

        await fx.loop.stop()


class TestMakePositionPid:
    def test_output_range_matches_m3508_current_limits(self) -> None:
        pid = make_position_pid(kp=1.0)
        assert isinstance(pid, PIDController)
        assert pid.output_min == pytest.approx(float(CURRENT_MIN))
        assert pid.output_max == pytest.approx(float(CURRENT_MAX))

    def test_default_interval_is_within_100_to_200hz(self) -> None:
        assert 1.0 / 200.0 <= DEFAULT_INTERVAL_S <= 1.0 / 100.0


def _pair_group(*, name: str = "y_axis", tolerance: float = 2.0) -> SyncGroup:
    """lift / tilt を逆回転ペアとして束ねたグループ (逆回転は scale の符号で表す)。"""
    return SyncGroup(
        name=name,
        members=(MotorSpec("lift", 1.0, 0.0), MotorSpec("tilt", -1.0, 0.0)),
        tolerance=tolerance,
    )


async def _target_pair(fx: _Fixture, value: float) -> None:
    """ペアに「同じ動作」を指示する (逆回転側は符号を反転)。"""
    await fx.loop.set_target("lift", ControlMode.POSITION, value)
    await fx.loop.set_target("tilt", ControlMode.POSITION, -value)


class TestSyncGroupRegistration:
    async def test_unknown_motor_rejected(self) -> None:
        fx = _Fixture()
        group = SyncGroup(
            name="y_axis",
            members=(MotorSpec("lift", 1.0, 0.0), MotorSpec("ghost", -1.0, 0.0)),
            tolerance=2.0,
        )
        with pytest.raises(ValueError, match="ghost"):
            fx.loop.add_sync_group(group)

    async def test_duplicate_group_rejected(self) -> None:
        fx = _Fixture()
        fx.loop.add_sync_group(_pair_group())
        with pytest.raises(ValueError, match="y_axis"):
            fx.loop.add_sync_group(_pair_group())

    async def test_group_names_exposed(self) -> None:
        fx = _Fixture()
        fx.loop.add_sync_group(_pair_group())
        assert fx.loop.sync_group_names == ("y_axis",)


class TestPairedStaleFeedback:
    async def test_stale_member_zeroes_whole_group(self) -> None:
        fx = _Fixture(kp=100.0)
        fx.loop.add_sync_group(_pair_group())
        await _target_pair(fx, 10.0)
        await fx.tick()
        assert fx.manager.last_currents == (1000, -1000, 0, 0)

        # lift だけ途絶させる (tilt のフィードバックは新鮮なまま)
        fx.mono.advance(0.6)
        fx.wall.advance(0.6)
        fx.feed("tilt", 0.0)
        await fx.loop.step()

        # 片方だけ止めると残った側が押し続けて機構が壊れる
        assert fx.manager.last_currents == (0, 0, 0, 0)

    async def test_ungrouped_stale_axis_does_not_affect_others(self) -> None:
        fx = _Fixture(kp=100.0)
        await _target_pair(fx, 10.0)
        await fx.tick()

        fx.mono.advance(0.6)
        fx.wall.advance(0.6)
        fx.feed("tilt", 0.0)
        await fx.loop.step()

        # グループ未登録なら従来どおり軸単位判定 (後方互換)
        assert fx.manager.last_currents == (0, -1000, 0, 0)

    async def test_healthy_member_pid_reset_on_group_stale(self) -> None:
        fx = _Fixture(kp=0.0, ki=10.0)
        fx.loop.add_sync_group(_pair_group())
        await _target_pair(fx, 10.0)
        await fx.tick()
        await fx.tick()
        assert fx.loop.pid("tilt").integral != pytest.approx(0.0)

        fx.mono.advance(0.6)
        fx.wall.advance(0.6)
        fx.feed("tilt", 0.0)
        await fx.loop.step()

        assert fx.loop.pid("tilt").integral == pytest.approx(0.0)

    async def test_group_recovers_after_feedback_returns(self) -> None:
        fx = _Fixture(kp=100.0)
        fx.loop.add_sync_group(_pair_group())
        await _target_pair(fx, 10.0)
        await fx.tick(dt=0.6)
        assert fx.manager.last_currents == (0, 0, 0, 0)

        fx.feed("lift", 0.0)
        fx.feed("tilt", 0.0)
        await fx.tick()
        assert fx.manager.last_currents == (1000, -1000, 0, 0)


class TestSyncDeviation:
    async def test_reverse_rotation_pair_has_no_deviation(self) -> None:
        fx = _Fixture(kp=100.0)
        fx.loop.add_sync_group(_pair_group())
        await _target_pair(fx, 20.0)

        # 左右が正しく同一動作している状態 (逆回転なので符号が逆)
        fx.feed("lift", 10.0)
        fx.feed("tilt", -10.0)
        await fx.tick()

        assert fx.loop.sync_violations == frozenset()
        assert fx.manager.last_currents[0] == pytest.approx(1000, abs=5)
        assert fx.manager.last_currents[1] == pytest.approx(-1000, abs=5)

    async def test_violation_zeroes_group_and_latches(self) -> None:
        fx = _Fixture(kp=100.0)
        fx.loop.add_sync_group(_pair_group(tolerance=2.0))
        await _target_pair(fx, 20.0)

        fx.feed("lift", 15.0)
        fx.feed("tilt", -5.0)
        await fx.tick()

        assert fx.loop.sync_violations == frozenset({"y_axis"})
        assert fx.manager.last_currents == (0, 0, 0, 0)

        fx.feed("lift", -5.0)
        fx.feed("tilt", -5.0)
        await fx.tick()
        # 偏差が許容内に戻ってもラッチは外れない
        assert fx.manager.last_currents == (0, 0, 0, 0)
        assert fx.loop.sync_violations == frozenset({"y_axis"})

    async def test_violation_resets_pid(self) -> None:
        fx = _Fixture(kp=0.0, ki=10.0)
        fx.loop.add_sync_group(_pair_group(tolerance=2.0))
        await _target_pair(fx, 20.0)
        await fx.tick()
        await fx.tick()
        assert fx.loop.pid("lift").integral != pytest.approx(0.0)

        fx.feed("lift", 15.0)
        fx.feed("tilt", -5.0)
        await fx.tick()

        assert fx.loop.pid("lift").integral == pytest.approx(0.0)
        assert fx.loop.pid("tilt").integral == pytest.approx(0.0)

    async def test_reset_sync_violation_restores_output(self) -> None:
        fx = _Fixture(kp=100.0)
        fx.loop.add_sync_group(_pair_group(tolerance=2.0))
        await _target_pair(fx, 20.0)

        fx.feed("lift", 15.0)
        fx.feed("tilt", -5.0)
        await fx.tick()
        assert fx.manager.last_currents == (0, 0, 0, 0)

        # 人間が機構のずれを直した状態 (逆回転ペアなので符号が逆で揃う)
        fx.feed("lift", 5.0)
        fx.loop.reset_sync_violation()
        await fx.tick()

        assert fx.loop.sync_violations == frozenset()
        assert fx.manager.last_currents[0] == pytest.approx(1500, abs=5)
        assert fx.manager.last_currents[1] == pytest.approx(-1500, abs=5)

    async def test_reset_does_not_disable_detection(self) -> None:
        """解除は「監視を再び有効にする」であって「ずれを無かったことにする」ではない。

        機構が直っていないまま解除された場合、次の周期で再びラッチして電流 0 に
        戻らなければ、操縦者は復帰したつもりで左右直結の軸を押し込むことになる。
        """
        fx = _Fixture(kp=100.0)
        fx.loop.add_sync_group(_pair_group(tolerance=2.0))
        await _target_pair(fx, 20.0)

        fx.feed("lift", 15.0)
        fx.feed("tilt", -5.0)
        await fx.tick()
        assert fx.loop.sync_violations == frozenset({"y_axis"})

        fx.loop.reset_sync_violation()
        # ずれたまま次の周期へ (人間は機構を直していない)
        await fx.tick()

        assert fx.loop.sync_violations == frozenset({"y_axis"})
        assert fx.manager.last_currents == (0, 0, 0, 0)

    async def test_reset_sync_violation_by_name(self) -> None:
        fx = _Fixture(kp=100.0)
        fx.loop.add_sync_group(_pair_group(tolerance=2.0))
        await _target_pair(fx, 20.0)
        fx.feed("lift", 15.0)
        fx.feed("tilt", -5.0)
        await fx.tick()

        with pytest.raises(KeyError):
            fx.loop.reset_sync_violation("unknown")
        fx.loop.reset_sync_violation("y_axis")
        assert fx.loop.sync_violations == frozenset()

    async def test_violation_is_logged_with_axis_and_values(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        fx = _Fixture(kp=100.0)
        fx.loop.add_sync_group(_pair_group(tolerance=2.0))
        await _target_pair(fx, 20.0)

        with caplog.at_level("ERROR", logger="lib.control.position_loop"):
            fx.feed("lift", 15.0)
            fx.feed("tilt", -5.0)
            await fx.tick()

        # 試合中に「なぜ止まったか」が分からないと復旧できない
        assert "y_axis" in caplog.text
        assert "2.0" in caplog.text

    async def test_stale_member_excluded_from_deviation(self) -> None:
        fx = _Fixture(kp=100.0)
        fx.loop.add_sync_group(_pair_group(tolerance=2.0))
        await _target_pair(fx, 20.0)

        # lift を途絶させたまま tilt だけ大きく動かす (比較対象が 1 個なので判定しない)
        fx.mono.advance(0.6)
        fx.wall.advance(0.6)
        fx.feed("tilt", -30.0)
        await fx.loop.step()

        assert fx.loop.sync_violations == frozenset()
        assert fx.manager.last_currents == (0, 0, 0, 0)

    async def test_latch_survives_estop_release(self) -> None:
        fx = _Fixture(kp=100.0)
        fx.loop.add_sync_group(_pair_group(tolerance=2.0))
        await _target_pair(fx, 20.0)
        fx.feed("lift", 15.0)
        fx.feed("tilt", -5.0)
        await fx.tick()
        assert fx.loop.sync_violations == frozenset({"y_axis"})

        fx.estop = True
        await fx.tick()
        fx.estop = False
        await fx.tick()

        # 機構のずれは緊急停止の解除では直らない
        assert fx.loop.sync_violations == frozenset({"y_axis"})

    async def test_no_violation_while_paused(self) -> None:
        fx = _Fixture(kp=100.0)
        fx.loop.add_sync_group(_pair_group(tolerance=2.0))
        await _target_pair(fx, 20.0)
        await fx.loop.pause()

        fx.feed("lift", 15.0)
        fx.feed("tilt", -5.0)
        await fx.tick()

        # 動作確認は 1 台ずつ動かすため、その間の偏差は機構のずれではない
        assert fx.loop.sync_violations == frozenset()


class TestGroupOrigin:
    async def test_set_group_origin_here_zeroes_all_members(self) -> None:
        fx = _Fixture(kp=100.0)
        fx.loop.add_sync_group(_pair_group())
        await _target_pair(fx, 20.0)
        fx.feed("lift", 12.0)
        fx.feed("tilt", -12.0)
        await fx.tick()

        fx.loop.set_group_origin_here("y_axis")

        assert fx.lift.multi_turn_position == pytest.approx(0.0)
        assert fx.tilt.multi_turn_position == pytest.approx(0.0)
        # 原点が動くと既存の目標値の意味も変わるため、目標は解除される
        assert fx.loop.target("lift") is None
        assert fx.loop.target("tilt") is None

        await fx.tick()
        assert fx.manager.last_currents == (0, 0, 0, 0)

    async def test_unknown_group_raises(self) -> None:
        fx = _Fixture()
        with pytest.raises(KeyError):
            fx.loop.set_group_origin_here("y_axis")

    async def test_set_origin_here_on_paired_motor_zeroes_whole_group(self) -> None:
        """ペアの片側だけ原点確定すると、正常動作でも即座に偏差超過で止まる。

        原点が左右で別々の瞬間に決まると、その差がそのまま消えないオフセットになる。
        1 台ぶんの API から入っても機構の単位 (グループ) で確定させる。
        """
        fx = _Fixture(kp=100.0)
        fx.loop.add_sync_group(_pair_group(tolerance=2.0))
        await _target_pair(fx, 20.0)
        fx.feed("lift", 12.0)
        fx.feed("tilt", -12.0)
        await fx.tick()

        fx.loop.set_origin_here("lift")

        assert fx.lift.multi_turn_position == pytest.approx(0.0)
        assert fx.tilt.multi_turn_position == pytest.approx(0.0)
        assert fx.loop.target("tilt") is None

    async def test_set_origin_here_on_solo_motor_touches_only_itself(self) -> None:
        fx = _Fixture(kp=100.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        await fx.loop.set_target("tilt", ControlMode.POSITION, 10.0)
        fx.feed("lift", 12.0)

        fx.loop.set_origin_here("lift")

        assert fx.loop.target("lift") is None
        assert fx.loop.target("tilt") == pytest.approx(10.0)
