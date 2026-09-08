from __future__ import annotations

import math
from collections.abc import Callable

import pytest

from lib.sequence.homing import _FOLLOW_ATTEMPTS, _STALL_LIMIT, HomingError, HomingRunner
from lib.sequence.motors import AxisHandle, MotorHandle
from lib.sequence.positions import AxisSpec, load_position_table
from tests.fake_can import mock_can_manager
from tests.fake_drivers import StubFeedbackDriver

_COMMAND_FUSE = 200


def _table(**homing_overrides: object):
    homing: dict = {
        "sensor": "origin_sensor",
        "direction": -1,
        "search_distance": 5.0,
        "step": 1.0,
        "settle_s": 0.0,
    }
    homing.update(homing_overrides)
    return load_position_table(
        {
            "axes": {
                "y_axis": {
                    "unit": "mm",
                    "command_unit": "deg",
                    "tolerance": 0.1,
                    "sync_tolerance": 100.0,
                    "homing": homing,
                    "motors": {"y_axis_r": {"scale": 2.0}, "y_axis_l": {"scale": -2.0}},
                }
            },
            "positions": {"y_axis": {"home": 0.0}},
        },
        source="<test>",
    )


def _rotate_table():
    return load_position_table(
        {
            "axes": {
                "rotate": {
                    "unit": "deg",
                    "command_unit": "rad",
                    "tolerance": 2.0,
                    "sync_tolerance": 5.0,
                    "homing": {
                        "sensor": "rotate_origin_sensor",
                        "direction": -1,
                        "search_distance": 180.0,
                        "step": 2.0,
                        "settle_s": 0.05,
                    },
                    "motors": {
                        "rotate_r": {"scale": math.pi / 180.0},
                        "rotate_l": {"scale": -math.pi / 180.0},
                    },
                }
            },
            "positions": {"rotate": {"home": 0.0}},
        },
        source="<test>",
    )


def _paired_table(**homing_overrides: object):
    homing: dict = {
        "sensors": {"y_axis_r": "sensor_r", "y_axis_l": "sensor_l"},
        "direction": -1,
        "search_distance": 30.0,
        "step": 1.0,
        "settle_s": 0.0,
        "align_distance": 5.0,
    }
    homing.update(homing_overrides)
    return load_position_table(
        {
            "axes": {
                "y_axis": {
                    "unit": "mm",
                    "command_unit": "deg",
                    "tolerance": 0.1,
                    "sync_tolerance": 100.0,
                    "homing": homing,
                    "motors": {"y_axis_r": {"scale": 2.0}, "y_axis_l": {"scale": -2.0}},
                }
            },
            "positions": {"y_axis": {"home": 0.0}},
        },
        source="<test>",
    )


class _SensorModel:
    def __init__(
        self,
        *,
        motor: str | None = None,
        active_after: int | None = None,
        active_at_or_below: float | None = None,
        active_band: tuple[float, float] | None = None,
        chatter: bool = False,
        prelatched: bool = False,
    ) -> None:
        self.motor = motor
        self._active_after = active_after
        self._active_at_or_below = active_at_or_below
        self._active_band = active_band
        self._chatter = chatter
        self._observations = 0
        self._path: tuple[float, float] | None = None
        self._chatter_latch = False
        self._prelatched = prelatched
        self.position: Callable[[], float] = lambda: 0.0

    def _extend_path(self) -> tuple[float, float]:
        current = self.position()
        low, high = self._path if self._path is not None else (current, current)
        self._path = (min(low, current), max(high, current))
        return self._path

    def active(self) -> bool:
        self._observations += 1
        self._extend_path()
        if self._chatter:
            return True
        if self._active_band is not None:
            low, high = self._active_band
            return low <= self.position() <= high
        if self._active_at_or_below is not None:
            return self.position() <= self._active_at_or_below
        if self._active_after is None:
            return False
        return self._observations > self._active_after

    def latched(self) -> bool:
        self._observations += 1
        low, high = self._extend_path()
        current = self.position()
        self._path = (current, current)
        if self._prelatched:
            self._prelatched = False
            return True
        if self._chatter:
            self._chatter_latch = not self._chatter_latch
            return not self._chatter_latch
        if self._active_band is not None:
            band_low, band_high = self._active_band
            return low <= band_high and high >= band_low
        if self._active_at_or_below is not None:
            return low <= self._active_at_or_below
        if self._active_after is None:
            return False
        return self._observations > self._active_after


