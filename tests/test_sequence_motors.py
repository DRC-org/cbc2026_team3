from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import can
import pytest

from lib.drivers.base import ControlMode
from lib.sequence.engine import Sequence, step
from lib.sequence.motors import (
    AxisHandle,
    EStopActiveError,
    MotorGroup,
    MotorHandle,
    WaitInterruptedError,
    build_motor_group,
)
from lib.sequence.positions import AxisSpec, MotorSpec, PositionLookupError
from tests.fake_drivers import StubFeedbackDriver


class _FakeDriver(StubFeedbackDriver):
    """送った指令を記録するテスト用ドライバ (観測値の投入は基底の set_observed)。"""

    def __init__(self, name: str = "m1", can_id: int = 1) -> None:
        super().__init__(name, can_id)
        self.encoded: list[tuple[ControlMode, float]] = []

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        self.encoded.append((mode, value))
        return super().encode_target(mode, value)


def _make_can_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.send = AsyncMock()
    return mgr


def _make_handle(**kwargs) -> tuple[MotorHandle, _FakeDriver, MagicMock]:
    driver = _FakeDriver()
    mgr = _make_can_manager()
    handle = MotorHandle("m1", driver, mgr, **kwargs)
    return handle, driver, mgr


class TestMotorHandleSend:
    async def test_set_position_encodes_and_sends(self) -> None:
        handle, driver, mgr = _make_handle()

        await handle.set_target(ControlMode.POSITION, 120.0)

        assert driver.encoded == [(ControlMode.POSITION, 120.0)]
        mgr.send.assert_awaited_once()
        sent_name, sent_msg = mgr.send.await_args.args
        assert sent_name == "m1"
        assert sent_msg.arbitration_id == 0x101

    async def test_set_velocity_current_duty(self) -> None:
        handle, driver, mgr = _make_handle()

        await handle.set_target(ControlMode.VELOCITY, 50.0)
        await handle.set_target(ControlMode.CURRENT, 500.0)
        await handle.set_target(ControlMode.DUTY, 0.3)

        assert driver.encoded == [
            (ControlMode.VELOCITY, 50.0),
            (ControlMode.CURRENT, 500.0),
            (ControlMode.DUTY, 0.3),
        ]
        assert mgr.send.await_count == 3

    async def test_last_target_is_recorded(self) -> None:
        handle, _driver, _mgr = _make_handle()

        assert handle.has_target is False
        assert handle.target is None
        assert handle.mode is None

        await handle.set_target(ControlMode.POSITION, 90.0)

        assert handle.has_target is True
        assert handle.target == 90.0
        assert handle.mode is ControlMode.POSITION

    def test_state_delegates_to_driver(self) -> None:
        handle, driver, _mgr = _make_handle()
        driver.set_observed(position=12.0)
        assert handle.state.position == 12.0

    def test_name_and_driver_exposed(self) -> None:
        handle, driver, _mgr = _make_handle()
        assert handle.name == "m1"
        assert handle.driver is driver


class TestMotorHandleEStop:
    async def test_set_position_rejected_while_estop_active(self) -> None:
        active = True
        handle, driver, mgr = _make_handle(is_estop_active=lambda: active)

        with pytest.raises(EStopActiveError):
            await handle.set_target(ControlMode.POSITION, 10.0)

        assert driver.encoded == []
        mgr.send.assert_not_awaited()

    async def test_all_setters_rejected_while_estop_active(self) -> None:
        handle, _driver, mgr = _make_handle(is_estop_active=lambda: True)

        for coro in (
            handle.set_target(ControlMode.VELOCITY, 1.0),
            handle.set_target(ControlMode.CURRENT, 1.0),
            handle.set_target(ControlMode.DUTY, 0.1),
        ):
            with pytest.raises(EStopActiveError):
                await coro

        mgr.send.assert_not_awaited()

    async def test_send_allowed_when_estop_released(self) -> None:
        active = True

        def is_active() -> bool:
            return active

        handle, driver, mgr = _make_handle(is_estop_active=is_active)
        active = False

        await handle.set_target(ControlMode.POSITION, 10.0)

        assert driver.encoded == [(ControlMode.POSITION, 10.0)]
        mgr.send.assert_awaited_once()

    async def test_estop_rejection_does_not_update_target(self) -> None:
        handle, _driver, _mgr = _make_handle(is_estop_active=lambda: True)

        with pytest.raises(EStopActiveError):
            await handle.set_target(ControlMode.POSITION, 10.0)

        assert handle.has_target is False


