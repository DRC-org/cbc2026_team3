from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Mapping
from unittest.mock import AsyncMock, MagicMock

import can
import pytest

from lib.drivers.base import ControlMode
from lib.match_state import Court
from lib.sequence.engine import AxisSyncError, Sequence, SequenceTimeoutError, step
from lib.sequence.motors import AxisHandle, MotorGroup, MotorHandle, WaitInterruptedError
from lib.sequence.positions import load_position_table
from tests.fake_drivers import StubFeedbackDriver


class _EchoDriver(StubFeedbackDriver):
    def __init__(self, name: str, *, reaches: bool = True, bias: float = 0.0) -> None:
        super().__init__(name, 1)
        self.commands: list[tuple[ControlMode, float]] = []
        self._reaches = reaches
        self._bias = bias

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        self.commands.append((mode, value))
        if self._reaches:
            self.set_observed(position=value + self._bias)
        return super().encode_target(mode, value)


def _make_group(*names: str, reaches: bool = True) -> tuple[MotorGroup, dict[str, _EchoDriver]]:
    mgr = MagicMock()
    mgr.send = AsyncMock()
    group = MotorGroup()
    drivers: dict[str, _EchoDriver] = {}
    for name in names:
        driver = _EchoDriver(name, reaches=reaches)
        drivers[name] = driver
        group.add(MotorHandle(name, driver, mgr, poll_interval=0.001))
    return group, drivers


def _clear_on_first_poll(group: MotorGroup) -> None:
    for handle in group.handles:
        driver = handle.driver
        original = driver.is_target_reached
        fired = False

        def _hooked(*args: object, _orig=original, **kwargs: object) -> bool:
            nonlocal fired
            if not fired:
                fired = True
                for other in group.handles:
                    other.clear_target()
            return bool(_orig(*args, **kwargs))

        driver.is_target_reached = _hooked  # type: ignore[method-assign]


_POSITION_CONFIG = {
    "axes": {
        "lift_motor": {"unit": "mm", "command_unit": "deg", "scale": 100.0, "timeout_s": 0.05},
        "arm_joint": {"unit": "deg", "command_unit": "rad", "scale": math.pi / 180.0},
        "gripper": {"unit": "deg", "command_unit": "deg"},
    },
    "positions": {
        "lift_motor": {"home": 0.0, "work": 3.0, "place": {"red": 1.0, "blue": 2.0}},
        "arm_joint": {"home": 0.0, "extended": 30.0},
        "gripper": {"open": 20.0, "closed": 0.0},
    },
}


class _MoveSequence(Sequence):
    def __init__(self) -> None:
        super().__init__("move_seq")
        self.executed: list[str] = []

    @step("移動")
    async def move(self) -> None:
        self.executed.append("move")
        await self.move_to({"lift_motor": "work", "arm_joint": "extended"})

    @step("次")
    async def after(self) -> None:
        self.executed.append("after")


class TestBindPositions:
    def test_accessing_unbound_positions_raises(self) -> None:
        seq = _MoveSequence()

        with pytest.raises(RuntimeError, match="bind_positions"):
            _ = seq.positions

    def test_bind_positions(self) -> None:
        seq = _MoveSequence()
        table = load_position_table(_POSITION_CONFIG)

        seq.bind_positions(table)

        assert seq.positions is table