class _Recorder:
    def __init__(
        self,
        *,
        sensors: dict[str, _SensorModel] | None = None,
        active_after: int | None = None,
        active_at_or_below: float | None = None,
        active_band: tuple[float, float] | None = None,
        chatter: bool = False,
        prelatched: bool = False,
        stale: bool = False,
        stale_sensors: tuple[str, ...] = (),
        motor_stale: bool = False,
        capturable: bool = True,
    ) -> None:
        self.commands: list[dict[str, float]] = []
        self.origins: list[str] = []
        self.captured_at: list[float] = []
        self.captured_each: list[dict[str, float]] = []
        self.drivers: dict[str, StubFeedbackDriver] = {}
        self.spec: AxisSpec | None = None
        self.sleeps = 0
        self._default = _SensorModel(
            active_after=active_after,
            active_at_or_below=active_at_or_below,
            active_band=active_band,
            chatter=chatter,
            prelatched=prelatched,
        )
        self._sensors = sensors
        for model in (self._default, *(sensors or {}).values()):
            model.position = self._position_of(model)
        self._stale = stale
        self._stale_sensors = stale_sensors
        self._motor_stale = motor_stale
        self._capturable = capturable

    def _position_of(self, model: _SensorModel) -> Callable[[], float]:
        return (
            self.axis_position if model.motor is None else lambda: self.motor_position(model.motor)
        )

    def _sensor(self, name: str) -> _SensorModel:
        if self._sensors is None:
            return self._default
        return self._sensors[name]

    def axis_position(self) -> float:
        if not self.drivers:
            return 0.0
        assert self.spec is not None
        return self.spec.to_value(
            {name: driver.feedback_position() for name, driver in self.drivers.items()}
        )

    def motor_position(self, motor: str | None) -> float:
        if not self.drivers or motor is None:
            return 0.0
        assert self.spec is not None
        spec = next(m for m in self.spec.motors if m.name == motor)
        return spec.to_value(self.drivers[motor].feedback_position())

    def sensor_active(self, name: str) -> bool:
        return self._sensor(name).active()

    def sensor_latched(self, name: str) -> bool:
        return self._sensor(name).latched()

    def sensor_is_stale(self, name: str) -> bool:
        return self._stale or name in self._stale_sensors

    def motor_is_stale(self, _name: str) -> bool:
        return self._motor_stale

    def origin_capturable(self, _axis: str) -> bool:
        return self._capturable

    async def capture_origin(self, axis: str) -> None:
        self.origins.append(axis)
        if self.drivers:
            self.captured_at.append(self.axis_position())
            self.captured_each.append({motor: self.motor_position(motor) for motor in self.drivers})

    async def sleep(self, _seconds: float) -> None:
        self.sleeps += 1


def _handle(
    spec: AxisSpec,
    recorder: _Recorder,
    *,
    start_value: float = 0.0,
    follows: bool | Callable[[], bool] = True,
) -> AxisHandle:
    mgr = mock_can_manager()
    drivers = {}
    handles = []
    for motor in spec.motors:
        driver = StubFeedbackDriver(motor.name, 1)
        driver.set_observed(position=motor.to_command(start_value))
        drivers[motor.name] = driver
        handles.append(MotorHandle(motor.name, driver, mgr))

    handle = AxisHandle(spec, handles)
    original = handle.set_target_value

    async def _record(commands):
        recorder.commands.append(dict(commands))
        assert len(recorder.commands) <= _COMMAND_FUSE, (
            f"指令が {_COMMAND_FUSE} 通を超えました (どの歯止めも効いていません)"
        )
        if follows() if callable(follows) else follows:
            for name, value in commands.items():
                drivers[name].set_observed(position=value)
        return await original(commands)

    handle.set_target_value = _record  # type: ignore[method-assign]
    recorder.drivers = drivers
    recorder.spec = spec
    return handle


