from __future__ import annotations

import asyncio
import struct

import can
import pytest

from lib.control.target_refresh import (
    DEFAULT_INTERVAL_S,
    FIRMWARE_COMMAND_TIMEOUT_S,
    GenericTargetRefresher,
    QueryDrivenTargetRefresher,
    hold_target_refresh,
)
from lib.drivers.base import ControlMode
from lib.drivers.dm3520 import Dm3520Driver
from lib.drivers.generic import GenericDriver
from lib.sequence.motors import MotorHandle
from tests.feedback_frames import confirm_dm3520_ranges, feed_dm3520


class _StubCANManager:
    def __init__(self) -> None:
        self.sent: list[tuple[str, can.Message]] = []
        self.fail_motors: set[str] = set()

    async def send(self, motor_name: str, msg: can.Message) -> None:
        if motor_name in self.fail_motors:
            raise can.CanError("送信失敗 (テスト)")
        self.sent.append((motor_name, msg))

    def names(self) -> list[str]:
        return [name for name, _ in self.sent]


def _target_value(msg: can.Message) -> float:
    return struct.unpack_from("<h", msg.data, 1)[0] / 10000.0


class _Fixture:
    def __init__(self) -> None:
        self.manager = _StubCANManager()
        self.estop = False
        self.conveyor = GenericDriver("conveyor", can_id=1, control_type=ControlMode.DUTY)
        self.wall = GenericDriver("wall_f", can_id=2)
        self.handles = {
            driver.name: MotorHandle(
                driver.name,
                driver,
                self.manager,  # type: ignore[arg-type]
                is_estop_active=lambda: self.estop,
            )
            for driver in (self.conveyor, self.wall)
        }
        self.refresher = GenericTargetRefresher(
            list(self.handles.values()),
            is_estop_active=lambda: self.estop,
        )

    def clear_sent(self) -> None:
        self.manager.sent.clear()


class TestResend:
    async def test_resends_last_target(self) -> None:
        fx = _Fixture()
        await fx.handles["conveyor"].set_target(ControlMode.DUTY, 0.3)
        fx.clear_sent()

        await fx.refresher.step()

        assert fx.manager.names() == ["conveyor"]
        assert _target_value(fx.manager.sent[0][1]) == pytest.approx(0.3)

    async def test_resends_every_step(self) -> None:
        fx = _Fixture()
        await fx.handles["conveyor"].set_target(ControlMode.DUTY, 0.3)
        fx.clear_sent()

        await fx.refresher.step()
        await fx.refresher.step()
        await fx.refresher.step()

        assert fx.manager.names() == ["conveyor"] * 3

    async def test_no_send_without_target(self) -> None:
        fx = _Fixture()

        await fx.refresher.step()

        assert fx.manager.sent == []

    async def test_target_is_not_altered_by_resend(self) -> None:
        fx = _Fixture()
        await fx.handles["conveyor"].set_target(ControlMode.DUTY, 0.3)

        await fx.refresher.step()

        assert fx.handles["conveyor"].target == pytest.approx(0.3)
        assert fx.handles["conveyor"].mode is ControlMode.DUTY


class TestEStopInterlock:
    async def test_no_send_while_estop_active(self) -> None:
        fx = _Fixture()
        await fx.handles["conveyor"].set_target(ControlMode.DUTY, 0.3)
        fx.clear_sent()
        fx.estop = True

        await fx.refresher.step()

        assert fx.manager.sent == []

    async def test_clear_targets_prevents_restart_after_release(self) -> None:
        fx = _Fixture()
        await fx.handles["conveyor"].set_target(ControlMode.DUTY, 0.3)
        fx.estop = True

        fx.refresher.clear_targets()
        fx.estop = False
        fx.clear_sent()
        await fx.refresher.step()

        assert fx.manager.sent == []