class TestMotorHandleTargetSink:
    async def test_sink_replaces_can_send(self) -> None:
        calls: list[tuple[ControlMode, float]] = []

        async def sink(mode: ControlMode, value: float) -> None:
            calls.append((mode, value))

        handle, driver, mgr = _make_handle(target_sink=sink)

        await handle.set_target(ControlMode.POSITION, 45.0)

        assert calls == [(ControlMode.POSITION, 45.0)]
        assert driver.encoded == []
        mgr.send.assert_not_awaited()

    async def test_sink_still_records_target(self) -> None:
        async def sink(mode: ControlMode, value: float) -> None:
            return None

        handle, driver, _mgr = _make_handle(target_sink=sink)
        await handle.set_target(ControlMode.POSITION, 45.0)
        driver.set_observed(position=45.0)

        assert handle.target == 45.0
        assert await handle.wait_reached(timeout=0.05) is True

    async def test_sink_blocked_by_estop(self) -> None:
        calls: list[tuple[ControlMode, float]] = []

        async def sink(mode: ControlMode, value: float) -> None:
            calls.append((mode, value))

        handle, _driver, _mgr = _make_handle(target_sink=sink, is_estop_active=lambda: True)

        with pytest.raises(EStopActiveError):
            await handle.set_target(ControlMode.POSITION, 45.0)

        assert calls == []


class TestMotorHandleWaitReached:
    async def test_returns_true_when_already_reached(self) -> None:
        handle, driver, _mgr = _make_handle()
        await handle.set_target(ControlMode.POSITION, 10.0)
        driver.set_observed(position=10.2)

        assert await handle.wait_reached(timeout=0.05) is True

    async def test_returns_false_on_timeout(self) -> None:
        handle, driver, _mgr = _make_handle()
        await handle.set_target(ControlMode.POSITION, 10.0)
        driver.set_observed(position=100.0)

        assert await handle.wait_reached(timeout=0.05) is False

    async def test_returns_true_when_reached_later(self) -> None:
        handle, driver, _mgr = _make_handle()
        await handle.set_target(ControlMode.POSITION, 10.0)
        driver.set_observed(position=100.0)

        async def arrive() -> None:
            await asyncio.sleep(0.03)
            driver.set_observed(position=10.0)

        task = asyncio.create_task(arrive())
        try:
            assert await handle.wait_reached(timeout=1.0) is True
        finally:
            await task

    async def test_explicit_tolerance(self) -> None:
        handle, driver, _mgr = _make_handle()
        await handle.set_target(ControlMode.POSITION, 10.0)
        driver.set_observed(position=13.0)

        assert await handle.wait_reached(tolerance=5.0, timeout=0.05) is True
        assert await handle.wait_reached(tolerance=0.5, timeout=0.05) is False

    async def test_no_target_is_reached(self) -> None:
        handle, _driver, _mgr = _make_handle()
        assert await handle.wait_reached(timeout=0.05) is True

    async def test_clear_target(self) -> None:
        handle, driver, _mgr = _make_handle()
        await handle.set_target(ControlMode.POSITION, 10.0)
        driver.set_observed(position=100.0)

        handle.clear_target()

        assert handle.has_target is False
        assert await handle.wait_reached(timeout=0.05) is True

    async def test_target_cleared_mid_wait_raises_interrupted(self) -> None:
        """緊急停止などが待機中に目標を刈り取ったら「到達」にすり替えず中断と分かる形にする。

        回帰対象: MotorHandle.is_reached() は「目標が無ければ到達済み」を返すため、
        wait_reached() の実行中に clear_target() が入ると、かつては黙って True を
        返し、move_to() が中断された動作をステップ成功として記録していた。
        """
        handle, driver, _mgr = _make_handle()
        await handle.set_target(ControlMode.POSITION, 10.0)
        driver.set_observed(position=100.0)  # 到達しないまま待たせる

        async def interrupt() -> None:
            await asyncio.sleep(0.03)
            handle.clear_target()

        task = asyncio.create_task(interrupt())
        try:
            with pytest.raises(WaitInterruptedError):
                await handle.wait_reached(timeout=1.0)
        finally:
            await task


