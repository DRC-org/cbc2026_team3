"""リミットスイッチによる零点確定 (lib/sequence/homing.py)。

**「当たるまで動かす」動作なので、止まることを最優先で見る。** 配線が抜けた
センサは「いつまでも当たらない」形でしか現れず、歯止めが無いと機構端まで
押し込み続ける。緊急停止は操縦者が押さないと効かないので、無人の歯止めが要る。

**歯止めは実測位置で数える。** 指令の積算で数えると、指令が実位置から離れている
ぶんがそのまま上限を素通りする —— 手動で +15mm へ動かした後の 1 歩目が
「15.5mm を一気に引き戻す指令」になり、その移動を search_distance が 1mm も
消費しない、という形で実際に踏む。
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
    (指令は実測 + step で組むので、差はちょうど step)** がそのまま追従完了に化ける。
    壊れる条件は ``step <= tolerance`` で、**その境界が両者の一致点**にあるので、
    ここは 2.0deg で揃えてある。

    **実機の rotate (config/main_hand_positions.yaml) とは意図して違う**
    (実機は step 1.0deg / tolerance 2.0deg)。実機の 2 つは別々の理由で決まる値
    —— step は静止摩擦の下限 (0.5deg では 1 歩も動かない)、tolerance は到達の
    許容差 —— なので、実機へ追随させるとこの境界は機構側の都合で動く。
    離れた時点で検証したい条件そのものが消え、追従待ちを壊しても緑で通る。
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

    センサの模し方は 4 通りある:

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
      中にいる) だが、ラッチには ON を記録しなかった窓が混ざる
    - ``fails_open_above`` — **この位置より戻ったところで接点が開いたまま固着する。**
      当てた後に二度と ON を報告しなくなるスイッチで、**寄せ直しの段が新しい探索
      距離を貰っていないか**を見るために要る (位置だけで決まるセンサ模型では、
      粗探索が当てた区間へ細探索も必ず当たるので、上限の食い違いが結果に出ない)

    **現在値 (``sensor_active``) とラッチ (``sensor_latched``) を作り分けている**のは、
    探索と離脱が別のものを見るという設計を固定するため。位置モデルではラッチを
    「**前回読んだ位置から今の位置までの経路**が ON 区間と交わったか」として作る ——
    センサの FEEDBACK は 100Hz で届くので、そのあいだに通り抜けた区間は落ちない。
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
        stale_after_commands: int | None = None,
        motor_stale_after_commands: int | None = None,
        unreadable_after_commands: int | None = None,
        fails_open_above: float | None = None,
        latched_supported: bool = True,
        energized: bool | None = True,
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
        #: 指令をこの本数出した後に途絶する (探索の**途中**での途絶を作る)。
        #: 途絶したセンサ・モータの読み口は最後に届いた値を返し続けるので、
        #: 途中の途絶は「いつまでも当たらない」形にしかならない
        self._stale_after_commands = stale_after_commands
        self._motor_stale_after_commands = motor_stale_after_commands
        #: 指令をこの本数出した後、**現在値だけ**が読めなくなる (鮮度の判定は
        #: False のまま)。三値の `None` を `False` へ丸めていないかを、他の層
        #: (`_feedback_lost`) を持たない条件で見るために要る
        self._unreadable_after_commands = unreadable_after_commands
        self._fails_open_above = fails_open_above
        #: 接点が開いたまま固着したか (`fails_open_above` を戻ったら二度と戻らない)
        self._failed_open = False
        self._reached_below = False
        self._latched_supported = latched_supported
        self._energized = energized
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
        if self._fails_open_above is not None:
            # **一度当ててから戻ったところで開き、二度と戻らない** (接点の固着)。
            # 「当たる前から死んでいる」模型にすると粗探索そのものが失敗するので、
            # 一度 ON 区間へ入ったことを条件にする
            if current <= self._fails_open_above:
                self._reached_below = True
            elif self._reached_below:
                self._failed_open = True
        low, high = self._path if self._path is not None else (current, current)
        self._path = (min(low, current), max(high, current))
        return self._path

    def sensor_active(self, _name: str) -> bool | None:
        """**今**接触しているか。離脱の判定と「既に触れているか」がこれを見る。

        **三値。** 途絶している間は `None` (読めていない) を返す —— 実機の読み口
        (`main._make_sensor_reader`) がそうなので、`False` を返す模型にすると
        「途絶したセンサが離脱完了に化ける」経路をテストが作れない。
        """
        self._observations += 1
        self._extend_path()
        if self.sensor_is_stale(_name):
            return None
        if (
            self._unreadable_after_commands is not None
            and len(self.commands) >= self._unreadable_after_commands
        ):
            return None
        if self._failed_open:
            return False
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

    def sensor_latched(self, _name: str) -> bool | None:
        """前回読んでから一度でも接触したか。**読むと消える。** 探索だけが見る。

        位置モデルでは前回読んでから通った**経路**が ON 区間と交わったかを答える
        (100Hz の FEEDBACK は区間の通過を取りこぼさない)。経路は離脱段のあいだも
        伸びるので、**探索の前に捨てないと離脱中の接触が残る。**
        """
        self._observations += 1
        low, high = self._extend_path()
        # 読んだら消える。次の窓は**この位置から**始まる (センサは動き続ける機構を
        # 100Hz で見ているので、読み取りと読み取りのあいだに経路は途切れない)
        current = self.axis_position() if self.drivers else 0.0
        self._path = (current, current)
        if not self._latched_supported:
            # ラッチを提供しないドライバ。**現在値へ落とさない** (落とすと
            # 「ON 区間が step より狭い機構で取りこぼす探索」へ黙って戻る)
            return None
        if self._failed_open:
            return False
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
        if self._stale_after_commands is not None:
            return len(self.commands) >= self._stale_after_commands
        return self._stale

    def motor_is_stale(self, _name: str) -> bool:
        if self._motor_stale_after_commands is not None:
            return len(self.commands) >= self._motor_stale_after_commands
        return self._motor_stale

    def motor_is_energized(self, _name: str) -> bool | None:
        """励磁されているか。**三値** (`None` = 報告しないドライバ = M3508)。"""
        return self._energized

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

    ``follows`` が True の機構は指令された位置へそのまま動く (追従する機構)。
    False は引っかかって 1mm も動かない機構で、**指令の積算ではなく実測で
    数えているか**を見るために要る (積算で数える実装では、動いていないのに
    探索距離を使い切って「到達しませんでした」で降りてしまう)。
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

    実機の機構は 1 歩ぶんを待ち 1 回では動き切らない (静止摩擦を超え直し、
    加速してから止まる)。**その途中の実測を「進まなかった」と数えると、正常に
    動いている機構が停滞判定で落ちる** —— 追従を待っているかどうかは、この
    「歩幅の半分にも満たない 1 回目」を通せるかにしか現れない。

    `sleep` を差し替えるので、**`_runner` はこの関数の後に組み立てること**
    (`HomingRunner` は生成時に `recorder.sleep` を掴む)。
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
        motor_is_energized=recorder.motor_is_energized,
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

    指令は「実測 + step」で組むので、1 歩も動いていないときの実測と指令の差は
    ちょうど `step`。`step <= tolerance` の軸では**動く前に必ず追従完了**になり、
    待ちが丸ごと消える。そのとき停滞判定が数えるのは「待ったのに進まなかった」
    ではなく「待っていないので進んでいない」で、**正常に動いている機構が
    0.3 秒で HomingError になる** (実機の rotate で発生)。

    待つ側と数える側は同じ「歩幅の半分」を見る。片方だけを別の量にすると、
    どちらの向きにも噛み合わない (待ちすぎるか、待たないか)。
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


class TestStopsWhenFeedbackIsLostMidSearch:
    """**事前確認だけでは「探索を始めた後の途絶」に気付けない。**

    センサの読み口 (`main._make_sensor_reader` / `GenericDriver.sensor_active`) は
    途絶しても最後に届いたフラグを返し続けるので、探索中にサーボ基板が再起動したり
    `can_generic` が瞬断したりしても、症状は「いつまでも当たらない」だけになる。
    そのまま `search_distance` いっぱいまで進むか、止めるのは停滞判定 (`step/2`
    未満が 3 歩連続) しかない —— ドライバ内蔵の位置ループが端を押し続けている
    状態で、さらに 3 歩ぶん押し込むことになる。
    """

    async def test_探索の途中でセンサが途絶したら止める(self) -> None:
        table = _table(direction=-1, step=1.0, search_distance=50.0)
        spec = table.axis("y_axis")
        # 2 歩目まで進んだところで途絶する。センサは一度も ON にならない
        rec = _Recorder(stale_after_commands=2)

        with pytest.raises(HomingError, match="応答していません"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == []
        # 探索距離 (50mm) を使い切るまで進まない
        assert len(rec.commands) <= 3

    async def test_探索の途中で軸のフィードバックが途絶したら止める(self) -> None:
        """実測が更新されなくなると、再アンカーが同じ位置を指し続ける。

        指令は「最後に読めた実測 + step」で固定され、機構は動いているのに
        位置が進まないので、停滞判定が拾うまで 3 歩ぶん押し込む。
        """
        table = _table(direction=-1, step=1.0, search_distance=50.0)
        spec = table.axis("y_axis")
        rec = _Recorder(motor_stale_after_commands=2)

        with pytest.raises(HomingError, match="現在位置を読めません"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == []
        assert len(rec.commands) <= 3

    async def test_途絶で降りる前にその場の実測位置を目標へ送り直す(self) -> None:
        """**最後に送った「実測 + step」が生きたままだと押し続ける。**

        ドライバ内蔵の位置ループ (DM3520 / EDULITE 05) は PC が黙っても目標を
        保持するので、降りるだけでは機構が端へ向かい続ける。接触を検出したときと
        同じく、検出位置を目標に送り直してから降りること。
        """
        table = _table(direction=-1, step=1.0, search_distance=50.0, settle_s=0.0)
        spec = table.axis("y_axis")
        rec = _Recorder(stale_after_commands=2)
        # **追従しきらない機構でしか差が出ない** —— 指令どおり動く機構では
        # 「実測 + step」と実測が一致してしまい、送り直しの有無が現れない
        handle = _slow_handle(spec, rec, per_tick=0.6)

        with pytest.raises(HomingError, match="応答していません"):
            await _runner(rec).home(spec, handle)

        axis_commands = [cmd["y_axis_r"] / 2.0 for cmd in rec.commands]
        # 最後の 1 通は「今いる場所で止まれ」。送り直さない実装では、1 歩先
        # (実測 + step) を指した指令が生き残る
        assert axis_commands[-1] == pytest.approx(rec.axis_position())
        assert axis_commands[-1] != pytest.approx(axis_commands[-2])
        assert rec.origins == []

    async def test_離脱の途中で途絶しても止める(self) -> None:
        """歯止めは向きごとに書き分けない (`_seek` を 1 本にしてある理由)。"""
        table = _table(direction=-1, step=1.0, search_distance=50.0)
        spec = table.axis("y_axis")
        # 最初から触れている (= 離脱段へ入る) 状態で、2 歩目に途絶する
        rec = _Recorder(active_after=0, stale_after_commands=2)

        with pytest.raises(HomingError, match="応答していません"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == []
        assert len(rec.commands) <= 3

    async def test_読めないセンサを離脱完了と読まない(self) -> None:
        """**三値の `None` を `False` へ丸めない層を、単独で見る。**

        `_feedback_lost` (鮮度の再確認) を持たない条件 —— 鮮度の判定は「正常」と
        答えるのに現在値だけが読めない —— を作ると、この層だけが効いている。
        丸める実装では、配線が抜けたセンサが「押されていない = 離脱できた」に
        化け、**触れたままの位置で原点を確定する**。
        """
        table = _table(direction=-1, step=1.0, search_distance=50.0, release_distance=5.0)
        spec = table.axis("y_axis")
        # 常に ON (離脱段から抜けられない) センサが、1 歩目の後に読めなくなる
        rec = _Recorder(active_after=0, unreadable_after_commands=1)

        with pytest.raises(HomingError, match="離せませんでした"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == []


class TestChecksTheDriveIsReady:
    """**1 歩も動かす前に、原因が 1 つに定まる形で降りる。**

    無励磁のまま探索へ入っても停滞判定が 3 歩で拾うが、その文言は
    「機構の引っかかり・探索方向・励磁」の 3 択なので、操縦者は原因を絞れない。
    """

    async def test_無励磁の軸では一歩も動かさない(self) -> None:
        table = _table()
        spec = table.axis("y_axis")
        rec = _Recorder(energized=False)

        with pytest.raises(HomingError, match="無励磁"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.commands == []
        assert rec.origins == []

    async def test_励磁を報告しないドライバは無励磁として扱わない(self) -> None:
        """`is_energized()` の `None` は「分からない」であって無励磁ではない。

        倒すと M3508 (常に None) の軸が一切零点確定できなくなる。
        """
        table = _table(search_distance=10.0, step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=3, energized=None)

        await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == ["y_axis"]

    async def test_ラッチを持たないセンサでは探索を開始しない(self) -> None:
        """**現在値へ落とす実装は「取りこぼす探索」そのもの。**

        ON 区間が `step` より狭い機構では指令 1 回ぶんの通過を丸ごと落とし、
        取りこぼした探索が向かう先は機構の破損側である。黙って落ちるくらいなら
        1 歩も動かさずに降りるほうが安全。
        """
        table = _table()
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=-2.0, latched_supported=False)

        with pytest.raises(HomingError, match="ラッチ"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.commands == []
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

    リミットスイッチの ON 区間が `step` より狭いと、指令 1 回でそこを跨いでしまい、
    `settle_s` 後の観測時にはもう OFF になっている。実機の `rotate` (step 2.0deg /
    limit_speed 2.0rad/s = 約 18ms で通過 / settle_s 50ms) がこれで、**スイッチに
    当たっているのに探索が止まらず、可動範囲の端を越えて回り続けた** (その後
    左右の同期ずれで自動緊急停止)。センサの FEEDBACK は 100Hz で届いているので、
    受信のたびにラッチしておけば区間の通過を 1 通も取りこぼさない。
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

        `capture_origin` (EDULITE 05 は `disable` を挟む) が届くまでの時間ぶん、
        機構は破損側へ押し込まれる。検出位置を目標に送り直せば、行き過ぎは
        「検出の遅れ」のぶんだけに縮む。
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

        手動操縦でスイッチを跨いだ後や、前回の零点確定で触れた後に動作確認を
        起動すると、探索を始める時点で既に ON が溜まっている。捨てないと 1 歩目の
        観測でいきなり到達と読み、**スイッチではなく探索開始位置が原点になる**。
        症状は「原点合わせをしたのに位置がずれる」だけ。
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

    リミットスイッチの ON 区間には幅がある。触れたその場を原点にすると
    「区間のどこで始めたか」がそのまま原点のばらつきになり、区間幅ぶん
    (step の何倍にもなる) の誤差が座標へ焼き付く。しかも症状は「原点合わせを
    したのに位置がずれる」だけで、始めた位置が毎回違うので再現もしない。

    離脱は**探索と逆向き**なので、機構端で始まったときに押し込まない、という
    元の性質は保たれる。
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

        探索のラッチは「一度でも ON になったか」なので、同じ形を離脱へ持ち込むと
        「一度でも OFF になったか」で抜けることになる。接点がばたついている間は
        ON を記録しなかった窓が混ざるので、**まだ ON 区間の中にいるのに離脱完了と
        読み、区間内のどこかを原点にする** (離脱そのものの目的が消える)。

        取りこぼしの向きも非対称で、探索の取りこぼしは機構の破損側へ進み続けるのに
        対し、離脱の取りこぼしは「余計に離れる」だけで次の探索が寄せ直す。
        """
        table = _table(step=1.0)
        spec = table.axis("y_axis")
        # 現在値は常に ON (区間から出ていない)。ラッチには OFF の窓が混ざる
        rec = _Recorder(chatter=True)

        with pytest.raises(HomingError, match="離せませんでした"):
            await _runner(rec).home(spec, _handle(spec, rec))

        # ラッチで離脱を判定する実装は 1 歩目で離脱完了と読み、そのまま原点を切る
        assert rec.origins == []

    async def test_離脱の許容は刻み幅から切り離せる(self) -> None:
        """**離脱の許容は ON 区間の広さで決まる値で、刻み幅とは無関係。**

        既定 (`step` の 20 倍) のままだと、精度のために `step` を詰めた瞬間に
        離脱の許容も一緒に縮み、**精度を上げるほどスイッチから離れられなくなる**。
        実機の sub_y_axis で step 0.5 -> 0.1 にした途端、許容が 10mm から 2mm へ
        落ちて ON 区間 (実測 2mm 以上) を抜けきれずに失敗した (2026-09-09)。
        """
        # step 0.1 の既定許容は 2.0mm。ON 区間 -3.0 以上はそれより広い
        table = _table(direction=-1, step=0.1, search_distance=5.0)
        spec = table.axis("y_axis")
        # 区間の奥 -6.0 から出るには 3.1mm 要るので、既定の 2.0mm では届かない
        rec = _Recorder(active_at_or_below=-3.0)
        with pytest.raises(HomingError, match="離せませんでした"):
            await _runner(rec).home(spec, _handle(spec, rec, start_value=-6.0))
        assert rec.origins == []

        # release_distance を実測へ広げれば同じ機構で通る
        table = _table(direction=-1, step=0.1, search_distance=5.0, release_distance=10.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=-3.0)
        await _runner(rec).home(spec, _handle(spec, rec, start_value=-6.0))
        assert rec.captured_at == pytest.approx([-3.0], abs=0.1)

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


