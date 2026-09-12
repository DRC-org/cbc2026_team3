from __future__ import annotations

import inspect
import math
from unittest.mock import AsyncMock, MagicMock

import can
import pytest

from lib.drivers.base import ControlMode
from lib.manual import ManualControlError, ManualController, OperationMode
from lib.match_state import Court
from lib.sequence.motors import EStopActiveError, MotorGroup, MotorHandle
from lib.sequence.positions import (
    CourtUnresolvedError,
    PositionLookupError,
    load_position_table,
)
from tests.fake_drivers import StubFeedbackDriver


class _EchoDriver(StubFeedbackDriver):
    def __init__(self, name: str, *, follow: bool = True) -> None:
        super().__init__(name, 1)
        self.commands: list[tuple[ControlMode, float]] = []
        self._follow = follow

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        self.commands.append((mode, value))
        if self._follow:
            self.set_observed(position=value)
        return super().encode_target(mode, value)


_CONFIG = {
    "axes": {
        "y_axis": {
            "unit": "mm",
            "command_unit": "deg",
            "sync_tolerance": 2.0,
            "manual": {"min": -2.0, "max": 20.0, "steps": [0.5, 2.0]},
            "motors": {
                "y_axis_r": {"scale": 55.0},
                "y_axis_l": {"scale": -55.0},
            },
        },
        "rotate": {
            "unit": "deg",
            "command_unit": "rad",
            "scale": math.pi / 180.0,
            "manual": {"min": -5.0, "max": 30.0},
        },
        "gripper": {"unit": "deg", "command_unit": "deg"},
        "conveyor": {"unit": "duty", "command_mode": "duty", "settle_s": 0.0},
    },
    "positions": {
        "y_axis": {"home": 0.0, "work": 10.0, "place": {"red": 3.0, "blue": 6.0}},
        "rotate": {"home": 0.0, "pick": 8.0},
        "gripper": {"open": 5.0, "closed": 0.0},
        "conveyor": {"stop": 0.0, "run": 0.3},
    },
}


def _build(
    *, e_stop: bool = False, follow: bool = True
) -> tuple[ManualController, dict[str, _EchoDriver], MagicMock]:
    table = load_position_table(_CONFIG, source="<test>")
    mgr = MagicMock()
    mgr.send = AsyncMock()
    group = MotorGroup()
    drivers: dict[str, _EchoDriver] = {}
    for name in ("y_axis_r", "y_axis_l", "rotate", "gripper", "conveyor"):
        driver = _EchoDriver(name, follow=follow)
        drivers[name] = driver
        group.add(MotorHandle(name, driver, mgr, is_estop_active=lambda: e_stop))
    return ManualController(group, table), drivers, mgr


class TestOperationMode:
    def test_モータの制御モードとは別の語彙である(self) -> None:
        assert {mode.value for mode in OperationMode} == {"sequence", "manual"}
        assert OperationMode.MANUAL.value not in {mode.value for mode in ControlMode}


class TestPresetCommand:
    async def test_manual_を持たない離散軸でもプリセットは送れる(self) -> None:
        manual, drivers, _ = _build()
        value = await manual.move_to_position("gripper", "open")
        assert value == 5.0
        assert drivers["gripper"].commands == [(ControlMode.POSITION, 5.0)]

    async def test_duty_軸のプリセットは_duty_指令として出る(self) -> None:
        manual, drivers, _ = _build()
        await manual.move_to_position("conveyor", "run")
        assert drivers["conveyor"].commands == [(ControlMode.DUTY, 0.3)]

    async def test_コート別の位置は現在のコートで解決する(self) -> None:
        manual, drivers, _ = _build()
        manual.set_court(Court.BLUE)
        await manual.move_to_position("y_axis", "place")
        assert drivers["y_axis_r"].commands == [(ControlMode.POSITION, pytest.approx(6.0 * 55.0))]
        assert drivers["y_axis_l"].commands == [(ControlMode.POSITION, pytest.approx(-6.0 * 55.0))]

    async def test_未定義の位置名は理由付きで拒否する(self) -> None:
        manual, drivers, _ = _build()
        with pytest.raises(Exception, match="定義されていません"):
            await manual.move_to_position("gripper", "半開き")
        assert drivers["gripper"].commands == []