class TestClearSingleTarget:
    async def test_only_the_named_handle_loses_its_target(self) -> None:
        fx = _Fixture()
        await fx.handles["conveyor"].set_target(ControlMode.DUTY, 0.3)
        await fx.handles["wall_f"].set_target(ControlMode.POSITION, 45.0)

        fx.refresher.clear_target("conveyor")

        assert fx.handles["conveyor"].has_target is False
        assert fx.handles["wall_f"].target == 45.0

    async def test_unknown_name_is_a_no_op(self) -> None:
        fx = _Fixture()
        await fx.handles["conveyor"].set_target(ControlMode.DUTY, 0.3)

        fx.refresher.clear_target("no_such_motor")

        assert fx.handles["conveyor"].target == 0.3


class TestPauseForMotorCheck:
    async def test_paused_refresher_sends_nothing(self) -> None:
        fx = _Fixture()
        await fx.handles["conveyor"].set_target(ControlMode.DUTY, 0.3)
        fx.clear_sent()

        await fx.refresher.pause(reason="動作確認")
        await fx.refresher.step()

        assert fx.manager.sent == []

    async def test_resume_restores_resending(self) -> None:
        fx = _Fixture()
        await fx.handles["conveyor"].set_target(ControlMode.DUTY, 0.3)
        await fx.refresher.pause(reason="動作確認")
        fx.refresher.resume()
        fx.clear_sent()

        await fx.refresher.step()

        assert fx.manager.names() == ["conveyor"]


class TestFailureIsolation:
    async def test_one_motor_failure_does_not_block_others(self) -> None:
        fx = _Fixture()
        await fx.handles["conveyor"].set_target(ControlMode.DUTY, 0.3)
        await fx.handles["wall_f"].set_target(ControlMode.POSITION, 90.0)
        fx.clear_sent()
        fx.manager.fail_motors = {"conveyor"}

        await fx.refresher.step()

        assert fx.manager.names() == ["wall_f"]

    async def test_run_survives_step_exception(self) -> None:
        fx = _Fixture()
        await fx.handles["conveyor"].set_target(ControlMode.DUTY, 0.3)
        fx.manager.fail_motors = {"conveyor"}
        ticks = 0

        async def _sleep(_delay: float) -> None:
            nonlocal ticks
            ticks += 1
            if ticks >= 3:
                fx.refresher.request_stop()
            await asyncio.sleep(0)

        fx.refresher.set_sleep(_sleep)
        await fx.refresher.run()

        assert ticks == 3


class TestLifecycle:
    async def test_start_and_stop_leaves_no_task(self) -> None:
        fx = _Fixture()
        fx.refresher.start()
        assert fx.refresher.is_running is True

        await fx.refresher.stop()

        assert fx.refresher.is_running is False

    async def test_double_start_raises(self) -> None:
        fx = _Fixture()
        fx.refresher.start()
        try:
            with pytest.raises(RuntimeError):
                fx.refresher.start()
        finally:
            await fx.refresher.stop()

    async def test_stop_without_start_is_noop(self) -> None:
        fx = _Fixture()
        await fx.refresher.stop()
        assert fx.refresher.is_running is False


class TestWatchdogMargin:
    def test_interval_has_margin_over_firmware_watchdog(self) -> None:
        assert DEFAULT_INTERVAL_S * 5 <= FIRMWARE_COMMAND_TIMEOUT_S


class _Dm3520Fixture:
    def __init__(self, **driver_kwargs: object) -> None:
        self.manager = _StubCANManager()
        self.estop = False
        params: dict = {"master_id": 0x11}
        params.update(driver_kwargs)
        self.slide = Dm3520Driver("sub_slide", 0x05, **params)  # type: ignore[arg-type]
        confirm_dm3520_ranges(self.slide)
        self.handle = MotorHandle(
            "sub_slide",
            self.slide,
            self.manager,  # type: ignore[arg-type]
            is_estop_active=lambda: self.estop,
        )
        self.refresher = QueryDrivenTargetRefresher(
            [self.handle],
            self.manager,  # type: ignore[arg-type]
            is_estop_active=lambda: self.estop,
        )

    def clear_sent(self) -> None:
        self.manager.sent.clear()

    @staticmethod
    def position_of(msg: can.Message) -> float:
        p_des, _ = struct.unpack("<ff", msg.data)
        return p_des


