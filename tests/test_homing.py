from __future__ import annotations

import math
from collections.abc import Callable

import pytest

from lib.control.limit_guard import LimitGuard
from lib.drivers.generic import GenericDriver
from lib.sequence.homing import _FOLLOW_ATTEMPTS, _STALL_LIMIT, HomingError, HomingRunner
from lib.sequence.motors import AxisHandle, LimitClamp, MotorHandle
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
    """1 本のスイッチ。**現在値と接触回数を別々に模す。**

    模し方は 4 通りある:

    - ``active_after`` — 指定回数目の観測で ON になる。歩数だけを見るテスト向け
    - ``active_at_or_below`` — **実測位置が境界以下なら ON。** リミットスイッチの
      ON 区間を表す。区間には幅があるので「触れた状態から始めたときにどこを原点に
      するか」は回数では表せない (どこで始めても観測 1 回目から ON になり、
      離れたかどうかが位置に依存する)
    - ``active_band`` — **幅を持った ON 区間 (閉区間)。** 区間が ``step`` より狭いと、
      指令 1 回でそこを跨いでしまい**観測の瞬間にはもう OFF** になる。実機の
      ``rotate`` (step 2.0deg を約 18ms で通過) がこれで、現在値だけを見る探索は
      止まらない
    - ``chatter`` — 接点がばたついている状態。現在値は ON のまま (まだ ON 区間の
      中にいる) だが、接触回数のほうは ON を記録しなかった窓が混ざって増える

    **現在値 (``sensor_active``) と接触回数 (``sensor_contact_count``) を作り分けて
    いる**のは、探索と離脱が別のものを見るという設計を固定するため。位置モデルでは
    回数を「**前回見た位置から今の位置までの経路**が ON 区間と交わったら 1 増える」
    として作る —— センサの FEEDBACK は 100Hz で届くので、そのあいだに通り抜けた
    区間は落ちない。**回数は読んでも減らない** (実機のドライバと同じ) ので、
    読み手が何人いてもこのモデルは壊れない。

    ``motor`` を書いたスイッチは**そのモータ単独の実測位置**を見る (左右に 1 本ずつ
    付く軸のモデル)。書かなければ従来どおり軸位置 (平均) を見る。左右で別々の位置に
    反応することそのものが整列段の前提なので、平均で見るモデルでは整列段を模せない。
    """

    def __init__(
        self,
        *,
        motor: str | None = None,
        active_after: int | None = None,
        active_at_or_below: float | None = None,
        active_band: tuple[float, float] | None = None,
        chatter: bool = False,
        contacted_before: bool = False,
    ) -> None:
        self.motor = motor
        self._active_after = active_after
        self._active_at_or_below = active_at_or_below
        self._active_band = active_band
        self._chatter = chatter
        self._observations = 0
        #: 前回接触回数を見てから通った実測位置の範囲 (min, max)。
        #: **センサを読むたびに広げる** —— FEEDBACK は 100Hz で届くので、
        #: 離脱段のあいだに通った位置もここに載る (探索前に基準を取り直さないと残る)
        self._path: tuple[float, float] | None = None
        #: 前回見た時点で ON 区間の中にいたか。**立ち上がり (OFF→ON) だけを
        #: 数える**ために要る (触れ続けている間ずっと増える回数は意味を持たない)
        self._on = False
        #: チャタリングの応答 (False から始める。回数の増減で離脱を判定する実装は
        #: この 1 回目で「離脱完了」と読む)
        self._chatter_on = False
        #: 零点確定を始める前に既に数えた接触 (手動操縦でスイッチを跨いだ、
        #: 前回の零点確定で触れた、など)。**読んでも消えない**
        self._count = 1 if contacted_before else 0
        #: 見ている位置を返す口 (`_Recorder` が軸位置かモータ単独の位置を差す)
        self.position: Callable[[], float] = lambda: 0.0

    def _extend_path(self) -> tuple[float, float]:
        """今の実測位置を経路の窓へ加える (100Hz の FEEDBACK に相当)。"""
        current = self.position()
        low, high = self._path if self._path is not None else (current, current)
        self._path = (min(low, current), max(high, current))
        return self._path

    def active(self) -> bool:
        self._observations += 1
        self._extend_path()
        if self._chatter:
            return True  # まだ ON 区間の中にいる
        return self._on_now()

    def contact_count(self) -> int:
        """これまでに数えた接触の回数。**読んでも減らない。** 探索だけが見る。

        位置モデルでは前回見てから通った**経路**が ON 区間と交わったかで数える
        (100Hz の FEEDBACK は区間の通過を取りこぼさない)。経路は離脱段のあいだも
        伸びるので、**探索の前に基準を取り直さないと離脱中の接触が混ざる。**
        """
        self._observations += 1
        low, high = self._extend_path()
        # 次の窓は**この位置から**始まる (センサは動き続ける機構を 100Hz で
        # 見ているので、読み取りと読み取りのあいだに経路は途切れない)
        self._path = (self.position(), self.position())
        crossed = self._crossed(low, high)
        if crossed and not self._on:
            self._count += 1
        # 触れたままなら次は数えない (立ち上がりだけを数える)
        self._on = crossed and (True if self._chatter else self._on_now())
        return self._count

    def _crossed(self, low: float, high: float) -> bool:
        """前回見てから今までの**経路**が ON 区間と交わったか。"""
        if self._chatter:
            # 接点がばたついているので、ON を記録した窓としなかった窓が交互に並ぶ
            self._chatter_on = not self._chatter_on
            return self._chatter_on
        if self._active_band is not None:
            band_low, band_high = self._active_band
            return low <= band_high and high >= band_low
        if self._active_at_or_below is not None:
            return low <= self._active_at_or_below
        return self._on_now()

    def _on_now(self) -> bool:
        """**今の位置**が ON 区間の中か。"""
        if self._active_band is not None:
            low, high = self._active_band
            return low <= self.position() <= high
        if self._active_at_or_below is not None:
            return self.position() <= self._active_at_or_below
        if self._active_after is None:
            return False
        return self._observations > self._active_after