class TestContinuousCommand:
    async def test_manual_を持たない軸への絶対値指定は拒否する(self) -> None:
        manual, drivers, _ = _build()
        with pytest.raises(ManualControlError, match="連続操作の対象外"):
            await manual.set_value("gripper", 2.5)
        assert drivers["gripper"].commands == []

    async def test_manual_を持たない軸へのジョグは拒否する(self) -> None:
        manual, drivers, _ = _build()
        with pytest.raises(ManualControlError, match="連続操作の対象外"):
            await manual.jog("conveyor", 0.1)
        assert drivers["conveyor"].commands == []

    async def test_拒否理由に連続操作できる軸名を添える(self) -> None:
        manual, _, _ = _build()
        with pytest.raises(ManualControlError, match="y_axis, rotate"):
            await manual.set_value("gripper", 1.0)

    async def test_未定義の軸は定義済みの軸名を添えて拒否する(self) -> None:
        manual, _, _ = _build()
        with pytest.raises(ManualControlError, match="y_axis"):
            await manual.set_value("存在しない軸", 1.0)

    async def test_絶対値指定は単位換算して送る(self) -> None:
        manual, drivers, _ = _build()
        sent = await manual.set_value("rotate", 12.0)
        assert sent == pytest.approx(12.0)
        assert drivers["rotate"].commands == [
            (ControlMode.POSITION, pytest.approx(12.0 * math.pi / 180.0))
        ]


class TestClamp:
    async def test_上限を超える指定は上限へ丸める(self) -> None:
        manual, drivers, _ = _build()
        sent = await manual.set_value("y_axis", 999.0)
        assert sent == 20.0
        assert drivers["y_axis_r"].commands == [(ControlMode.POSITION, pytest.approx(20.0 * 55.0))]

    async def test_下限を下回る指定は下限へ丸める(self) -> None:
        manual, drivers, _ = _build()
        sent = await manual.set_value("y_axis", -999.0)
        assert sent == -2.0
        assert drivers["y_axis_r"].commands == [(ControlMode.POSITION, pytest.approx(-2.0 * 55.0))]

    async def test_ジョグも範囲を出ない(self) -> None:
        manual, _, _ = _build()
        await manual.set_value("y_axis", 19.0)
        assert await manual.jog("y_axis", 5.0) == 20.0
        assert await manual.jog("y_axis", 5.0) == 20.0

    async def test_範囲の外から始めた_1_歩は刻み幅を超えない(self) -> None:
        """起点が範囲の外に居ても、1 歩は刻み幅ぶんしか動かないこと。

        素の clamp を通していた頃は、境界まで一気に飛んだ (実機で実測 +9.96mm・
        max 2.0mm の軸へ -1.0 を送り約 8mm 動いた)。**零点確定がまだの軸は原点が
        電源投入位置なので、範囲の外に居るのは異常ではなく普通である。**
        """
        manual, drivers, _ = _build(follow=False)
        # 上限 20.0 の外側 (30.0mm 相当) に居る
        drivers["y_axis_r"].set_observed(position=30.0 * 55.0)
        drivers["y_axis_l"].set_observed(position=-30.0 * 55.0)

        assert await manual.jog("y_axis", -1.0) == pytest.approx(29.0)
        assert [value for _, value in drivers["y_axis_r"].commands] == [pytest.approx(29.0 * 55.0)]

    async def test_範囲の外からさらに外へは動かさない(self) -> None:
        """寄る向きだけを許す。外側へ広げる向きは従来どおり境界で止める。"""
        manual, drivers, _ = _build(follow=False)
        drivers["y_axis_r"].set_observed(position=30.0 * 55.0)
        drivers["y_axis_l"].set_observed(position=-30.0 * 55.0)

        assert await manual.jog("y_axis", 1.0) == pytest.approx(30.0)

    async def test_丸めた値がジョグの起点になる(self) -> None:
        manual, _, _ = _build()
        await manual.set_value("y_axis", 999.0)
        assert await manual.jog("y_axis", -1.0) == pytest.approx(19.0)


