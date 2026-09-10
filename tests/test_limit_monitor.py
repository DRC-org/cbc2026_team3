"""移動中の可動端インターロック (lib/control/limit_monitor.py)。

`MotionGuard` は指令を書く瞬間しか見ていないので、遠い目標を 1 回書くと途中で
スイッチが立っても誰も止めない (2026-09-09 実機: `sub_y_axis` が作動点を 3mm 越えた)。
ここで見るのは、周期監視がその穴を塞げているか:

- 目標へ向かう先の端が押されている / 読めていないなら、その場の実測位置で止める
- **観測周期 (20ms) より狭い ON 区間**も取りこぼさない (接触の累計で拾う)
- **離れる向きは邪魔しない** (零点確定の離脱段と、端に張り付いた軸の手動退避)
- 一度止めた軸を毎周期撃ち直さない
- `guard.limits` を書いていない軸は触らない
- 止めた回数を数える (`move_to` が「曲げられた移動」を失敗として読む材料)
"""

from __future__ import annotations

import asyncio
import logging

import can
import pytest

from lib.control.limit_monitor import LimitMonitor
from lib.drivers.base import ControlMode
from lib.match_state import Court
from lib.motion_guard import SensorSuspension
from lib.sequence.engine import Sequence, SequenceTimeoutError
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

    def __init__(
        self,
        table: PositionTable,
        sensors: dict[str, bool | None],
        *,
        counters: bool = True,
    ) -> None:
        self.sent: list[float] = []
        self.sensors = sensors
        self.contacts: dict[str, int] = {}
        self.counters = counters
        self.table = table
        # 読み口は `main()` と同じ形 (現在値も累計も覆いを通す) で組む。片方だけ生で
        # 渡した配線は、整列段の覆いに穴が開いていることに気付けない
        self.suspension = SensorSuspension()
        read = self.suspension.wrap(self.read)
        count = self.suspension.wrap_count(self.count)

        async def sink(_mode: ControlMode, value: float) -> None:
            self.sent.append(value)

        self.driver = StubFeedbackDriver("sub_y_axis", 1)
        self.handle = MotorHandle("sub_y_axis", self.driver, mock_can_manager(), target_sink=sink)
        group = MotorGroup(sensor_active=read)
        group.add(self.handle)
        self.monitor = LimitMonitor(
            table,
            group,
            sensor_active=read,
            sensor_contact_count=count,
            court=lambda: Court.RED,
        )

    def read(self, name: str) -> bool | None:
        return self.sensors.get(name, False)

    def count(self, name: str) -> int | None:
        """接触の累計。`counters=False` の rig は**カウンタを持たないドライバ**を表す。"""
        if not self.counters:
            return None
        return self.contacts.get(name, 0)

    def pulse(self, name: str) -> None:
        """観測周期より狭い接触。現在値は OFF のままカウンタだけが増える。"""
        self.contacts[name] = self.contacts.get(name, 0) + 1

    def set_observed(self, value: float) -> None:
        self.driver.set_observed(position=value * _SCALE)

    async def command(self, value: float) -> None:
        """歯止めを通さずに目標を書く (長距離移動を 1 回指令した直後の状態)。"""
        await self.handle.set_target(ControlMode.POSITION, value * _SCALE)
        self.sent.clear()


def _rig(*, guard: object = _GUARD, counters: bool = True, **sensors: bool | None) -> _Rig:
    return _Rig(_table(guard=guard), dict(sensors), counters=counters)


class _FollowDriver(StubFeedbackDriver):
    """`follow` を立てた間だけ指令どおり動く機体 (立てるまでは到達しない)。"""

    def __init__(self, name: str) -> None:
        super().__init__(name, 1)
        self.follow = False

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        if self.follow:
            self.set_observed(position=value)
        return super().encode_target(mode, value)


class _MoveSequence(Sequence):
    def __init__(self) -> None:
        super().__init__("limit_seq")


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


