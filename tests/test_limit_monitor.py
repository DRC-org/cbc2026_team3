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

    async def test_押されていた端から離れる途中の接触は止めない(self) -> None:
        """離れ際の接点のバウンド。指令の時点で ON だった端は退避の妨げにしない。"""
        rig = _rig(rear_switch=True)
        rig.set_observed(-447.5)
        await rig.command(-440.0)
        await rig.monitor.step()

        rig.sensors["rear_switch"] = False
        rig.pulse("rear_switch")
        await rig.monitor.step()

        assert rig.sent == []


class TestUnexpectedContact:
    """**進む向きと反対側として宣言された端が、移動中に押されたら止める。**

    2026-09-10 実機: `sub_y_axis` を `retracted` (-430) へ手動で 1 回書き、後端の
    リミットスイッチを踏み越えた。監視は進む向き (minus) の `sub_y_axis_r_limit_sensor`
    だけを見ていたが、そのセンサは一度も ON にならず、押されていたのは反対側として
    宣言された端だった (NC 化の配線替えでスロットが入れ替わった)。向き・配線・
    `scale` の符号のどれを取り違えても症状は同じで、**目標を書いた時点で OFF だった端が
    どちら側であれ ON になった**ことだけが共通の徴候である。
    離れる向きの退避 (書いた時点で ON) は今までどおり通す。
    """

    async def test_反対側として宣言された端が移動中に押されたら止める(self) -> None:
        """事故そのもの。minus へ進む移動中に plus 側の front_switch が ON になった。"""
        rig = _rig(rear_switch=False, front_switch=False)
        rig.set_observed(-440.0)
        await rig.command(-450.0)
        await rig.monitor.step()
        assert rig.sent == []

        rig.set_observed(-445.0)
        rig.sensors["front_switch"] = True
        await rig.monitor.step()

        assert rig.sent == [-445.0 * _SCALE]
        record = rig.monitor.intervention("sub_y_axis")
        assert record.count == 1
        assert record.reason is not None
        assert "front_switch" in record.reason

    async def test_反対側の狭い接触も止める(self) -> None:
        """現在値が OFF に戻っていても、この周期に数えた接触は押されたと読む。"""
        rig = _rig(rear_switch=False, front_switch=False)
        rig.set_observed(-440.0)
        await rig.command(-450.0)
        await rig.monitor.step()

        rig.pulse("front_switch")
        await rig.monitor.step()

        assert rig.sent == [-440.0 * _SCALE]

    async def test_指令の時点で押されていた端からは離れられる(self) -> None:
        """端に張り付いた軸の退避と零点確定の離脱段。塞ぐと戻せなくなる。"""
        rig = _rig(rear_switch=False, front_switch=True)
        rig.set_observed(-1.0)
        await rig.command(-10.0)

        await rig.monitor.step()
        await rig.monitor.step()

        assert rig.sent == []

    async def test_反対側の端が読めなくなっても離れる向きは通す(self) -> None:
        """途絶した端は退避の妨げにしない。読めていない端へ向かう指令は入口が拒む。"""
        rig = _rig(rear_switch=False, front_switch=False)
        rig.set_observed(-440.0)
        await rig.command(-450.0)
        await rig.monitor.step()

        rig.sensors["front_switch"] = None
        await rig.monitor.step()

        assert rig.sent == []

    async def test_止めた後に押されたままの端から離れる指令は通す(self) -> None:
        """止めた瞬間の目標ではなく、次に書かれた目標の時点の状態を基準にする。"""
        rig = _rig(rear_switch=False, front_switch=False)
        rig.set_observed(-440.0)
        await rig.command(-450.0)
        await rig.monitor.step()
        rig.sensors["front_switch"] = True
        await rig.monitor.step()
        assert len(rig.sent) == 1

        await rig.command(-430.0)
        await rig.monitor.step()

        assert len(rig.sent) == 1

    async def test_踏み越えた端を戻る途中の接触は一度止め_次の指令で通す(self) -> None:
        """踏み越えた先から戻る移動は、配線の取り違えと監視からは見分けが付かない。

        一度は止めて操縦者に見せ、押されたままの端から離れる次の指令は通す
        (指令の時点で ON なので退避として読める)。
        """
        rig = _rig(rear_switch=False)
        rig.set_observed(-450.0)
        await rig.command(-440.0)
        await rig.monitor.step()

        rig.set_observed(-447.0)
        rig.sensors["rear_switch"] = True
        await rig.monitor.step()
        assert rig.sent == [-447.0 * _SCALE]

        await rig.command(-440.0)
        await rig.monitor.step()
        rig.sensors["rear_switch"] = False
        rig.set_observed(-444.0)
        await rig.monitor.step()

        assert len(rig.sent) == 1

    async def test_進む向きの端が押されたときの文面は変えない(self) -> None:
        """既存の判定が先に言う。反対側の文面が混ざると配線を疑う方向が逆になる。"""
        rig = _rig(rear_switch=True, front_switch=False)
        rig.set_observed(-447.5)
        await rig.command(-450.0)

        await rig.monitor.step()

        reason = rig.monitor.intervention("sub_y_axis").reason
        assert reason is not None
        assert "rear_switch" in reason
        assert "反対" not in reason


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
