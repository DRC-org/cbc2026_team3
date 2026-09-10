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
from lib.motion_guard import GuardViolation
from lib.sequence.engine import (
    NO_LIMIT_INTERVENTION,
    AxisSyncError,
    LimitIntervention,
    LimitInterventionError,
    Sequence,
    SequenceTimeoutError,
    step,
)
from lib.sequence.motors import (
    AxisHandle,
    MotorGroup,
    MotorHandle,
    WaitInterruptedError,
    build_axis_state_reader,
)
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

        async def _clear_right_after_commanding(
            self: AxisHandle, values: object, **kwargs: object
        ) -> None:
            await original(self, values, **kwargs)  # type: ignore[arg-type]
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


class _Interventions:
    """`LimitMonitor` の代わり。**回数と理由だけを注入で運ぶ** (層を 1 枚で見る)。"""

    def __init__(self) -> None:
        self.records: dict[str, LimitIntervention] = {}

    def __call__(self, axis: str) -> LimitIntervention:
        return self.records.get(axis, NO_LIMIT_INTERVENTION)

    def stop(self, axis: str, reason: str) -> None:
        self.records[axis] = LimitIntervention(count=self(axis).count + 1, reason=reason)


class _BentDriver(_EchoDriver):
    """指令を受けた瞬間に可動端保護が割り込んだ機体。

    保護は目標を実測へ書き直すので、**到達判定は必ず成立する** (`reaches=True` の
    まま止まる)。回数を見ないと、軸が途中に居るのにシーケンスだけが先へ進む。
    """

    def __init__(self, name: str, interventions: _Interventions, axis: str) -> None:
        super().__init__(name)
        self._interventions = interventions
        self._axis = axis

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        self._interventions.stop(self._axis, "可動端センサ 'rear_switch' が押されているため")
        return super().encode_target(mode, value)


class TestLimitIntervention:
    """**曲げられた移動を成功と読まない。**"""

    def _sequence(self, driver: _EchoDriver, interventions: _Interventions) -> Sequence:
        mgr = MagicMock()
        mgr.send = AsyncMock()
        group = MotorGroup()
        group.add(MotorHandle(driver.name, driver, mgr, poll_interval=0.001))
        seq = _MoveSequence()
        seq.bind_motors(group)
        seq.bind_positions(load_position_table(_POSITION_CONFIG))
        seq.bind_limit_interventions(interventions)
        return seq

    async def test_止められた移動は失敗する(self) -> None:
        interventions = _Interventions()
        seq = self._sequence(_BentDriver("lift_motor", interventions, "lift_motor"), interventions)

        with pytest.raises(LimitInterventionError, match="rear_switch"):
            await seq.move_to({"lift_motor": "work"})

    async def test_保護の介入はタイムアウトと別の型で出る(self) -> None:
        """型で区別できないと、操縦者が `timeout_s` を伸ばす側を疑い続ける。"""
        interventions = _Interventions()
        seq = self._sequence(_BentDriver("lift_motor", interventions, "lift_motor"), interventions)

        with pytest.raises(SequenceTimeoutError) as caught:
            await seq.move_to({"lift_motor": "work"})

        assert type(caught.value) is LimitInterventionError

    async def test_到達しなかっただけならタイムアウトのまま(self) -> None:
        """保護が絡まない失敗まで新しい型にすると、区別そのものが消える。"""
        interventions = _Interventions()
        seq = self._sequence(_EchoDriver("lift_motor", reaches=False), interventions)

        with pytest.raises(SequenceTimeoutError) as caught:
            await seq.move_to({"lift_motor": "work"})

        assert not isinstance(caught.value, LimitInterventionError)

    async def test_止められなかった移動は成功する(self) -> None:
        interventions = _Interventions()
        seq = self._sequence(_EchoDriver("lift_motor"), interventions)

        await seq.move_to({"lift_motor": "work"})

    async def test_前の移動で数えたぶんでは失敗しない(self) -> None:
        """回数は単調増加なので、移動ごとに前後で比べないと二度目以降が必ず落ちる。"""
        interventions = _Interventions()
        seq = self._sequence(_EchoDriver("lift_motor"), interventions)
        interventions.stop("lift_motor", "前の移動で止まった")

        await seq.move_to({"lift_motor": "work"})

    async def test_配線しなければ従来どおり(self) -> None:
        mgr = MagicMock()
        mgr.send = AsyncMock()
        group = MotorGroup()
        group.add(MotorHandle("lift_motor", _EchoDriver("lift_motor"), mgr, poll_interval=0.001))
        seq = _MoveSequence()
        seq.bind_motors(group)
        seq.bind_positions(load_position_table(_POSITION_CONFIG))

        await seq.move_to({"lift_motor": "work"})


_TWO_AXIS_CONFIG = {
    "axes": {
        "lift_motor": {"unit": "mm", "command_unit": "deg", "scale": 100.0, "timeout_s": 0.05},
        "arm_joint": {"unit": "deg", "command_unit": "deg", "timeout_s": 0.05},
    },
    "positions": {
        "lift_motor": {"work": 3.0},
        "arm_joint": {"extended": 30.0},
    },
}