class TestNarrowContact:
    """**現在値だけでは観測周期より狭い ON 区間を丸ごと落とす。**

    零点確定が探索の到達判定で踏んだのと同じ穴 (`rotate` はスイッチの 2deg を
    約 18ms で通過する)。走行中の保護は 50Hz なので同じ材料 —— 接触の累計 ——
    でしか塞げない。
    """

    async def test_観測時に_OFF_でも接触が数えられていれば止める(self) -> None:
        rig = _rig(rear_switch=False)
        rig.set_observed(-447.5)
        await rig.command(-450.0)
        await rig.monitor.step()
        assert rig.sent == []

        rig.pulse("rear_switch")
        await rig.monitor.step()

        assert rig.sent == [-447.5 * _SCALE]

    async def test_同じ接触で二度は止めない(self) -> None:
        """基準値を周期ごとに 1 度だけ進める。二重に数えると次の移動も即座に止まる。"""
        rig = _rig(rear_switch=False)
        rig.set_observed(-447.5)
        await rig.command(-450.0)
        await rig.monitor.step()
        rig.pulse("rear_switch")
        await rig.monitor.step()
        assert rig.monitor.intervention("sub_y_axis").count == 1

        await rig.command(-460.0)
        await rig.monitor.step()

        assert rig.sent == []
        assert rig.monitor.intervention("sub_y_axis").count == 1

    async def test_監視を始める前の接触は数えない(self) -> None:
        """前回の零点確定や手動操縦で数えたぶんまで見ると、最初の移動でいきなり止まる。"""
        rig = _rig(rear_switch=False)
        rig.contacts["rear_switch"] = 7
        rig.set_observed(-447.5)
        await rig.command(-450.0)

        await rig.monitor.step()

        assert rig.sent == []

    async def test_カウンタを提供しないセンサは現在値で判断する(self) -> None:
        """`None` へ倒すと、カウンタの無いドライバの軸が 1 歩も動けなくなる。"""
        rig = _rig(counters=False, rear_switch=True)
        rig.set_observed(-447.5)
        await rig.command(-450.0)
        await rig.monitor.step()
        assert rig.sent == [-447.5 * _SCALE]

        # カウンタが増えても現在値が OFF なら止めない (拾う材料そのものが無い)
        rig.sensors["rear_switch"] = False
        rig.pulse("rear_switch")
        await rig.command(-460.0)
        await rig.monitor.step()

        assert rig.sent == []

    async def test_読めていないセンサは接触の有無に依らず止める(self) -> None:
        """三値を潰さない。カウンタが増えていなくても `None` は「進んでよい」ではない。"""
        rig = _rig(rear_switch=None)
        rig.set_observed(-447.5)
        await rig.command(-450.0)

        await rig.monitor.step()

        assert rig.sent == [-447.5 * _SCALE]

    async def test_離れる向きなら接触が増えても止めない(self) -> None:
        rig = _rig(rear_switch=False)
        rig.set_observed(-447.5)
        await rig.command(-440.0)
        await rig.monitor.step()

        rig.pulse("rear_switch")
        await rig.monitor.step()

        assert rig.sent == []


