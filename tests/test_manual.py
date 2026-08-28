"""手動操縦の指令経路 (lib/manual.py)。

見るのは 4 点だけで、いずれも「手動が新たに開けうる穴」に対応する:

- 可動範囲を宣言していない軸へ連続値を送れないこと (プリセットは送れること)
- 送る値が必ず可動範囲へ丸められること
- 左右直結ペアが **1 回の AxisHandle 経由**で同時に指令されること
- 緊急停止中は 1 通も出ないこと (インターロックは MotorHandle が持つ)

``AxisHandle`` 以下の経路はシーケンスと共通なので、そこの振る舞いは
test_sequence_move_to.py が既に見ている。ここでは手動固有の判断だけを見る。
"""

from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock

import can
import pytest

from lib.drivers.base import ControlMode
from lib.manual import ManualControlError, ManualController, OperationMode
from lib.match_state import Court
from lib.sequence.motors import EStopActiveError, MotorGroup, MotorHandle
from lib.sequence.positions import load_position_table
from tests.fake_drivers import StubFeedbackDriver


class _EchoDriver(StubFeedbackDriver):
    """指令値をフィードバックへ反映するテスト用ドライバ。

    ``follow=False`` にすると指令を受けてもフィードバックが動かない。実機で
    追従が遅れている状況 (ジョグの連打がまさにそれ) を作るために要る。
    追従するドライバだけでテストを書くと「起点を目標値で積む」実装と
    「毎回フィードバックから取る」実装が同じ結果になり、区別できない。
    """

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
        # 左右直結ペア。逆回転を scale の符号で表す
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
        # 離散状態アクチュエータ。manual: を持たないので連続操作の対象外
        "gripper": {"unit": "deg", "command_unit": "deg"},
        # duty 軸。位置の概念が無く、現在値も報告できない
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
        # lib.drivers.base.ControlMode (position/velocity/duty) と混ざらないこと。
        # 同じ名前だと「duty モードのロボット」のような読み違えが起きる
        assert {mode.value for mode in OperationMode} == {"sequence", "manual"}
        assert OperationMode.MANUAL.value not in {mode.value for mode in ControlMode}


class TestPresetCommand:
    """位置名による指令は全軸で使える (既定義の点しか送らないため)。"""

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
        # blue=6.0mm。逆回転ペアなので左右で符号が反転する
        assert drivers["y_axis_r"].commands == [(ControlMode.POSITION, pytest.approx(6.0 * 55.0))]
        assert drivers["y_axis_l"].commands == [(ControlMode.POSITION, pytest.approx(-6.0 * 55.0))]

    async def test_未定義の位置名は理由付きで拒否する(self) -> None:
        manual, drivers, _ = _build()
        with pytest.raises(Exception, match="定義されていません"):
            await manual.move_to_position("gripper", "半開き")
        assert drivers["gripper"].commands == []


class TestContinuousCommand:
    """連続値の指令は manual: を宣言した軸だけ。"""

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
    """可動範囲を出ないこと。手動が構造的保証を外した代わりの唯一の境界。"""

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
        # 端に張り付いた状態で押し続けても越えない
        assert await manual.jog("y_axis", 5.0) == 20.0
        assert await manual.jog("y_axis", 5.0) == 20.0

    async def test_丸めた値がジョグの起点になる(self) -> None:
        # 丸める前の値を起点にすると、上限で連打したぶんだけ「戻すのに空押しが要る」
        manual, _, _ = _build()
        await manual.set_value("y_axis", 999.0)
        assert await manual.jog("y_axis", -1.0) == pytest.approx(19.0)


