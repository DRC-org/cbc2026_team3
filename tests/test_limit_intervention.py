"""リミット保護が介入した移動を「成功」と読ませない (`Sequence.move_to`)。

**保護そのものはここでは見ない。** 触れたら止まること (層①) は
`tests/test_limit_guard.py`、禁止方向がクランプされること (層②) は
`tests/test_limit_clamp.py` が層ごとに見ている。ここで見たいのは 1 つだけ ——
**保護が要求を曲げた移動が、シーケンスのステップとして失敗すること**である。

なぜ要るか: 層①の停止は「目標を実測位置へ引き戻す」ことなので、`MotorHandle` が
持つ目標は触れた位置そのものになる。到達判定は**送った目標との比較**なので必ず
成立し、`wait_reached` は True を返す —— 軸は端に居るのに、シーケンスは位置定数に
書いた場所へ到達したとして次のステップへ進む。CLAUDE.md が繰り返し禁じている
「黙って別の位置で成功する機体」そのものである。

**したがって、この判定は「今ラッチしているか」に置けない。** 接点がバウンドして
触れて離れればラッチは外れるが、書き戻された目標は残るので軸は触れた位置で
止まったままになる (元の目標を再送する経路は無い)。単調カウンタでしか拾えない。
"""

from __future__ import annotations

import asyncio

import pytest

from lib.control.limit_guard import LimitGuard, combine_limit_guards
from lib.sequence.engine import Sequence, SequenceTimeoutError, step
from lib.sequence.motors import AxisHandle, MotorGroup, MotorHandle, build_axis_handle
from lib.sequence.positions import load_position_table
from tests.fake_can import mock_can_manager
from tests.fake_drivers import StubFeedbackDriver

_SENSOR = "origin_sensor"

#: 実機の y_axis と同じ逆回転ペア。**単一モータ軸にしない** —— 引き戻しも
#: クランプも軸単位の指令 1 回に載っていることは、左右 2 台で初めて確かめられる
_SCALE = 2.0

_CONFIG = {
    "axes": {
        "y_axis": {
            "unit": "mm",
            "command_unit": "deg",
            "tolerance": 0.1,
            # 到達しない軸を待ち続けないための上限。**テストの筋書きはこの時間を
            # 使い切らない** —— 使い切って落ちたなら、介入ではなく到達待ちの
            # タイムアウトで失敗しており、見たいものを見ていない
            "timeout_s": 1.0,
            "sync_tolerance": 100.0,
            "limits": [{"sensor": _SENSOR, "direction": -1}],
            "motors": {"y_axis_r": {"scale": _SCALE}, "y_axis_l": {"scale": -_SCALE}},
        }
    },
    "positions": {"y_axis": {"home": 0.0, "minus_end": -50.0, "plus_end": 50.0}},
}


class _MoveSequence(Sequence):
    @step("移動")
    async def move(self) -> None:  # pragma: no cover - 直接 move_to を呼ぶ
        await self.move_to({"y_axis": "minus_end"})


class _Fixture:
    """**両方の層を生かした**本番と同じ配線。機構だけがテストの手で動く。

    実測位置を指令へ追従させないのは、追従させると指令した瞬間に到達してしまい
    「移動の途中で触れる」窓そのものが作れないため。
    """

    def __init__(self, *, combined: bool = False, bind: bool = True) -> None:
        self.table = load_position_table(_CONFIG, source="<test>")
        self.spec = self.table.axis("y_axis")
        self.active = False
        self.count = 0

        mgr = mock_can_manager()
        self.group = MotorGroup()
        self.drivers: dict[str, StubFeedbackDriver] = {}
        for motor in self.spec.motors:
            driver = StubFeedbackDriver(motor.name, 1)
            driver.set_observed(position=0.0)
            self.drivers[motor.name] = driver
            self.group.add(MotorHandle(motor.name, driver, mgr, poll_interval=0.001))

        self.guard = LimitGuard(
            [self.spec],
            sensor_active=lambda _name: self.active,
            sensor_contact_count=lambda _name: self.count,
            sensor_is_stale=lambda _name: False,
            # 本番 (`main._make_axis_handle_lookup`) と同じ遅延取得。ここで組むので
            # `bind_limit_guard` が後から結んだ保護がそのまま乗る
            axis_handle=self._axis_handle,
        )
        if bind:
            # 統合動作確認は両ハンドの保護を束ねた口を通る
            self.group.bind_limit_guard(
                combine_limit_guards([LimitGuard([], **self._sensor_taps()), self.guard])
                if combined
                else self.guard
            )

        self.sequence = _MoveSequence("limit_seq")
        self.sequence.bind_motors(self.group)
        self.sequence.bind_positions(self.table)

    def _sensor_taps(self) -> dict:
        """軸を 1 つも持たない保護 (もう片方のハンド) を組むための注入口。"""
        return {
            "sensor_active": lambda _name: False,
            "sensor_contact_count": lambda _name: 0,
            "sensor_is_stale": lambda _name: False,
            "axis_handle": lambda _name: None,
        }

    def _axis_handle(self, axis: str) -> AxisHandle | None:
        if axis != self.spec.name:
            return None
        return build_axis_handle(self.table.axis(axis), self.group)

    # -- 機構とスイッチ --------------------------------------------------- #

    def place(self, value: float) -> None:
        """実測の軸位置を与える (順換算はモータ定義に委ねる)。"""
        for motor in self.spec.motors:
            self.drivers[motor.name].set_observed(position=motor.to_command(value))

    def touch(self) -> None:
        self.active = True
        self.count += 1

    def release(self) -> None:
        self.active = False

    # -- シーケンス ------------------------------------------------------- #

    def start_move(self, position: str) -> asyncio.Task:
        return asyncio.create_task(self.sequence.move_to({"y_axis": position}))

    @property
    def interventions(self) -> int:
        guard = self.group.limit_guard
        return 0 if guard is None else guard.intervention("y_axis").count

    def reached(self) -> bool:
        """送った目標に対する到達判定 (**引き戻された目標でも成立する**)。"""
        return all(self.group[name].is_reached() for name in self.spec.motor_names)