class TestTwoStageSearch:
    """**精度と時間は 1 つの刻みでは両立しない。** `homing.coarse_step` がその答え。

    原点のばらつきは刻み幅そのものなので、0.1mm の精度を要求する軸では `step` を
    0.1mm まで詰めることになる。ところが実ストローク 750mm の `sub_y_axis` を
    0.1mm 刻みで探索すると 8000 歩 ≒ 8 分かかり、試合前の点検に入らない
    (暫定として `search_distance` を 5.0 に絞り、「既に触れている状態から」しか
    通らない設定で凌いでいた)。

    粗い刻みで当てる → 離脱 → `step` で寄せ直す、の 2 段にすれば、**確定位置の
    粒度は最後の段の `step` のまま**所要時間だけが縮む。
    """

    async def test_粗い刻みで当ててから細かい刻みで寄せ直す(self) -> None:
        """段の境目 (当てる → 離れる → 寄せ直す) がそのまま指令列に出る。"""
        table = _table(
            direction=-1, step=0.1, coarse_step=1.0, search_distance=20.0, release_distance=2.0
        )
        spec = table.axis("y_axis")
        # ON 区間の入口は -5.45。粗い刻み (1.0) の格子とは意図してずらしてある ——
        # 揃えると粗い段が入口ちょうどで止まり、寄せ直しの有無が結果に出ない
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
        # 粗い段のまま確定すると -6.0 (入口から 0.55mm)。寄せ直せば刻み幅に収まる
        assert rec.captured_at[0] == pytest.approx(-5.45, abs=0.1)
        # 戻り値は 2 段の合計 (粗 6.0 + 細 0.5)。離脱のぶんは含めない
        assert travelled == pytest.approx(6.5)

    @pytest.mark.parametrize("entry", [-5.45, -5.0, -4.62, -7.3])
    async def test_粗い格子のどこで当てても確定位置は入口から細かい一歩以内(
        self, entry: float
    ) -> None:
        """**これが二段にする目的そのもの。**

        粗い段が止まるのは「入口を跨いだ最初の格子点」なので、入口が格子のどこに
        あるかで最大 1 粗ステップぶんばらつく。離脱して細かい刻みで寄せ直せば、
        ばらつきは最後の段の `step` に収まる。
        """
        table = _table(
            direction=-1, step=0.1, coarse_step=1.0, search_distance=20.0, release_distance=2.0
        )
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=entry)

        await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.captured_at[0] == pytest.approx(entry, abs=0.1)

    async def test_粗探索と細探索の合計が探索距離を超えない(self) -> None:
        """**唯一の無人の歯止めの意味を変えない。**

        段ごとに `search_distance` を与え直すと、上限が黙って 2 倍になる。
        配線が抜けたセンサでは両段とも「いつまでも当たらない」ので、そのぶん
        機構を押し込む距離がそのまま倍になる。

        **位置だけで決まるセンサ模型ではこの食い違いが結果に出ない** —— 粗探索が
        当てた区間には寄せ直しも必ず当たるので、上限が 4 倍あっても同じ場所で
        止まる。当てた後に開いたまま固着するスイッチ (`fails_open_above`) にすると、
        寄せ直しに渡した上限がそのまま「押し込む距離」として現れる。
        """
        table = _table(
            direction=-1, step=0.1, coarse_step=1.0, search_distance=4.0, release_distance=2.0
        )
        spec = table.axis("y_axis")
        # 粗探索は上限 4.0 をちょうど使い切って -4.0 で当たる。離脱で -3.0 まで
        # 戻ったところで接点が開いたまま固着し、寄せ直しは二度と当たらない
        rec = _Recorder(active_at_or_below=-3.45, fails_open_above=-3.5)

        with pytest.raises(HomingError, match="到達しませんでした"):
            await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == []
        axis_commands = [cmd["y_axis_r"] / 2.0 for cmd in rec.commands]
        # 寄せ直しに渡るのは離脱で戻った 1.0mm だけ。段ごとに search_distance を
        # 与え直す実装は、そこから更に 4.0mm (40 歩) 機構を押し込む
        assert min(axis_commands) == pytest.approx(-4.0)

    async def test_離脱で戻った分は探索距離を消費しない(self) -> None:
        """**離脱は探索と逆向きなので「押し込んだ量」に数えない。**

        数えると、粗探索が上限をほぼ使い切った軸では寄せ直しの上限が 0 以下になり、
        1 歩目から「-0.5mm 動かしても到達しません」という読めない文言で落ちる。
        `search_distance` は実ストロークに合わせる値なので、二段探索の軸ではこれが
        普通に起きる。寄せ直しが走るのは離脱で戻った地面の上だけで、**到達しうる
        最も深い点は離脱前の位置より深くならない** —— 歯止めの意味は変わらない。
        """
        table = _table(
            direction=-1, step=0.1, coarse_step=1.0, search_distance=4.0, release_distance=2.0
        )
        spec = table.axis("y_axis")
        # 粗探索が上限 4.0 をちょうど使い切る位置で当たる
        rec = _Recorder(active_at_or_below=-3.45)

        await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == ["y_axis"]
        assert rec.captured_at[0] == pytest.approx(-3.45, abs=0.1)
        axis_commands = [cmd["y_axis_r"] / 2.0 for cmd in rec.commands]
        # 押し込んだ最も深い点は粗探索の到達点のまま (寄せ直しはその手前で止まる)
        assert min(axis_commands) == pytest.approx(-4.0)

    async def test_粗探索の停滞判定は粗い刻みを基準にする(self) -> None:
        """基準を細かい側にすると、粗い 1 歩の 4 割しか動かない機構が正常に見える。

        引っかかった機構は「指令しても進まない」形でしか現れないので、
        基準がずれると探索距離を使い切るまで押し当て続けることになる。
        """
        table = _table(
            direction=-1, step=0.1, coarse_step=1.0, search_distance=20.0, release_distance=2.0
        )
        spec = table.axis("y_axis")
        rec = _Recorder()  # 一度も当たらない
        # 1 回の待ちで 0.08mm。粗い 1 歩 (1.0mm) の待ち 5 回ぶんでも 0.4mm しか進まない
        handle = _slow_handle(spec, rec, per_tick=0.08)

        with pytest.raises(HomingError, match="動きません"):
            await _runner(rec).home(spec, handle)

        # 細かい側 (0.05mm) を基準にすると 0.4mm は「進んだ」に化け、
        # 20mm を使い切るまで 50 歩押し当て続ける
        assert len(rec.commands) <= _STALL_LIMIT + 1
        assert rec.origins == []

    async def test_細探索の停滞判定は細かい刻みを基準にする(self) -> None:
        """**粗い側の基準を持ち込むと、動いている機構を数歩で止める。**

        寄せ直しは 1 歩 0.1mm しか進まないので、粗い側の基準 (1.0mm) で数えると
        どの歩も「進まなかった」になり、正常な機構が `_STALL_LIMIT` 歩で落ちる。
        """
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
        # 停滞判定より多くの歩数を踏んでいる (踏まなければこの検証は空振りする)
        assert len(fine_commands) > _STALL_LIMIT
        assert rec.captured_at[0] == pytest.approx(-5.05, abs=0.1)

    async def test_粗探索が区間を跨ぎ切ったら寄せ直さずに降りる(self) -> None:
        """**粗い 1 歩が ON 区間より広い設定は config からは検証できない。**

        跨ぎ切ると、当てた時点でもう区間の外 (機構端の側) に居る。そこから
        寄せ直すと探索方向へ走り抜けるので、動かす前に「今 ON か」を問い直す。
        """
        table = _table(
            direction=-1, step=0.1, coarse_step=1.0, search_distance=20.0, release_distance=2.0
        )
        spec = table.axis("y_axis")
        # ON 区間は -5.6〜-5.2 (幅 0.4mm)。粗い刻み 1.0mm は 1 歩で跨ぎ切る
        rec = _Recorder(active_band=(-5.6, -5.2))

        with pytest.raises(HomingError, match="coarse_step"):
            await _runner(rec).home(spec, _handle(spec, rec))

        # 粗探索 6 歩 + その場へ止め直す 1 通で終わり。1 歩も追加で動かさない
        assert len(rec.commands) == 7
        assert rec.origins == []

    async def test_粗い刻みを書かない軸は従来どおり単段で寄せる(self) -> None:
        """`rotate` (step 2.0deg で 90 歩) のように 1 つの刻みで足りる軸に二段は要らない。"""
        table = _table(direction=-1, step=1.0, search_distance=20.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_at_or_below=-5.45)

        await _runner(rec).home(spec, _handle(spec, rec))

        axis_commands = [cmd["y_axis_r"] / 2.0 for cmd in rec.commands]
        # 離脱も寄せ直しも挟まらない (挟むと確定位置が入口へ寄って -6.0 でなくなる)
        assert axis_commands == pytest.approx([-1.0, -2.0, -3.0, -4.0, -5.0, -6.0, -6.0])
        assert rec.captured_at == pytest.approx([-6.0])


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