def _slow_handle(
    spec: AxisSpec,
    recorder: _Recorder,
    *,
    per_tick: float,
    start_value: float = 0.0,
) -> AxisHandle:
    handle = _handle(spec, recorder, follows=False, start_value=start_value)
    drivers = recorder.drivers
    scales = {motor.name: abs(motor.scale) for motor in spec.motors}
    base_sleep = recorder.sleep

    async def _advance(seconds: float) -> None:
        await base_sleep(seconds)
        if not recorder.commands:
            return
        for name, target in recorder.commands[-1].items():
            current = drivers[name].feedback_position()
            remaining = target - current
            moved = math.copysign(min(abs(remaining), per_tick * scales[name]), remaining)
            drivers[name].set_observed(position=current + moved)

    recorder.sleep = _advance  # type: ignore[method-assign]
    return handle


def _axis_commands(recorder: _Recorder, motor: str) -> list[float]:
    assert recorder.spec is not None
    spec = next(m for m in recorder.spec.motors if m.name == motor)
    return [spec.to_value(cmd[motor]) for cmd in recorder.commands]


def _runner(recorder: _Recorder) -> HomingRunner:
    return HomingRunner(
        sensor_active=recorder.sensor_active,
        sensor_latched=recorder.sensor_latched,
        sensor_is_stale=recorder.sensor_is_stale,
        motor_is_stale=recorder.motor_is_stale,
        origin_capturable=recorder.origin_capturable,
        capture_origin=recorder.capture_origin,
        sleep=recorder.sleep,
    )