class TestJogOrigin:
    """ジョグの起点は直前の手動目標。フィードバックではない。"""

    async def test_初回はフィードバックから起点を取る(self) -> None:
        manual, drivers, _ = _build(follow=False)
        # 55 deg/mm なので 5.5mm 相当の位置に居る
        drivers["y_axis_r"].set_observed(position=5.5 * 55.0)
        drivers["y_axis_l"].set_observed(position=-5.5 * 55.0)
        assert await manual.jog("y_axis", 1.0) == pytest.approx(6.5)

    async def test_追従が遅れていても押した回数ぶん積み上がる(self) -> None:
        # 起点を毎回フィードバックから取ると、追従が遅れているあいだの連打が吸われ、
        # 3 回押しても 1 回ぶんしか進まない (実機では「押しても動かない」に見える)
        manual, drivers, _ = _build(follow=False)
        results = [await manual.jog("y_axis", 2.0) for _ in range(3)]
        assert results == [pytest.approx(2.0), pytest.approx(4.0), pytest.approx(6.0)]
        # 送った指令値もフィードバックではなく目標値の積み上がりに従う
        assert [value for _, value in drivers["y_axis_r"].commands] == [
            pytest.approx(2.0 * 55.0),
            pytest.approx(4.0 * 55.0),
            pytest.approx(6.0 * 55.0),
        ]

    async def test_緊急停止で起点を捨てる(self) -> None:
        # 停止中に自重で下がっていた場合、古い起点から再開すると 1 回目が飛ぶ
        manual, drivers, _ = _build(follow=False)
        await manual.set_value("y_axis", 15.0)
        manual.on_e_stop()
        drivers["y_axis_r"].set_observed(position=2.0 * 55.0)
        drivers["y_axis_l"].set_observed(position=-2.0 * 55.0)
        assert await manual.jog("y_axis", 1.0) == pytest.approx(3.0)

    async def test_モード切替の_reset_でも起点を捨てる(self) -> None:
        manual, drivers, _ = _build(follow=False)
        await manual.set_value("y_axis", 15.0)
        manual.reset()
        drivers["y_axis_r"].set_observed(position=0.0)
        drivers["y_axis_l"].set_observed(position=0.0)
        assert await manual.jog("y_axis", 1.0) == pytest.approx(1.0)


class TestPairedAxis:
    """左右直結ペアは 1 軸として同時に指令される。"""

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
        """送信を逐次 await すると、その時間差ぶんだけ直結機構がねじれる。

        ``AxisHandle.set_target_value`` は ``asyncio.gather`` で束ねている。
        ここを素の for + await へ書き換えると、下の送信ログが
        start/end/start/end (逐次) になり、この検証が落ちる。
        """
        import asyncio

        table = load_position_table(_CONFIG, source="<test>")
        events: list[str] = []

        async def _send(name: str, _msg: object) -> None:
            events.append(f"start:{name}")
            # 1 回でも制御を手放せば、同時に走っている送信が割り込める
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
        # モータ名で指令できる口を生やすと、そこを通った瞬間に機構がねじれる
        manual, _, _ = _build()
        public = {name for name in dir(manual) if not name.startswith("_")}
        assert not {name for name in public if "motor" in name}


class TestEStopInterlock:
    """緊急停止中は 1 通も出ない (インターロックは MotorHandle が持つ)。"""

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
    """UI へ配る軸一覧。軸名も可動範囲も UI 側にハードコードさせない。"""

    def _by_name(self, manual: ManualController) -> dict[str, dict]:
        return {axis["name"]: axis for axis in manual.axes_info()}

    def test_全軸が並びプリセット名を持つ(self) -> None:
        manual, _, _ = _build()
        axes = self._by_name(manual)
        assert set(axes) == {"y_axis", "rotate", "gripper", "conveyor"}
        assert axes["gripper"]["positions"] == ["open", "closed"]

    def test_連続操作できる軸だけが可動範囲を持つ(self) -> None:
        manual, _, _ = _build()
        axes = self._by_name(manual)
        assert axes["y_axis"]["manual"] == {"min": -2.0, "max": 20.0, "steps": [0.5, 2.0]}
        assert axes["gripper"]["manual"] is None
        assert axes["conveyor"]["manual"] is None

    def test_現在値はフィードバックから人間の単位へ戻す(self) -> None:
        manual, drivers, _ = _build()
        drivers["y_axis_r"].set_observed(position=7.0 * 55.0)
        drivers["y_axis_l"].set_observed(position=-7.0 * 55.0)
        assert self._by_name(manual)["y_axis"]["value"] == pytest.approx(7.0)

    def test_位置を測れない軸の現在値は_None(self) -> None:
        # DC 基板はエンコーダを持たない。逆換算した 0 を載せると
        # 「測ったように見える 0」が UI へ流れ込む
        manual, _, _ = _build()
        assert self._by_name(manual)["conveyor"]["value"] is None

    async def test_手動で送った目標値が載る(self) -> None:
        manual, _, _ = _build()
        assert self._by_name(manual)["rotate"]["target"] is None
        await manual.set_value("rotate", 9.0)
        assert self._by_name(manual)["rotate"]["target"] == pytest.approx(9.0)
