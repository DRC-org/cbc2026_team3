"""リミットスイッチによる零点確定 (lib/sequence/homing.py)。

**「当たるまで動かす」動作なので、止まることを最優先で見る。** 配線が抜けた
センサは「いつまでも当たらない」形でしか現れず、歯止めが無いと機構端まで
押し込み続ける。緊急停止は操縦者が押さないと効かないので、無人の歯止めが要る。
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

    def __init__(self, *, active_after: int | None = None, stale: bool = False) -> None:
        self.commands: list[dict[str, float]] = []
        self.origins: list[str] = []
        self.sleeps = 0
        self._active_after = active_after
        self._observations = 0
        self._stale = stale

    def sensor_active(self, _name: str) -> bool:
        self._observations += 1
        if self._active_after is None:
            return False
        return self._observations > self._active_after

    def sensor_is_stale(self, _name: str) -> bool:
        return self._stale

    def capture_origin(self, axis: str) -> None:
        self.origins.append(axis)

    async def sleep(self, _seconds: float) -> None:
        self.sleeps += 1


def _handle(spec: AxisSpec, recorder: _Recorder) -> AxisHandle:
    mgr = mock_can_manager()
    handles = []
    for name in spec.motor_names:
        driver = StubFeedbackDriver(name, 1)
        handles.append(MotorHandle(name, driver, mgr))

    handle = AxisHandle(spec, handles)
    original = handle.set_target_value

    async def _record(commands):  # noqa: ANN001, ANN202
        recorder.commands.append(dict(commands))
        return await original(commands)

    handle.set_target_value = _record  # type: ignore[method-assign]
    return handle


def _runner(recorder: _Recorder) -> HomingRunner:
    return HomingRunner(
        sensor_active=recorder.sensor_active,
        sensor_is_stale=recorder.sensor_is_stale,
        capture_origin=recorder.capture_origin,
        sleep=recorder.sleep,
    )


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