class TestJogOrigin:
    async def test_初回はフィードバックから起点を取る(self) -> None:
        manual, drivers, _ = _build(follow=False)
        drivers["y_axis_r"].set_observed(position=5.5 * 55.0)
        drivers["y_axis_l"].set_observed(position=-5.5 * 55.0)
        assert await manual.jog("y_axis", 1.0) == pytest.approx(6.5)

    async def test_追従が遅れていても押した回数ぶん積み上がる(self) -> None:
        manual, drivers, _ = _build(follow=False)
        results = [await manual.jog("y_axis", 2.0) for _ in range(3)]
        assert results == [pytest.approx(2.0), pytest.approx(4.0), pytest.approx(6.0)]
        assert [value for _, value in drivers["y_axis_r"].commands] == [
            pytest.approx(2.0 * 55.0),
            pytest.approx(4.0 * 55.0),
            pytest.approx(6.0 * 55.0),
        ]

    async def test_緊急停止で起点を捨てる(self) -> None:
        manual, drivers, _ = _build(follow=False)
        await manual.set_value("y_axis", 15.0)
        manual.on_e_stop()
        drivers["y_axis_r"].set_observed(position=2.0 * 55.0)
        drivers["y_axis_l"].set_observed(position=-2.0 * 55.0)
        assert await manual.jog("y_axis", 1.0) == pytest.approx(3.0)

    async def test_シーケンスが位置名で動かした後はその目標から動く(self) -> None:
        """箱 2 で詰めた値を箱 3 で起点にすると 160mm 飛ぶ (2026-09-12 に机上で発見)。"""
        manual, _, _ = _build(follow=False)
        await manual.jog("y_axis", 2.0)  # 箱 2 で詰めた
        # シーケンスが手動を通さずに次の箱へ動かした
        await manual._motors.y_axis_r.set_target(ControlMode.POSITION, 12.0 * 55.0)
        await manual._motors.y_axis_l.set_target(ControlMode.POSITION, -12.0 * 55.0)
        assert await manual.jog("y_axis", 2.0) == pytest.approx(14.0)

    async def test_モード切替の_reset_でも起点を捨てる(self) -> None:
        manual, drivers, _ = _build(follow=False)
        await manual.set_value("y_axis", 15.0)
        manual.reset()
        drivers["y_axis_r"].set_observed(position=0.0)
        drivers["y_axis_l"].set_observed(position=0.0)
        assert await manual.jog("y_axis", 1.0) == pytest.approx(1.0)


class TestNudgeBaseline:
    async def test_基準は同じ_epoch_の最初のジョグ直前の目標(self) -> None:
        manual, _, _ = _build(follow=False)
        await manual.set_value("y_axis", 10.0)
        await manual.jog("y_axis", 2.0, epoch=("sequence", 3))
        await manual.jog("y_axis", 2.0, epoch=("sequence", 3))
        assert manual.baseline("y_axis", ("sequence", 3)) == pytest.approx(10.0)
        # ステップが進んだら取り直す。前のステップの基準は返さない
        await manual.jog("y_axis", 1.0, epoch=("sequence", 4))
        assert manual.baseline("y_axis", ("sequence", 4)) == pytest.approx(14.0)
        assert manual.baseline("y_axis", ("sequence", 3)) is None
        assert manual.baseline("rotate", ("sequence", 4)) is None

    async def test_axes_info_は今の_epoch_の基準だけ配る(self) -> None:
        manual, _, _ = _build(follow=False)
        await manual.jog("y_axis", 2.0, epoch="a")
        by_name = {axis["name"]: axis for axis in manual.axes_info(epoch="a")}
        assert by_name["y_axis"]["baseline"] == pytest.approx(0.0)
        by_name = {axis["name"]: axis for axis in manual.axes_info(epoch="b")}
        assert by_name["y_axis"]["baseline"] is None