class _Recorder:
    """指令と原点確定を記録する。

    センサは既定では**名前を無視する 1 本**として振る舞う (単数形の軸のモデル)。
    ``sensors`` を渡すと**センサ名ごとに独立したモデル**になり、左右に 1 本ずつ
    付く軸を模せる。接触回数も名前ごとに独立するので、どちらが先に押されたかを
    整列段まで持ち越せていない実装はここで露見する。
    """

    def __init__(
        self,
        *,
        sensors: dict[str, _SensorModel] | None = None,
        active_after: int | None = None,
        active_at_or_below: float | None = None,
        active_band: tuple[float, float] | None = None,
        chatter: bool = False,
        contacted_before: bool = False,
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
            contacted_before=contacted_before,
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

    def sensor_contact_count(self, name: str) -> int:
        return self._sensor(name).contact_count()

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
    limit_guard: LimitClamp | None = None,
) -> AxisHandle:
    """実測位置を持つ軸ハンドル。

    ``limit_guard`` を渡すと**指令の入口のクランプ (層②) まで通る**。零点確定が
    保護を外し損ねていれば、探索の指令そのものが端で頭打ちになる。

    ``follows`` が True の機構は指令された位置へそのまま動く (追従する機構)。
    False は引っかかって 1mm も動かない機構で、**指令の積算ではなく実測で
    数えているか**を見るために要る (積算で数える実装では、動いていないのに
    探索距離を使い切って「到達しませんでした」で降りてしまう)。

    呼び出し可能なものを渡すと**指令のたびに追従の可否を問い直す** —— 探索は
    正常に終わるのに整列段だけが動かない機構 (遊びが無く片側駆動が成立しない機構)
    を模すために要る。

    指令が `_COMMAND_FUSE` 通を超えたらその場で落とす。歯止め (探索距離・整列段の
    移動量上限・停滞判定) を 1 つ外すと、追従する機構は**永久に動き続ける** ——
    それは実機で機構端まで走ることそのものだが、テストとしては無限ループになって
    落ちない。**歯止めを外したことが「テストが固まる」ではなく「落ちる」形で出る
    ようにする。**
    """
    mgr = mock_can_manager()
    drivers = {}
    handles = []
    for motor in spec.motors:
        driver = StubFeedbackDriver(motor.name, 1)
        driver.set_observed(position=motor.to_command(start_value))
        drivers[motor.name] = driver
        handles.append(MotorHandle(motor.name, driver, mgr))

    handle = AxisHandle(spec, handles, limit_guard=limit_guard)
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
        sensor_contact_count=recorder.sensor_contact_count,
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
        # センサを読むのは「開始前の確認」「探索前の基準取り直し」「1 歩ごと」の順。
        # 3 回目 = 1 歩目で ON になる (基準を取り直した後に立ち上がる必要がある)
        rec = _Recorder(active_after=2)

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
        # センサを読むのは「開始前の確認」「探索前の基準取り直し」「1 歩ごと」の順。
        # 4 回目 = 2 歩目で ON になる
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
        rec = _Recorder(active_after=2)  # 3 回目 = 1 歩目で ON

        await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.commands[0] == {"y_axis_r": -2.0, "y_axis_l": 2.0}

    async def test_左右ペアを同じフレームで指令する(self) -> None:
        table = _table()
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=2)  # 3 回目 = 1 歩目で ON

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

    async def test_一歩目で跨いだ接触も取りこぼさない(self) -> None:
        """**基準値は 1 歩目を出す前に取る。** 1 歩目の観測で取ると窓が 1 歩ぶん遅れる。

        探索の 1 歩目でいきなり区間を跨ぐ機構 (スイッチのすぐ手前から始めた場合)
        では、その接触は 1 歩目の観測でしか現れない。基準値をその観測時点で取ると
        自分の接触を基準へ吸い込んでしまい、以後は二度と区間を通らないので
        **探索距離いっぱいまで押し込んでから「到達しませんでした」で降りる。**
        """
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        # ON 区間 -0.55〜-0.45mm は 1 歩目 (0.0 → -1.0) の途中にある
        rec = _Recorder(active_band=(-0.55, -0.45))

        travelled = await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == ["y_axis"]
        assert travelled == pytest.approx(1.0)

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

    async def test_探索を始める前の接触を到達と読まない(self) -> None:
        """**接触回数は消えずに残る。** 見るのは基準値からの増加でなければならない。

        手動操縦でスイッチを跨いだ後や、前回の零点確定で触れた後に動作確認を
        起動すると、探索を始める時点で回数が既に 0 ではない。回数そのもの
        (0 かどうか) で判定すると 1 歩目の観測でいきなり到達と読み、
        **スイッチではなく探索開始位置が原点になる**。症状は「原点合わせを
        したのに位置がずれる」だけ。
        """
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        # 今は触れていない (区間は -5.0 以下) が、始める前の接触が回数に残っている
        rec = _Recorder(active_at_or_below=-5.0, contacted_before=True)

        await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.captured_at == pytest.approx([-5.0])


