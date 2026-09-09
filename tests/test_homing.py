from __future__ import annotations

import math
from collections.abc import Callable

import pytest

from lib.drivers.generic import GenericDriver
from lib.match_state import Court
from lib.motion_guard import GuardViolation, SensorSuspension
from lib.sequence.homing import (
    _FOLLOW_ATTEMPTS,
    _STALL_LIMIT,
    HomingError,
    HomingRunner,
    measure_switch,
    run_homing,
)
from lib.sequence.motors import AxisHandle, MotorGroup, MotorHandle, build_axis_state_reader
from lib.sequence.positions import AxisSpec, load_position_table
from tests.fake_can import mock_can_manager
from tests.fake_drivers import StubFeedbackDriver
from tests.feedback_frames import feed_generic

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
    """``tolerance`` と ``homing.step`` が一致する軸。

    1 歩ぶんの追従待ちに ``tolerance`` を流用すると壊れる境界が ``step == tolerance``
    にあるので 2.0deg で揃えてある (**実機の rotate は step 1.0 / tolerance 2.0**。
    実機へ追随させると検証したい境界そのものが消える)。
    """
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
    """1 本ぶんのスイッチ。センサの模し方は 5 通りある。

    - ``active_after`` — 指定回数目の観測で ON。歩数だけを見るテスト向け
    - ``active_at_or_below`` — 位置が境界以下なら ON。ON 区間を位置で表す
    - ``active_band`` — 幅を持った ON 区間。``step`` より狭いと現在値では取りこぼす
    - ``chatter`` — 現在値は ON のまま、区間の中に居続けるので接触は増えない
    - ``fails_open_above`` — 一度当ててからこの位置より戻ると開いたまま固着する
      (位置だけで決まる模型では、粗探索が当てた区間へ寄せ直しも必ず当たるので、
      段ごとの上限の食い違いが結果に出ない)

    **現在値と接触カウンタを作り分けている**のは、探索と離脱が別のものを見るという
    設計を固定するため。カウンタは「前回読んだ位置から今の位置までの**経路**が
    ON 区間と交わったか」で 1 ずつ進める (FEEDBACK は 100Hz なので通り抜けた区間は
    落ちない)。**読んでも減らない**ので、読み手が増えても取りこぼさない。
    """

    def __init__(
        self,
        *,
        motor: str | None = None,
        active_after: int | None = None,
        active_at_or_below: float | None = None,
        active_band: tuple[float, float] | None = None,
        chatter: bool = False,
        precounted: bool = False,
        fails_open_above: float | None = None,
        count_supported: bool = True,
    ) -> None:
        self.motor = motor
        self._active_after = active_after
        self._active_at_or_below = active_at_or_below
        self._active_band = active_band
        self._chatter = chatter
        self._observations = 0
        self._path: tuple[float, float] | None = None
        self._contacts = 1 if precounted else 0
        self._fails_open_above = fails_open_above
        self._reached_below = False
        self._failed_open = False
        self._count_supported = count_supported
        self.position: Callable[[], float] = lambda: 0.0

    def _extend_path(self) -> tuple[float, float]:
        current = self.position()
        if self._fails_open_above is not None:
            # 当たる前から死んでいる模型にすると粗探索そのものが失敗するので、
            # 一度 ON 区間へ入ったことを固着の条件にする
            if current <= self._fails_open_above:
                self._reached_below = True
            elif self._reached_below:
                self._failed_open = True
        low, high = self._path if self._path is not None else (current, current)
        self._path = (min(low, current), max(high, current))
        return self._path

    def active(self) -> bool:
        self._observations += 1
        self._extend_path()
        if self._failed_open:
            return False
        if self._chatter:
            return True  # まだ ON 区間の中にいる
        if self._active_band is not None:
            low, high = self._active_band
            return low <= self.position() <= high
        if self._active_at_or_below is not None:
            return self.position() <= self._active_at_or_below
        if self._active_after is None:
            return False
        return self._observations > self._active_after

    def contact_count(self) -> int | None:
        self._observations += 1
        low, high = self._extend_path()
        # 次の窓は今の位置から始まる。**カウンタ自体は戻さない**
        current = self.position()
        self._path = (current, current)
        if not self._count_supported:
            # **現在値へ落とさない** (落とすと「取りこぼす探索」へ黙って戻る)
            return None
        if self._failed_open or self._chatter:
            return self._contacts
        if self._active_band is not None:
            band_low, band_high = self._active_band
            if low <= band_high and high >= band_low:
                self._contacts += 1
        elif self._active_at_or_below is not None:
            if low <= self._active_at_or_below:
                self._contacts += 1
        elif self._active_after is not None and self._observations > self._active_after:
            self._contacts += 1
        return self._contacts