class TestSuspendedSensor:
    """整列段のあいだ覆ったセンサでは止めない (`SensorSuspension`)。

    左右に 1 本ずつスイッチが付く軸では、片方が当たった後にまだ当たっていない側だけを
    端へ進める。軸としては端へ向かう向きなので、覆いが**現在値と接触の累計の両方**に
    掛かっていないと、この監視が目標を実測へ書き直して整列段が必ず失敗する
    (実機ログ: 整列段の最中に `y_axis_l_origin_sensor` で「移動中に可動端で停止」)。
    """

    async def test_覆っている間は接触を数えても止めない(self) -> None:
        rig = _rig(rear_switch=False)
        rig.set_observed(-447.5)
        await rig.command(-450.0)
        await rig.monitor.step()

        with rig.suspension.suspend(["rear_switch"]):
            rig.pulse("rear_switch")
            await rig.monitor.step()

            assert rig.sent == []

    async def test_覆っている間は押されていても止めない(self) -> None:
        rig = _rig(rear_switch=False)
        rig.set_observed(-447.5)
        await rig.command(-450.0)

        with rig.suspension.suspend(["rear_switch"]):
            rig.sensors["rear_switch"] = True
            await rig.monitor.step()

            assert rig.sent == []

    async def test_名指ししていないセンサは覆っている間も止める(self) -> None:
        """軸まるごと外すと、反対端の守りまで消える。"""
        rig = _rig(rear_switch=False)
        rig.set_observed(-447.5)
        await rig.command(-450.0)
        await rig.monitor.step()

        with rig.suspension.suspend(["front_switch"]):
            rig.pulse("rear_switch")
            await rig.monitor.step()

            assert rig.sent == [-447.5 * _SCALE]

    async def test_覆いを外せば押されたスイッチで止める(self) -> None:
        """覆いが外れ残ると、その端は試合が終わるまで守られない。"""
        rig = _rig(rear_switch=False)
        rig.set_observed(-447.5)
        await rig.command(-450.0)

        with rig.suspension.suspend(["rear_switch"]):
            rig.sensors["rear_switch"] = True
            await rig.monitor.step()
        await rig.monitor.step()

        assert rig.sent == [-447.5 * _SCALE]


class TestIntervention:
    """**保護が目標を実測へ書き直すと到達判定は必ず成立する。**

    `move_to` はこの回数でしか「軸は途中に居るのにシーケンスだけが進んだ」を
    見分けられない (終了時の状態では、触れて離れた接点のバウンドを取りこぼす)。
    """

    async def test_止めた回数を数え_理由にセンサ名を載せる(self) -> None:
        rig = _rig(rear_switch=True)
        rig.set_observed(-447.5)
        await rig.command(-450.0)

        await rig.monitor.step()

        record = rig.monitor.intervention("sub_y_axis")
        assert record.count == 1
        assert record.reason is not None
        assert "rear_switch" in record.reason

    async def test_止めていない軸は_0(self) -> None:
        rig = _rig(rear_switch=True)

        assert rig.monitor.intervention("sub_y_axis").count == 0
        assert rig.monitor.intervention("居ない軸").count == 0

    async def test_センサが離れても減らない(self) -> None:
        """バウンドした接点で回数が戻ると、曲げられた移動が成功として読まれる。"""
        rig = _rig(rear_switch=True)
        rig.set_observed(-447.5)
        await rig.command(-450.0)
        await rig.monitor.step()

        rig.sensors["rear_switch"] = False
        await rig.command(-440.0)
        await rig.monitor.step()

        assert rig.monitor.intervention("sub_y_axis").count == 1

    async def test_目標を書き直すたびに数える(self) -> None:
        rig = _rig(rear_switch=True)
        rig.set_observed(-447.5)
        await rig.command(-450.0)
        await rig.monitor.step()
        await rig.monitor.step()
        await rig.command(-460.0)
        await rig.monitor.step()

        assert rig.monitor.intervention("sub_y_axis").count == 2