class TestMotorGroup:
    def _group(self) -> tuple[MotorGroup, dict[str, _FakeDriver], MagicMock]:
        mgr = _make_can_manager()
        drivers = {
            "lift_motor": _FakeDriver("lift_motor", 1),
            "arm_joint": _FakeDriver("arm_joint", 2),
        }
        group = build_motor_group(mgr, drivers)
        return group, drivers, mgr

    def test_attribute_access(self) -> None:
        group, drivers, _mgr = self._group()
        assert isinstance(group.lift_motor, MotorHandle)
        assert group.lift_motor.driver is drivers["lift_motor"]

    def test_unknown_attribute_lists_available_motors(self) -> None:
        group, _drivers, _mgr = self._group()
        with pytest.raises(AttributeError) as excinfo:
            _ = group.no_such_motor
        message = str(excinfo.value)
        assert "no_such_motor" in message
        assert "lift_motor" in message
        assert "arm_joint" in message

    def test_getitem_and_contains(self) -> None:
        group, _drivers, _mgr = self._group()
        assert group["arm_joint"].name == "arm_joint"
        assert "arm_joint" in group
        assert "missing" not in group
        with pytest.raises(KeyError):
            _ = group["missing"]

    def test_iteration_and_names(self) -> None:
        group, _drivers, _mgr = self._group()
        assert list(group) == ["lift_motor", "arm_joint"]
        assert group.names == ("lift_motor", "arm_joint")
        assert len(group) == 2
        assert [h.name for h in group.handles] == ["lift_motor", "arm_joint"]

    async def test_send_through_group(self) -> None:
        group, drivers, mgr = self._group()
        await group.lift_motor.set_target(ControlMode.CURRENT, 300.0)

        assert drivers["lift_motor"].encoded == [(ControlMode.CURRENT, 300.0)]
        assert mgr.send.await_args.args[0] == "lift_motor"

    async def test_estop_and_sink_propagate_from_builder(self) -> None:
        mgr = _make_can_manager()
        drivers = {"lift_motor": _FakeDriver("lift_motor", 1)}
        group = build_motor_group(mgr, drivers, is_estop_active=lambda: True)

        with pytest.raises(EStopActiveError):
            await group.lift_motor.set_target(ControlMode.POSITION, 1.0)


class _UnboundSequence(Sequence):
    def __init__(self) -> None:
        super().__init__("unbound")
        self.executed: list[str] = []

    @step("動く")
    async def move(self) -> None:
        self.executed.append("move")


class _BoundSequence(Sequence):
    def __init__(self) -> None:
        super().__init__("bound")
        self.reached: bool | None = None

    @step("持ち上げ")
    async def lift(self) -> None:
        await self.motors.lift_motor.set_target(ControlMode.POSITION, 100.0)
        self.reached = await self.motors.lift_motor.wait_reached(timeout=0.05)


class TestSequenceMotorBinding:
    async def test_unbound_sequence_runs_as_before(self) -> None:
        seq = _UnboundSequence()
        await seq.run()
        assert seq.executed == ["move"]

    def test_unbound_sequence_constructor_signature_unchanged(self) -> None:
        seq = _UnboundSequence()
        assert seq.name == "unbound"
        assert seq.has_motors is False

    def test_accessing_motors_unbound_raises(self) -> None:
        seq = _UnboundSequence()
        with pytest.raises(RuntimeError) as excinfo:
            _ = seq.motors
        assert "unbound" in str(excinfo.value)

    async def test_bound_sequence_can_drive_motors(self) -> None:
        mgr = _make_can_manager()
        driver = _FakeDriver("lift_motor", 1)
        group = build_motor_group(mgr, {"lift_motor": driver})

        seq = _BoundSequence()
        seq.bind_motors(group)
        assert seq.has_motors is True

        driver.set_observed(position=100.0)
        await seq.run()

        assert driver.encoded == [(ControlMode.POSITION, 100.0)]
        assert seq.reached is True


