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

import pytest

from lib.sequence.homing import HomingError, HomingRunner
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


class _Recorder:
    """指令と原点確定を記録する。センサは指定回数目の観測で ON になる。"""

    def __init__(
        self,
        *,
        active_after: int | None = None,
        stale: bool = False,
        motor_stale: bool = False,
        capturable: bool = True,
    ) -> None:
        self.commands: list[dict[str, float]] = []
        self.origins: list[str] = []
        #: `_handle` が組んだモータ名 → ドライバ (実測位置の差し替え口)
        self.drivers: dict[str, StubFeedbackDriver] = {}
        self.sleeps = 0
        self._active_after = active_after
        self._observations = 0
        self._stale = stale
        self._motor_stale = motor_stale
        self._capturable = capturable

    def sensor_active(self, _name: str) -> bool:
        self._observations += 1
        if self._active_after is None:
            return False
        return self._observations > self._active_after

    def sensor_is_stale(self, _name: str) -> bool:
        return self._stale

    def motor_is_stale(self, _name: str) -> bool:
        return self._motor_stale

    def origin_capturable(self, _axis: str) -> bool:
        return self._capturable

    def capture_origin(self, axis: str) -> None:
        self.origins.append(axis)

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
    return handle


def _runner(recorder: _Recorder) -> HomingRunner:
    return HomingRunner(
        sensor_active=recorder.sensor_active,
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
        # 1 回目の観測 (開始前の確認) では OFF、3 回目で ON
        rec = _Recorder(active_after=2)

        travelled = await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.origins == ["y_axis"]
        assert travelled == pytest.approx(2.0)

    async def test_戻り値は実測の移動量(self) -> None:
        """指令の積算ではない。追従しきっていない機構では両者がずれる。"""
        table = _table(search_distance=10.0, step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=2)

        travelled = await _runner(rec).home(spec, _handle(spec, rec, start_value=15.0))

        # 15mm から 2 歩 (負方向) 動いたので実測は 13mm。移動量は 2mm
        assert travelled == pytest.approx(2.0)

    async def test_既に触れていれば動かさずに確定する(self) -> None:
        """押し込む方向へ動かさない。機構端で始まったときに壊さないため。"""
        table = _table()
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=0)  # 最初の観測から ON

        travelled = await _runner(rec).home(spec, _handle(spec, rec))

        assert rec.commands == []
        assert rec.origins == ["y_axis"]
        assert travelled == 0.0

    async def test_探索方向を符号で表す(self) -> None:
        table = _table(direction=-1, step=1.0)
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=1)

        await _runner(rec).home(spec, _handle(spec, rec))

        # 人間の単位で -1mm。scale は右 +2 / 左 -2 なので指令は ∓2deg
        assert rec.commands == [{"y_axis_r": -2.0, "y_axis_l": 2.0}]

    async def test_左右ペアを同じフレームで指令する(self) -> None:
        """別々の時刻に動かすとその場で機構が壊れる。"""
        table = _table()
        spec = table.axis("y_axis")
        rec = _Recorder(active_after=1)

        await _runner(rec).home(spec, _handle(spec, rec))

        # 1 回の指令に左右が揃っていること (2 回に分かれていない)
        assert len(rec.commands) == 1
        assert set(rec.commands[0]) == {"y_axis_r", "y_axis_l"}


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