class TestStopCommandPassesTheEntryGuard:
    """**止めるための指令が、止まっていないことを理由に拒否されてはならない。**

    書き戻しを値へ換算して戻すと (`to_value(to_commands(v))`)、複数モータ軸では
    平均を挟むぶん往復が丸め誤差を生む。誤差は非ゼロの `delta` として入口の
    `MotionGuard` に届き、押されている端の向きに転べば引き戻しごと拒否される
    (症状は「止めるはずの周期で何も送らず、軸は走り続ける」)。
    指令の単位のまま書き戻せば `delta` は厳密に 0 になる。
    """

    _PAIR_SCALE = 1.0668451
    # 平均を挟む往復が負側へ転ぶ実例。指令単位のまま書き戻せば往復そのものが無い
    _LEFT = 133.75234475746345
    _RIGHT = -133.60652081589743

    def _rig(self) -> tuple[LimitMonitor, list[tuple[str, float]], list[MotorHandle]]:
        table = load_position_table(
            {
                "axes": {
                    "pair": {
                        "unit": "mm",
                        "command_unit": "rad",
                        "motors": {
                            "pair_l": {"scale": self._PAIR_SCALE},
                            "pair_r": {"scale": -self._PAIR_SCALE},
                        },
                        "guard": {"limits": {"plus": "front", "minus": "rear"}},
                    }
                },
                "positions": {"pair": {"home": 0.0}},
            },
            source="<test>",
        )
        sent: list[tuple[str, float]] = []
        handles: list[MotorHandle] = []
        group = MotorGroup(sensor_active=lambda name: name == "rear")
        for name, feedback in (("pair_l", self._LEFT), ("pair_r", self._RIGHT)):

            async def sink(_mode: ControlMode, value: float, _name: str = name) -> None:
                sent.append((_name, value))

            driver = StubFeedbackDriver(name, 1)
            driver.set_observed(position=feedback)
            handle = MotorHandle(name, driver, mock_can_manager(), target_sink=sink)
            group.add(handle)
            handles.append(handle)
        monitor = LimitMonitor(
            table,
            group,
            sensor_active=lambda name: name == "rear",
            sensor_contact_count=lambda _name: 0,
            court=lambda: Court.RED,
        )
        return monitor, sent, handles

    async def _command_toward_rear(self, handles: list[MotorHandle]) -> None:
        """歯止めを通さずに後端へ 10mm ぶんの目標を書く (逆回転ペアなので符号が逆)。"""
        for handle, feedback, scale in zip(
            handles,
            (self._LEFT, self._RIGHT),
            (self._PAIR_SCALE, -self._PAIR_SCALE),
            strict=True,
        ):
            await handle.set_target(ControlMode.POSITION, feedback - 10.0 * scale)

    async def test_丸め誤差で引き戻しが拒否されない(self, caplog: pytest.LogCaptureFixture) -> None:
        monitor, sent, handles = self._rig()
        await self._command_toward_rear(handles)
        sent.clear()

        with caplog.at_level(logging.ERROR, logger="lib.control.limit_monitor"):
            await monitor.step()

        assert [record.getMessage() for record in caplog.records] == []
        assert sorted(sent) == sorted([("pair_l", self._LEFT), ("pair_r", self._RIGHT)])
        assert monitor.intervention("pair").count == 1

    async def test_止めた後は撃ち直さない(self) -> None:
        """書き戻した値がそのまま次の周期の目標と一致する (換算の往復で桁が落ちない)。"""
        monitor, sent, handles = self._rig()
        await self._command_toward_rear(handles)
        sent.clear()

        await monitor.step()
        await monitor.step()

        assert len(sent) == 2