_PAIR_MOTORS = (
    MotorSpec(name="pair_r", scale=10.0, offset=0.0),
    MotorSpec(name="pair_l", scale=-10.0, offset=0.0),
)


def _make_axis(
    name: str,
    motors: tuple[MotorSpec, ...],
    *,
    manager: MagicMock,
    sync_tolerance: float | None = None,
    command_mode: ControlMode = ControlMode.POSITION,
) -> tuple[AxisHandle, dict[str, _FakeDriver], list[MotorHandle]]:
    """AxisSpec・ドライバ・MotorHandle 群をまとめて組み立てる。

    MotorHandle まで返すのは、送信が失敗した後にどのモータへ目標が残っているかを
    ``has_target`` で見るテストがあるため (``AxisHandle`` はハンドルを公開しない)。
    """
    drivers = {spec.name: _FakeDriver(spec.name, i + 1) for i, spec in enumerate(motors)}
    axis_spec = AxisSpec(
        name=name,
        unit="mm",
        command_unit="deg",
        timeout_s=1.0,
        tolerance=None,
        motors=motors,
        sync_tolerance=sync_tolerance,
        command_mode=command_mode,
    )
    handles = [
        MotorHandle(motor, drivers[motor], manager, poll_interval=0.001) for motor in drivers
    ]
    return AxisHandle(axis_spec, handles), drivers, handles