class _Recorder:
    def __init__(
        self,
        *,
        sensors: dict[str, _SensorModel] | None = None,
        active_after: int | None = None,
        active_at_or_below: float | None = None,
        active_band: tuple[float, float] | None = None,
        chatter: bool = False,
        precounted: bool = False,
        fails_open_above: float | None = None,
        count_supported: bool = True,
        stale: bool = False,
        stale_sensors: tuple[str, ...] = (),
        stale_after_commands: int | None = None,
        motor_stale: bool = False,
        motor_stale_after_commands: int | None = None,
        unreadable_after_commands: int | None = None,
        energized: bool | None = True,
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
            precounted=precounted,
            fails_open_above=fails_open_above,
            count_supported=count_supported,
        )
        self._sensors = sensors
        for model in (self._default, *(sensors or {}).values()):
            model.position = self._position_of(model)
        self._stale = stale
        self._stale_sensors = stale_sensors
        self._stale_after_commands = stale_after_commands
        self._motor_stale = motor_stale
        self._motor_stale_after_commands = motor_stale_after_commands
        self._unreadable_after_commands = unreadable_after_commands
        self._energized = energized
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

    def sensor_active(self, name: str) -> bool | None:
        """**今**接触しているか。**三値** —— 実機の読み口は途絶中に `None` を返す。"""
        value = self._sensor(name).active()
        if self.sensor_is_stale(name):
            return None
        if (
            self._unreadable_after_commands is not None
            and len(self.commands) >= self._unreadable_after_commands
        ):
            return None
        return value

    def sensor_contact_count(self, name: str) -> int | None:
        return self._sensor(name).contact_count()

    def sensor_is_stale(self, name: str) -> bool:
        if self._stale_after_commands is not None:
            return len(self.commands) >= self._stale_after_commands
        return self._stale or name in self._stale_sensors

    def motor_is_stale(self, _name: str) -> bool:
        if self._motor_stale_after_commands is not None:
            return len(self.commands) >= self._motor_stale_after_commands
        return self._motor_stale

    def motor_is_energized(self, _name: str) -> bool | None:
        return self._energized

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
    sensor_active: Callable[[str], bool | None] | None = None,
) -> AxisHandle:
    """``follows`` が False の機構は引っかかって 1mm も動かない。

    指令の積算ではなく実測で数えているかは、この機構でしか現れない。

    ``sensor_active`` を渡すと**可動端の歯止めが効いた状態**になる (`guard:` を
    書いた軸だけ。渡さない軸は三値の `None` しか返らないので歯止めが全部に
    掛かり、零点確定そのものが成立しない)。
    """
    mgr = mock_can_manager()
    drivers = {}
    handles = []
    for motor in spec.motors:
        driver = StubFeedbackDriver(motor.name, 1)
        driver.set_observed(position=motor.to_command(start_value))
        drivers[motor.name] = driver
        handles.append(MotorHandle(motor.name, driver, mgr))

    handle = AxisHandle(spec, handles, sensor_active=sensor_active)
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
    """1 回の待ちにつき ``per_tick`` だけ指令へ寄る機構。

    実機は 1 歩ぶんを待ち 1 回では動き切らない。**その途中を「進まなかった」と
    数えると正常な機構が停滞判定で落ちる。** ``sleep`` を差し替えるので
    ``_runner`` はこの関数の後に組み立てること。
    """
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


def _runner(recorder: _Recorder, *, suspension: SensorSuspension | None = None) -> HomingRunner:
    """``suspension`` を渡すと、整列段のあいだ原点センサを歯止めから外す配線が入る。

    **`sensor_active` には覆いを掛けない口を渡す** —— 探索も離脱も「今 ON か」で
    進むので、覆った値を渡すと零点確定が自分の目を塞ぐ。
    """
    extra = {} if suspension is None else {"suspend_sensors": suspension.suspend}
    return HomingRunner(
        sensor_active=recorder.sensor_active,
        sensor_contact_count=recorder.sensor_contact_count,
        sensor_is_stale=recorder.sensor_is_stale,
        motor_is_stale=recorder.motor_is_stale,
        motor_is_energized=recorder.motor_is_energized,
        origin_capturable=recorder.origin_capturable,
        capture_origin=recorder.capture_origin,
        sleep=recorder.sleep,
        **extra,  # type: ignore[arg-type]
    )


