from __future__ import annotations

import logging
import math
from unittest.mock import AsyncMock, MagicMock

import can
import pytest

from lib.drivers.base import ControlMode, MotorDriver, MotorState
from lib.match_state import Court
from lib.sequence.engine import Sequence, SequenceTimeoutError, step
from lib.sequence.motors import MotorGroup, MotorHandle
from lib.sequence.positions import load_position_table


class _EchoDriver(MotorDriver):
    """指令値をそのままフィードバックに反映する (常に即到達する) テスト用ドライバ。"""

    def __init__(self, name: str, *, reaches: bool = True) -> None:
        super().__init__(name, 1)
        self.commands: list[tuple[ControlMode, float]] = []
        self._reaches = reaches

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        self.commands.append((mode, value))
        if self._reaches:
            self._state = MotorState(position=value)
        return can.Message(arbitration_id=0x100, data=bytes(8), is_extended_id=False)

    def decode_feedback(self, msg: can.Message) -> MotorState:  # pragma: no cover
        return self._state

    def matches_feedback(self, msg: can.Message) -> bool:  # pragma: no cover
        return False


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
    def test_unbound_sequence_reports_no_positions(self) -> None:
        seq = _MoveSequence()

        assert seq.has_positions is False

    def test_accessing_unbound_positions_raises(self) -> None:
        seq = _MoveSequence()

        with pytest.raises(RuntimeError, match="bind_positions"):
            _ = seq.positions

    def test_bind_positions(self) -> None:
        seq = _MoveSequence()
        table = load_position_table(_POSITION_CONFIG)

        seq.bind_positions(table)

        assert seq.has_positions is True
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

    async def test_timeout_raises(self) -> None:
        """到達しないまま軸ごとのタイムアウトを過ぎたら例外で止める。"""
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


class TestRunStopsOnTimeout:
    async def test_run_stops_and_logs(self, caplog: logging.LogCaptureFixture) -> None:
        """タイムアウト例外は run() が握って停止する。後続ステップは実行しない。"""
        seq = _MoveSequence()
        group, _ = _make_group("lift_motor", "arm_joint", reaches=False)
        seq.bind_motors(group)
        seq.bind_positions(load_position_table(_POSITION_CONFIG))

        with caplog.at_level(logging.ERROR):
            await seq.run()

        assert seq.executed == ["move"]
        assert seq.progress["running"] is False
        assert "移動" in caplog.text


class TestBackwardCompatibility:
    async def test_sequence_without_positions_still_runs(self) -> None:
        """位置定数を bind しないシーケンスは従来どおり動く。"""

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