class TestAxisHandle:
    def _pair(self, *, sync_tolerance: float | None) -> tuple[AxisHandle, dict[str, _FakeDriver]]:
        axis, drivers, _ = _make_axis(
            "pair",
            _PAIR_MOTORS,
            manager=_make_can_manager(),
            sync_tolerance=sync_tolerance,
        )
        return axis, drivers

    def test_name_is_axis_name(self) -> None:
        handle, _ = self._pair(sync_tolerance=1.0)

        assert handle.name == "pair"

    async def test_set_target_value_sends_per_motor_commands(self) -> None:
        handle, drivers = self._pair(sync_tolerance=1.0)

        await handle.set_target_value({"pair_r": 30.0, "pair_l": -30.0})

        assert drivers["pair_r"].encoded == [(ControlMode.POSITION, 30.0)]
        assert drivers["pair_l"].encoded == [(ControlMode.POSITION, -30.0)]

    async def test_ペアの片側が失敗したら成功した側の目標だけを捨てる(self) -> None:
        """ペア軸に片側だけ効く操作を、エラー経路でも作らない。

        素の `gather` は最初の例外で抜けるが**残りのタスクはキャンセルされずに
        完走する**ので、片方の送信だけが失敗すると成功した側にだけ新しい目標が残る。
        問い合わせ駆動のモータ (EDULITE 05 / DM3520) では 20Hz の再送がその 1 台だけを
        新目標へ押し続け、もう片方には `idle_target_value()` を書き続ける ——
        左右直結の軸が片側だけ動き、最後は偏差超過で全体緊急停止になる。

        一方**失敗した側の旧目標は残す**。1 通も飛んでいない以上、旧目標こそが
        基板が現に実行している状態と一致している。
        """
        mgr = _make_can_manager()
        axis, _, handles = _make_axis("pair", _PAIR_MOTORS, manager=mgr, sync_tolerance=1.0)
        targets = {handle.name: handle for handle in handles}

        await axis.set_target_value({"pair_r": 10.0, "pair_l": -10.0})

        async def _send(name: str, _msg: can.Message) -> None:
            if name == "pair_r":
                raise can.CanError("送信失敗 (テスト)")

        mgr.send = AsyncMock(side_effect=_send)
        with pytest.raises(can.CanError):
            await axis.set_target_value({"pair_r": 30.0, "pair_l": -30.0})

        # 送信が通った側は捨てる。残ると 20Hz の再送がこの 1 台だけを新目標へ押し続ける
        assert targets["pair_l"].has_target is False
        # 送信が失敗した側は旧目標のまま (捨てると再送が止まり、基板の出力ごと落ちる)
        assert targets["pair_r"].target == 10.0

    async def test_単一モータ軸は送信が失敗しても旧目標が残る(self) -> None:
        """捨てる範囲を「指令した全員」へ広げると、モータ 1 台の軸まで巻き添えになる。

        電磁弁・ポンプ・コンベア・サーボはいずれも単一モータの generic 軸で、
        `GenericTargetRefresher` は「目標が無い = 送らない」なので、目標を捨てた
        瞬間に 20Hz の再送が止まる —— 500ms 後にファームの `command_timeout_ms` が
        満了し、電磁弁は消磁して**吸着中のワークが落ちる**。送信 1 通が失敗しただけで
        起きてはならない。
        """
        mgr = _make_can_manager()
        axis, _, handles = _make_axis(
            "valve_3",
            (MotorSpec(name="valve_3_sol", scale=1.0, offset=0.0),),
            manager=mgr,
            command_mode=ControlMode.ON_OFF,
        )
        # 弁を開いてワークを吸着している状態
        await axis.set_target_value({"valve_3_sol": 1.0})

        mgr.send = AsyncMock(side_effect=can.CanError("送信失敗 (テスト)"))
        with pytest.raises(can.CanError):
            await axis.set_target_value({"valve_3_sol": 1.0})

        assert handles[0].target == 1.0
        # 再送が止まっていないこと (止まれば 500ms 後に消磁する)
        mgr.send = AsyncMock()
        assert await handles[0].resend_target() is True

    def test_sync_violation_is_none_without_sync_tolerance(self) -> None:
        handle, drivers = self._pair(sync_tolerance=None)
        drivers["pair_r"].set_observed(position=30.0)
        drivers["pair_l"].set_observed(position=0.0)

        assert handle.sync_violation() is None

    def test_sync_violation_is_none_when_reverse_pair_is_aligned(self) -> None:
        """逆回転は scale の符号で吸収されるので、揃っていれば偏差 0 で超過しない。"""
        handle, drivers = self._pair(sync_tolerance=1.0)
        drivers["pair_r"].set_observed(position=30.0)
        drivers["pair_l"].set_observed(position=-30.0)

        assert handle.sync_violation() is None

    def test_sync_violation_reports_human_unit_deviation(self) -> None:
        handle, drivers = self._pair(sync_tolerance=1.0)
        drivers["pair_r"].set_observed(position=30.0)
        drivers["pair_l"].set_observed(position=-10.0)

        # 3.0mm と 1.0mm の差
        assert handle.sync_violation() == pytest.approx(2.0)

    def test_sync_violation_is_none_within_tolerance(self) -> None:
        """超過しているかの判定は SyncGroup と同じ境界で行う。"""
        handle, drivers = self._pair(sync_tolerance=1.0)
        drivers["pair_r"].set_observed(position=30.0)
        drivers["pair_l"].set_observed(position=-25.0)

        assert handle.sync_violation() is None

    def test_observed_values_はモータ単独の軸位置を返す(self) -> None:
        """平均 (`observed_value`) と対。**ずれそのものを見る零点確定の整列段用。**

        平均は左右がずれていればどちらか一方が必ず誤りなので、片側だけを進める
        整列段では使えない (進めた側の移動が半分に薄まる)。
        """
        handle, drivers = self._pair(sync_tolerance=1.0)
        drivers["pair_r"].set_observed(position=30.0)
        drivers["pair_l"].set_observed(position=-10.0)

        # 逆回転は scale の符号で吸収する (3.0mm と 1.0mm)
        assert handle.observed_values() == pytest.approx({"pair_r": 3.0, "pair_l": 1.0})
        # 平均はそのずれを畳んでしまう
        assert handle.observed_value() == pytest.approx(2.0)

    def test_observed_values_は位置を持たない軸を拒否する(self) -> None:
        """DC 基板も電磁弁基板も position が常に 0 で、逆換算すると「測ったように
        見える 0」を返してしまう (`observed_value` と同じ理由)。
        """
        axis, _drivers, _handles = _make_axis(
            "valve_3",
            (MotorSpec(name="valve_3_sol", scale=1.0, offset=0.0),),
            manager=_make_can_manager(),
            command_mode=ControlMode.ON_OFF,
        )

        with pytest.raises(PositionLookupError):
            axis.observed_values()