class TestDm3520PollsEvenWithoutTarget:
    async def test_sends_hold_target_before_any_command(self) -> None:
        fx = _Dm3520Fixture()
        feed_dm3520(fx.slide, position=1.5)
        fx.clear_sent()

        await fx.refresher.step()

        assert fx.manager.names() == ["sub_slide"]
        assert fx.position_of(fx.manager.sent[0][1]) == pytest.approx(1.5, abs=1e-3)

    async def test_hold_target_is_latched_not_re_measured(self) -> None:
        fx = _Dm3520Fixture()
        feed_dm3520(fx.slide, position=1.5)
        await fx.refresher.step()
        fx.clear_sent()

        feed_dm3520(fx.slide, position=1.0)
        await fx.refresher.step()

        assert fx.position_of(fx.manager.sent[0][1]) == pytest.approx(1.5, abs=1e-3)

    async def test_velocity_mode_holds_stop(self) -> None:
        fx = _Dm3520Fixture(mode=ControlMode.VELOCITY)
        feed_dm3520(fx.slide, velocity=3.0)
        fx.clear_sent()

        await fx.refresher.step()

        assert struct.unpack("<f", fx.manager.sent[0][1].data)[0] == 0.0

    async def test_resends_the_operator_target_once_it_exists(self) -> None:
        fx = _Dm3520Fixture()
        await fx.handle.set_target(ControlMode.POSITION, 2.5)
        fx.clear_sent()

        await fx.refresher.step()

        assert fx.position_of(fx.manager.sent[0][1]) == pytest.approx(2.5)

    async def test_latch_is_retaken_after_the_target_is_cleared(self) -> None:
        fx = _Dm3520Fixture()
        await fx.handle.set_target(ControlMode.POSITION, 2.5)
        await fx.refresher.step()

        fx.handle.clear_target()
        feed_dm3520(fx.slide, position=0.25)
        fx.clear_sent()
        await fx.refresher.step()

        assert fx.position_of(fx.manager.sent[0][1]) == pytest.approx(0.25, abs=1e-3)


class TestDm3520EStop:
    async def test_keeps_polling_during_estop(self) -> None:
        fx = _Dm3520Fixture()
        feed_dm3520(fx.slide, position=1.0)
        fx.estop = True
        fx.clear_sent()

        await fx.refresher.step()

        assert fx.manager.names() == ["sub_slide"]
        assert fx.position_of(fx.manager.sent[0][1]) == pytest.approx(1.0, abs=1e-3)

    async def test_never_sends_enable(self) -> None:
        fx = _Dm3520Fixture()
        await fx.handle.set_target(ControlMode.POSITION, 2.5)
        fx.estop = True
        fx.clear_sent()

        await fx.refresher.step()

        for _, msg in fx.manager.sent:
            assert bytes(msg.data)[-1] != 0xFC

    async def test_does_not_resend_the_pre_estop_target(self) -> None:
        fx = _Dm3520Fixture()
        await fx.handle.set_target(ControlMode.POSITION, 2.5)
        feed_dm3520(fx.slide, position=0.5)
        fx.estop = True
        fx.clear_sent()

        await fx.refresher.step()

        assert fx.position_of(fx.manager.sent[0][1]) == pytest.approx(0.5, abs=1e-3)

    async def test_does_not_latch_during_estop(self) -> None:
        fx = _Dm3520Fixture()
        fx.estop = True
        feed_dm3520(fx.slide, position=1.0)
        await fx.refresher.step()

        feed_dm3520(fx.slide, position=0.2)
        fx.clear_sent()
        await fx.refresher.step()

        assert fx.position_of(fx.manager.sent[0][1]) == pytest.approx(0.2, abs=1e-3)

    async def test_clear_targets_drops_the_target(self) -> None:
        fx = _Dm3520Fixture()
        await fx.handle.set_target(ControlMode.POSITION, 2.5)

        fx.refresher.clear_targets()

        assert fx.handle.has_target is False