class TestFailureReasonsAreCombined:
    """**保護の介入と未到達が同時に起きたら、両方を 1 つの例外で出す。**

    片方で先に `raise` すると、もう片方の軸で何が起きたかが失敗表示から消える。
    切り分けは「止められた軸」と「届かなかった軸」の対応で進むので辿れなくなる。
    """

    def _sequence(self, drivers: list[_EchoDriver], interventions: _Interventions) -> Sequence:
        mgr = MagicMock()
        mgr.send = AsyncMock()
        group = MotorGroup()
        for driver in drivers:
            group.add(MotorHandle(driver.name, driver, mgr, poll_interval=0.001))
        seq = _MoveSequence()
        seq.bind_motors(group)
        seq.bind_positions(load_position_table(_TWO_AXIS_CONFIG))
        seq.bind_limit_interventions(interventions)
        return seq

    def _both(self, interventions: _Interventions) -> Sequence:
        return self._sequence(
            [
                _BentDriver("lift_motor", interventions, "lift_motor"),
                _EchoDriver("arm_joint", reaches=False),
            ],
            interventions,
        )

    async def test_両方起きたら両方が出る(self) -> None:
        interventions = _Interventions()
        seq = self._both(interventions)

        with pytest.raises(SequenceTimeoutError) as caught:
            await seq.move_to({"lift_motor": "work", "arm_joint": "extended"})

        message = str(caught.value)
        assert "rear_switch" in message
        assert "arm_joint->extended" in message

    async def test_保護が絡めば型は保護のほう(self) -> None:
        """未到達も同時に起きたからといって、時間切れへ丸めない。"""
        interventions = _Interventions()
        seq = self._both(interventions)

        with pytest.raises(SequenceTimeoutError) as caught:
            await seq.move_to({"lift_motor": "work", "arm_joint": "extended"})

        assert type(caught.value) is LimitInterventionError

    async def test_保護だけなら到達の話を混ぜない(self) -> None:
        """単独の失敗が読みにくくなっては本末転倒。"""
        interventions = _Interventions()
        seq = self._sequence(
            [_BentDriver("lift_motor", interventions, "lift_motor")], interventions
        )

        with pytest.raises(LimitInterventionError) as caught:
            await seq.move_to({"lift_motor": "work"})

        assert "到達しませんでした" not in str(caught.value)

    async def test_未到達だけなら保護の話を混ぜない(self) -> None:
        interventions = _Interventions()
        seq = self._sequence([_EchoDriver("lift_motor", reaches=False)], interventions)

        with pytest.raises(SequenceTimeoutError) as caught:
            await seq.move_to({"lift_motor": "work"})

        assert "可動端保護" not in str(caught.value)


_GUARDED_CONFIG = {
    "axes": {
        "lift": {"unit": "mm", "command_unit": "deg", "scale": 1.0, "tolerance": 1.0},
        "slide": {
            "unit": "mm",
            "command_unit": "deg",
            "scale": 1.0,
            "tolerance": 1.0,
            "guard": {"requires": [{"axis": "lift", "at": "top"}]},
        },
        "pitch": {
            "unit": "deg",
            "command_unit": "deg",
            "tolerance": 1.0,
            "guard": {"not_with": ["offset"]},
        },
        "offset": {"unit": "deg", "command_unit": "deg", "tolerance": 1.0},
    },
    "positions": {
        "lift": {"top": -10.0, "bottom": 0.0},
        "slide": {"back": -5.0, "front": 0.0},
        "pitch": {"open": 0.0, "close": 90.0},
        "offset": {"open": 0.0, "close": 90.0},
    },
}


def _guarded_sequence() -> tuple[Sequence, MotorGroup, dict[str, _EchoDriver]]:
    table = load_position_table(_GUARDED_CONFIG, source="<test>")
    group, drivers = _make_group("lift", "slide", "pitch", "offset")
    group.bind_axis_state(
        build_axis_state_reader(table, group, court=lambda: Court.RED, is_stale=lambda _name: False)
    )
    seq = Sequence("guarded")
    seq.bind_motors(group)
    seq.bind_positions(table)
    return seq, group, drivers


class TestMoveToChecksEveryAxisBeforeSending:
    """**全軸を検査してから 1 通目を出す。**

    軸ごとに「検査 → 送信」を回すと、2 軸目が拒否された時点で 1 軸目は既に
    走っている。ピッチとオフセットではそれが一番危ない半端な姿勢になる。
    """

    async def test_1_軸が拒否されたらもう_1_軸にも_1_通も出ない(self) -> None:
        seq, _, drivers = _guarded_sequence()

        # 拒否される軸をあとに置く。先に置くと「たまたま順番で守られた」だけになる
        with pytest.raises(GuardViolation, match="lift"):
            await seq.move_to({"offset": "close", "slide": "back"})

        assert drivers["offset"].commands == []
        assert drivers["slide"].commands == []

    async def test_not_with_の対は同じ指令で拒否される(self) -> None:
        seq, _, drivers = _guarded_sequence()

        with pytest.raises(GuardViolation, match="同じ指令では動かせません"):
            await seq.move_to({"pitch": "close", "offset": "close"})

        assert drivers["pitch"].commands == []
        assert drivers["offset"].commands == []

    async def test_別々の指令なら順に通る(self) -> None:
        seq, _, drivers = _guarded_sequence()

        await seq.move_to({"pitch": "close"})
        await seq.move_to({"offset": "close"})

        assert drivers["pitch"].commands == [(ControlMode.POSITION, 90.0)]
        assert drivers["offset"].commands == [(ControlMode.POSITION, 90.0)]

    async def test_同じ指令に入る軸は書き終わった後の目標で評価する(self) -> None:
        """`lift` は既に top に居るが、目標には古い bottom が残っている状態。"""
        seq, group, drivers = _guarded_sequence()
        await seq.move_to({"lift": "bottom"})
        drivers["lift"].set_observed(position=-10.0)
        drivers["lift"].commands.clear()

        await seq.move_to({"lift": "top", "slide": "back"})

        assert drivers["slide"].commands == [(ControlMode.POSITION, -5.0)]
        assert group["lift"].target == -10.0