class TestPairedAxis:
    async def test_ペア軸は_1_回の指令で両モータへ届く(self) -> None:
        manual, drivers, _ = _build()
        await manual.set_value("y_axis", 4.0)
        assert len(drivers["y_axis_r"].commands) == 1
        assert len(drivers["y_axis_l"].commands) == 1

    async def test_逆回転は_scale_の符号で表され左右が反対向きになる(self) -> None:
        manual, drivers, _ = _build()
        await manual.set_value("y_axis", 4.0)
        (_, right), (_, left) = drivers["y_axis_r"].commands + drivers["y_axis_l"].commands
        assert right == pytest.approx(-left)

    async def test_左右へ逐次_await_せず同時に送る(self) -> None:
        import asyncio

        table = load_position_table(_CONFIG, source="<test>")
        events: list[str] = []

        async def _send(name: str, _msg: object) -> None:
            events.append(f"start:{name}")
            await asyncio.sleep(0)
            events.append(f"end:{name}")

        mgr = MagicMock()
        mgr.send = _send
        group = MotorGroup()
        for name in ("y_axis_r", "y_axis_l"):
            group.add(MotorHandle(name, _EchoDriver(name), mgr))

        await ManualController(group, table).set_value("y_axis", 4.0)

        assert events == [
            "start:y_axis_r",
            "start:y_axis_l",
            "end:y_axis_r",
            "end:y_axis_l",
        ], f"逐次送信になっている: {events}"

    async def test_片方のモータだけを動かす_API_を持たない(self) -> None:
        manual, _, _ = _build()
        public = {name for name in dir(manual) if not name.startswith("_")}
        motor_named = {name for name in public if "motor" in name}
        assert not {n for n in motor_named if inspect.iscoroutinefunction(getattr(manual, n))}
        assert motor_named == {"reset_axes_for_motors"}


class TestEStopInterlock:
    async def test_絶対値指定が拒否される(self) -> None:
        manual, drivers, mgr = _build(e_stop=True)
        with pytest.raises(EStopActiveError):
            await manual.set_value("y_axis", 4.0)
        assert drivers["y_axis_r"].commands == []
        mgr.send.assert_not_awaited()

    async def test_プリセット指定が拒否される(self) -> None:
        manual, drivers, mgr = _build(e_stop=True)
        with pytest.raises(EStopActiveError):
            await manual.move_to_position("gripper", "open")
        assert drivers["gripper"].commands == []
        mgr.send.assert_not_awaited()


class TestAxesInfo:
    def _by_name(self, manual: ManualController) -> dict[str, dict]:
        return {axis["name"]: axis for axis in manual.axes_info()}

    def test_全軸が並びプリセット名と値を持つ(self) -> None:
        manual, _, _ = _build()
        axes = self._by_name(manual)
        assert set(axes) == {"y_axis", "rotate", "gripper", "conveyor"}
        assert axes["gripper"]["positions"] == [
            {"name": "open", "value": 5.0},
            {"name": "closed", "value": 0.0},
        ]

    def test_コート未確定ならコート別の位置は値を出さない(self) -> None:
        # 「引けなかった」を片方のコートの値で埋めると、選び忘れが画面から読めない
        manual, _, _ = _build()
        assert self._by_name(manual)["y_axis"]["positions"][2] == {
            "name": "place",
            "value": None,
        }

    def test_コート別の位置は現在のコートの値で載る(self) -> None:
        manual, _, _ = _build()
        manual.set_court(Court.RED)
        assert self._by_name(manual)["y_axis"]["positions"][2] == {
            "name": "place",
            "value": 3.0,
        }

        manual.set_court(Court.BLUE)
        assert self._by_name(manual)["y_axis"]["positions"][2] == {
            "name": "place",
            "value": 6.0,
        }

    def test_値を引けない位置は空値で載せ配信を落とさない(self, monkeypatch) -> None:
        manual, _, _ = _build()

        def _boom(axis: str, name: str, **kwargs: object) -> float:
            if name == "work":
                raise PositionLookupError("引けない")
            return _raw(axis, name, **kwargs)

        _raw = manual._positions.raw
        monkeypatch.setattr(manual._positions, "raw", _boom)

        positions = self._by_name(manual)["y_axis"]["positions"]
        assert {"name": "work", "value": None} in positions
        assert {"name": "home", "value": 0.0} in positions

    def test_連続操作できる軸だけが可動範囲を持つ(self) -> None:
        manual, _, _ = _build()
        axes = self._by_name(manual)
        assert axes["y_axis"]["manual"] == {
            "min": -2.0,
            "max": 20.0,
            "steps": [0.5, 2.0],
            "labels": None,
        }
        assert axes["gripper"]["manual"] is None
        assert axes["conveyor"]["manual"] is None

    def test_現在値はフィードバックから人間の単位へ戻す(self) -> None:
        manual, drivers, _ = _build()
        drivers["y_axis_r"].set_observed(position=7.0 * 55.0)
        drivers["y_axis_l"].set_observed(position=-7.0 * 55.0)
        assert self._by_name(manual)["y_axis"]["value"] == pytest.approx(7.0)

    def test_位置を測れない軸の現在値は_None(self) -> None:
        manual, _, _ = _build()
        assert self._by_name(manual)["conveyor"]["value"] is None

    async def test_手動で送った目標値が載る(self) -> None:
        manual, _, _ = _build()
        assert self._by_name(manual)["rotate"]["target"] is None
        await manual.set_value("rotate", 9.0)
        assert self._by_name(manual)["rotate"]["target"] == pytest.approx(9.0)

    def test_ペア軸の左右偏差を軸の単位で載せる(self) -> None:
        manual, drivers, _ = _build()
        drivers["y_axis_r"].set_observed(position=7.0 * 55.0)
        drivers["y_axis_l"].set_observed(position=-5.0 * 55.0)
        assert self._by_name(manual)["y_axis"]["deviation"] == pytest.approx(2.0)

    def test_許容差も一緒に配る(self) -> None:
        manual, _, _ = _build()
        axes = self._by_name(manual)
        assert axes["y_axis"]["sync_tolerance"] == pytest.approx(2.0)
        assert axes["rotate"]["sync_tolerance"] is None

    def test_単独モータ軸の偏差は_None(self) -> None:
        manual, _, _ = _build()
        assert self._by_name(manual)["rotate"]["deviation"] is None

    def test_位置を測れない軸の偏差は_None(self) -> None:
        manual, _, _ = _build()
        assert self._by_name(manual)["conveyor"]["deviation"] is None


