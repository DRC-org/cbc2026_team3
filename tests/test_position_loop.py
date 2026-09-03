from __future__ import annotations

import asyncio
import itertools
import struct
from collections.abc import Awaitable, Callable

import can
import pytest

from lib.axis_sync import MotorSpec, SyncGroup
from lib.config_schema import TuningSettings
from lib.control.pid import PIDController
from lib.control.position_loop import (
    DEFAULT_INTERVAL_S,
    DEFAULT_MAX_DT_S,
    M3508PositionLoop,
    make_position_pid,
)
from lib.control.trajectory import TrapezoidalProfile
from lib.drivers.base import ControlMode
from lib.drivers.m3508 import CURRENT_MAX, CURRENT_MIN, M3508Driver
from lib.tuning.recorder import Capture
from tests.fake_clock import FakeClock
from tests.feedback_frames import feed_m3508, m3508_counts_for_deg

BUS = "m3508_bus"


class _StubCANManager:
    """M3508PositionLoop が触る API だけを実装したスタブ。

    同名のスタブが tests/test_target_refresh.py にもあるが 1 つにまとめてはならない。
    位置制御ループはバス単位 (``send_to_bus``) でしか送ってはならず、モータ単位の
    ``send`` を生やすと「同一バスの M3508 は 1 フレームに束ねる」制約を破る書き方が
    テストの上では通ってしまう。持たせない API が制約の証明になっている。
    """

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