class TestStartsFromTheMeasuredPosition:
    async def test_一歩目は実測位置から一歩ぶんだけ動かす(self) -> None:
        table = _table(direction=-1, step=0.5, search_distance=30.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=1)

        await _runner(rec).home(spec, _handle(spec, rec, start_value=15.0))

        assert rec.commands[0] == {"y_axis_r": 29.0, "y_axis_l": -29.0}

    async def test_探索距離は実測の移動量で数える(self) -> None:
        table = _table(search_distance=5.0, step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder()

        with pytest.raises(HomingError, match="動きません"):
            await _runner(rec).home(spec, _handle(spec, rec, follows=False))

        assert rec.origins == []

    async def test_進まない機構は数歩で降りる(self) -> None:
        table = _table(search_distance=100.0, step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder()

        with pytest.raises(HomingError, match="動きません"):
            await _runner(rec).home(spec, _handle(spec, rec, follows=False))

        assert len(rec.commands) <= 5

    async def test_指令は常に実測から一歩ぶんしか離れない(self) -> None:
        table = _table(search_distance=10.0, step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder()
        handle = _handle(spec, rec, follows=False)
        drivers = rec.drivers
        deviations: list[float] = []

        original = handle.set_target_value

        async def _half(commands):
            await original(commands)
            for name, value in commands.items():
                current = drivers[name].feedback_position()
                deviations.append(abs(value - current))
                drivers[name].set_observed(position=current + (value - current) / 2.0)

        handle.set_target_value = _half  # type: ignore[method-assign]

        with pytest.raises(HomingError):
            await _runner(rec).home(spec, handle)

        assert deviations
        assert max(deviations) <= 2.0 + 1e-9


class TestWaitsForEachStep:
    async def test_到達許容差が歩幅以上でも一歩ぶんの追従を待つ(self) -> None:
        spec = _rotate_table().axis("rotate")
        rec = _Recorder()

        with pytest.raises(HomingError, match="動きません"):
            await _runner(rec).home(spec, _handle(spec, rec, follows=False))

        assert rec.sleeps == _STALL_LIMIT * _FOLLOW_ATTEMPTS

    async def test_ゆっくり追従する機構は停滞と数えず次の歩へ進む(self) -> None:
        spec = _rotate_table().axis("rotate")
        rec = _Recorder(active_at_or_below=-3.0)
        handle = _slow_handle(spec, rec, per_tick=0.6)

        travelled = await _runner(rec).home(spec, handle)

        assert rec.origins == ["rotate"]
        assert rec.captured_at == pytest.approx([-3.0])
        assert travelled == pytest.approx(3.0)
        assert rec.sleeps < _FOLLOW_ATTEMPTS * len(rec.commands)

    async def test_離脱でも一歩ぶんの追従を待つ(self) -> None:
        spec = _rotate_table().axis("rotate")
        rec = _Recorder(active_after=0)
        handle = _slow_handle(spec, rec, per_tick=0.6)

        with pytest.raises(HomingError, match="離せませんでした"):
            await _runner(rec).home(spec, handle)

        assert rec.sleeps > len(rec.commands)


class TestStops:
    async def test_探索距離を超えたら止める(self) -> None:
        table = _table(search_distance=5.0, step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder()

        with pytest.raises(HomingError, match="到達しませんでした"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert len(rec.commands) == 5
        assert rec.origins == []

    async def test_センサが途絶していたら一歩も動かさない(self) -> None:
        table = _table()
        spec = table.axis("y_axis")
        rec = _Recorder(stale=True)

        with pytest.raises(HomingError, match="応答していません"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.commands == []
        assert rec.origins == []

    async def test_軸のフィードバックが途絶していたら一歩も動かさない(self) -> None:
        table = _table()
        spec = table.axis("y_axis")
        rec = _Recorder(motor_stale=True)

        with pytest.raises(HomingError, match="現在位置を読めません"):
            await _runner(rec).home(spec, _handle(spec, rec, start_value=15.0))

        assert rec.commands == []
        assert rec.origins == []

    async def test_原点を確定できない軸では一歩も動かさない(self) -> None:
        table = _table()
        spec = table.axis("y_axis")
        rec = _Recorder(capturable=False)

        with pytest.raises(HomingError, match="原点を確定する手段がありません"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.commands == []
        assert rec.origins == []

    async def test_失敗したら原点を確定しない(self) -> None:
        table = _table()
        spec = table.axis("y_axis")
        rec = _Recorder()

        with pytest.raises(HomingError):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == []


class TestReachesOrigin:
    async def test_当たった位置で原点を確定する(self) -> None:
        table = _table(search_distance=10.0, step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=3)

        travelled = await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == ["y_axis"]
        assert travelled == pytest.approx(2.0)

    async def test_戻り値は実測の移動量(self) -> None:
        table = _table(search_distance=10.0, step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=3)

        travelled = await _runner(rec).home(spec, _handle(spec, rec, start_value=15.0))

        assert travelled == pytest.approx(2.0)

    async def test_探索方向を符号で表す(self) -> None:
        table = _table(direction=-1, step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=1)

        await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.commands[0] == {"y_axis_r": -2.0, "y_axis_l": 2.0}

    async def test_左右ペアを同じフレームで指令する(self) -> None:
        table = _table()
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=1)

        await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.commands
        assert all(set(cmd) == {"y_axis_r", "y_axis_l"} for cmd in rec.commands)


class TestDoesNotMissTheContact:
    async def test_一歩の途中で通り過ぎた接触を検出する(self) -> None:
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_band=(-1.55, -1.45))

        travelled = await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == ["y_axis"]
        assert travelled == pytest.approx(2.0)
        assert rec.captured_at == pytest.approx([-2.0])

    async def test_検出したらその場へ止め直す(self) -> None:
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=-1.5)
        handle = _slow_handle(spec, rec, per_tick=0.6)

        await _runner(rec).home(spec, handle)

        axis_commands = [cmd["y_axis_r"] / 2.0 for cmd in rec.commands]
        assert rec.captured_at == pytest.approx([-1.8])
        assert axis_commands[-1] == pytest.approx(-1.8)
        assert axis_commands[-2] == pytest.approx(-2.2)

    async def test_探索の前に古いラッチを捨てる(self) -> None:
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=-5.0, prelatched=True)

        await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.captured_at == pytest.approx([-5.0])


class TestReleasesBeforeSeeking:
    async def test_触れた状態から始めたら離れてから寄せ直す(self) -> None:
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=-1.0)

        await _runner(rec).home(spec, _handle(spec, rec, start_value=-3.0))

        axis_commands = [cmd["y_axis_r"] / 2.0 for cmd in rec.commands]
        assert axis_commands == pytest.approx([-2.0, -1.0, 0.0, 0.0, -1.0, -1.0])
        assert rec.origins == ["y_axis"]
        assert rec.captured_at == pytest.approx([-1.0])

    @pytest.mark.parametrize("start", [-1.2, -2.0, -3.0, -4.5])
    async def test_区間のどこで始めても確定位置は入口から一歩以内(self, start: float) -> None:
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=-1.0)

        await _runner(rec).home(spec, _handle(spec, rec, start_value=start))

        assert rec.captured_at[0] == pytest.approx(-1.0, abs=1.0)

    async def test_離脱はラッチではなく現在値で判定する(self) -> None:
        table = _table(step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder(chatter=True)

        with pytest.raises(HomingError, match="離せませんでした"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == []

    async def test_離れられなければ原点を確定せず降りる(self) -> None:
        table = _table(step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=0)

        with pytest.raises(HomingError, match="離せませんでした"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == []

    async def test_離脱が進まなくなったら降りる(self) -> None:
        table = _table(step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=0)

        with pytest.raises(HomingError, match="動きません"):
            await _runner(rec).home(spec, _handle(spec, rec, follows=False))

        assert len(rec.commands) <= _STALL_LIMIT + 1
        assert rec.origins == []

    async def test_極性の取り違えを疑わせる(self) -> None:
        table = _table(step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=0)

        with pytest.raises(HomingError, match="sensorActiveLow"):
            await _runner(rec).home(spec, _handle(spec, rec))

    async def test_離脱の上限は探索距離を使わない(self) -> None:
        table = _table(step=1.0, search_distance=500.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=0)

        with pytest.raises(HomingError, match="離せませんでした"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert len(rec.commands) < 30


class TestAlignsBothSwitches:
    @staticmethod
    def _sensors(right: float, left: float) -> dict[str, _SensorModel]:
        return {
            "sensor_r": _SensorModel(motor="y_axis_r", active_at_or_below=right),
            "sensor_l": _SensorModel(motor="y_axis_l", active_at_or_below=left),
        }

    async def test_同時に押される機構では整列段が一歩も指令を出さない(self) -> None:
        spec = _paired_table().axis("y_axis")
        rec = _Recorder(sensors=self._sensors(right=-1.0, left=-1.0))

        await _runner(rec).home(spec, _handle(spec, rec))

        assert _axis_commands(rec, "y_axis_r") == pytest.approx([-1.0, -1.0])
        assert rec.origins == ["y_axis"]

    async def test_右が先に押されたら左のモータだけを進める(self) -> None:
        spec = _paired_table().axis("y_axis")
        rec = _Recorder(sensors=self._sensors(right=-1.0, left=-3.0))

        await _runner(rec).home(spec, _handle(spec, rec))

        assert all(set(cmd) == {"y_axis_r", "y_axis_l"} for cmd in rec.commands)
        assert _axis_commands(rec, "y_axis_r") == pytest.approx([-1.0] * 5)
        assert _axis_commands(rec, "y_axis_l") == pytest.approx([-1.0, -1.0, -2.0, -3.0, -3.0])

    async def test_左が先に押されても同じように整列する(self) -> None:
        spec = _paired_table().axis("y_axis")
        rec = _Recorder(sensors=self._sensors(right=-3.0, left=-1.0))

        await _runner(rec).home(spec, _handle(spec, rec))

        assert all(set(cmd) == {"y_axis_r", "y_axis_l"} for cmd in rec.commands)
        assert _axis_commands(rec, "y_axis_l") == pytest.approx([-1.0] * 5)
        assert _axis_commands(rec, "y_axis_r") == pytest.approx([-1.0, -1.0, -2.0, -3.0, -3.0])

    async def test_押されない側を上限以上進めない(self) -> None:
        spec = _paired_table(align_distance=3.0).axis("y_axis")
        rec = _Recorder(
            sensors={
                "sensor_r": _SensorModel(motor="y_axis_r", active_at_or_below=-1.0),
                "sensor_l": _SensorModel(motor="y_axis_l"),
            }
        )

        with pytest.raises(HomingError, match="y_axis_l") as excinfo:
            await _runner(rec).home(spec, _handle(spec, rec))

        message = str(excinfo.value)
        assert "sensor_l" in message
        assert "sensorActiveLow" in message
        assert rec.origins == []
        assert min(_axis_commands(rec, "y_axis_l")) >= -4.0

    async def test_整列段で動かない機構は停滞判定で降りる(self) -> None:
        spec = _paired_table().axis("y_axis")
        rec = _Recorder(sensors=self._sensors(right=-1.0, left=-3.0))
        handle = _handle(spec, rec, follows=lambda: len(rec.commands) <= 2)

        with pytest.raises(HomingError, match="動きません") as excinfo:
            await _runner(rec).home(spec, handle)

        assert "y_axis_l" in str(excinfo.value)
        assert rec.origins == []
        assert len(rec.commands) <= 2 + _STALL_LIMIT

    async def test_保持側は引きずられても指令を書き換えない(self) -> None:
        spec = _paired_table().axis("y_axis")
        rec = _Recorder(sensors=self._sensors(right=-1.0, left=-4.0))
        handle = _handle(spec, rec)
        drivers = rec.drivers
        recorded = handle.set_target_value

        async def _drag(commands):
            await recorded(commands)
            spec_r = next(m for m in spec.motors if m.name == "y_axis_r")
            current = rec.motor_position("y_axis_r")
            drivers["y_axis_r"].set_observed(position=spec_r.to_command(current - 0.3))

        handle.set_target_value = _drag  # type: ignore[method-assign]

        await _runner(rec).home(spec, handle)

        align = _axis_commands(rec, "y_axis_r")[2:]
        assert len(align) >= 3
        assert align == pytest.approx([align[0]] * len(align))
        assert rec.motor_position("y_axis_r") != pytest.approx(align[0])

    async def test_センサが一本でも途絶していたら一歩も動かさない(self) -> None:
        spec = _paired_table().axis("y_axis")
        rec = _Recorder(
            sensors=self._sensors(right=-1.0, left=-3.0),
            stale_sensors=("sensor_l",),
        )

        with pytest.raises(HomingError, match="sensor_l") as excinfo:
            await _runner(rec).home(spec, _handle(spec, rec))

        assert "応答していません" in str(excinfo.value)
        assert rec.commands == []
        assert rec.origins == []

    async def test_片方だけ触れた状態から始めても両方が離れるまで動かす(self) -> None:
        spec = _paired_table().axis("y_axis")
        rec = _Recorder(sensors=self._sensors(right=-1.0, left=-5.0))

        await _runner(rec).home(spec, _handle(spec, rec, start_value=-3.0))

        assert _axis_commands(rec, "y_axis_r")[:4] == pytest.approx([-2.0, -1.0, 0.0, 0.0])
        assert rec.origins == ["y_axis"]
        assert rec.captured_each == [pytest.approx({"y_axis_r": -1.0, "y_axis_l": -5.0})]

    async def test_原点確定は全センサが接触した後に一度だけ(self) -> None:
        spec = _paired_table().axis("y_axis")
        rec = _Recorder(sensors=self._sensors(right=-1.0, left=-3.0))

        await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == ["y_axis"]
        assert rec.captured_each == [pytest.approx({"y_axis_r": -1.0, "y_axis_l": -3.0})]

    async def test_単数センサの軸に整列段は無い(self) -> None:
        spec = _table(direction=-1, step=1.0, search_distance=10.0).axis("y_axis")
        assert spec.homing is not None and spec.homing.sensors is None
        rec = _Recorder(active_at_or_below=-1.0)

        await _runner(rec).home(spec, _handle(spec, rec))

        assert _axis_commands(rec, "y_axis_r") == pytest.approx([-1.0, -1.0])
        assert _axis_commands(rec, "y_axis_l") == pytest.approx([-1.0, -1.0])
        assert rec.origins == ["y_axis"]


class TestSpecValidation:
    @pytest.mark.parametrize(
        ("override", "message"),
        [
            ({"direction": 0}, "direction"),
            ({"direction": 2}, "direction"),
            ({"search_distance": 0}, "search_distance"),
            ({"search_distance": -1}, "search_distance"),
            ({"step": 0}, "step"),
            ({"step": 10.0}, "search_distance"),
        ],
    )
    def test_不正な値を拒否する(self, override: dict, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            _table(**override)

    def test_必須キーの欠落を拒否する(self) -> None:
        with pytest.raises(ValueError, match="search_distance"):
            load_position_table(
                {
                    "axes": {
                        "lift": {
                            "unit": "mm",
                            "command_unit": "deg",
                            "homing": {"sensor": "s", "direction": 1, "step": 1.0},
                        }
                    },
                    "positions": {},
                },
                source="<test>",
            )

    def test_未知のキーを拒否する(self) -> None:
        with pytest.raises(ValueError, match="speed"):
            _table(speed=1.0)

    def test_到達判定を持たない軸には書けない(self) -> None:
        with pytest.raises(ValueError, match="homing は位置指令の軸"):
            load_position_table(
                {
                    "axes": {
                        "conveyor": {
                            "unit": "duty",
                            "command_mode": "duty",
                            "homing": {
                                "sensor": "s",
                                "direction": 1,
                                "search_distance": 1.0,
                                "step": 0.1,
                            },
                        }
                    },
                    "positions": {},
                },
                source="<test>",
            )
