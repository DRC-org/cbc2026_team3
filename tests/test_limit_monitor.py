"""移動中の可動端インターロック (lib/control/limit_monitor.py)。

`MotionGuard` は指令を書く瞬間しか見ていないので、遠い目標を 1 回書くと途中で
スイッチが立っても誰も止めない (2026-09-09 実機: `sub_y_axis` が作動点を 3mm 越えた)。
ここで見るのは、周期監視がその穴を塞げているか:

- 目標へ向かう先の端が押されている / 読めていないなら、その場の実測位置で止める
- **離れる向きは邪魔しない** (零点確定の離脱段と、端に張り付いた軸の手動退避)
- 一度止めた軸を毎周期撃ち直さない
- `guard.limits` を書いていない軸は触らない
"""

from __future__ import annotations

import logging

import pytest

from lib.control.limit_monitor import LimitMonitor
from lib.drivers.base import ControlMode
from lib.match_state import Court
from lib.sequence.motors import MotorGroup, MotorHandle
from lib.sequence.positions import PositionTable, load_position_table
from tests.fake_can import mock_can_manager
from tests.fake_drivers import StubFeedbackDriver

_SCALE = 2.0

_GUARD: dict = {
    "limits": {"plus": "front_switch", "minus": "rear_switch"},
    "max_step": 500.0,
    "stall_torque": 1.0,
}


def _table(*, guard: object = _GUARD) -> PositionTable:
    axis: dict = {
        "unit": "mm",
        "command_unit": "rad",
        "scale": _SCALE,
        "tolerance": 0.1,
    }
    if guard is not None:
        axis["guard"] = guard
    return load_position_table(
        {"axes": {"sub_y_axis": axis}, "positions": {"sub_y_axis": {"home": 0.0}}},
        source="<test>",
    )


class _Rig:
    """1 モータ軸ぶんの監視対象と、実際に送られた指令値の記録。

    送信は ``target_sink`` で受ける。**止め直しを撃ち続けていないか**は、
    拒否されたかどうかではなく送信そのものを数えないと見えない。
    """

    def __init__(self, table: PositionTable, sensors: dict[str, bool | None]) -> None:
        self.sent: list[float] = []
        self.sensors = sensors
        self.table = table

        async def sink(_mode: ControlMode, value: float) -> None:
            self.sent.append(value)

        self.driver = StubFeedbackDriver("sub_y_axis", 1)
        self.handle = MotorHandle("sub_y_axis", self.driver, mock_can_manager(), target_sink=sink)
        group = MotorGroup(sensor_active=self.read)
        group.add(self.handle)
        self.monitor = LimitMonitor(table, group, sensor_active=self.read, court=lambda: Court.RED)

    def read(self, name: str) -> bool | None:
        return self.sensors.get(name, False)

    def set_observed(self, value: float) -> None:
        self.driver.set_observed(position=value * _SCALE)

    async def command(self, value: float) -> None:
        """歯止めを通さずに目標を書く (長距離移動を 1 回指令した直後の状態)。"""
        await self.handle.set_target(ControlMode.POSITION, value * _SCALE)
        self.sent.clear()


def _rig(*, guard: object = _GUARD, **sensors: bool | None) -> _Rig:
    return _Rig(_table(guard=guard), dict(sensors))