async def _enter_wait(task: asyncio.Task) -> None:
    """`move_to` が指令を送り終えて到達待ちに入るまで進める。

    ここで既に終わっていたら、その後に作る「移動の途中」は移動の途中ではない。
    """
    await asyncio.sleep(0.01)
    assert not task.done(), "到達待ちに入る前に move_to が終わっています"


class TestTouchedWhileMoving:
    """**移動の途中で触れたら失敗する。** この段の主目的。"""

    async def test_移動の途中で触れた移動は失敗する(self) -> None:
        fx = _Fixture()
        task = fx.start_move("minus_end")
        await _enter_wait(task)

        fx.place(-5.0)
        fx.touch()
        await fx.guard.step()

        with pytest.raises(SequenceTimeoutError, match="リミットスイッチ"):
            await task
        # **到達判定そのものは成立している。** 失敗させたのは介入カウンタだけで
        # あり、比較を消せばこのステップは -5mm の位置で「成功」する
        assert fx.reached() is True
        assert fx.interventions == 1

    async def test_触れて離れた移動も失敗する(self) -> None:
        """接点のバウンド。**終了時のラッチを見る実装では 1 件も拾えない。**

        ラッチは外れるが、層①が書き戻した目標は残るので軸は触れた位置で
        止まったままになる。
        """
        fx = _Fixture()
        task = fx.start_move("minus_end")
        await _enter_wait(task)

        fx.place(-5.0)
        fx.touch()
        await fx.guard.step()
        fx.release()
        await fx.guard.step()

        assert fx.guard.latched == {}
        with pytest.raises(SequenceTimeoutError, match="リミットスイッチ"):
            await task
        assert fx.reached() is True

    async def test_失敗の理由にセンサ名を添える(self) -> None:
        """「到達しませんでした」だけでは、保護と配線不良で手当てが正反対になる。"""
        fx = _Fixture()
        task = fx.start_move("minus_end")
        await _enter_wait(task)

        fx.place(-5.0)
        fx.touch()
        await fx.guard.step()

        with pytest.raises(SequenceTimeoutError, match=_SENSOR):
            await task


class TestClampedAtEntry:
    """**入口で頭打ちになった移動も失敗する** (層②を通った介入)。"""

    async def test_触れたまま出した移動は失敗する(self) -> None:
        fx = _Fixture()
        fx.place(-5.0)
        fx.touch()
        await fx.guard.step()
        # 目標を 1 つも持たない軸へは書き戻さないので、ここまでの介入は 0 件
        assert fx.interventions == 0

        with pytest.raises(SequenceTimeoutError, match="リミットスイッチ"):
            await fx.sequence.move_to({"y_axis": "minus_end"})

        assert fx.reached() is True
        assert fx.interventions == 1


class TestNoIntervention:
    """**曲げていない移動まで失敗させない。** 保護は退避路を塞がない。"""

    async def test_保護が介入しない移動は成功する(self) -> None:
        fx = _Fixture()
        task = fx.start_move("minus_end")
        await _enter_wait(task)

        fx.place(-50.0)

        await task
        assert fx.interventions == 0

    async def test_触れていても逆方向の移動は成功する(self) -> None:
        """**復帰できない軸を作らないことがこの保護の前提である。**"""
        fx = _Fixture()
        fx.place(-5.0)
        fx.touch()
        await fx.guard.step()

        task = fx.start_move("plus_end")
        await _enter_wait(task)
        fx.place(50.0)

        await task
        assert fx.interventions == 0

    async def test_保護を結んでいない軸は従来どおり(self) -> None:
        """カウンタを引く先が無くても `move_to` は成立する (保護なしの構成)。"""
        fx = _Fixture(bind=False)
        task = fx.start_move("minus_end")
        await _enter_wait(task)

        fx.place(-50.0)

        await task


class TestCombinedGuard:
    """統合動作確認は両ハンドの保護を束ねた `CombinedLimitGuard` を通る。

    束ねる側にカウンタが無いと、**動作確認のステップだけが保護の介入を
    見落とす** —— 一番「動くはずのものが動いたか」を確かめたい経路である。
    """

    async def test_束ねた保護でも介入した移動は失敗する(self) -> None:
        fx = _Fixture(combined=True)
        task = fx.start_move("minus_end")
        await _enter_wait(task)

        fx.place(-5.0)
        fx.touch()
        await fx.guard.step()

        with pytest.raises(SequenceTimeoutError, match="リミットスイッチ"):
            await task
        assert fx.interventions == 1

    async def test_束ねた保護でも介入しない移動は成功する(self) -> None:
        fx = _Fixture(combined=True)
        task = fx.start_move("minus_end")
        await _enter_wait(task)

        fx.place(-50.0)

        await task
        assert fx.interventions == 0