class TestSurvivesASecondReader:
    """**同じセンサを読む相手がいても取りこぼさない。**

    リミットスイッチ保護は零点確定と同じ接触情報を読むので、「読むと消える」ラッチ
    だと先に読んだ側が相手のぶんまで消し、探索が区間の通過を落とす。
    **ここだけは模型ではなく実ドライバを通す** —— 「読んでも減らない」性質は
    `GenericDriver` が持つものなので、模型のセンサで確かめても実装が消えたことに
    ならない。フレームは実機と同じ組み立て (`tests/feedback_frames.py`) で流し込む。
    """

    #: ON 区間 (幅 0.1mm)。1 歩 1.0mm なので観測位置はどれも区間の外にある
    _BAND = (-1.55, -1.45)

    async def test_保護層が毎周期読んでも探索は接触を検出する(self) -> None:
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        rec = _Recorder()
        sensor = GenericDriver("origin_sensor", 0x43)
        handle = _handle(spec, rec)
        commanded = handle.set_target_value
        previous = 0.0

        async def _travel(commands: dict[str, float]) -> None:
            """1 歩ぶん動かし、通った経路ぶんの FEEDBACK を実機と同じ形で流す。"""
            nonlocal previous
            await commanded(commands)
            current = rec.axis_position()
            low, high = min(previous, current), max(previous, current)
            if low <= self._BAND[1] and high >= self._BAND[0]:
                feed_generic(sensor, sensor=True)  # 区間を通った 1 通 (100Hz のうち)
            feed_generic(sensor, sensor=False)  # 観測の時点ではもう抜けている
            previous = current

        handle.set_target_value = _travel  # type: ignore[method-assign]

        # 2 人目の読み手 (この後の段で入るリミットスイッチ保護)。同じセンサを毎周期読む
        guard_reads: list[int] = []

        async def _sleep(seconds: float) -> None:
            guard_reads.append(sensor.sensor_contact_count)
            await rec.sleep(seconds)

        runner = HomingRunner(
            sensor_active=lambda _name: sensor.sensor_active,
            sensor_contact_count=lambda _name: sensor.sensor_contact_count,
            sensor_is_stale=rec.sensor_is_stale,
            motor_is_stale=rec.motor_is_stale,
            origin_capturable=rec.origin_capturable,
            capture_origin=rec.capture_origin,
            sleep=_sleep,
        )
        travelled = await runner.home(spec, handle)

        # 保護層は探索より先に接触を読んでいる (読んでいなければこのテストは無意味)
        assert max(guard_reads) >= 1
        # それでも探索は区間を跨いだ 2 歩目で止まる
        assert rec.origins == ["y_axis"]
        assert travelled == pytest.approx(2.0)


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