class TestDm3520ClearSingleTarget:
    def _two_motor_fixture(self) -> tuple[_StubCANManager, dict[str, MotorHandle], object]:
        manager = _StubCANManager()
        dropped = Dm3520Driver("dropped", 0x01, master_id=0x11)
        healthy = Dm3520Driver("healthy", 0x02, master_id=0x12)
        confirm_dm3520_ranges(dropped)
        confirm_dm3520_ranges(healthy)
        handles = {
            driver.name: MotorHandle(driver.name, driver, manager)  # type: ignore[arg-type]
            for driver in (dropped, healthy)
        }
        refresher = QueryDrivenTargetRefresher(
            list(handles.values()),
            manager,
            is_estop_active=lambda: False,  # type: ignore[arg-type]
        )
        return manager, handles, refresher

    async def test_named_motor_relatches_fresh_position(self) -> None:
        manager, handles, refresher = self._two_motor_fixture()
        dropped_driver = handles["dropped"].driver
        healthy_driver = handles["healthy"].driver

        feed_dm3520(dropped_driver, position=3.0)  # type: ignore[arg-type]
        feed_dm3520(healthy_driver, position=0.0)  # type: ignore[arg-type]
        await refresher.step()

        feed_dm3520(dropped_driver, position=1.0)  # type: ignore[arg-type]

        refresher.clear_target("dropped")
        manager.sent.clear()
        await refresher.step()

        sent = {name: msg for name, msg in manager.sent}
        p_des, _ = struct.unpack("<ff", sent["dropped"].data)
        assert p_des == pytest.approx(1.0, abs=1e-3)

    async def test_other_motor_on_same_refresher_keeps_its_latch(self) -> None:
        manager, handles, refresher = self._two_motor_fixture()
        dropped_driver = handles["dropped"].driver
        healthy_driver = handles["healthy"].driver

        feed_dm3520(dropped_driver, position=3.0)  # type: ignore[arg-type]
        feed_dm3520(healthy_driver, position=2.0)  # type: ignore[arg-type]
        await refresher.step()

        feed_dm3520(healthy_driver, position=0.5)  # type: ignore[arg-type]

        refresher.clear_target("dropped")
        manager.sent.clear()
        await refresher.step()

        sent = {name: msg for name, msg in manager.sent}
        p_des, _ = struct.unpack("<ff", sent["healthy"].data)
        assert p_des == pytest.approx(2.0, abs=1e-3)


class TestDm3520PauseForMotorCheck:
    async def test_paused_refresher_sends_nothing(self) -> None:
        fx = _Dm3520Fixture()
        feed_dm3520(fx.slide, position=1.0)
        await fx.refresher.pause(reason="動作確認")
        fx.clear_sent()

        await fx.refresher.step()

        assert fx.manager.sent == []

    async def test_resume_restores_sending(self) -> None:
        fx = _Dm3520Fixture()
        feed_dm3520(fx.slide, position=1.0)
        await fx.refresher.pause(reason="動作確認")
        fx.refresher.resume()
        fx.clear_sent()

        await fx.refresher.step()

        assert fx.manager.names() == ["sub_slide"]


class TestDm3520FailureIsolation:
    async def test_send_failure_does_not_escape(self) -> None:
        fx = _Dm3520Fixture()
        feed_dm3520(fx.slide, position=1.0)
        fx.manager.fail_motors.add("sub_slide")

        await fx.refresher.step()

        assert fx.manager.sent == []