class TestStartsFromTheMeasuredPosition:
    async def test_一歩目は実測位置から一歩ぶんだけ動かす(self) -> None:
        table = _table(direction=-1, step=0.5, search_distance=30.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=1)

        # 手動ジョグで +15mm へ動かした後に動作確認を起動した状態
        await _runner(rec).home(spec, _handle(spec, rec, start_value=15.0))

        # 0 起点だと -0.5mm = 15.5mm の引き戻しになる
        assert rec.commands[0] == {"y_axis_r": 29.0, "y_axis_l": -29.0}

    async def test_探索距離は実測の移動量で数える(self) -> None:
        table = _table(search_distance=5.0, step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder()

        with pytest.raises(HomingError, match="動きません"):
            await _runner(rec).home(spec, _handle(spec, rec, follows=False))

        # 「5mm 動かしても到達しませんでした」ではない (実際には動いていない)
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

        # 1 step = 1mm = 指令単位で 2deg。超えると位置制御ループが電流上限まで押す
        assert deviations
        assert max(deviations) <= 2.0 + 1e-9


class TestWaitsForEachStep:
    async def test_到達許容差が歩幅以上でも一歩ぶんの追従を待つ(self) -> None:
        spec = _rotate_table().axis("rotate")
        rec = _Recorder()

        with pytest.raises(HomingError, match="動きません"):
            await _runner(rec).home(spec, _handle(spec, rec, follows=False))

        # tolerance (2.0deg) で判定すると 1 回目の確認で追従完了になり、
        # 待ちの回数が歩数と同じ (_STALL_LIMIT) まで落ちる
        assert rec.sleeps == _STALL_LIMIT * _FOLLOW_ATTEMPTS

    async def test_ゆっくり追従する機構は停滞と数えず次の歩へ進む(self) -> None:
        spec = _rotate_table().axis("rotate")
        rec = _Recorder(active_at_or_below=-3.0)
        handle = _slow_handle(spec, rec, per_tick=0.6)

        travelled = await _runner(rec).home(spec, handle)

        assert rec.origins == ["rotate"]
        assert rec.captured_at == pytest.approx([-3.0])
        assert travelled == pytest.approx(3.0)
        # 毎歩 _FOLLOW_ATTEMPTS を使い切ると 90 歩の探索が 20 秒を超える
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
        """未受信の 0.0 を現在位置と信じると、1 歩目が全ストロークのジャンプになる。"""
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


class TestStopsWhenFeedbackIsLostMidSearch:
    """**事前確認だけでは「探索を始めた後の途絶」に気付けない。**

    センサの読み口は途絶しても最後に届いたフラグを返し続けるので、探索中の途絶は
    「いつまでも当たらない」形にしかならない —— 止めるのは探索距離の上限
    (そこまで押し込んでから) か停滞判定だけになる。
    """

    async def test_探索の途中でセンサが途絶したら止める(self) -> None:
        table = _table(direction=-1, step=1.0, search_distance=50.0)
        spec = table.axis("y_axis")
        rec = _Recorder(stale_after_commands=2)

        with pytest.raises(HomingError, match="応答していません"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == []
        assert len(rec.commands) <= 3

    async def test_探索の途中で軸のフィードバックが途絶したら止める(self) -> None:
        table = _table(direction=-1, step=1.0, search_distance=50.0)
        spec = table.axis("y_axis")
        rec = _Recorder(motor_stale_after_commands=2)

        with pytest.raises(HomingError, match="現在位置を読めません"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == []
        assert len(rec.commands) <= 3

    async def test_途絶で降りる前にその場の実測位置を目標へ送り直す(self) -> None:
        """最後に送った「実測 + step」が生きたままだと、位置ループが押し続ける。"""
        table = _table(direction=-1, step=1.0, search_distance=50.0, settle_s=0.0)
        spec = table.axis("y_axis")
        rec = _Recorder(stale_after_commands=2)
        # 追従しきる機構では「実測 + step」と実測が一致し、送り直しの有無が現れない
        handle = _slow_handle(spec, rec, per_tick=0.6)

        with pytest.raises(HomingError, match="応答していません"):
            await _runner(rec).home(spec, handle)

        axis_commands = [cmd["y_axis_r"] / 2.0 for cmd in rec.commands]
        assert axis_commands[-1] == pytest.approx(rec.axis_position())
        assert axis_commands[-1] != pytest.approx(axis_commands[-2])
        assert rec.origins == []

    async def test_離脱の途中で途絶しても止める(self) -> None:
        """歯止めは向きごとに書き分けない (`_seek` を 1 本にしてある理由)。"""
        table = _table(direction=-1, step=1.0, search_distance=50.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=0, stale_after_commands=2)

        with pytest.raises(HomingError, match="応答していません"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == []
        assert len(rec.commands) <= 3

    async def test_読めないセンサを離脱完了と読まない(self) -> None:
        """**`None` を `False` へ丸めない層を、他の層を持たない条件で単独で見る。**

        鮮度の判定は「正常」と答えるのに現在値だけが読めない状態を作る。丸める実装は
        「押されていない = 離脱できた」と読み、触れたままの位置で原点を確定する。
        """
        table = _table(direction=-1, step=1.0, search_distance=50.0, release_distance=5.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=0, unreadable_after_commands=1)

        with pytest.raises(HomingError, match="離せませんでした"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == []


class TestChecksTheDriveIsReady:
    async def test_無励磁の軸では一歩も動かさない(self) -> None:
        """停滞判定でも 3 歩で拾えるが、その文言では原因を 1 つに絞れない。"""
        table = _table()
        spec = table.axis("y_axis")
        rec = _Recorder(energized=False)

        with pytest.raises(HomingError, match="無励磁"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.commands == []
        assert rec.origins == []

    async def test_励磁を報告しないドライバは無励磁として扱わない(self) -> None:
        """倒すと M3508 (`is_energized()` は常に None) の軸が零点確定できなくなる。"""
        table = _table(search_distance=10.0, step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=3, energized=None)

        await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == ["y_axis"]

    async def test_接触カウンタを持たないセンサでは探索を開始しない(self) -> None:
        """現在値へ落とす実装は「取りこぼす探索」そのもので、向かう先は破損側。"""
        table = _table()
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=-2.0, count_supported=False)

        with pytest.raises(HomingError, match="カウンタ"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.commands == []
        assert rec.origins == []


class TestReachesOrigin:
    async def test_当たった位置で原点を確定する(self) -> None:
        table = _table(search_distance=10.0, step=1.0)
        spec = table.axis("y_axis")
        # センサを読むのは「開始前の確認」「探索前のラッチ捨て」「1 歩ごと」の順
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
        """別々の時刻に動かすとその場で機構が壊れる。"""
        table = _table()
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=1)

        await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.commands
        assert all(set(cmd) == {"y_axis_r", "y_axis_l"} for cmd in rec.commands)


class TestDoesNotMissTheContact:
    """**探索の到達判定は接触カウンタで見る。「今 ON か」では取りこぼす。**

    ON 区間が `step` より狭いと指令 1 回で跨いでしまい、`settle_s` 後の観測では
    もう OFF になっている (実機の `rotate` は step 2.0deg を約 18ms で通過する)。
    現在値だけを見る実装はスイッチを越えて回り続けた。
    """

    async def test_一歩の途中で通り過ぎた接触を検出する(self) -> None:
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        # ON 区間 -1.55〜-1.45 (幅 0.1mm)。観測位置はどれも区間の外を通る
        rec = _Recorder(active_band=(-1.55, -1.45))

        travelled = await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == ["y_axis"]
        assert travelled == pytest.approx(2.0)
        assert rec.captured_at == pytest.approx([-2.0])

    async def test_検出したらその場へ止め直す(self) -> None:
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=-1.5)
        # 検出した実測位置と、そのとき生きている指令が別の値になる構成が要る
        handle = _slow_handle(spec, rec, per_tick=0.6)

        await _runner(rec).home(spec, handle)

        axis_commands = [cmd["y_axis_r"] / 2.0 for cmd in rec.commands]
        assert rec.captured_at == pytest.approx([-1.8])
        assert axis_commands[-1] == pytest.approx(-1.8)
        assert axis_commands[-2] == pytest.approx(-2.2)

    async def test_探索の前に基準値を取り直す(self) -> None:
        """取り直さないと 1 歩目で到達と読み、スイッチではなく探索開始位置が原点になる。"""
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=-5.0, precounted=True)

        await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.captured_at == pytest.approx([-5.0])


class TestSurvivesAnotherReader:
    """**接触は読んでも消えない。**

    走行中の接触を拾う常駐監視が同じセンサを毎周期読んでも、零点確定の到達判定は
    そのぶんを失わない。読むと消える実装では先に読んだ側が相手のぶんまで消し、
    実機で起きた「当たっているのに探索が止まらず端を越えて回り続けた」が再発する。
    """

    async def test_常駐監視が毎周期読んでも接触を取りこぼさない(self) -> None:
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        sensor = GenericDriver("origin_sensor", 0x41)
        rec = _Recorder()
        handle = _handle(spec, rec)
        watched: list[int] = []

        async def sleep(_seconds: float) -> None:
            rec.sleeps += 1
            # ON 区間は 1 歩の途中で通り抜けるので、観測時にはもう OFF
            if rec.axis_position() <= -1.5 and sensor.sensor_contact_count == 0:
                feed_generic(sensor, sensor=True)
                feed_generic(sensor, sensor=False)
            # 常駐監視が零点確定より先に同じカウンタを読む
            watched.append(sensor.sensor_contact_count)

        runner = HomingRunner(
            sensor_active=lambda _name: sensor.sensor_active,
            sensor_contact_count=lambda _name: sensor.sensor_contact_count,
            sensor_is_stale=lambda _name: False,
            motor_is_stale=lambda _name: False,
            motor_is_energized=lambda _name: True,
            origin_capturable=lambda _axis: True,
            capture_origin=rec.capture_origin,
            sleep=sleep,
        )

        await runner.home(spec, handle)

        assert max(watched) == 1
        assert rec.origins == ["y_axis"]
        assert rec.captured_at == pytest.approx([-2.0])


class TestReleasesBeforeSeeking:
    """**触れた状態から始めたら、一度離れてから寄せ直す。**

    ON 区間には幅があるので、触れたその場を原点にすると「区間のどこで始めたか」が
    そのまま原点のばらつきになる。離脱は探索と逆向きなので、機構端で始まったときに
    押し込まない性質は保たれる。
    """

    async def test_触れた状態から始めたら離れてから寄せ直す(self) -> None:
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=-1.0)

        await _runner(rec).home(spec, _handle(spec, rec, start_value=-3.0))

        axis_commands = [cmd["y_axis_r"] / 2.0 for cmd in rec.commands]
        # 0.0 と -1.0 が 2 通ずつ並ぶのは、検出位置へ止め直す指令が続くため
        assert axis_commands == pytest.approx([-2.0, -1.0, 0.0, 0.0, -1.0, -1.0])
        assert rec.origins == ["y_axis"]
        assert rec.captured_at == pytest.approx([-1.0])

    @pytest.mark.parametrize("start", [-1.2, -2.0, -3.0, -4.5])
    async def test_区間のどこで始めても確定位置は入口から一歩以内(self, start: float) -> None:
        """**これがこの処理の目的そのもの。** 離脱しない実装ではここが区間幅ぶん開く。"""
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=-1.0)

        await _runner(rec).home(spec, _handle(spec, rec, start_value=start))

        assert rec.captured_at[0] == pytest.approx(-1.0, abs=1.0)

    async def test_離脱は接触カウンタではなく現在値で判定する(self) -> None:
        """揃えると「一度でも OFF になったか」になり、チャタリングで離脱完了と読む。"""
        table = _table(step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder(chatter=True)

        with pytest.raises(HomingError, match="離せませんでした"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == []

    async def test_離脱の許容は刻み幅から切り離せる(self) -> None:
        """**離脱の許容は ON 区間の広さで決まる値で、刻み幅とは無関係。**

        既定 (step の 20 倍) のままだと、精度のために step を詰めた瞬間に許容も
        一緒に縮む (実機で step 0.5 -> 0.1 にして 10mm -> 2mm へ落ち、ON 区間を
        抜けきれずに失敗した)。
        """
        table = _table(direction=-1, step=0.1, search_distance=5.0)
        spec = table.axis("y_axis")
        # 区間の奥 -6.0 から出るには 3.1mm 要るので、既定の 2.0mm では届かない
        rec = _Recorder(active_at_or_below=-3.0)
        with pytest.raises(HomingError, match="離せませんでした"):
            await _runner(rec).home(spec, _handle(spec, rec, start_value=-6.0))
        assert rec.origins == []

        table = _table(direction=-1, step=0.1, search_distance=5.0, release_distance=10.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=-3.0)
        await _runner(rec).home(spec, _handle(spec, rec, start_value=-6.0))
        assert rec.captured_at == pytest.approx([-3.0], abs=0.1)

    async def test_離れられなければ原点を確定せず降りる(self) -> None:
        table = _table(step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=0)

        with pytest.raises(HomingError, match="離せませんでした"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == []

    async def test_離脱が進まなくなったら降りる(self) -> None:
        """歩数上限だけでは step * 20 ぶん押し当て続けてから降りることになる。"""
        table = _table(step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=0)

        with pytest.raises(HomingError, match="動きません"):
            await _runner(rec).home(spec, _handle(spec, rec, follows=False))

        assert len(rec.commands) <= _STALL_LIMIT + 1
        assert rec.origins == []

    async def test_極性の取り違えを疑わせる(self) -> None:
        """極性が逆だとどこへ動かしても ON のまま。接点の固着と切り分けられない。"""
        table = _table(step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=0)

        with pytest.raises(HomingError, match="sensorActiveLow"):
            await _runner(rec).home(spec, _handle(spec, rec))

    async def test_離脱の上限は探索距離を使わない(self) -> None:
        """流用すると、実ストロークまで伸びた探索距離ぶん反対端へ走り抜ける。"""
        table = _table(step=1.0, search_distance=500.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=0)

        with pytest.raises(HomingError, match="離せませんでした"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert len(rec.commands) < 30


class TestTwoStageSearch:
    """**精度と時間は 1 つの刻みでは両立しない。** `homing.coarse_step` がその答え。

    0.1mm 刻みで実ストローク 750mm を探索すると 8000 歩 ≒ 8 分かかり点検に入らない。
    粗い刻みで当てる → 離脱 → `step` で寄せ直す、で**確定位置の粒度は最後の段の
    `step` のまま**所要時間だけが縮む。
    """

    async def test_粗い刻みで当ててから細かい刻みで寄せ直す(self) -> None:
        table = _table(
            direction=-1, step=0.1, coarse_step=1.0, search_distance=20.0, release_distance=2.0
        )
        spec = table.axis("y_axis")
        # 入口 -5.45 は粗い刻みの格子と意図してずらしてある (揃えると寄せ直しの
        # 有無が結果に出ない)
        rec = _Recorder(active_at_or_below=-5.45)

        travelled = await _runner(rec).home(spec, _handle(spec, rec))

        axis_commands = [cmd["y_axis_r"] / 2.0 for cmd in rec.commands]
        assert axis_commands == pytest.approx(
            [
                # 粗探索 (1.0 刻み)。-6.0 で ON 区間へ入り、その場へ止め直す
                -1.0, -2.0, -3.0, -4.0, -5.0, -6.0, -6.0,
                # 離脱。**粗い側の刻みで区間の外まで戻る**
                -5.0, -5.0,
                # 寄せ直し (0.1 刻み)。入口 -5.45 を跨いだ -5.5 で確定
                -5.1, -5.2, -5.3, -5.4, -5.5, -5.5,
            ]
        )  # fmt: skip
        assert rec.origins == ["y_axis"]
        assert rec.captured_at[0] == pytest.approx(-5.45, abs=0.1)
        # 戻り値は 2 段の合計 (粗 6.0 + 細 0.5)。離脱のぶんは含めない
        assert travelled == pytest.approx(6.5)

    @pytest.mark.parametrize("entry", [-5.45, -5.0, -4.62, -7.3])
    async def test_粗い格子のどこで当てても確定位置は入口から細かい一歩以内(
        self, entry: float
    ) -> None:
        """**これが二段にする目的そのもの。**"""
        table = _table(
            direction=-1, step=0.1, coarse_step=1.0, search_distance=20.0, release_distance=2.0
        )
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=entry)

        await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.captured_at[0] == pytest.approx(entry, abs=0.1)

    async def test_粗探索と細探索の合計が探索距離を超えない(self) -> None:
        """段ごとに `search_distance` を与え直すと、唯一の無人の歯止めが 2 倍になる。

        **位置だけで決まるセンサ模型ではこの食い違いが結果に出ない** —— 粗探索が
        当てた区間には寄せ直しも必ず当たる。当てた後に開いたまま固着するスイッチに
        すると、寄せ直しに渡した上限がそのまま押し込む距離として現れる。
        """
        table = _table(
            direction=-1, step=0.1, coarse_step=1.0, search_distance=4.0, release_distance=2.0
        )
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=-3.45, fails_open_above=-3.5)

        with pytest.raises(HomingError, match="到達しませんでした"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == []
        axis_commands = [cmd["y_axis_r"] / 2.0 for cmd in rec.commands]
        # 寄せ直しに渡るのは離脱で戻った 1.0mm だけ
        assert min(axis_commands) == pytest.approx(-4.0)

    async def test_離脱で戻った分は探索距離を消費しない(self) -> None:
        """数えると、粗探索が上限を使い切った軸で寄せ直しの上限が 0 以下になる。

        `search_distance` は実ストロークに合わせる値なので、二段探索の軸では普通に
        起きる。**到達しうる最も深い点は離脱前より深くならない**ので歯止めの意味は
        変わらない。
        """
        table = _table(
            direction=-1, step=0.1, coarse_step=1.0, search_distance=4.0, release_distance=2.0
        )
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=-3.45)

        await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == ["y_axis"]
        assert rec.captured_at[0] == pytest.approx(-3.45, abs=0.1)
        axis_commands = [cmd["y_axis_r"] / 2.0 for cmd in rec.commands]
        assert min(axis_commands) == pytest.approx(-4.0)

    async def test_粗探索の停滞判定は粗い刻みを基準にする(self) -> None:
        """基準が細かい側だと、粗い 1 歩の 4 割しか動かない機構が正常に見える。"""
        table = _table(
            direction=-1, step=0.1, coarse_step=1.0, search_distance=20.0, release_distance=2.0
        )
        spec = table.axis("y_axis")
        rec = _Recorder()
        # 待ち 5 回ぶんでも粗い 1 歩 (1.0mm) の 0.4mm しか進まない
        handle = _slow_handle(spec, rec, per_tick=0.08)

        with pytest.raises(HomingError, match="動きません"):
            await _runner(rec).home(spec, handle)

        assert len(rec.commands) <= _STALL_LIMIT + 1
        assert rec.origins == []

    async def test_細探索の停滞判定は細かい刻みを基準にする(self) -> None:
        """粗い側の基準 (1.0mm) で数えるとどの歩も「進まなかった」になる。"""
        table = _table(
            direction=-1, step=0.1, coarse_step=2.0, search_distance=20.0, release_distance=4.0
        )
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=-5.05)

        await _runner(rec).home(spec, _handle(spec, rec))

        axis_commands = [cmd["y_axis_r"] / 2.0 for cmd in rec.commands]
        # 離脱の終わり (-4.0 へ止め直した指令) から後が寄せ直しの段
        release_end = len(axis_commands) - 1 - axis_commands[::-1].index(pytest.approx(-4.0))
        fine_commands = axis_commands[release_end + 1 :]
        # 踏まなければこの検証は空振りする
        assert len(fine_commands) > _STALL_LIMIT
        assert rec.captured_at[0] == pytest.approx(-5.05, abs=0.1)

    async def test_粗探索が区間を跨ぎ切ったら寄せ直さずに降りる(self) -> None:
        """跨ぎ切ると当てた時点でもう区間の外 (機構端の側) に居る。"""
        table = _table(
            direction=-1, step=0.1, coarse_step=1.0, search_distance=20.0, release_distance=2.0
        )
        spec = table.axis("y_axis")
        # ON 区間 -5.6〜-5.2 (幅 0.4mm) を粗い刻み 1.0mm は 1 歩で跨ぎ切る
        rec = _Recorder(active_band=(-5.6, -5.2))

        with pytest.raises(HomingError, match="coarse_step"):
            await _runner(rec).home(spec, _handle(spec, rec))

        # 粗探索 6 歩 + その場へ止め直す 1 通。1 歩も追加で動かさない
        assert len(rec.commands) == 7
        assert rec.origins == []

    async def test_粗い刻みを書かない軸は従来どおり単段で寄せる(self) -> None:
        table = _table(direction=-1, step=1.0, search_distance=20.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=-5.45)

        await _runner(rec).home(spec, _handle(spec, rec))

        axis_commands = [cmd["y_axis_r"] / 2.0 for cmd in rec.commands]
        assert axis_commands == pytest.approx([-1.0, -2.0, -3.0, -4.0, -5.0, -6.0, -6.0])
        assert rec.captured_at == pytest.approx([-6.0])


class TestAlignsBothSwitches:
    """左右に 1 本ずつスイッチが付く軸は、片方が当たった後に残りを揃える段が要る。"""

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
        """片側だけを進める段の唯一の無人の歯止め。"""
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
        """再アンカーすると、遊びの無い機構では保持側が相方に連れられて進み続ける。"""
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

    async def test_整列段の途中で途絶したら止める(self) -> None:
        """歯止めは段ごとに書き分けない (探索と同じ 1 歩ごとの確認が要る)。"""
        spec = _paired_table().axis("y_axis")
        rec = _Recorder(
            sensors=self._sensors(right=-1.0, left=-4.0),
            motor_stale_after_commands=3,
        )

        with pytest.raises(HomingError, match="現在位置を読めません"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == []
        assert len(rec.commands) <= 4

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
    """設定の誤りは起動時に落とす。試合直前に「動かない」で気付くのでは遅い。"""

    @pytest.mark.parametrize(
        ("override", "message"),
        [
            ({"direction": 0}, "direction"),
            ({"direction": 2}, "direction"),
            ({"search_distance": 0}, "search_distance"),
            ({"search_distance": -1}, "search_distance"),
            ({"step": 0}, "step"),
            ({"step": 10.0}, "search_distance"),  # 1 歩も踏めない
            ({"coarse_step": 0, "release_distance": 5.0}, "coarse_step"),
            ({"coarse_step": -1.0, "release_distance": 5.0}, "coarse_step"),
            # 粗くない粗探索 (既定の step は 1.0)。時間だけを倍にする
            ({"coarse_step": 1.0, "release_distance": 5.0}, "coarse_step"),
            # 探索距離より粗い刻み (既定の search_distance は 5.0)
            ({"coarse_step": 10.0, "release_distance": 5.0}, "coarse_step"),
            # 離脱は粗い刻みで動くので、step から作る既定では桁が合わない
            ({"coarse_step": 2.0}, "release_distance"),
        ],
    )
    def test_不正な値を拒否する(self, override: dict, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            _table(**override)

    def test_必須キーの欠落を拒否する(self) -> None:
        # 探索距離を既定値で埋められると、無人の歯止めが黙って消える
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
        """duty / on_off は指令が届いたかを観測できず、少しずつ寄せる操作が成立しない。"""
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


class TestStopsInTheCommandUnits:
    """「その場で止まれ」は**実測を指令の単位のまま**書き戻す。

    値へ換算して指令へ戻すと (`to_commands(to_value(…))`)、複数モータ軸では平均を
    挟むぶん往復が丸め誤差を生み、それが非ゼロの `delta` として可動端の歯止めへ
    届く —— **止めるための指令が、止まっていないことを理由に拒否される**
    (実測 約 3%)。加えて平均へ寄せる指令は、止めるべき瞬間に左右を動かしに行く。
    """

    def _lagging_handle(
        self, spec: AxisSpec, rec: _Recorder, seen: list[dict[str, float]]
    ) -> AxisHandle:
        """左だけ 0.5mm 遅れて追従する機構 (左右の実測が常に食い違う)。

        `seen` には各指令の**直前の実測**を積む。書き戻した値がそれと一致するかは、
        左右が揃った機構では平均と区別が付かない。
        """
        handle = _handle(spec, rec, follows=False)
        drivers = rec.drivers
        original = handle.set_target_value

        async def _move(commands):
            seen.append({name: driver.feedback_position() for name, driver in drivers.items()})
            await original(commands)
            for name, value in commands.items():
                motor = next(m for m in spec.motors if m.name == name)
                lag = 0.5 if name == "y_axis_l" else 0.0
                drivers[name].set_observed(position=motor.to_command(motor.to_value(value) + lag))

        handle.set_target_value = _move  # type: ignore[method-assign]
        return handle

    async def test_接触したら各モータの実測をそのまま書き戻す(self) -> None:
        spec = _table(direction=-1, step=1.0, search_distance=10.0).axis("y_axis")
        rec = _Recorder(active_at_or_below=-2.0)
        seen: list[dict[str, float]] = []

        await _runner(rec).home(spec, self._lagging_handle(spec, rec, seen))

        assert rec.commands[-1] == pytest.approx(seen[-1])
        # 左右が同じ値になっていたら平均へ寄せている (逆回転ペアなので符号は逆)
        left, right = rec.commands[-1]["y_axis_l"], rec.commands[-1]["y_axis_r"]
        assert left != pytest.approx(-right)

    async def test_途絶で降りるときも実測をそのまま書き戻す(self) -> None:
        spec = _table(direction=-1, step=1.0, search_distance=10.0).axis("y_axis")
        rec = _Recorder(active_at_or_below=-20.0, motor_stale_after_commands=2)
        seen: list[dict[str, float]] = []

        with pytest.raises(HomingError, match="現在位置を読めません"):
            await _runner(rec).home(spec, self._lagging_handle(spec, rec, seen))

        assert rec.commands[-1] == pytest.approx(seen[-1])


class TestAlignsWithTheGuardArmed:
    """左右にスイッチを持つ軸の整列段と、可動端の歯止めの噛み合わせ。

    整列段は**まだ当たっていない側だけ**を端へ進めるが、軸としては端へ向かう向き
    なので、既に押された 1 本を見た歯止めがその指令を拒否する。外すのはその軸の
    `homing.sensor_names` だけ (`SensorSuspension`)。
    """

    def _guarded_table(self):
        return load_position_table(
            {
                "axes": {
                    "y_axis": {
                        "unit": "mm",
                        "command_unit": "deg",
                        "tolerance": 0.1,
                        "sync_tolerance": 100.0,
                        "homing": {
                            "sensors": {"y_axis_r": "sensor_r", "y_axis_l": "sensor_l"},
                            "direction": -1,
                            "search_distance": 30.0,
                            "step": 1.0,
                            "settle_s": 0.0,
                            "align_distance": 5.0,
                        },
                        "guard": {"limits": {"minus": ["sensor_r", "sensor_l"]}},
                        "motors": {"y_axis_r": {"scale": 2.0}, "y_axis_l": {"scale": -2.0}},
                    }
                },
                "positions": {"y_axis": {"home": 0.0}},
            },
            source="<test>",
        )

    def _recorder(self) -> _Recorder:
        return _Recorder(
            sensors={
                "sensor_r": _SensorModel(motor="y_axis_r", active_at_or_below=-1.0),
                "sensor_l": _SensorModel(motor="y_axis_l", active_at_or_below=-3.0),
            }
        )

    def _handle(self, spec: AxisSpec, rec: _Recorder, reader) -> AxisHandle:
        """**指令を通してから動かす機構。**

        共有の `_handle` は歯止めより先に実測を動かすので、実測と目標が常に
        一致して可動端の判定が `delta == 0` で素通りする (テストが何も見ていない
        状態になる)。
        """
        handle = _handle(spec, rec, follows=False, sensor_active=reader)
        original = handle.set_target_value

        async def _move(commands):
            await original(commands)
            for name, value in commands.items():
                rec.drivers[name].set_observed(position=value)

        handle.set_target_value = _move  # type: ignore[method-assign]
        return handle

    async def test_覆いを配線すれば整列段が完走する(self) -> None:
        spec = self._guarded_table().axis("y_axis")
        rec = self._recorder()
        suspension = SensorSuspension()
        handle = self._handle(spec, rec, suspension.wrap(rec.sensor_active))

        await _runner(rec, suspension=suspension).home(spec, handle)

        assert rec.origins == ["y_axis"]
        assert rec.captured_each == [pytest.approx({"y_axis_r": -1.0, "y_axis_l": -3.0})]

    async def test_覆いを配線しないと整列段は歯止めに拒否される(self) -> None:
        """**配線し忘れは「守りが消える」ではなく「その軸だけ零点確定できない」。**

        安全側へ倒れるので黙って通ることは無いが、症状は config からもログからも
        読めないので、この形で固定しておく。
        """
        spec = self._guarded_table().axis("y_axis")
        rec = self._recorder()
        handle = self._handle(spec, rec, rec.sensor_active)

        with pytest.raises(GuardViolation, match="sensor_r"):
            await _runner(rec).home(spec, handle)

        assert rec.origins == []

    async def test_探索中の歯止めは外さない(self) -> None:
        """**外すのは整列段だけ。** 探索の途中でスイッチが立ったのに次の 1 歩が
        出てしまう状態を作らない (接触の検出を取りこぼしたときの最後の砦)。
        """
        spec = self._guarded_table().axis("y_axis")
        rec = self._recorder()
        # 接触を 1 度も数えないセンサ (探索は当たったことに気付かないまま歩き続ける)
        rec.sensor_contact_count = lambda _name: 0  # type: ignore[method-assign]
        suspension = SensorSuspension()
        handle = self._handle(spec, rec, suspension.wrap(rec.sensor_active))

        with pytest.raises(GuardViolation, match="sensor_r"):
            await _runner(rec, suspension=suspension).home(spec, handle)

        assert rec.origins == []

    async def test_覆いは整列段を抜けたら外れる(self) -> None:
        spec = self._guarded_table().axis("y_axis")
        rec = self._recorder()
        suspension = SensorSuspension()
        handle = self._handle(spec, rec, suspension.wrap(rec.sensor_active))

        await _runner(rec, suspension=suspension).home(spec, handle)

        assert not suspension.is_suspended("sensor_r")
        assert not suspension.is_suspended("sensor_l")


_INTERFERING_CONFIG = {
    "axes": {
        "lift": {
            "unit": "mm",
            "command_unit": "deg",
            "tolerance": 1.0,
            "homing": {
                "sensor": "lift_sensor",
                "direction": 1,
                "search_distance": 30.0,
                "step": 1.0,
                "settle_s": 0.0,
                "release_distance": 10.0,
            },
        },
        "slide": {
            "unit": "mm",
            "command_unit": "deg",
            "tolerance": 1.0,
            "homing": {
                "sensor": "slide_sensor",
                "direction": -1,
                "search_distance": 30.0,
                "step": 1.0,
                "settle_s": 0.0,
            },
            "guard": {"requires": [{"axis": "lift", "at": "top"}]},
        },
    },
    "positions": {"lift": {"top": -20.0, "bottom": 0.0}, "slide": {"back": -5.0}},
}


class _RecordingHoming:
    """回された軸の名前だけを控える零点確定の代役。1 歩も動かさない。"""

    def __init__(self) -> None:
        self.homed: list[str] = []

    async def home(self, spec: AxisSpec, _handle: AxisHandle) -> float:
        self.homed.append(spec.name)
        return 0.0


class _OneStepMeasure:
    """測定の 1 歩だけを打つ代役。零点確定と同じ入口を通ることだけを見る。"""

    async def measure(self, spec: AxisSpec, handle: AxisHandle, **kwargs: object) -> None:
        homing = spec.homing
        assert homing is not None
        await handle.set_target_value(spec.to_commands(homing.direction * homing.step))


class _OneStepHoming:
    """探索の 1 歩だけを打つ代役。**指令の入口 (= 歯止め) を必ず通る。**"""

    async def home(self, spec: AxisSpec, handle: AxisHandle) -> float:
        homing = spec.homing
        assert homing is not None
        await handle.set_target_value(spec.to_commands(homing.direction * homing.step))
        return homing.step


def _interfering_group(table, *, lift_mm: float) -> MotorGroup:
    mgr = mock_can_manager()
    group = MotorGroup(sensor_active=lambda _name: False)
    for axis, value in (("lift", lift_mm), ("slide", 0.0)):
        motor = table.axis(axis).motors[0]
        driver = StubFeedbackDriver(motor.name, 1)
        driver.set_observed(position=motor.to_command(value))
        group.add(MotorHandle(motor.name, driver, mgr))
    group.bind_axis_state(
        build_axis_state_reader(table, group, court=lambda: Court.RED, is_stale=lambda _name: False)
    )
    return group


class TestHomingOrderFollowsInterference:
    """`requires` を書いた軸は、条件の軸を先に確定して寄せないと 1 歩も探索できない。

    零点確定は探索の 1 歩ごとに `AxisHandle.set_target_value` を通るので、干渉の
    判定がそのまま効く。**順序だけが理由で拒否される**ので、`run_homing` は
    参照先を先に回す。
    """

    def _table(self):
        return load_position_table(_INTERFERING_CONFIG, source="<test>")

    async def test_参照先を先に回す(self) -> None:
        table = self._table()
        runner = _RecordingHoming()

        await run_homing(
            runner,  # type: ignore[arg-type]
            table,
            _interfering_group(table, lift_mm=-20.0),
            court=Court.RED,
            axes=["slide", "lift"],
        )

        assert runner.homed == ["lift", "slide"]

    async def test_寄せてから確定すれば通る(self) -> None:
        table = self._table()
        group = _interfering_group(table, lift_mm=-20.0)

        await run_homing(
            _OneStepHoming(),  # type: ignore[arg-type]
            table,
            group,
            court=Court.RED,
            axes=["slide"],
        )

        assert group["slide"].target is not None

    async def test_寄せる前だと手当ての入った拒否が返る(self) -> None:
        """**会場ではこの 1 行が手順書になる。** 数値だけでは次に何をするか読めない。"""
        table = self._table()
        group = _interfering_group(table, lift_mm=-10.0)

        with pytest.raises(GuardViolation) as exc:
            await run_homing(
                _OneStepHoming(),  # type: ignore[arg-type]
                table,
                group,
                court=Court.RED,
                axes=["slide"],
            )

        assert "lift" in str(exc.value)
        assert "top" in str(exc.value)
        assert "寄せてください" in str(exc.value)
        assert group["slide"].target is None

    async def test_作動点測定も同じ条件が掛かる(self) -> None:
        """`measure_switch` は零点確定と同じ `AxisHandle` を通るので同じ歯止めに乗る。

        `docs/checks_and_health.md` の表がそう書いてあるので、経路が分かれたら
        ここが落ちる。
        """
        table = self._table()
        group = _interfering_group(table, lift_mm=-10.0)

        with pytest.raises(GuardViolation, match="寄せてください"):
            await measure_switch(
                _OneStepMeasure(),  # type: ignore[arg-type]
                table,
                group,
                court=Court.RED,
                axis="slide",
                direction=-1,
            )

    async def test_その場で止まれは条件の軸が読めなくても通る(self) -> None:
        """途絶で降りる直前の書き戻しが拒否されると、押し込む向きの古い目標が残る。

        `HomingRunner` が「その場で止まれ」を書くのは、機体まるごとの応答が
        怪しくなった瞬間である。条件の軸も同時に読めなくなるので、**そこで拒否
        されると止める指令だけが出ない**。
        """
        table = self._table()
        spec = table.axis("slide")
        rec = _Recorder()
        # 読み口を配線しない = 条件の軸は常に「読めていない」
        handle = _handle(spec, rec, start_value=-3.0, sensor_active=rec.sensor_active)

        await _runner(rec)._stop_here(spec, handle)

        assert rec.commands == [pytest.approx({"slide": spec.motors[0].to_command(-3.0)})]