class _Fixture:
    """ループ + スタブ一式。各テストで使い回す。"""

    def __init__(
        self,
        *,
        kp: float = 100.0,
        ki: float = 0.0,
        estop: bool = False,
        feedback_timeout_ms: float = 500.0,
        tuning: TuningSettings | None = None,
    ) -> None:
        self.mono = FakeClock()
        self.wall = FakeClock(start=5000.0)
        self.manager = _StubCANManager()
        self.estop = estop
        #: capture_sink が受け取った記録。記録を有効にしたテストだけが使う
        self.captures: list[Capture] = []
        self.loop = M3508PositionLoop(
            self.manager,
            BUS,
            feedback_timeout_ms=feedback_timeout_ms,
            is_estop_active=lambda: self.estop,
            tuning=tuning,
            capture_sink=self.captures.append,
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
        feed_m3508(driver, angle_raw=m3508_counts_for_deg(deg) % 8192, rpm=rpm)
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

        def flaky_update(setpoint: float, measurement: float, dt: float, **kwargs: float) -> float:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("PID 内部エラー (テスト)")
            return original_update(setpoint, measurement, dt, **kwargs)

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


# 記録用の設定。窓を短くしてテストの周期数を抑える (制御周期 5ms が 10 回で 50ms)
RECORDING = TuningSettings(
    enabled=True, window_s=0.05, pre_trigger_s=0.01, min_step_deg=0.5, max_points=300
)


def _max_gap(capture: Capture) -> float:
    """記録内の最大時間差。連続していない波形を炙り出すために使う。

    「捨てたか」を件数で見ると、捨て損ねた窓がたまたま閉じないだけのケースを
    緑にしてしまう。見るべきは**時間が飛んだ波形が残っていないこと**そのもの。
    """
    times = [s.t for s in capture.samples]
    return max((b - a for a, b in itertools.pairwise(times)), default=0.0)


class TestStepRecording:
    """PID 調整支援のためのステップ応答記録。

    **記録は制御に一切影響してはならない**ことと、**意味の変わった記録を残さない**
    ことの 2 つを見る。後者を外すと、途中で電流 0 に落とされた波形が
    「行き過ぎもせず整定もしない応答」として画面に出て、操縦者はゲインが悪いと読む。
    """

    async def test_recording_is_off_without_tuning_settings(self) -> None:
        fx = _Fixture(kp=100.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        for _ in range(20):
            await fx.tick()
        assert fx.captures == []

    async def test_target_step_produces_a_capture(self) -> None:
        fx = _Fixture(kp=100.0, tuning=RECORDING)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        for _ in range(20):
            await fx.tick()

        assert [c.motor for c in fx.captures] == ["lift"]
        assert len(fx.captures[0].samples) > 1

    async def test_capture_records_the_gains_in_effect(self) -> None:
        """波形とゲインの対応が崩れると、届いた記録が新旧どちらのものか分からない。"""
        fx = _Fixture(kp=100.0, tuning=RECORDING)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        for _ in range(20):
            await fx.tick()

        assert fx.captures[0].gains.kp == pytest.approx(100.0)

    async def test_open_loop_current_command_is_not_recorded(self) -> None:
        """ホーミングの押し当てはゲインと無関係。応答として残すと誤読される。"""
        fx = _Fixture(kp=100.0, tuning=RECORDING)
        await fx.loop.set_target("lift", ControlMode.CURRENT, 500.0)
        for _ in range(20):
            await fx.tick()
        assert fx.captures == []

    async def test_stale_feedback_discards_the_window(self) -> None:
        fx = _Fixture(kp=100.0, tuning=RECORDING, feedback_timeout_ms=20.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        for _ in range(20):
            await fx.tick()
        assert fx.captures == []

    async def test_e_stop_does_not_leave_a_window_spanning_the_stop(self) -> None:
        """緊急停止をまたいだ波形を 1 本に綴じてはならない。

        停止中は記録関数そのものが呼ばれないので、窓を捨てずに残すと**時間の
        飛んだ波形**ができる。解除後に同じ目標で動かした場合はステップとしても
        検出されないため、停止前後が地続きの 1 回の応答として解析される。
        """
        fx = _Fixture(kp=100.0, tuning=RECORDING)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        for _ in range(4):
            fx.feed("lift", 1.0)
            await fx.tick()

        fx.estop = True
        for _ in range(10):
            fx.feed("lift", 1.0)
            await fx.tick()

        fx.estop = False
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        for _ in range(20):
            fx.feed("lift", 1.0)
            await fx.tick()

        for capture in fx.captures:
            assert _max_gap(capture) <= 3 * DEFAULT_INTERVAL_S

    async def test_pause_does_not_leave_a_window_spanning_the_pause(self) -> None:
        """動作確認が同じバスを握っている間の動きは、このループの指令ではない。"""
        fx = _Fixture(kp=100.0, tuning=RECORDING)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        for _ in range(4):
            fx.feed("lift", 1.0)
            await fx.tick()

        await fx.loop.pause()
        for _ in range(10):
            fx.feed("lift", 1.0)
            await fx.tick()

        fx.loop.resume()
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        for _ in range(20):
            fx.feed("lift", 1.0)
            await fx.tick()

        for capture in fx.captures:
            assert _max_gap(capture) <= 3 * DEFAULT_INTERVAL_S

    async def test_gain_change_discards_the_window(self) -> None:
        """前半が旧ゲイン・後半が新ゲインの波形はどちらの結果でもない。
        しかも送信直後に届くので、操縦者は新しいゲインの応答だと読む。"""
        fx = _Fixture(kp=100.0, tuning=RECORDING)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        for _ in range(4):
            fx.feed("lift", 1.0)
            await fx.tick()

        fx.loop.set_pid_gains("lift", {"kp": 5.0})
        for _ in range(20):
            fx.feed("lift", 1.0)
            await fx.tick()

        # 差し替え後に開いた窓しか残らない = 記録されたゲインは新しい方
        assert all(c.gains.kp == pytest.approx(5.0) for c in fx.captures)

    async def test_clear_target_discards_the_window(self) -> None:
        fx = _Fixture(kp=100.0, tuning=RECORDING)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        for _ in range(4):
            await fx.tick()

        fx.loop.clear_target("lift")
        for _ in range(20):
            await fx.tick()

        assert fx.captures == []

    async def test_restarting_the_loop_does_not_join_windows(self) -> None:
        """停止していた間の動きは記録できていない。窓を持ち越すと時間の飛んだ波形になる。"""
        fx = _Fixture(kp=100.0, tuning=RECORDING)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        for _ in range(4):
            fx.feed("lift", 1.0)
            await fx.tick()

        # 停止と再起動を挟む (シーケンス切替や再接続で起きる)
        fx.mono.advance(5.0)
        fx.wall.advance(5.0)
        await fx.loop._on_run_start()

        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        for _ in range(20):
            fx.feed("lift", 1.0)
            await fx.tick()

        for capture in fx.captures:
            assert _max_gap(capture) <= 3 * DEFAULT_INTERVAL_S

    async def test_paired_motors_are_recorded_separately(self) -> None:
        """左右で追従が違うことを見るのが調整の目的なので、束ねてはならない。"""
        fx = _Fixture(kp=100.0, tuning=RECORDING)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        await fx.loop.set_target("tilt", ControlMode.POSITION, 10.0)
        for _ in range(20):
            await fx.tick()

        assert sorted(c.motor for c in fx.captures) == ["lift", "tilt"]

    async def test_recording_does_not_change_the_command(self) -> None:
        """記録は制御に一切影響してはならない。"""
        plain = _Fixture(kp=100.0)
        recorded = _Fixture(kp=100.0, tuning=RECORDING)
        for fx in (plain, recorded):
            await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
            await fx.tick()

        assert plain.manager.last_currents == recorded.manager.last_currents


class TestSaturationReadout:
    """飽和の可視化。ゲインを変えても応答が変わらない理由が画面から読めるようにする。"""

    async def test_saturated_when_output_hits_the_limit(self) -> None:
        fx = _Fixture(kp=100000.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        await fx.tick()
        assert fx.loop.is_saturated("lift") is True

    async def test_not_saturated_in_the_normal_range(self) -> None:
        fx = _Fixture(kp=1.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 1.0)
        await fx.tick()
        assert fx.loop.is_saturated("lift") is False

    async def test_not_saturated_without_a_target(self) -> None:
        """目標を持たない周期の 0 出力を「下限に張り付いている」と読んではならない。"""
        fx = _Fixture(kp=100.0)
        await fx.tick()
        assert fx.loop.is_saturated("lift") is False

    async def test_saturation_clears_on_e_stop(self) -> None:
        fx = _Fixture(kp=100000.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        await fx.tick()

        fx.estop = True
        await fx.tick()

        assert fx.loop.is_saturated("lift") is False
        assert fx.manager.last_currents[0] == 0

    async def test_output_is_the_pid_command(self) -> None:
        fx = _Fixture(kp=100.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
        await fx.tick()
        assert fx.manager.last_currents[0] == 1000


def _pair_group_with_gain(
    *, sync_kp: float, sync_limit: float = 1e9, tolerance: float = 10.0
) -> SyncGroup:
    """同期補正を有効にした lift / tilt のペア。

    ``tolerance`` を既定より緩めてあるのは、補正そのものを見たいテストで偏差ラッチが
    先に効いてしまわないようにするため (ラッチの側は専用のテストが見る)。
    """
    return SyncGroup(
        name="y_axis",
        members=(MotorSpec("lift", 1.0, 0.0), MotorSpec("tilt", -1.0, 0.0)),
        tolerance=tolerance,
        sync_kp=sync_kp,
        sync_limit=sync_limit,
    )


async def _skew_pair(fx: _Fixture) -> None:
    """ペアに同じ目標を与えたうえで、lift だけ進んだ状態にする。

    人間の単位で lift が tilt より進むので、補正は「lift を減速し tilt を加速する」
    向きに出る (逆回転ペアなのでどちらも同じ符号の操作量になる)。
    """
    await _target_pair(fx, 10.0)
    fx.feed("lift", 2.0)
    fx.feed("tilt", 0.0)


async def _skew_pair_open_loop(fx: _Fixture) -> None:
    """lift は位置制御、tilt は開ループ電流指令 (ホーミングの押し当てと同じ状態)。"""
    await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
    await fx.loop.set_target("tilt", ControlMode.CURRENT, -300.0)
    fx.feed("lift", 2.0)
    fx.feed("tilt", 0.0)


async def _skew_pair_half_targeted(fx: _Fixture) -> None:
    """lift にだけ目標がある状態。"""
    await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)
    fx.feed("lift", 2.0)
    fx.feed("tilt", 0.0)


async def _currents(
    setup: Callable[[_Fixture], Awaitable[None]],
    *,
    group: SyncGroup | None = None,
    estop: bool = False,
) -> tuple[int, int, int, int]:
    """同じ状況を「補正あり / なし」で作り分け、その周期の電流指令を返す。

    補正量そのものの正しさは tests/test_axis_sync.py が厳密に見る。ここが見るのは
    経路の結線 —— 補正が電流指令まで届くか、届かない条件で本当に届かないか —— なので、
    基準 (``group=None``) との差で判定する。電流は整数 counts なので、実数の補正を
    丸めた差には 1 counts のずれが乗りうる。
    """
    fx = _Fixture(kp=100.0)
    if group is not None:
        fx.loop.add_sync_group(group)
    await setup(fx)
    fx.estop = estop
    await fx.tick()
    return fx.manager.last_currents


class TestSyncCorrection:
    """左右直結ペアを揃える同期補正が、電流指令として実際に出ること。

    ずれを検出して止める 3 層とは向きが逆で、**駆動中にずれを縮める唯一の経路**。
    独立した 2 つの PID には左右を揃える力がどこにも無いので、ここが効かないと
    追従差は原理的に残り続ける。
    """

    async def test_correction_shifts_both_currents_equally(self) -> None:
        """進んだ側は減速し、遅れた側は加速する。**逆回転ペアではその量が等しい。**

        量が等しいことは「補正が軸としての運動を動かさず、左右の内部のずれだけを
        縮める」ことと同じ意味である。片方にしか乗らない実装や、符号を落とした
        実装ではここが崩れる。
        """
        baseline = await _currents(_skew_pair, group=_pair_group_with_gain(sync_kp=0.0))
        corrected = await _currents(_skew_pair, group=_pair_group_with_gain(sync_kp=50.0))

        shift_lift = corrected[0] - baseline[0]
        shift_tilt = corrected[1] - baseline[1]

        assert shift_lift < 0, "進んだ側は減速する"
        assert shift_tilt < 0, "遅れた側は加速する (逆回転なので指令は負方向)"
        assert shift_lift == pytest.approx(shift_tilt, abs=1)

    async def test_no_correction_without_gain(self) -> None:
        """既定 (sync_kp=0.0) ではグループを登録していないときと同じ電流になる。"""
        with_group = await _currents(_skew_pair, group=_pair_group_with_gain(sync_kp=0.0))

        assert with_group == await _currents(_skew_pair)

    async def test_no_correction_when_aligned(self) -> None:
        """揃っている機体には余計な電流を出さない。"""
        fx = _Fixture(kp=100.0)
        fx.loop.add_sync_group(_pair_group_with_gain(sync_kp=50.0))
        await _target_pair(fx, 10.0)

        await fx.tick()

        assert fx.manager.last_currents == (1000, -1000, 0, 0)

    async def test_no_correction_while_group_is_stale(self) -> None:
        """途絶したグループには補正も出ない (電流 0 が優先)。

        補正だけが生き残ると、力を抜いたはずの周期で押し合う。
        """
        fx = _Fixture(kp=100.0)
        fx.loop.add_sync_group(_pair_group_with_gain(sync_kp=50.0))
        await _skew_pair(fx)
        await fx.tick()
        assert fx.manager.last_currents != (0, 0, 0, 0)

        # tilt だけ新鮮に保ち、lift を途絶させる
        fx.mono.advance(0.6)
        fx.wall.advance(0.6)
        fx.feed("tilt", 0.0)
        await fx.loop.step()

        assert fx.manager.last_currents == (0, 0, 0, 0)

    async def test_no_correction_after_deviation_latch(self) -> None:
        """偏差ラッチ中も補正を出さない。

        ラッチは「人間がずれを直すまで力を抜く」宣言なので、補正で自動的に
        揃えにいってはならない (人間が原因に気付かないまま駆動が続く)。
        """
        fx = _Fixture(kp=100.0)
        fx.loop.add_sync_group(_pair_group_with_gain(sync_kp=50.0, tolerance=1.0))
        await _skew_pair(fx)

        await fx.tick()

        assert "y_axis" in fx.loop.sync_violations
        assert fx.manager.last_currents == (0, 0, 0, 0)

    async def test_no_correction_when_a_member_is_open_loop(self) -> None:
        """片方が開ループ指令なら、グループの**どちらにも**補正を出さない。

        モータ単位で「自分が位置制御中なら補正する」と書くと、ホーミングの押し当てで
        片方だけがモードを変えた瞬間にもう 1 台へだけ補正が乗り、左右で打ち消し合う
        はずの力が軸ごと押し動かす力になる。
        """
        corrected = await _currents(_skew_pair_open_loop, group=_pair_group_with_gain(sync_kp=50.0))

        assert corrected == await _currents(_skew_pair_open_loop)

    async def test_no_correction_when_a_member_has_no_target(self) -> None:
        """目標を持たないメンバが居るグループにも出さない。"""
        corrected = await _currents(
            _skew_pair_half_targeted, group=_pair_group_with_gain(sync_kp=50.0)
        )

        assert corrected == await _currents(_skew_pair_half_targeted)

    async def test_correction_is_clamped_to_output_range(self) -> None:
        """補正込みで出力レンジに収まる (PID の外で足すと上限を超えた指令が出る)。"""
        corrected = await _currents(_skew_pair, group=_pair_group_with_gain(sync_kp=1e6))

        assert corrected[0] == CURRENT_MIN
        assert corrected[1] == CURRENT_MIN

    async def test_sync_limit_caps_the_correction(self) -> None:
        """押し合いの歯止め。大きなずれでも sync_limit を超える補正は出ない。"""
        baseline = await _currents(_skew_pair, group=_pair_group_with_gain(sync_kp=0.0))
        corrected = await _currents(
            _skew_pair, group=_pair_group_with_gain(sync_kp=1e6, sync_limit=200.0)
        )

        assert corrected[0] - baseline[0] == -200
        assert corrected[1] - baseline[1] == -200

    async def test_no_correction_while_estop_active(self) -> None:
        """緊急停止中は補正も出ない。"""
        corrected = await _currents(
            _skew_pair, group=_pair_group_with_gain(sync_kp=50.0), estop=True
        )

        assert corrected == (0, 0, 0, 0)


# --------------------------------------------------------------------------- #
#  台形速度プロファイルの結線
# --------------------------------------------------------------------------- #

#: y_axis の実測換算 [deg/mm] (ピニオン モジュール 1 / 歯数 40 で確定)
Y_AXIS_SCALE = 55.0131
#: 実運用ストローク [mm]。最終目標をステップで入れると原理的に行き過ぎる距離
LONG_MOVE_MM = 15.0
#: 実機で詰めた y_axis の出力上限 [counts] (C620 フルスケールの約 12%)
Y_AXIS_OUTPUT_LIMIT = 2000.0


class _Plant:
    """電流指令で駆動される 1 軸の機構模型。

    表すのは 2 点だけ —— 電流はトルク (角加速度) を作る、速度は粘性で頭打ちになる。
    飽和するかどうかは「PID にどれだけ大きな偏差を見せるか」で決まるので、係数が
    多少違っても結論は動かない。**係数を実機に寄せることが目的ではない。**

    速度が頭打ちになることは模型の飾りではなく前提条件で、これが無いと 1 周期で
    半回転を超える移動が起こり、C620 の単回転角アンラップ (半周を超える差分は
    0 を跨いだ折り返しと推定する) が破綻して測定そのものが無意味になる。
    """

    #: 電流 1 counts あたりの角加速度 [deg/s^2]。上限 2000 counts で 200mm/s^2 相当
    GAIN = 5.5
    #: 粘性 [1/s]。上限 2000 counts での終端速度が 3000deg/s (1 周期 15deg) になる値
    DAMPING = GAIN * 2000.0 / 3000.0

    def __init__(self) -> None:
        self.position = 0.0
        self.velocity = 0.0

    def step(self, current: float, dt: float) -> None:
        self.velocity += (self.GAIN * current - self.DAMPING * self.velocity) * dt
        self.position += self.velocity * dt


class _ProfileRig:
    """y_axis の実測 PID を載せた 1 モータのループ + 機構模型。

    ``motion`` を渡さなければ従来どおり最終目標をステップで PID へ入れる。同じ機構・
    同じゲインのまま入力の作り方だけを変えられるので、飽和の有無がプロファイルに
    由来することを他の条件を動かさずに示せる。
    """

    def __init__(self, *, motion: tuple[float, float] | None, velocity_ff: float = 0.0) -> None:
        self.mono = FakeClock()
        self.wall = FakeClock(start=5000.0)
        self.manager = _StubCANManager()
        self.loop = M3508PositionLoop(
            self.manager,
            BUS,
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
            time_source=self.mono,
            feedback_clock=self.wall,
        )
        self.driver = M3508Driver("y_axis_r", can_id=1)
        # config/main_hand.yaml の実測値 (2026-09-03 に実機で詰めたもの)
        pid = make_position_pid(32.0, 10.0, 1.0, integral_limit=400.0, dead_band=1.0)
        pid.output_min = -Y_AXIS_OUTPUT_LIMIT
        pid.output_max = Y_AXIS_OUTPUT_LIMIT
        self.loop.add_motor("y_axis_r", self.driver, pid)
        if motion is not None:
            max_velocity, max_acceleration = motion
            self.loop.set_motion_profile(
                "y_axis_r",
                TrapezoidalProfile(
                    # 制限は指令単位 (deg) で渡す。mm からの換算は配線層 (main.py) の仕事
                    max_velocity=max_velocity * Y_AXIS_SCALE,
                    max_acceleration=max_acceleration * Y_AXIS_SCALE,
                ),
                velocity_ff=velocity_ff,
            )
        self.plant = _Plant()
        self.outputs: list[int] = []
        self.saturations: list[bool] = []
        self.positions_mm: list[float] = []
        self._feed()

    def _feed(self) -> None:
        feed_m3508(self.driver, angle_raw=m3508_counts_for_deg(self.plant.position) % 8192)
        self.manager.feedback_at["y_axis_r"] = self.wall.now

    async def move_to(self, millimetres: float) -> None:
        await self.loop.set_target("y_axis_r", ControlMode.POSITION, millimetres * Y_AXIS_SCALE)

    async def run(self, seconds: float) -> None:
        for _ in range(round(seconds / DEFAULT_INTERVAL_S)):
            self.mono.advance(DEFAULT_INTERVAL_S)
            self.wall.advance(DEFAULT_INTERVAL_S)
            await self.loop.step()
            current = self.manager.last_currents[0]
            self.outputs.append(current)
            self.saturations.append(self.loop.is_saturated("y_axis_r"))
            self.plant.step(current, DEFAULT_INTERVAL_S)
            self._feed()
            self.positions_mm.append(self.plant.position / Y_AXIS_SCALE)

    @property
    def peak_output(self) -> int:
        return max(abs(value) for value in self.outputs)

    @property
    def position_mm(self) -> float:
        return self.plant.position / Y_AXIS_SCALE


class TestLongMoveDoesNotSaturate:
    """**この作業の本題。** 実運用ストロークで P 項が飽和したままにならないこと。

    偏差 1.14mm で P 項が上限に届く (scale 55.0131 / kp 32 / output_limit 2000) ため、
    最終目標をステップで入れると移動距離が 2.3mm を超えた時点で「フル電流の定加速 →
    減速に使える距離が足りない」に落ちる。飽和中は合計がクランプされるので D 項も
    出力に現れず、``kd`` を上げても直らない。
    """

    async def test_15mm_の移動で出力が飽和しない(self) -> None:
        rig = _ProfileRig(motion=(10.0, 50.0))

        await rig.move_to(LONG_MOVE_MM)
        await rig.run(2.5)

        assert not any(rig.saturations)
        assert rig.peak_output < Y_AXIS_OUTPUT_LIMIT
        # 到達許容差 (config の tolerance 1.0mm) の中へ収まる
        assert rig.position_mm == pytest.approx(LONG_MOVE_MM, abs=1.0)

    async def test_同じ機構でも最終目標をステップで入れると飽和して行き過ぎる(self) -> None:
        """プロファイルを外した対照。飽和が模型の作りではなく入力の作り方に由来する。"""
        rig = _ProfileRig(motion=None)

        await rig.move_to(LONG_MOVE_MM)
        await rig.run(2.5)

        assert any(rig.saturations)
        assert max(rig.positions_mm) > LONG_MOVE_MM + 1.0

    async def test_中間目標は速度制限を守って立ち上がる(self) -> None:
        """飽和しない理由が「目標が届いていない」ではないこと。

        実際に軸が動いていることまで見ておかないと、目標を捨てる実装でもこのクラスが
        緑になる (飽和しないことは「動かない」でも成立してしまう)。
        """
        rig = _ProfileRig(motion=(10.0, 50.0))

        await rig.move_to(LONG_MOVE_MM)
        await rig.run(0.5)

        # 加速 0.2s (1mm) + 巡航 0.3s (3mm) で 4mm 前後
        assert 2.0 < rig.position_mm < 6.0


def _flying(fx: _Fixture, name: str = "lift") -> None:
    """``name`` に台形プロファイルを後付けする (制限は指令単位 deg のまま)。"""
    fx.loop.set_motion_profile(
        name,
        TrapezoidalProfile(max_velocity=50.0, max_acceleration=500.0),
        velocity_ff=0.0,
    )


async def _fly(fx: _Fixture, name: str = "lift", *, ticks: int = 100) -> float:
    """中間目標を飛行中の状態にし、そこまでに進んだ中間目標 [deg] を返す。

    実測を 0 に据え置くので、出力はそのまま ``kp * 中間目標`` になる。これで
    「その周期に PID が見た中間目標」を公開 API だけで読める。
    """
    await fx.loop.set_target(name, ControlMode.POSITION, 1000.0)
    for _ in range(ticks):
        await fx.tick()
        fx.feed(name, 0.0)
    return fx.manager.last_currents[0] / 5.0


class TestProfileIsDiscardedOnSafetyPaths:
    """止まっていた間に進んだはずの中間目標へ、復帰 1 周期目に飛ばないこと。

    復帰の瞬間に機構がどこに居るかは分からない (自重で落ちる・動作確認に動かされる)。
    起点を据え置くと、その差がまるごと 1 周期の偏差として PID に入る。

    **層ごとに 1 つずつ確かめる。** 5 つの経路 (緊急停止 = ``_reset_axis`` /
    一時停止からの復帰 / フィードバック途絶 / 相方の異常による blocked / 開ループ
    指令) はどれも単独で機体を守っているので、まとめて 1 本にすると 1 枚外しても
    落ちない。
    """

    #: 復帰までに機構が落ちる量 [deg]。据え置いた起点との差がここまで開く
    FALL_DEG = -30.0

    def _rig(self) -> _Fixture:
        # kp を小さくして出力レンジの端から離す (飽和で差が潰れると層が見えない)
        fx = _Fixture(kp=5.0)
        _flying(fx)
        return fx

    def _assert_reanchored(self, fx: _Fixture, flown: float) -> None:
        current = fx.manager.last_currents[0]
        # 実測から起こし直せば偏差は 1 周期の進みぶん (50deg/s * 5ms = 0.25deg) しかない
        assert abs(current) < 5.0, f"中間目標が実測から離れている (出力 {current})"
        # 据え置いた場合との差が本当に出る状況だったかも見る (前提が崩れていないか)
        assert abs(flown - self.FALL_DEG) > 10.0

    async def test_緊急停止からの復帰で中間目標が飛ばない(self) -> None:
        fx = self._rig()
        flown = await _fly(fx)

        fx.estop = True
        await fx.tick()
        fx.estop = False
        # 停止中に自重で落ちた
        fx.feed("lift", self.FALL_DEG)
        await fx.loop.set_target("lift", ControlMode.POSITION, 1000.0)
        await fx.tick()

        self._assert_reanchored(fx, flown)

    async def test_一時停止からの復帰で中間目標が飛ばない(self) -> None:
        """動作確認が同一バスを握っている間、機構はこのループの指令と無関係に動く。"""
        fx = self._rig()
        flown = await _fly(fx)

        await fx.loop.pause()
        await fx.tick()
        # 動作確認が動かした先
        fx.feed("lift", self.FALL_DEG)
        fx.loop.resume()
        await fx.tick()

        self._assert_reanchored(fx, flown)

    async def test_フィードバック途絶からの復帰で中間目標が飛ばない(self) -> None:
        fx = self._rig()
        flown = await _fly(fx)

        # 目標もモードも残るので、ここだけが起点を捨てる層になる
        fx.wall.advance(1.0)
        await fx.tick()
        fx.feed("lift", self.FALL_DEG)
        await fx.tick()

        self._assert_reanchored(fx, flown)

    async def test_相方の異常で力を抜いた後も中間目標が飛ばない(self) -> None:
        """自分は健全でも、直結した相方が止まれば力は抜ける (その間に機構は動く)。"""
        fx = self._rig()
        fx.loop.add_sync_group(
            SyncGroup(
                "y_axis",
                (MotorSpec("lift", 1.0, 0.0), MotorSpec("tilt", -1.0, 0.0)),
                tolerance=1e6,
            )
        )
        await fx.loop.set_target("tilt", ControlMode.POSITION, 0.0)
        flown = await _fly(fx)

        # tilt だけ途絶させる → lift は blocked (自分のフィードバックは新しいまま)
        fx.wall.advance(1.0)
        fx.feed("lift", 0.0)
        await fx.tick()
        fx.feed("lift", self.FALL_DEG)
        fx.feed("tilt", 0.0)
        await fx.tick()

        self._assert_reanchored(fx, flown)

    async def test_開ループの押し当ての後も中間目標が飛ばない(self) -> None:
        """ホーミングは機構端まで開ループで押す。その間の移動は軌道に入っていない。"""
        fx = self._rig()
        flown = await _fly(fx)

        await fx.loop.set_target("lift", ControlMode.CURRENT, -300.0)
        await fx.tick()
        fx.feed("lift", self.FALL_DEG)
        await fx.loop.set_target("lift", ControlMode.POSITION, 1000.0)
        await fx.tick()

        self._assert_reanchored(fx, flown)


class TestProfileRetarget:
    async def test_移動中の目標差し替えは起点を実測へ戻さない(self) -> None:
        """左右直結ペアで実測から起こし直すと、追従誤差の差が軌道長の差になる。

        差し替えのたびに実測起点へ戻す実装だと、追従が遅れているこの状況で中間目標
        まで実測位置へ戻ってしまう。
        """
        fx = _Fixture(kp=5.0)
        _flying(fx)
        flown = await _fly(fx)

        await fx.loop.set_target("lift", ControlMode.POSITION, 900.0)
        await fx.tick()

        # 進んでいた中間目標がそのまま続く (実測 0 へは戻らない)
        assert fx.manager.last_currents[0] / 5.0 > flown


class TestVelocityFeedforward:
    """速度 FF は ``PIDController.update(feedforward=...)`` へ渡すこと。

    外で足して後からクランプすると、アンチワインドアップが FF を知らないまま積分を
    進める。「FF 込みでは出力が飽和していて機構が動けないのに、積分だけが育つ」
    状態になり、拘束が外れた瞬間に暴走する。
    """

    async def test_FF_で飽和している間は積分が育たない(self) -> None:
        fx = _Fixture(kp=0.0, ki=1.0)
        pid = fx.loop.pid("lift")
        # FF だけで上限に届く組み合わせにする (P 項では届かせない)
        pid.output_min, pid.output_max = -500.0, 500.0
        pid.dead_band = 0.0
        fx.loop.set_motion_profile(
            "lift",
            TrapezoidalProfile(max_velocity=100.0, max_acceleration=500.0),
            velocity_ff=10.0,
        )

        await _fly(fx, ticks=200)

        # 実測は 0 のままなので偏差は開き続ける。FF を PID の内側へ渡していれば
        # 飽和が見えて積分は止まり、外で足していれば PID からは余裕があるように
        # 見えて積分だけが育つ
        assert pid.integral < 0.1

    async def test_FF_は参照速度に比例して出力へ乗る(self) -> None:
        fx = _Fixture(kp=0.0, ki=0.0)
        fx.loop.set_motion_profile(
            "lift",
            TrapezoidalProfile(max_velocity=100.0, max_acceleration=500.0),
            velocity_ff=2.0,
        )

        await _fly(fx, ticks=100)

        # 0.2s で v_max 100deg/s に達しているので FF = 2.0 * 100
        assert fx.manager.last_currents[0] == pytest.approx(200, abs=1)

    async def test_負の係数は受け付けない(self) -> None:
        fx = _Fixture(kp=5.0)
        with pytest.raises(ValueError, match="velocity_ff"):
            fx.loop.set_motion_profile(
                "lift",
                TrapezoidalProfile(max_velocity=50.0, max_acceleration=500.0),
                velocity_ff=-1.0,
            )

    async def test_未登録のモータには設定できない(self) -> None:
        fx = _Fixture(kp=5.0)
        with pytest.raises(KeyError):
            fx.loop.set_motion_profile(
                "missing", TrapezoidalProfile(max_velocity=50.0, max_acceleration=500.0)
            )


class TestProfileDoesNotChangeTheContract:
    async def test_中間目標は_PID_と同じ_dt_で進む(self) -> None:
        """周期が伸びた分だけ軌道も進むこと。

        固定周期で進めると、asyncio が詰まって周期が伸びた瞬間に「PID は 20ms 分の
        偏差を見ているのに軌道は 5ms しか進んでいない」状態になり、参照速度と実際の
        中間目標の進み方が食い違う。
        """
        fx = _Fixture(kp=100.0)
        _flying(fx)
        await fx.loop.set_target("lift", ControlMode.POSITION, 1000.0)
        for _ in range(100):
            await fx.tick()
            fx.feed("lift", 0.0)
        before = fx.manager.last_currents[0]

        await fx.tick(dt=0.02)

        # 巡航 (50deg/s) の 20ms なので中間目標は 1.0deg 進む (5ms なら 0.25deg)
        assert (fx.manager.last_currents[0] - before) / 100.0 == pytest.approx(1.0, abs=0.05)

    async def test_ゲイン差し替えでは軌道を捨てない(self) -> None:
        """ゲインが変わっても軌道は変わらない。捨てると移動中の変更で機構が飛ぶ。"""
        fx = _Fixture(kp=5.0)
        _flying(fx)
        flown = await _fly(fx)

        fx.loop.set_pid_gains("lift", {"kp": 5.0})
        await fx.tick()

        assert fx.manager.last_currents[0] / 5.0 > flown

    async def test_記録に載る目標は最終目標であって中間目標ではない(self) -> None:
        """中間目標を記録の目標にすると、指標 (行き過ぎ・整定) の意味が変わる。

        ランプは「ステップ」として検出されないので、記録そのものが起きなくなる方向へ
        倒れる。件数だけを見ても最初の 1 回は必ずトリガされるので (記録器は初回を
        無条件に起点にする)、**記録された目標値そのもの**を見る。
        """
        fx = _Fixture(kp=5.0, tuning=RECORDING)
        _flying(fx)

        await _fly(fx, ticks=60)

        assert fx.captures, "最終目標のステップが記録の窓を開いていない"
        assert {sample.target for sample in fx.captures[0].samples} == {1000.0}

    async def test_プロファイルを持たない軸は従来どおりステップ入力(self) -> None:
        fx = _Fixture(kp=100.0)
        await fx.loop.set_target("lift", ControlMode.POSITION, 10.0)

        await fx.tick()

        assert fx.manager.last_currents[0] == 1000
