"""リミットスイッチによる零点確定 (lib/sequence/homing.py)。

設計と理由は docs/invariants.md「零点確定 (ホーミング) には止める仕組みが 3 つ要る」
「探索の起点は実測位置であって 0 ではない」「探索の到達判定はセンサのラッチで見る」。

見るのは**止まること**が最優先 —— 配線が抜けたセンサは「いつまでも当たらない」形で
しか現れず、緊急停止は操縦者が押さないと効かないので無人の歯止めが要る。歯止めを
実測位置で数えるのは、指令の積算だと手動で +15mm へ動かした後の 1 歩目が
search_distance を 1mm も消費しないため。
"""

from __future__ import annotations

import math

import pytest

from lib.sequence.homing import _FOLLOW_ATTEMPTS, _STALL_LIMIT, HomingError, HomingRunner
from lib.sequence.motors import AxisHandle, MotorHandle
from lib.sequence.positions import AxisSpec, load_position_table
from tests.fake_can import mock_can_manager
from tests.fake_drivers import StubFeedbackDriver


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
    """``tolerance`` と ``homing.step`` が一致する条件を作るためのテーブル。

    1 歩ぶんの追従待ちに ``tolerance`` を流用すると、**1 歩も動いていない実測
    (指令は実測 + step なので差はちょうど step)** が追従完了に化ける。壊れる条件は
    ``step <= tolerance`` で、**その境界が両者の一致点**にあるので 2.0deg で揃える。

    **実機の rotate (step 1.0deg / tolerance 2.0deg) とは意図して違う。** 実機の 2 つは
    別々の理由で決まる値 (step は静止摩擦の下限、tolerance は到達の許容差) なので、
    追随させると境界が機構側の都合で動き、検証したい条件そのものが消える。
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


class _Recorder:
    """指令と原点確定を記録する。

    センサの模し方は 4 通り:

    - ``active_after`` — 指定回数目の観測で ON。歩数だけを見るテスト向け
    - ``active_at_or_below`` — **実測位置が境界以下なら ON。** ON 区間には幅があるので
      「触れた状態から始めたときどこを原点にするか」は回数では表せない
    - ``active_band`` — **幅を持った ON 区間 (閉区間)。** ``step`` より狭いと指令 1 回で
      跨いでしまい**観測の瞬間にはもう OFF** になる (実機の ``rotate`` がこれ)
    - ``chatter`` — 接点のばたつき。現在値は ON のままだが、ラッチには ON を記録しな
      かった窓が混ざる

    **現在値 (``sensor_active``) とラッチ (``sensor_latched``) を作り分けている**のは、
    探索と離脱が別のものを見る設計を固定するため。ラッチは「**前回読んだ位置から今の
    位置までの経路**が ON 区間と交わったか」として作る (FEEDBACK は 100Hz なので通り
    抜けた区間は落ちない)。
    """

    def __init__(
        self,
        *,
        active_after: int | None = None,
        active_at_or_below: float | None = None,
        active_band: tuple[float, float] | None = None,
        chatter: bool = False,
        prelatched: bool = False,
        stale: bool = False,
        motor_stale: bool = False,
        capturable: bool = True,
    ) -> None:
        self.commands: list[dict[str, float]] = []
        self.origins: list[str] = []
        #: 原点を確定した瞬間の実測位置 [軸の unit]。確定位置そのものを見るために要る
        self.captured_at: list[float] = []
        #: `_handle` が組んだモータ名 → ドライバ (実測位置の差し替え口)
        self.drivers: dict[str, StubFeedbackDriver] = {}
        #: `_handle` が組んだ軸。逆換算をテストへ書き写さないために持つ
        self.spec: AxisSpec | None = None
        self.sleeps = 0
        self._active_after = active_after
        self._active_at_or_below = active_at_or_below
        self._active_band = active_band
        self._chatter = chatter
        self._observations = 0
        #: 前回ラッチを読んでから通った実測位置の範囲 (min, max)。
        #: **センサを読むたびに広げる** —— FEEDBACK は 100Hz で届くので、
        #: 離脱段のあいだに通った位置もラッチには載る (探索前に捨てないと残る)
        self._path: tuple[float, float] | None = None
        #: チャタリングのラッチ応答 (False から始める。ラッチで離脱を判定する実装は
        #: この 1 回目で「離脱完了」と読む)
        self._chatter_latch = False
        #: 零点確定を始める前から溜まっているラッチ (手動操縦でスイッチを跨いだ、
        #: 前回の零点確定で触れた、など)。1 回読めば消える
        self._prelatched = prelatched
        self._stale = stale
        self._motor_stale = motor_stale
        self._capturable = capturable

    def axis_position(self) -> float:
        """実測の軸位置。逆換算は `AxisSpec` に委ねる (scale をテストへ書き写さない)。"""
        assert self.spec is not None
        return self.spec.to_value(
            {name: driver.feedback_position() for name, driver in self.drivers.items()}
        )

    def _extend_path(self) -> tuple[float, float]:
        """今の実測位置をラッチの窓へ加える (100Hz の FEEDBACK に相当)。"""
        current = self.axis_position() if self.drivers else 0.0
        low, high = self._path if self._path is not None else (current, current)
        self._path = (min(low, current), max(high, current))
        return self._path

    def sensor_active(self, _name: str) -> bool:
        """**今**接触しているか。離脱の判定と「既に触れているか」がこれを見る。"""
        self._observations += 1
        self._extend_path()
        if self._chatter:
            return True  # まだ ON 区間の中にいる
        if self._active_band is not None:
            low, high = self._active_band
            return low <= self.axis_position() <= high
        if self._active_at_or_below is not None:
            return self.axis_position() <= self._active_at_or_below
        if self._active_after is None:
            return False
        return self._observations > self._active_after

    def sensor_latched(self, _name: str) -> bool:
        """前回読んでから一度でも接触したか。**読むと消える。** 探索だけが見る。

        位置モデルでは前回読んでから通った**経路**が ON 区間と交わったかを答える。
        経路は離脱段のあいだも伸びるので、**探索の前に捨てないと離脱中の接触が残る。**
        """
        self._observations += 1
        low, high = self._extend_path()
        # 読んだら消える。次の窓は**この位置から**始まる (センサは動き続ける機構を
        # 100Hz で見ているので、読み取りと読み取りのあいだに経路は途切れない)
        current = self.axis_position() if self.drivers else 0.0
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

    def sensor_is_stale(self, _name: str) -> bool:
        return self._stale

    def motor_is_stale(self, _name: str) -> bool:
        return self._motor_stale

    def origin_capturable(self, _axis: str) -> bool:
        return self._capturable

    async def capture_origin(self, axis: str) -> None:
        """原点確定は非同期。CAN の往復 (EDULITE 05 の SET_ZERO) を挟む実装がある。"""
        self.origins.append(axis)
        if self.drivers:
            self.captured_at.append(self.axis_position())

    async def sleep(self, _seconds: float) -> None:
        self.sleeps += 1


def _handle(
    spec: AxisSpec,
    recorder: _Recorder,
    *,
    start_value: float = 0.0,
    follows: bool = True,
) -> AxisHandle:
    """実測位置を持つ軸ハンドル。

    ``follows=False`` は引っかかって 1mm も動かない機構で、**指令の積算ではなく実測で
    数えているか**を見るために要る (積算だと、動いていないのに探索距離を使い切って
    「到達しませんでした」で降りる)。
    """
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
        if follows:
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
    """1 回の待ち (`settle_s` ごとの再確認) につき ``per_tick`` [軸の unit] だけ指令へ寄る機構。

    実機の機構は 1 歩ぶんを待ち 1 回では動き切らない。**その途中の実測を「進まなかった」
    と数えると正常な機構が停滞判定で落ちる** —— 待っているかどうかは、この「歩幅の
    半分にも満たない 1 回目」を通せるかにしか現れない。

    `sleep` を差し替えるので、**`_runner` はこの関数の後に組み立てること**。
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
    """**1 歩目は現在位置からの 1 step。** 0 起点の絶対値指令にしてはならない。"""

    async def test_一歩目は実測位置から一歩ぶんだけ動かす(self) -> None:
        table = _table(direction=-1, step=0.5, search_distance=30.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=1)

        # 手動ジョグで +15mm へ動かした後に動作確認を起動した状態
        await _runner(rec).home(spec, _handle(spec, rec, start_value=15.0))

        # 14.5mm ぶんの指令 (scale は右 +2 / 左 -2)。
        # 0 起点だと -0.5mm = 15.5mm の引き戻しになる
        assert rec.commands[0] == {"y_axis_r": 29.0, "y_axis_l": -29.0}

    async def test_探索距離は実測の移動量で数える(self) -> None:
        """指令の積算で数えると、動いていない機構でも上限を使い切ってしまう。"""
        table = _table(search_distance=5.0, step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder()

        # 引っかかって 1mm も動かない機構。実測は 1 歩も進まない
        with pytest.raises(HomingError, match="動きません"):
            await _runner(rec).home(spec, _handle(spec, rec, follows=False))

        # 「5mm 動かしても到達しませんでした」ではない (実際には動いていない)
        assert rec.origins == []

    async def test_進まない機構は数歩で降りる(self) -> None:
        """指令を実測へ再アンカーしているので、進まない機構は永久に上限へ届かない。"""
        table = _table(search_distance=100.0, step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder()

        with pytest.raises(HomingError, match="動きません"):
            await _runner(rec).home(spec, _handle(spec, rec, follows=False))

        # 100 歩ぶん押し続けたりしない
        assert len(rec.commands) <= 5

    async def test_指令は常に実測から一歩ぶんしか離れない(self) -> None:
        """追従が遅い機構でも指令だけが先行しない (先行すると電流上限まで押す)。"""
        table = _table(search_distance=10.0, step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder()
        # 指令の半分しか動かない機構 (追従が遅い機構の代わり)
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

        # 人間の単位で 1 step = 1mm、指令単位では 2deg。指令を出した瞬間の
        # 実測との差が 1 step ぶんを超えない (超えると位置制御ループが電流上限まで押す)
        assert deviations
        assert max(deviations) <= 2.0 + 1e-9


class TestWaitsForEachStep:
    """**1 歩ぶんの追従を待つ許容差に `spec.tolerance` を使ってはならない。**

    指令は「実測 + step」で組むので、1 歩も動いていないときの差はちょうど `step`。
    `step <= tolerance` の軸では**動く前に必ず追従完了**になって待ちが消え、**正常に
    動いている機構が 0.3 秒で HomingError になる** (実機の rotate で発生)。

    待つ側と数える側は同じ「歩幅の半分」を見る (片方だけ別の量にすると、待ちすぎるか
    待たないかのどちらかになる)。
    """

    async def test_到達許容差が歩幅以上でも一歩ぶんの追従を待つ(self) -> None:
        """`tolerance` == `step` == 2.0deg。`step <= tolerance` の境界。"""
        spec = _rotate_table().axis("rotate")
        rec = _Recorder()

        with pytest.raises(HomingError, match="動きません"):
            await _runner(rec).home(spec, _handle(spec, rec, follows=False))

        # 1 歩につき _FOLLOW_ATTEMPTS 回まで待ってから「進まなかった」と数えている。
        # tolerance (2.0deg) で判定すると 1 回目の確認で追従完了になり、
        # 待ちの回数が歩数と同じ (_STALL_LIMIT) まで落ちる
        assert rec.sleeps == _STALL_LIMIT * _FOLLOW_ATTEMPTS

    async def test_ゆっくり追従する機構は停滞と数えず次の歩へ進む(self) -> None:
        """1 回の待ちでは歩幅の半分も動かない機構でも、待てば 1 歩ぶん進む。"""
        spec = _rotate_table().axis("rotate")
        # 位置 <= -3.0deg が ON 区間。1 歩 2.0deg なので数歩かかる
        rec = _Recorder(active_at_or_below=-3.0)
        # 1 回の待ちで 0.6deg (歩幅 2.0 の半分に満たない) しか動かない機構
        handle = _slow_handle(spec, rec, per_tick=0.6)

        travelled = await _runner(rec).home(spec, handle)

        assert rec.origins == ["rotate"]
        assert rec.captured_at == pytest.approx([-3.0])
        assert travelled == pytest.approx(3.0)
        # 待ち切れずに _FOLLOW_ATTEMPTS を使い切ってはいない (使い切ると 1 歩あたり
        # settle_s * 5 = 0.25 秒かかり、90 歩の探索が 20 秒を超える)
        assert rec.sleeps < _FOLLOW_ATTEMPTS * len(rec.commands)

    async def test_離脱でも一歩ぶんの追従を待つ(self) -> None:
        """離脱と探索は同じ `_seek` を通る。片方だけ待つ実装を作らない。

        触れた状態から始めると離脱が先に走るので、待ちが消えていれば**探索へ
        入る前に**同じ形で落ちる (実機では離脱が 1 歩で終わったため露見しなかった)。
        """
        spec = _rotate_table().axis("rotate")
        rec = _Recorder(active_after=0)  # 最初の観測から常に ON = 離脱段が続く
        handle = _slow_handle(spec, rec, per_tick=0.6)

        with pytest.raises(HomingError, match="離せませんでした"):
            await _runner(rec).home(spec, handle)

        # 1 歩ごとに複数回待っている (待っていなければ停滞判定で「動きません」になる)
        assert rec.sleeps > len(rec.commands)


class TestStops:
    async def test_探索距離を超えたら止める(self) -> None:
        """**唯一の無人の歯止め。** 外すと機構端まで押し込み続ける。"""
        table = _table(search_distance=5.0, step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder()  # 一度も当たらない

        with pytest.raises(HomingError, match="到達しませんでした"):
            await _runner(rec).home(spec, _handle(spec, rec))

        # 5mm / 1mm = 5 歩で打ち切る (無限には動かさない)
        assert len(rec.commands) == 5
        assert rec.origins == []

    async def test_センサが途絶していたら一歩も動かさない(self) -> None:
        """死んだセンサは「いつまでも当たらない」形でしか現れない。

        探索距離いっぱいまで押し込んでから気付くのでは遅い。
        """
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
        """センサまで押し込んでから「確定できません」で降りては、動かした意味が無い。"""
        table = _table()
        spec = table.axis("y_axis")
        rec = _Recorder(capturable=False)

        with pytest.raises(HomingError, match="原点を確定する手段がありません"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.commands == []
        assert rec.origins == []

    async def test_失敗したら原点を確定しない(self) -> None:
        """当たっていないのに原点を切ると、以後の全ステップが同じだけずれる。"""
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
        # センサを読むのは「開始前の確認」「探索前のラッチ捨て」「1 歩ごと」の順。
        # 4 回目 = 2 歩目で ON になる
        rec = _Recorder(active_after=3)

        travelled = await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == ["y_axis"]
        assert travelled == pytest.approx(2.0)

    async def test_戻り値は実測の移動量(self) -> None:
        """指令の積算ではない。追従しきっていない機構では両者がずれる。"""
        table = _table(search_distance=10.0, step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=3)  # 2 歩目で ON (上と同じ数え方)

        travelled = await _runner(rec).home(spec, _handle(spec, rec, start_value=15.0))

        # 15mm から 2 歩 (負方向) 動いたので実測は 13mm。移動量は 2mm
        assert travelled == pytest.approx(2.0)

    async def test_探索方向を符号で表す(self) -> None:
        table = _table(direction=-1, step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=1)

        await _runner(rec).home(spec, _handle(spec, rec))

        # 人間の単位で -1mm。scale は右 +2 / 左 -2 なので指令は ∓2deg
        # (2 通目は検出位置へ止め直す指令。追従しきった機構では同じ値になる)
        assert rec.commands[0] == {"y_axis_r": -2.0, "y_axis_l": 2.0}

    async def test_左右ペアを同じフレームで指令する(self) -> None:
        """別々の時刻に動かすとその場で機構が壊れる。"""
        table = _table()
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=1)

        await _runner(rec).home(spec, _handle(spec, rec))

        # どの指令にも左右が揃っていること (2 回に分かれていない)
        assert rec.commands
        assert all(set(cmd) == {"y_axis_r", "y_axis_l"} for cmd in rec.commands)


class TestDoesNotMissTheContact:
    """**探索の到達判定はラッチで見る。「今 ON か」では取りこぼす。**

    ON 区間が `step` より狭いと指令 1 回でそこを跨ぎ、`settle_s` 後の観測時にはもう
    OFF になっている。実機の `rotate` (step 2.0deg を約 18ms で通過 / settle_s 50ms) が
    これで、**スイッチに当たっているのに探索が止まらず可動範囲の端を越えて回り続けた**。
    FEEDBACK は 100Hz で届くので、受信のたびにラッチすれば通過を取りこぼさない。
    """

    async def test_一歩の途中で通り過ぎた接触を検出する(self) -> None:
        """観測の瞬間には ON が 1 度も見えない構成。現在値だけを見る実装は止まらない。"""
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        # ON 区間は -1.55〜-1.45mm (幅 0.1mm)。1 歩 1.0mm なので観測位置
        # (-1.0, -2.0, -3.0, ...) はどれも区間の外を通る
        rec = _Recorder(active_band=(-1.55, -1.45))

        travelled = await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == ["y_axis"]
        # 区間を跨いだ 2 歩目で止まる (現在値だけでは 10mm 動いて「到達しませんでした」)
        assert travelled == pytest.approx(2.0)
        assert rec.captured_at == pytest.approx([-2.0])

    async def test_検出したらその場へ止め直す(self) -> None:
        """最後に送った「実測 + step」を残すと、原点確定まで越えた先へ向かい続ける。

        `capture_origin` が届くまでの時間ぶん機構は破損側へ押し込まれる。検出位置を
        目標に送り直せば、行き過ぎは「検出の遅れ」のぶんだけに縮む。
        """
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=-1.5)
        # 1 回の待ちで 0.6mm しか動かない機構。検出した実測位置と、そのとき生きて
        # いる指令 (実測 + step) が別の値になる構成でないと、この違いは現れない
        handle = _slow_handle(spec, rec, per_tick=0.6)

        await _runner(rec).home(spec, handle)

        axis_commands = [cmd["y_axis_r"] / 2.0 for cmd in rec.commands]
        assert rec.captured_at == pytest.approx([-1.8])
        # 最後の指令は検出位置そのもの。「実測 + step」(-1.2 - 1.0 = -2.2) を
        # 残したままにしない —— 残すと原点確定が届くまで越えた先へ向かい続ける
        assert axis_commands[-1] == pytest.approx(-1.8)
        assert axis_commands[-2] == pytest.approx(-2.2)

    async def test_探索の前に古いラッチを捨てる(self) -> None:
        """**ラッチは黙って溜まる。** 探索を始める前に 1 度捨てないと窓が広すぎる。

        手動操縦でスイッチを跨いだ後や前回の零点確定で触れた後に起動すると、探索開始
        時点で既に ON が溜まっている。捨てないと 1 歩目でいきなり到達と読み、**スイッチ
        ではなく探索開始位置が原点になる** (症状は「原点合わせをしたのに位置がずれる」)。
        """
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        # 今は触れていない (区間は -5.0 以下) が、始める前の接触がラッチに残っている
        rec = _Recorder(active_at_or_below=-5.0, prelatched=True)

        await _runner(rec).home(spec, _handle(spec, rec))

        # 1 歩目 (-1.0) ではなく、実際にスイッチへ当たる -5.0 で確定する
        assert rec.captured_at == pytest.approx([-5.0])


class TestReleasesBeforeSeeking:
    """**触れた状態から始めたら、一度離れてから寄せ直す。**

    ON 区間には幅があるので、触れたその場を原点にすると「区間のどこで始めたか」が
    そのまま原点のばらつき (区間幅ぶん) になる。症状は「原点合わせをしたのに位置が
    ずれる」だけで、始めた位置が毎回違うので再現もしない。

    離脱は**探索と逆向き**なので、機構端で始まったときに押し込まない性質は保たれる。
    """

    async def test_触れた状態から始めたら離れてから寄せ直す(self) -> None:
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        # 位置 <= -1.0 が ON 区間。-3.0 はその奥 (区間へ深く入り込んだ状態)
        rec = _Recorder(active_at_or_below=-1.0)

        await _runner(rec).home(spec, _handle(spec, rec, start_value=-3.0))

        # 軸の単位に戻した指令列 (右モータの scale は +2.0)
        axis_commands = [cmd["y_axis_r"] / 2.0 for cmd in rec.commands]
        # 離脱 (+ 方向) で区間を出てから、探索 (- 方向) で入口へ寄せ直す。
        # 0.0 と -1.0 が 2 通ずつ並ぶのは、検出したその位置へ止め直す指令が
        # 続くため (追従しきった機構では直前の指令と同じ値になる)
        assert axis_commands == pytest.approx([-2.0, -1.0, 0.0, 0.0, -1.0, -1.0])
        assert rec.origins == ["y_axis"]
        # その場 (-3.0) ではなく区間の入口で確定している
        assert rec.captured_at == pytest.approx([-1.0])

    @pytest.mark.parametrize("start", [-1.2, -2.0, -3.0, -4.5])
    async def test_区間のどこで始めても確定位置は入口から一歩以内(self, start: float) -> None:
        """**これがこの処理の目的そのもの。** 離脱しない実装ではここが区間幅ぶん開く。"""
        table = _table(direction=-1, step=1.0, search_distance=10.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=-1.0)

        await _runner(rec).home(spec, _handle(spec, rec, start_value=start))

        assert rec.captured_at[0] == pytest.approx(-1.0, abs=1.0)  # 入口 -1.0 から step 以内

    async def test_離脱はラッチではなく現在値で判定する(self) -> None:
        """**探索と離脱は対称に見えて非対称。揃えると離脱が壊れる。**

        同じ形を離脱へ持ち込むと「一度でも OFF になったか」で抜けることになり、接点が
        ばたついている間は **まだ ON 区間の中にいるのに離脱完了と読む** (離脱そのものの
        目的が消える)。取りこぼしの向きも非対称で、探索の取りこぼしは機構の破損側へ
        進み続けるのに対し、離脱の取りこぼしは「余計に離れる」だけで次の探索が寄せ直す。
        """
        table = _table(step=1.0)
        spec = table.axis("y_axis")
        # 現在値は常に ON (区間から出ていない)。ラッチには OFF の窓が混ざる
        rec = _Recorder(chatter=True)

        with pytest.raises(HomingError, match="離せませんでした"):
            await _runner(rec).home(spec, _handle(spec, rec))

        # ラッチで離脱を判定する実装は 1 歩目で離脱完了と読み、そのまま原点を切る
        assert rec.origins == []

    async def test_離れられなければ原点を確定せず降りる(self) -> None:
        """接点が固着したセンサは「いつまでも OFF にならない」形でしか現れない。"""
        table = _table(step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=0)  # 最初の観測から常に ON

        with pytest.raises(HomingError, match="離せませんでした"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == []

    async def test_離脱が進まなくなったら降りる(self) -> None:
        """逆向きの機構端に当たると、離脱は「動かないのに OFF にならない」形になる。

        歩数上限だけでは `step * 20` ぶん押し当て続けてから降りることになるので、
        停滞判定 (探索と共有する `_seek` の 1 本) が離脱にも効いている必要がある。
        """
        table = _table(step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=0)  # 最初の観測から常に ON

        with pytest.raises(HomingError, match="動きません"):
            # 引っかかって 1mm も動かない機構 (離脱の向きの機構端に当たった状態)
            await _runner(rec).home(spec, _handle(spec, rec, follows=False))

        # 停滞判定 (3 歩) で降りる。歩数上限 (20 歩) まで押し当て続けない
        assert len(rec.commands) <= _STALL_LIMIT + 1
        assert rec.origins == []

    async def test_極性の取り違えを疑わせる(self) -> None:
        """**極性が逆だとどこへ動かしても ON のまま**になる。実際に起こりうる
        設定ミスなので、接点の固着だけを疑わせるメッセージでは切り分けられない。
        """
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

        # 上限は step の定数倍。search_distance (500) ぶん動いてはいない
        assert len(rec.commands) < 30


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