class TestLimitMonitor:
    async def test_目標へ向かう先の端が押されたら実測位置で止める(self) -> None:
        """事故そのもの。作動点 -447 を越えた先の -450 を目標に走り続けていた。"""
        rig = _rig(rear_switch=True)
        rig.set_observed(-447.5)
        await rig.command(-450.0)

        await rig.monitor.step()

        assert rig.sent == [-447.5 * _SCALE]
        assert rig.handle.target == -447.5 * _SCALE

    async def test_離れる向きの移動は邪魔しない(self) -> None:
        """**塞ぐと端に張り付いた軸を戻せない。** 零点確定の離脱段もこの向きを使う。"""
        rig = _rig(rear_switch=True)
        rig.set_observed(-447.5)
        await rig.command(-440.0)

        await rig.monitor.step()

        assert rig.sent == []
        assert rig.handle.target == -440.0 * _SCALE

    async def test_片端しか宣言していない軸でも離れる向きは通す(self) -> None:
        """反対端のセンサが無い軸で `None` を「押されている」と読むと退避路が消える。"""
        rig = _rig(guard={"limits": {"minus": "rear_switch"}}, rear_switch=True)
        rig.set_observed(-447.5)
        await rig.command(-440.0)

        await rig.monitor.step()

        assert rig.sent == []

    async def test_センサが読めないときも止める(self) -> None:
        """`None` を `False` へ丸めると、配線の抜けたセンサが「進んでよい」に化ける。"""
        rig = _rig(rear_switch=None)
        rig.set_observed(-447.5)
        await rig.command(-450.0)

        await rig.monitor.step()

        assert rig.sent == [-447.5 * _SCALE]

    async def test_一度止めたら撃ち続けない(self) -> None:
        """20〜50Hz で `set_target_value` を撃ち続けるとバスが埋まる。"""
        rig = _rig(rear_switch=True)
        rig.set_observed(-447.5)
        await rig.command(-450.0)

        await rig.monitor.step()
        await rig.monitor.step()
        # 止めた後も実測はゆらぐ。押されている端へ向いたゆらぎを指令と読むと撃ち続ける
        rig.set_observed(-447.4)
        await rig.monitor.step()
        rig.set_observed(-447.6)
        await rig.monitor.step()

        assert len(rig.sent) == 1

    async def test_目標が書き直されたら改めて判定する(self) -> None:
        """止めっぱなしのラッチにしない。次の指令は指令として見る。"""
        rig = _rig(rear_switch=True)
        rig.set_observed(-447.5)
        await rig.command(-450.0)
        await rig.monitor.step()
        assert len(rig.sent) == 1

        await rig.command(-460.0)
        await rig.monitor.step()

        assert rig.sent == [-447.5 * _SCALE]

    async def test_センサが離れれば止めない(self) -> None:
        rig = _rig(rear_switch=True)
        rig.set_observed(-447.5)
        await rig.command(-450.0)
        await rig.monitor.step()

        rig.sensors["rear_switch"] = False
        await rig.command(-450.0)
        await rig.monitor.step()

        assert rig.sent == []

    async def test_発火は_WARNING_で残す(self, caplog: pytest.LogCaptureFixture) -> None:
        """無言で止まると「なぜ止まったか分からない」になる。"""
        rig = _rig(rear_switch=True)
        rig.set_observed(-447.5)
        await rig.command(-450.0)

        with caplog.at_level(logging.WARNING, logger="lib.control.limit_monitor"):
            await rig.monitor.step()

        assert any(
            record.levelno == logging.WARNING and "sub_y_axis" in record.getMessage()
            for record in caplog.records
        )


class TestOutOfScope:
    """**見るのは可動端だけ。** 広げると移動中に誤発火する。"""

    async def test_limits_の無い軸は監視しない(self) -> None:
        rig = _rig(guard={"max_step": 5.0, "stall_torque": 1.0}, rear_switch=True)
        rig.set_observed(-447.5)
        await rig.command(-450.0)

        await rig.monitor.step()

        assert rig.monitor.axis_names == ()
        assert rig.sent == []

    async def test_guard_を書かない軸は監視しない(self) -> None:
        rig = _rig(guard=None, rear_switch=True)
        rig.set_observed(-447.5)
        await rig.command(-450.0)

        await rig.monitor.step()

        assert rig.monitor.axis_names == ()
        assert rig.sent == []

    async def test_跳躍量は見ない(self) -> None:
        """`max_step` は「1 指令で離れてよい量」。移動中の実測に当てると必ず誤発火する。"""
        rig = _rig(guard={"limits": {"minus": "rear_switch"}, "max_step": 5.0})
        rig.set_observed(0.0)
        await rig.command(-450.0)

        await rig.monitor.step()

        assert rig.sent == []

    async def test_目標を持たない軸は触らない(self) -> None:
        rig = _rig(rear_switch=True)
        rig.set_observed(-447.5)

        await rig.monitor.step()

        assert rig.sent == []

    async def test_センサ読み口が壊れても周期処理は落ちない(self) -> None:
        """1 軸で失敗しても残りの軸を見続ける形にしておく。"""
        rig = _rig(rear_switch=True)
        rig.set_observed(-447.5)
        await rig.command(-450.0)

        def _explode(_name: str) -> bool | None:
            raise RuntimeError("センサ読み口が壊れた")

        rig.monitor._sensor_active = _explode  # type: ignore[attr-defined]
        await rig.monitor.step()

        assert rig.sent == []