class TestMoveToSeesTheIntervention:
    """`LimitMonitor` と `move_to` を実物どうしで噛み合わせる。

    **終了時の状態 (`stopped_axes`) では代わりにならない。** 触れて離れた接点では
    状態だけが戻り、逆に前の移動で付いたラッチは次の移動まで残る。
    """

    def _rig(self) -> tuple[Sequence, _FollowDriver, dict[str, bool | None], LimitMonitor]:
        table = load_position_table(
            {
                "axes": {
                    "sub_y_axis": {
                        "unit": "mm",
                        "command_unit": "rad",
                        "scale": _SCALE,
                        "tolerance": 0.1,
                        "timeout_s": 0.5,
                        "guard": {"limits": {"minus": "rear_switch"}},
                    }
                },
                "positions": {"sub_y_axis": {"far": -450.0, "near": -440.0}},
            },
            source="<test>",
        )
        sensors: dict[str, bool | None] = {"rear_switch": False}

        def read(name: str) -> bool | None:
            return sensors.get(name, False)

        driver = _FollowDriver("sub_y_axis")
        driver.set_observed(position=-447.5 * _SCALE)
        group = MotorGroup(sensor_active=read)
        group.add(MotorHandle("sub_y_axis", driver, mock_can_manager()))

        monitor = LimitMonitor(
            table,
            group,
            sensor_active=read,
            sensor_contact_count=lambda _name: 0,
            court=lambda: Court.RED,
        )
        sequence = _MoveSequence()
        sequence.bind_motors(group)
        sequence.bind_positions(table)
        sequence.bind_limit_interventions(
            lambda axis: monitor.intervention(axis),
        )
        return sequence, driver, sensors, monitor

    async def test_触れて離れた移動も失敗する(self) -> None:
        sequence, _driver, sensors, monitor = self._rig()
        move = asyncio.create_task(sequence.move_to({"sub_y_axis": "far"}))
        await asyncio.sleep(0.02)

        sensors["rear_switch"] = True
        await monitor.step()
        # バウンド: 軸は触れた位置で止まったまま、接点だけが戻る
        sensors["rear_switch"] = False
        await monitor.step()

        with pytest.raises(SequenceTimeoutError, match="rear_switch"):
            await move

    async def test_前の移動のラッチは次の移動を落とさない(self) -> None:
        """回数を前後で比べる形でしか成立しない (状態を見ると次の移動まで巻き添えになる)。"""
        sequence, driver, sensors, monitor = self._rig()
        move = asyncio.create_task(sequence.move_to({"sub_y_axis": "far"}))
        await asyncio.sleep(0.02)
        sensors["rear_switch"] = True
        await monitor.step()
        with pytest.raises(SequenceTimeoutError):
            await move

        # 端から離れる向きの移動。監視が回る前に到達するので、ラッチは残ったまま
        sensors["rear_switch"] = False
        driver.follow = True
        await sequence.move_to({"sub_y_axis": "near"})

        assert monitor.stopped_axes == frozenset({"sub_y_axis"})


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


def _table_with_requires() -> PositionTable:
    """可動端と**軸間干渉の両方**を宣言した軸。参照先はこの束に居ない。"""
    return load_position_table(
        {
            "axes": {
                "sub_y_axis": {
                    "unit": "mm",
                    "command_unit": "rad",
                    "scale": _SCALE,
                    "tolerance": 0.1,
                    "guard": {**_GUARD, "requires": [{"axis": "sub_lift", "at": "top"}]},
                },
                "sub_lift": {
                    "unit": "mm",
                    "command_unit": "rad",
                    "scale": _SCALE,
                    "tolerance": 1.0,
                },
            },
            "positions": {"sub_y_axis": {"home": 0.0}, "sub_lift": {"top": -20.0}},
        },
        source="<test>",
    )


class TestInterferenceIsNotWatchedHere:
    """**周期監視が見るのは可動端だけ。** 軸間干渉は見ない (理由は `docs/invariants.md` §4)。

    そのため監視が作る `AxisHandle` には読み口を配線しない。書き戻す「その場で
    止まれ」は `delta == 0` なので干渉の判定を必ず素通りする —— 素通りしないと、
    **止めるための指令が「条件の軸が読めていない」を理由に拒否される**。
    """

    def _rig(self, **sensors: bool | None) -> _Rig:
        return _Rig(_table_with_requires(), dict(sensors))

    async def test_干渉条件を持つ軸でも可動端では止められる(self) -> None:
        rig = self._rig(rear_switch=True)
        await rig.command(-10.0)

        await rig.monitor.step()

        assert rig.sent == [pytest.approx(0.0)]
        assert rig.monitor.intervention("sub_y_axis").count == 1

    async def test_干渉条件だけでは止めない(self) -> None:
        """参照先が読めていなくても、可動端が立っていなければ監視は何もしない。"""
        rig = self._rig()
        await rig.command(-10.0)

        await rig.monitor.step()

        assert rig.sent == []
        assert rig.monitor.intervention("sub_y_axis").count == 0