_COURT_CONFIG = {
    "axes": {
        "lift": {
            "unit": "mm",
            "command_unit": "rad",
            "scale": {"blue": 2.0, "red": -2.0},
            "manual": {"min": -30.0, "max": 0.0},
        }
    },
    "positions": {"lift": {"home": -5.0}},
}


class TestCourtScale:
    """コート別 scale の解決を手動操縦の経路が持つこと (`ManualController._axis`)。"""

    def _build(self, court: Court) -> tuple[ManualController, _EchoDriver]:
        table = load_position_table(_COURT_CONFIG, source="<test>")
        mgr = MagicMock()
        mgr.send = AsyncMock()
        group = MotorGroup()
        driver = _EchoDriver("lift")
        group.add(MotorHandle("lift", driver, mgr))
        return ManualController(group, table, court=court), driver

    def _unresolved(self) -> tuple[ManualController, _EchoDriver]:
        table = load_position_table(_COURT_CONFIG, source="<test>")
        mgr = MagicMock()
        mgr.send = AsyncMock()
        group = MotorGroup()
        driver = _EchoDriver("lift")
        group.add(MotorHandle("lift", driver, mgr))
        return ManualController(group, table), driver

    @pytest.mark.parametrize("send", ["set_value", "move_to_position", "jog"])
    async def test_コート未確定では指令を1通も出さない(self, send: str) -> None:
        """コマンドゲートの内側にある 2 枚目。**サーバーのゲートを外しても止まる。**"""
        ctrl, driver = self._unresolved()

        with pytest.raises(CourtUnresolvedError):
            if send == "move_to_position":
                await ctrl.move_to_position("lift", "home")
            elif send == "jog":
                await ctrl.jog("lift", -1.0)
            else:
                await ctrl.set_value("lift", -10.0)

        assert driver.commands == []

    async def test_set_value_uses_court_sign(self) -> None:
        red, red_driver = self._build(Court.RED)
        blue, blue_driver = self._build(Court.BLUE)

        await red.set_value("lift", -10.0)
        await blue.set_value("lift", -10.0)

        assert red_driver.commands == [(ControlMode.POSITION, 20.0)]
        assert blue_driver.commands == [(ControlMode.POSITION, -20.0)]

    async def test_move_to_position_and_jog_follow_court_change(self) -> None:
        ctrl, driver = self._build(Court.BLUE)
        ctrl.set_court(Court.RED)

        await ctrl.move_to_position("lift", "home")
        await ctrl.jog("lift", -1.0)

        assert driver.commands == [(ControlMode.POSITION, 10.0), (ControlMode.POSITION, 12.0)]

    def test_observed_value_and_axes_info_use_court_sign(self) -> None:
        ctrl, driver = self._build(Court.RED)
        driver.set_observed(position=20.0)

        assert ctrl.observed_value("lift") == pytest.approx(-10.0)
        assert ctrl.axes_info()[0]["value"] == pytest.approx(-10.0)