class TestMoveTo:
    async def test_sends_converted_targets(self) -> None:
        seq = _MoveSequence()
        group, drivers = _make_group("lift_motor", "arm_joint")
        seq.bind_motors(group)
        seq.bind_positions(load_position_table(_POSITION_CONFIG))

        await seq.move_to({"lift_motor": "work", "arm_joint": "extended"})

        assert drivers["lift_motor"].commands == [(ControlMode.POSITION, 300.0)]
        assert drivers["arm_joint"].commands[0][0] is ControlMode.POSITION
        assert drivers["arm_joint"].commands[0][1] == pytest.approx(math.radians(30.0))

    async def test_uses_current_court(self) -> None:
        seq = _MoveSequence()
        group, drivers = _make_group("lift_motor")
        seq.bind_motors(group)
        seq.bind_positions(load_position_table(_POSITION_CONFIG))
        seq.set_court(Court.BLUE)

        await seq.move_to({"lift_motor": "place"})

        assert drivers["lift_motor"].commands == [(ControlMode.POSITION, 200.0)]

    @pytest.mark.parametrize(("court", "expected"), [(Court.BLUE, -20.0), (Court.RED, 20.0)])
    async def test_court_scale_flips_command_sign(self, court: Court, expected: float) -> None:
        seq = _MoveSequence()
        group, drivers = _make_group("lift")
        seq.bind_motors(group)
        seq.bind_positions(
            load_position_table(
                {
                    "axes": {"lift": {"scale": {"blue": 2.0, "red": -2.0}, "timeout_s": 0.05}},
                    "positions": {"lift": {"up": -10.0}},
                }
            )
        )
        seq.set_court(court)

        await seq.move_to({"lift": "up"})

        assert drivers["lift"].commands == [(ControlMode.POSITION, expected)]

    async def test_timeout_raises(self) -> None:
        seq = _MoveSequence()
        group, _ = _make_group("lift_motor", reaches=False)
        seq.bind_motors(group)
        seq.bind_positions(load_position_table(_POSITION_CONFIG))

        with pytest.raises(SequenceTimeoutError, match="lift_motor"):
            await seq.move_to({"lift_motor": "work"})

    async def test_explicit_timeout_overrides_axis_default(self) -> None:
        seq = _MoveSequence()
        group, _ = _make_group("gripper", reaches=False)
        seq.bind_motors(group)
        seq.bind_positions(load_position_table(_POSITION_CONFIG))

        with pytest.raises(SequenceTimeoutError):
            await seq.move_to({"gripper": "open"}, timeout=0.02)

    async def test_unknown_motor_reports_available_names(self) -> None:
        seq = _MoveSequence()
        group, _ = _make_group("lift_motor")
        seq.bind_motors(group)
        seq.bind_positions(
            load_position_table(
                {
                    "axes": {"ghost": {}},
                    "positions": {"ghost": {"home": 0.0}},
                }
            )
        )

        with pytest.raises(AttributeError, match="lift_motor"):
            await seq.move_to({"ghost": "home"})

    async def test_指令の途中で例外が出ても到達待ちを取り残さない(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seq = _MoveSequence()
        mgr = MagicMock()

        async def _send(name: str, _msg: can.Message) -> None:
            if name == "arm_joint":
                raise can.CanError("送信失敗 (テスト)")

        mgr.send = AsyncMock(side_effect=_send)
        group = MotorGroup()
        for name in ("lift_motor", "arm_joint"):
            group.add(MotorHandle(name, _EchoDriver(name), mgr, poll_interval=0.001))
        seq.bind_motors(group)
        seq.bind_positions(load_position_table(_POSITION_CONFIG))

        created: list[str] = []
        original = AxisHandle.wait_reached

        def _record(self: AxisHandle, **kwargs: object):
            created.append(self.name)
            return original(self, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(AxisHandle, "wait_reached", _record)

        with pytest.raises(can.CanError):
            await seq.move_to({"lift_motor": "work", "arm_joint": "extended"})

        assert created == [], f"到達待ちのコルーチンが取り残された: {created}"


class TestRunStopsOnTimeout:
    async def test_run_stops_and_logs(self, caplog: logging.LogCaptureFixture) -> None:
        seq = _MoveSequence()
        group, _ = _make_group("lift_motor", "arm_joint", reaches=False)
        seq.bind_motors(group)
        seq.bind_positions(load_position_table(_POSITION_CONFIG))

        with caplog.at_level(logging.ERROR):
            await seq.run()

        assert seq.executed == ["move"]
        assert seq.progress["running"] is False
        assert "移動" in caplog.text


class TestMoveToInterruptedByEStop:
    async def test_move_to_raises_when_target_cleared_mid_wait(self) -> None:
        seq = _MoveSequence()
        group, _ = _make_group("lift_motor", "arm_joint", reaches=False)
        seq.bind_motors(group)
        seq.bind_positions(load_position_table(_POSITION_CONFIG))

        _clear_on_first_poll(group)

        with pytest.raises(WaitInterruptedError):
            await seq.move_to({"lift_motor": "work", "arm_joint": "extended"})

    async def test_target_cleared_between_command_and_wait_is_an_interruption(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seq = _MoveSequence()
        group, _ = _make_group("lift_motor", "arm_joint", reaches=False)
        seq.bind_motors(group)
        seq.bind_positions(load_position_table(_POSITION_CONFIG))

        original = AxisHandle.set_target_value

        async def _clear_right_after_commanding(self: AxisHandle, values: object) -> None:
            await original(self, values)  # type: ignore[arg-type]
            for handle in group.handles:
                handle.clear_target()

        monkeypatch.setattr(AxisHandle, "set_target_value", _clear_right_after_commanding)

        with pytest.raises(WaitInterruptedError):
            await seq.move_to({"lift_motor": "work", "arm_joint": "extended"})

    async def test_run_records_interruption_not_timeout(self) -> None:
        seq = _MoveSequence()
        group, _ = _make_group("lift_motor", "arm_joint", reaches=False)
        seq.bind_motors(group)
        seq.bind_positions(load_position_table(_POSITION_CONFIG))

        _clear_on_first_poll(group)

        await seq.run()

        assert seq.executed == ["move"]
        assert seq.progress["running"] is False
        assert seq.last_error is not None
        assert "到達しませんでした" not in seq.last_error.message
        assert "中断" in seq.last_error.message


class TestBackwardCompatibility:
    async def test_sequence_without_positions_still_runs(self) -> None:

        class _PlainSequence(Sequence):
            def __init__(self) -> None:
                super().__init__("plain")
                self.executed: list[str] = []

            @step("何もしない")
            async def noop(self) -> None:
                self.executed.append("noop")

        seq = _PlainSequence()
        await seq.run()

        assert seq.executed == ["noop"]


def _make_axis_group(
    options: Mapping[str, Mapping[str, float | bool]],
    *,
    send: AsyncMock | None = None,
) -> tuple[MotorGroup, dict[str, _EchoDriver]]:
    mgr = MagicMock()
    mgr.send = send if send is not None else AsyncMock()
    group = MotorGroup()
    drivers: dict[str, _EchoDriver] = {}
    for name, option in options.items():
        driver = _EchoDriver(
            name,
            reaches=bool(option.get("reaches", True)),
            bias=float(option.get("bias", 0.0)),
        )
        drivers[name] = driver
        group.add(MotorHandle(name, driver, mgr, poll_interval=0.001))
    return group, drivers


_PAIRED_CONFIG = {
    "axes": {
        "y_axis": {
            "unit": "mm",
            "command_unit": "deg",
            "timeout_s": 0.05,
            "tolerance": 5.0,
            "sync_tolerance": 2.0,
            "motors": {
                "y_axis_r": {"scale": 10.0},
                "y_axis_l": {"scale": -10.0},
            },
        },
        "wide_pair": {
            "unit": "mm",
            "command_unit": "deg",
            "timeout_s": 0.05,
            "tolerance": 0.5,
            "motors": {
                "wide_a": {"scale": 10.0},
                "wide_b": {"scale": -100.0},
            },
        },
        "conveyor": {
            "unit": "duty",
            "command_unit": "duty",
            "command_mode": "duty",
            "settle_s": 0.05,
            "timeout_s": 0.05,
        },
        "spinner": {
            "unit": "rpm",
            "command_unit": "rpm",
            "command_mode": "velocity",
            "settle_s": 0.0,
            "timeout_s": 0.05,
        },
    },
    "positions": {
        "y_axis": {"home": 0.0, "work": 3.0},
        "wide_pair": {"work": 1.0},
        "conveyor": {"run": 0.5, "stop": 0.0},
        "spinner": {"run": 100.0},
    },
}


def _paired_sequence(group: MotorGroup) -> _MoveSequence:
    seq = _MoveSequence()
    seq.bind_motors(group)
    seq.bind_positions(load_position_table(_PAIRED_CONFIG))
    return seq


class TestPairedAxis:
    async def test_sends_per_motor_commands(self) -> None:
        group, drivers = _make_axis_group({"y_axis_r": {}, "y_axis_l": {}})
        seq = _paired_sequence(group)

        await seq.move_to({"y_axis": "work"})

        assert drivers["y_axis_r"].commands == [(ControlMode.POSITION, 30.0)]
        assert drivers["y_axis_l"].commands == [(ControlMode.POSITION, -30.0)]

    async def test_commands_are_sent_concurrently(self) -> None:
        events: list[tuple[str, str]] = []

        async def _send(name: str, msg: can.Message) -> None:
            events.append(("start", name))
            if name == "y_axis_r":
                await asyncio.sleep(0.02)
            events.append(("done", name))

        group, _ = _make_axis_group(
            {"y_axis_r": {}, "y_axis_l": {}}, send=AsyncMock(side_effect=_send)
        )
        seq = _paired_sequence(group)

        await seq.move_to({"y_axis": "work"})

        assert events.index(("start", "y_axis_l")) < events.index(("done", "y_axis_r"))

    async def test_timeout_when_one_motor_does_not_reach(self) -> None:
        group, _ = _make_axis_group({"y_axis_r": {}, "y_axis_l": {"bias": 1000.0}})
        seq = _paired_sequence(group)

        with pytest.raises(SequenceTimeoutError, match="y_axis"):
            await seq.move_to({"y_axis": "work"})

    async def test_sync_error_raises_after_reach(self) -> None:
        group, _ = _make_axis_group({"y_axis_r": {}, "y_axis_l": {"bias": 30.0}})
        seq = _paired_sequence(group)

        with pytest.raises(AxisSyncError, match="y_axis"):
            await seq.move_to({"y_axis": "work"})

    async def test_sync_error_within_tolerance_passes(self) -> None:
        group, _ = _make_axis_group({"y_axis_r": {}, "y_axis_l": {"bias": 10.0}})
        seq = _paired_sequence(group)

        await seq.move_to({"y_axis": "work"})

    async def test_tolerance_is_converted_per_motor(self) -> None:
        group, _ = _make_axis_group({"wide_a": {"bias": 4.0}, "wide_b": {"bias": 40.0}})
        seq = _paired_sequence(group)

        await seq.move_to({"wide_pair": "work"})

    async def test_tolerance_still_rejects_out_of_range_motor(self) -> None:
        group, _ = _make_axis_group({"wide_a": {"bias": 6.0}, "wide_b": {"bias": 40.0}})
        seq = _paired_sequence(group)

        with pytest.raises(SequenceTimeoutError, match="wide_pair"):
            await seq.move_to({"wide_pair": "work"})


class TestNonPositionAxis:
    async def test_duty_axis_waits_settle_only(self) -> None:
        group, drivers = _make_axis_group({"conveyor": {"reaches": False}})
        seq = _paired_sequence(group)

        started = time.monotonic()
        await seq.move_to({"conveyor": "run"})
        elapsed = time.monotonic() - started

        assert drivers["conveyor"].commands == [(ControlMode.DUTY, 0.5)]
        assert elapsed >= 0.05

    async def test_velocity_axis_does_not_time_out(self) -> None:
        group, drivers = _make_axis_group({"spinner": {"reaches": False}})
        seq = _paired_sequence(group)

        await seq.move_to({"spinner": "run"})

        assert drivers["spinner"].commands == [(ControlMode.VELOCITY, 100.0)]