def _table_with_court_scale() -> PositionTable:
    """コート依存の軸と非依存の軸を 1 枚に載せた表。**どちらも可動端を宣言している。**"""
    return load_position_table(
        {
            "axes": {
                "sub_y_axis": {
                    "unit": "mm",
                    "command_unit": "rad",
                    "scale": _SCALE,
                    "tolerance": 0.1,
                    "guard": _GUARD,
                },
                "sub_lift": {
                    "unit": "mm",
                    "command_unit": "rad",
                    "scale": {"red": -_SCALE, "blue": _SCALE},
                    "tolerance": 1.0,
                    "guard": {
                        "limits": {"plus": "bottom_switch", "minus": "top_switch"},
                        "max_step": 500.0,
                        "stall_torque": 1.0,
                    },
                },
            },
            "positions": {"sub_y_axis": {"home": 0.0}, "sub_lift": {"top": -20.0}},
        },
        source="<test>",
    )


class _CourtRig:
    """コート依存軸と非依存軸を 1 台ずつ持つ監視。コートは差し替えられる。"""

    def __init__(self) -> None:
        self.sent: dict[str, list[float]] = {"sub_y_axis": [], "sub_lift": []}
        self.sensors: dict[str, bool | None] = {}
        self.court: Court | None = None
        table = _table_with_court_scale()
        group = MotorGroup(sensor_active=self.sensors.get)
        self.handles: dict[str, MotorHandle] = {}
        for name in ("sub_y_axis", "sub_lift"):
            handle = MotorHandle(
                name,
                StubFeedbackDriver(name, 1),
                mock_can_manager(),
                target_sink=self._sink(name),
            )
            group.add(handle)
            self.handles[name] = handle
        self.monitor = LimitMonitor(
            table,
            group,
            sensor_active=self.sensors.get,
            sensor_contact_count=lambda _name: None,
            court=lambda: self.court,
        )

    def _sink(self, name: str):

        async def sink(_mode: ControlMode, value: float) -> None:
            self.sent[name].append(value)

        return sink

    async def command(self, name: str, value: float) -> None:
        await self.handles[name].set_target(ControlMode.POSITION, value)
        self.sent[name].clear()


class TestCourtUnresolved:
    """コート未確定のあいだ、コート依存軸は監視できない。**穴にはならない。**

    決まるまでその軸へは指令が 1 通も通らない (入口が `CourtUnresolvedError` で
    落ちる) ので、監視する動きがそもそも無い。
    """

    async def test_コート依存軸は飛ばす(self, caplog: pytest.LogCaptureFixture) -> None:
        rig = _CourtRig()
        rig.sensors["top_switch"] = True
        await rig.command("sub_lift", -10.0)

        with caplog.at_level(logging.WARNING, logger="lib.control.limit_monitor"):
            await rig.monitor.step()

        assert rig.sent["sub_lift"] == []
        assert rig.monitor.intervention("sub_lift").count == 0
        # 飛ばすのであって、例外で落ちるのではない (毎周期の Traceback は本物の異常を埋める)
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []

    async def test_コート非依存軸の保護は生きている(self) -> None:
        rig = _CourtRig()
        rig.sensors["rear_switch"] = True
        await rig.command("sub_y_axis", -10.0)

        await rig.monitor.step()

        assert rig.sent["sub_y_axis"] == [pytest.approx(0.0)]
        assert rig.monitor.intervention("sub_y_axis").count == 1

    async def test_警告は軸ごとに1度だけ(self, caplog: pytest.LogCaptureFixture) -> None:
        rig = _CourtRig()
        await rig.command("sub_lift", -10.0)

        with caplog.at_level(logging.WARNING, logger="lib.control.limit_monitor"):
            await rig.monitor.step()
            await rig.monitor.step()
            await rig.monitor.step()

        held = [r for r in caplog.records if "コートが未確定" in r.getMessage()]
        assert len(held) == 1

    async def test_コートが決まれば監視に戻る(self) -> None:
        rig = _CourtRig()
        rig.sensors["top_switch"] = True
        await rig.command("sub_lift", -10.0)
        await rig.monitor.step()

        rig.court = Court.RED
        await rig.monitor.step()

        assert rig.sent["sub_lift"] == [pytest.approx(0.0)]
        assert rig.monitor.intervention("sub_lift").count == 1