class TestDm3520RangeGate:
    """レンジが未確認 / 食い違いのあいだは「今の姿勢を保て」を取らず、送りもしない。"""

    @staticmethod
    def _unconfirmed(**driver_kwargs: object) -> _Dm3520Fixture:
        fx = _Dm3520Fixture(**driver_kwargs)
        fx.slide = Dm3520Driver("sub_slide", 0x05, master_id=0x11, **driver_kwargs)  # type: ignore[arg-type]
        fx.handle = MotorHandle("sub_slide", fx.slide, fx.manager)  # type: ignore[arg-type]
        fx.refresher = QueryDrivenTargetRefresher(
            [fx.handle],
            fx.manager,  # type: ignore[arg-type]
            is_estop_active=lambda: fx.estop,
        )
        return fx

    async def test_unconfirmed_range_sends_nothing(self) -> None:
        fx = self._unconfirmed(p_max=1000.0)
        feed_dm3520(fx.slide, position=80.0)

        await fx.refresher.step()

        assert fx.manager.sent == []

    async def test_unconfirmed_range_sends_nothing_during_estop_either(self) -> None:
        fx = self._unconfirmed(p_max=1000.0)
        feed_dm3520(fx.slide, position=80.0)
        fx.estop = True

        await fx.refresher.step()

        assert fx.manager.sent == []

    async def test_mismatched_range_sends_nothing(self) -> None:
        fx = self._unconfirmed(p_max=1000.0)
        confirm_dm3520_ranges(fx.slide, p_max=12.5)
        feed_dm3520(fx.slide, position=80.0)

        await fx.refresher.step()

        assert fx.manager.sent == []

    async def test_latch_is_taken_only_after_the_range_is_confirmed(self) -> None:
        fx = self._unconfirmed(p_max=1000.0)
        feed_dm3520(fx.slide, position=80.0)
        await fx.refresher.step()

        confirm_dm3520_ranges(fx.slide)
        feed_dm3520(fx.slide, position=1.0)
        await fx.refresher.step()

        assert fx.manager.names() == ["sub_slide"]
        assert fx.position_of(fx.manager.sent[0][1]) == pytest.approx(1.0, abs=1e-2)

    async def test_latch_taken_before_a_power_loss_is_dropped(self) -> None:
        fx = _Dm3520Fixture(p_max=1000.0)
        feed_dm3520(fx.slide, position=1.0)
        await fx.refresher.step()

        # 再初期化はレンジの控えを捨てる (電源断からの復帰)
        fx.slide.reinitialization_steps()
        fx.clear_sent()
        await fx.refresher.step()
        confirm_dm3520_ranges(fx.slide)
        feed_dm3520(fx.slide, position=2.0)
        await fx.refresher.step()

        assert fx.manager.names() == ["sub_slide"]
        assert fx.position_of(fx.manager.sent[0][1]) == pytest.approx(2.0, abs=1e-2)


class TestHoldTargetRefresh:
    async def test_pauses_only_the_refreshers_that_hold_the_named_motors(self) -> None:
        fx = _Dm3520Fixture()
        other = _Fixture()

        async with hold_target_refresh([fx.refresher, other.refresher], ["sub_slide"], reason="t"):
            assert fx.refresher.is_paused is True
            assert other.refresher.is_paused is False

        assert fx.refresher.is_paused is False

    async def test_latch_taken_before_the_hold_is_dropped_on_exit(self) -> None:
        fx = _Dm3520Fixture()
        feed_dm3520(fx.slide, position=3.0)
        await fx.refresher.step()

        async with hold_target_refresh([fx.refresher], ["sub_slide"], reason="t"):
            feed_dm3520(fx.slide, position=1.0)
        fx.clear_sent()
        await fx.refresher.step()

        assert fx.position_of(fx.manager.sent[0][1]) == pytest.approx(1.0, abs=1e-3)

    async def test_explicit_target_is_dropped_on_exit(self) -> None:
        fx = _Dm3520Fixture()
        await fx.handle.set_target(ControlMode.POSITION, 2.5)

        async with hold_target_refresh([fx.refresher], ["sub_slide"], reason="t"):
            pass

        assert fx.handle.has_target is False

    async def test_resumes_even_when_the_body_raises(self) -> None:
        fx = _Dm3520Fixture()

        with pytest.raises(RuntimeError):
            async with hold_target_refresh([fx.refresher], ["sub_slide"], reason="t"):
                raise RuntimeError("励磁に失敗")

        assert fx.refresher.is_paused is False

    async def test_untargeted_motor_keeps_its_target(self) -> None:
        manager = _StubCANManager()
        a = Dm3520Driver("a", 0x01, master_id=0x11)
        b = Dm3520Driver("b", 0x02, master_id=0x12)
        handles = [MotorHandle(d.name, d, manager) for d in (a, b)]  # type: ignore[arg-type]
        refresher = QueryDrivenTargetRefresher(handles, manager)  # type: ignore[arg-type]
        await handles[1].set_target(ControlMode.POSITION, 2.0)

        async with hold_target_refresh([refresher], ["a"], reason="t"):
            pass

        assert handles[1].target == 2.0