class TestSuspendsTheLimitGuard:
    """**`limits:` を書いた軸でも零点確定は成功する。**

    リミットスイッチ保護は「触れたらその向きへの指令を止める」常駐保護なので、
    効いたまま探索するとスイッチに触れた瞬間に自分の指令が実測位置へ引き戻され、
    原点へ到達できない。零点確定は走っているあいだ**そのセンサの保護だけ**を外す。

    **模型ではなく本物の `LimitGuard` を通す。** 「外している」ことは 2 つの
    実装の噛み合わせでしか成立しないので、片方を模型にすると噛み合っていなくても
    緑になる。保護は探索の待ち (`settle_s` ごと) と同じ頻度で判定させる。
    """

    #: この位置以下でスイッチが ON。1 歩 1.0mm なので 2 歩目で踏む
    _THRESHOLD = -1.5

    def _guard(self, spec: AxisSpec, rec: _Recorder, handle: AxisHandle) -> LimitGuard:
        return LimitGuard(
            [spec],
            sensor_active=rec.sensor_active,
            sensor_contact_count=rec.sensor_contact_count,
            sensor_is_stale=rec.sensor_is_stale,
            axis_handle=lambda name: handle if name == spec.name else None,
            interval_s=0.001,
        )

    def _table(self):
        return load_position_table(
            {
                "axes": {
                    "y_axis": {
                        "unit": "mm",
                        "command_unit": "deg",
                        "tolerance": 0.1,
                        "sync_tolerance": 100.0,
                        "homing": {
                            "sensor": "origin_sensor",
                            "direction": -1,
                            "search_distance": 10.0,
                            "step": 1.0,
                            "settle_s": 0.0,
                        },
                        # **零点確定と同じスイッチ**を指す保護。向きも同じ
                        # (食い違いは load_position_table が起動時に拒否する)
                        "limits": [{"sensor": "origin_sensor", "direction": -1}],
                        "motors": {"y_axis_r": {"scale": 2.0}, "y_axis_l": {"scale": -2.0}},
                    }
                },
                "positions": {"y_axis": {"home": 0.0}},
            },
            source="<test>",
        )

    async def test_保護を外さなければ発動する(self) -> None:
        """下のテストが「保護が眠っているから緑」ではないことの土台。"""
        spec = self._table().axis("y_axis")
        rec = _Recorder(active_at_or_below=self._THRESHOLD)
        handle = _handle(spec, rec)
        guard = self._guard(spec, rec, handle)

        await handle.set_target_value(spec.to_commands(-5.0))
        rec.commands.clear()
        await guard.step()

        assert dict(guard.latched) == {"y_axis": ("origin_sensor",)}
        assert _axis_commands(rec, "y_axis_r") == pytest.approx([-5.0])
        # 介入カウンタも動く。下の「零点確定では 0 件」が「カウンタが最初から
        # 動かないから緑」ではないことの土台
        assert guard.intervention("y_axis").count == 1

    async def test_保護を外した零点確定は成功する(self) -> None:
        spec = self._table().axis("y_axis")
        rec = _Recorder(active_at_or_below=self._THRESHOLD)
        handle = _handle(spec, rec)
        guard = self._guard(spec, rec, handle)

        latched: list[dict] = []
        guard_writes: list[int] = []
        base_sleep = rec.sleep

        async def _sleep(seconds: float) -> None:
            # 探索の 1 歩ごとに保護も判定させる (実機では 50Hz で回っている)
            before = len(rec.commands)
            await guard.step()
            guard_writes.append(len(rec.commands) - before)
            latched.append(dict(guard.latched))
            await base_sleep(seconds)

        rec.sleep = _sleep  # type: ignore[method-assign]

        runner = HomingRunner(
            sensor_active=rec.sensor_active,
            sensor_contact_count=rec.sensor_contact_count,
            sensor_is_stale=rec.sensor_is_stale,
            motor_is_stale=rec.motor_is_stale,
            origin_capturable=rec.origin_capturable,
            capture_origin=rec.capture_origin,
            suspend_sensors=guard.suspend_sensors,
            sleep=_sleep,
        )
        await runner.home(spec, handle)

        assert rec.origins == ["y_axis"]
        # **走っているあいだ 1 度もラッチしていない** (ラッチすれば目標が
        # 引き戻され、探索は同じ場所を踏み続ける)
        assert latched == [{} for _ in latched]
        assert sum(guard_writes) == 0

    async def test_零点確定では保護が1度も介入しない(self) -> None:
        """**両方の層を生かしたまま**走らせても、介入カウンタは 1 も増えない。

        カウンタは「保護が要求を曲げた」ことを数える口で、`move_to` はこれを
        指令の前後で比べてステップの成否を決める。零点確定で増える実装は、
        探索の指令が実際に曲げられている (原点がずれる) ということなので、
        「0 件であること」はそのまま「外し忘れが無いこと」の証明になる。
        """
        spec = self._table().axis("y_axis")
        rec = _Recorder(active_at_or_below=self._THRESHOLD)
        # 保護は自分を止めるために軸ハンドルを要り、軸ハンドルは保護を要るので
        # 生成順が循環する。本番 (`main._make_axis_handle_lookup`) と同じく遅延で解く
        holder: dict[str, AxisHandle] = {}
        guard = LimitGuard(
            [spec],
            sensor_active=rec.sensor_active,
            sensor_contact_count=rec.sensor_contact_count,
            sensor_is_stale=rec.sensor_is_stale,
            axis_handle=holder.get,
            interval_s=0.001,
        )
        handle = _handle(spec, rec, limit_guard=guard)
        holder[spec.name] = handle
        base_sleep = rec.sleep

        async def _sleep(seconds: float) -> None:
            await guard.step()
            await base_sleep(seconds)

        rec.sleep = _sleep  # type: ignore[method-assign]
        runner = HomingRunner(
            sensor_active=rec.sensor_active,
            sensor_contact_count=rec.sensor_contact_count,
            sensor_is_stale=rec.sensor_is_stale,
            motor_is_stale=rec.motor_is_stale,
            origin_capturable=rec.origin_capturable,
            capture_origin=rec.capture_origin,
            suspend_sensors=guard.suspend_sensors,
            sleep=_sleep,
        )

        await runner.home(spec, handle)

        assert rec.origins == ["y_axis"]
        assert guard.intervention("y_axis").count == 0

    async def test_終わったら保護が戻る(self) -> None:
        """戻し忘れると、その後の試合中ずっとこの軸の保護が死んだまま残る。"""
        spec = self._table().axis("y_axis")
        rec = _Recorder(active_at_or_below=self._THRESHOLD)
        handle = _handle(spec, rec)
        guard = self._guard(spec, rec, handle)

        runner = HomingRunner(
            sensor_active=rec.sensor_active,
            sensor_contact_count=rec.sensor_contact_count,
            sensor_is_stale=rec.sensor_is_stale,
            motor_is_stale=rec.motor_is_stale,
            origin_capturable=rec.origin_capturable,
            capture_origin=rec.capture_origin,
            suspend_sensors=guard.suspend_sensors,
            sleep=rec.sleep,
        )
        await runner.home(spec, handle)

        assert guard.is_suspended("origin_sensor") is False
        await guard.step()
        assert guard.blocked_directions("y_axis") == frozenset({-1.0})


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
