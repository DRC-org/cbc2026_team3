"""リミット保護の**指令の入口**でのクランプ (層②)。

**この層だけを見る。** 50Hz の常駐層 (層①) は「触れたら目標を実測位置へ引き戻す」
ので、両方を生かした条件では層②を丸ごと消しても結果が同じに見える (20ms 遅れて
同じ位置へ落ち着く)。ここでは常駐層の指令口を ``None`` にして**引き戻しが 1 通も
出ない**条件を作り、入口のクランプだけが効いていることを見る。

見たいことは 4 つ:

1. 禁止方向の指令が端で頭打ちになり、**逆方向はそのまま通る**
2. 保護の無い軸・位置を持たない軸・実測が読めない軸では**歪めない**
3. 実際に送った指令が呼び出し側へ返り、**手動のジョグ起点が端の外へ伸びない**
4. 頭打ちになったステップは**成功しない** (別の位置で成功する機体を作らない)
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import AsyncMock, MagicMock

import can
import pytest

from lib.control.limit_guard import LimitGuard
from lib.drivers.base import ControlMode
from lib.manual import ManualController
from lib.sequence.engine import Sequence, SequenceTimeoutError, step
from lib.sequence.motors import LimitIntervention, MotorGroup, MotorHandle, build_axis_handle
from lib.sequence.positions import load_position_table
from tests.fake_drivers import StubFeedbackDriver

#: 実機の y_axis と同じ逆回転ペア。**単一モータ軸にしない** —— クランプが軸単位の
#: 指令 1 回に載っていることは、左右 2 台ぶんが同じ値へ揃うことでしか確かめられない
_SCALE = 2.0

_SENSOR = "origin_sensor"

_CONFIG = {
    "axes": {
        "y_axis": {
            "unit": "mm",
            "command_unit": "deg",
            "tolerance": 0.1,
            # 到達しない軸を待ち続けないための短い上限 (実機の値とは無関係)
            "timeout_s": 0.05,
            "sync_tolerance": 100.0,
            "limits": [{"sensor": _SENSOR, "direction": -1}],
            "manual": {"min": -100.0, "max": 100.0, "steps": [10.0]},
            "motors": {"y_axis_r": {"scale": _SCALE}, "y_axis_l": {"scale": -_SCALE}},
        },
        # duty 軸。位置も到達も観測できないので、逆換算そのものが走ってはならない
        "conveyor": {"unit": "%", "command_unit": "duty", "command_mode": "duty"},
    },
    "positions": {
        "y_axis": {"home": 0.0, "minus_end": -50.0, "plus_end": 50.0},
        "conveyor": {"run": 0.5, "stop": 0.0},
    },
}


class _EchoDriver(StubFeedbackDriver):
    """指令値をそのままフィードバックへ反映する (機構が必ず追い付く) ドライバ。"""

    def __init__(self, name: str) -> None:
        super().__init__(name, 1)
        self.commands: list[float] = []

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        self.commands.append(value)
        self.set_observed(position=value)
        return super().encode_target(mode, value)


class _BlindDriver(_EchoDriver):
    """実測位置を答えられないドライバ (未受信・逆換算不能)。"""

    def feedback_position(self) -> float:
        raise RuntimeError("フィードバックがありません")


class _RecordingClamp:
    """``clamp`` が呼ばれたかだけを見る差し替え。

    保護の対象でない軸で**逆換算すら走らない**ことは、判定が呼ばれないことでしか
    確かめられない (呼ばれてから素通しにする実装は、duty 軸で例外を出す)。
    """

    def __init__(self, axis_names: tuple[str, ...] = ()) -> None:
        self._axis_names = axis_names
        self.calls: list[tuple[str, float, float]] = []

    @property
    def axis_names(self) -> tuple[str, ...]:
        return self._axis_names

    def clamp(self, axis: str, value: float, observed: float) -> float:
        self.calls.append((axis, value, observed))
        return value

    def intervention(self, axis: str) -> LimitIntervention:
        """曲げないので介入も 0 件 (`LimitClamp` を満たすための実装)。"""
        return LimitIntervention()


class _Fixture:
    """保護を結んだモータ群 + スイッチ 1 本 (マイナス端)。

    ``LimitGuard`` の指令口は ``None`` に固定してある —— **層①が 1 通も送らない
    条件**を作るためで、ここで観測される指令はすべて入口のクランプの結果である。
    """

    def __init__(self, *, bind: bool = True, blind: bool = False) -> None:
        self.table = load_position_table(_CONFIG, source="<test>")
        self.spec = self.table.axis("y_axis")
        self.active = False
        self.count = 0
        self.stale = False

        can_manager = MagicMock()
        can_manager.send = AsyncMock()
        self.group = MotorGroup()
        self.drivers: dict[str, _EchoDriver] = {}
        for name in (*self.spec.motor_names, *self.table.axis("conveyor").motor_names):
            driver = (_BlindDriver if blind else _EchoDriver)(name)
            self.drivers[name] = driver
            self.group.add(MotorHandle(name, driver, can_manager, poll_interval=0.001))

        self.guard = LimitGuard(
            [self.spec],
            sensor_active=lambda _name: self.active,
            sensor_contact_count=lambda _name: self.count,
            sensor_is_stale=lambda _name: self.stale,
            # 層①の引き戻しを封じる (この条件で残るのは入口のクランプだけ)
            axis_handle=lambda _name: None,
        )
        if bind:
            self.group.bind_limit_guard(self.guard)

    # -- 操作 ------------------------------------------------------------ #

    async def latch(self) -> None:
        """スイッチに触れさせ、保護をラッチさせる。"""
        self.active = True
        self.count += 1
        await self.guard.step()

    async def release(self) -> None:
        self.active = False
        await self.guard.step()

    def place(self, value: float) -> None:
        """実測の軸位置を与える (順換算はモータ定義に委ねる)。"""
        for motor in self.spec.motors:
            self.drivers[motor.name].set_observed(position=motor.to_command(value))

    async def command(self, value: float, axis: str = "y_axis") -> float:
        """軸へ指令し、**実際に送られた軸位置**を返す。"""
        spec = self.table.axis(axis)
        handle = build_axis_handle(spec, self.group)
        return spec.to_value(await handle.set_target_value(spec.to_commands(value)))

    def commanded(self, axis: str = "y_axis") -> list[float]:
        """ドライバが実際に受け取った指令を軸の単位で並べる。"""
        spec = self.table.axis(axis)
        return [
            spec.to_value({motor.name: self.drivers[motor.name].commands[index]})
            for index in range(len(self.drivers[spec.motor_names[0]].commands))
            for motor in spec.motors
        ]


class TestBlockedDirection:
    """**触れている向きへは進めない。逆向きは必ず通る。**"""

    async def test_禁止方向の指令は端で頭打ちになる(self) -> None:
        fx = _Fixture()
        fx.place(-10.0)
        await fx.latch()

        sent = await fx.command(-50.0)

        assert sent == pytest.approx(-10.0)
        # 左右 2 台とも端の位置を受け取る (片側だけ動くとその場で機構が壊れる)
        assert fx.commanded() == [pytest.approx(-10.0), pytest.approx(-10.0)]

    async def test_逆方向の指令はそのまま通る(self) -> None:
        """**復帰できない軸を作らないことが、この保護の前提である。**"""
        fx = _Fixture()
        fx.place(-10.0)
        await fx.latch()

        assert await fx.command(30.0) == pytest.approx(30.0)
        assert fx.commanded() == [pytest.approx(30.0), pytest.approx(30.0)]

    async def test_ラッチしていない軸は素通しする(self) -> None:
        fx = _Fixture()
        fx.place(-10.0)

        assert await fx.command(-50.0) == pytest.approx(-50.0)

    async def test_スイッチから離れれば再び通る(self) -> None:
        """ラッチはセンサが OFF になったら自動で外れる (明示操作を要求しない)。"""
        fx = _Fixture()
        fx.place(-10.0)
        await fx.latch()
        await fx.release()

        assert await fx.command(-50.0) == pytest.approx(-50.0)

    async def test_保護を結んでいないモータ群では効かない(self) -> None:
        """結び忘れは「その経路だけ黙って効かない」形でしか現れない (層②の存在確認)。"""
        fx = _Fixture(bind=False)
        fx.place(-10.0)
        await fx.latch()

        assert await fx.command(-50.0) == pytest.approx(-50.0)


class TestNotClamped:
    """**歪めてよい指令とそうでない指令の境目。**"""

    async def test_保護の対象でない軸では判定すら呼ばない(self) -> None:
        """duty / on_off 軸に ``limits`` は書けない。**逆換算が走ってはならない。**"""
        fx = _Fixture(bind=False)
        clamp = _RecordingClamp()
        fx.group.bind_limit_guard(clamp)

        assert await fx.command(0.5, axis="conveyor") == pytest.approx(0.5)
        assert clamp.calls == []

    async def test_位置を持たない軸は実測が読めないので歪めない(self) -> None:
        """保護が対象だと主張しても、位置を観測できない軸の指令は変えない。"""
        fx = _Fixture(bind=False)
        clamp = _RecordingClamp(axis_names=("conveyor",))
        fx.group.bind_limit_guard(clamp)

        assert await fx.command(0.5, axis="conveyor") == pytest.approx(0.5)
        assert clamp.calls == []

    async def test_実測が読めないときはクランプしない(self) -> None:
        """未受信の 0.0 を現在位置と信じて端を推定すると、指令のほうを歪める。

        同じ状況では層①も引き戻し先を持てないので、ここで拒否しても保護は
        厚くならない。
        """
        fx = _Fixture(blind=True)
        await fx.latch()

        assert await fx.command(-50.0) == pytest.approx(-50.0)


class TestReturnValue:
    """**実際に送った指令を返す。** 返さないと呼び出し側が要求値を信じ続ける。"""

    async def test_頭打ちになった指令が返る(self) -> None:
        fx = _Fixture()
        fx.place(-10.0)
        await fx.latch()
        handle = build_axis_handle(fx.spec, fx.group)

        sent = await handle.set_target_value(fx.spec.to_commands(-50.0))

        assert sent == fx.spec.to_commands(-10.0)

    async def test_素通しなら要求した指令がそのまま返る(self) -> None:
        fx = _Fixture()
        handle = build_axis_handle(fx.spec, fx.group)
        commands = fx.spec.to_commands(-50.0)

        assert await handle.set_target_value(commands) == commands


class TestManualJog:
    """**ジョグの起点はクランプ後の値でなければならない。**

    起点は「直前に手動で送った目標値」なので (毎回フィードバックから取り直すと
    追従中の連打が吸われる)、クランプを知らない起点は押すたびに禁止側へ伸び、
    逆方向へ退避しようとしても「押した回数だけ戻らない」。
    """

    async def test_禁止方向へ連打しても起点が端の外へ伸びない(self) -> None:
        fx = _Fixture()
        manual = ManualController(fx.group, fx.table)
        await fx.latch()

        assert [await manual.jog("y_axis", -10.0) for _ in range(3)] == [0.0, 0.0, 0.0]
        # 起点が -30 まで伸びていれば、ここで返るのは -20 になる
        assert await manual.jog("y_axis", 10.0) == pytest.approx(10.0)

    async def test_絶対値指令もクランプ後の値を返す(self) -> None:
        """操縦者と画面が見るのは、要求値ではなく実際に送った値である。"""
        fx = _Fixture()
        manual = ManualController(fx.group, fx.table)
        await fx.latch()

        assert await manual.set_value("y_axis", -40.0) == pytest.approx(0.0)

    async def test_位置名指令もクランプ後の値を返す(self) -> None:
        fx = _Fixture()
        manual = ManualController(fx.group, fx.table)
        await fx.latch()

        assert await manual.move_to_position("y_axis", "minus_end") == pytest.approx(0.0)

    async def test_可動範囲のクランプは従来どおり効く(self) -> None:
        """境界は 2 つあり、どちらも拒否ではなくクランプで揃えてある。"""
        fx = _Fixture()
        manual = ManualController(fx.group, fx.table)

        assert await manual.set_value("y_axis", 500.0) == pytest.approx(100.0)


class _MoveSequence(Sequence):
    def __init__(self) -> None:
        super().__init__("limit_seq")

    @step("移動")
    async def move(self) -> None:
        await self.move_to({"y_axis": "minus_end"})


class TestSequenceMoveTo:
    """**頭打ちになったステップを成功させない。**

    頭打ちの指令はその瞬間の実測位置そのものなので、送った目標との比較 (到達判定)
    は必ず成立する。特別扱いせずに放置すると、位置定数に書いたのとは別の場所で
    成功するステップができ、次のステップはその姿勢を前提に動く。
    """

    def _sequence(self, fx: _Fixture) -> _MoveSequence:
        sequence = _MoveSequence()
        sequence.bind_motors(fx.group)
        sequence.bind_positions(fx.table)
        return sequence

    async def test_端を越える移動は失敗する(self) -> None:
        fx = _Fixture()
        await fx.latch()

        with pytest.raises(SequenceTimeoutError, match="y_axis"):
            await self._sequence(fx).move_to({"y_axis": "minus_end"})

    async def test_失敗の理由にリミットスイッチを名指しする(self) -> None:
        """「到達しなかった」だけでは、操縦者は配線不良と区別できない。"""
        fx = _Fixture()
        await fx.latch()

        with pytest.raises(SequenceTimeoutError, match="リミットスイッチ"):
            await self._sequence(fx).move_to({"y_axis": "minus_end"})

    async def test_逆方向の移動は従来どおり成功する(self) -> None:
        fx = _Fixture()
        await fx.latch()

        await self._sequence(fx).move_to({"y_axis": "plus_end"})

        assert fx.commanded() == [pytest.approx(50.0), pytest.approx(50.0)]


class TestHomingSuspend:
    """零点確定は「当たるまで動かす」動作なので、その間クランプは素通しになる。"""

    async def test_一時停止中はクランプしない(self) -> None:
        """外れていないと、スイッチに触れた瞬間に自分の指令が引き戻され原点へ
        到達できない (探索の 1 歩目がちょうどその窓に入る)。
        """
        fx = _Fixture()
        await fx.latch()

        with fx.guard.suspend_sensors([_SENSOR]):
            assert await fx.command(-50.0) == pytest.approx(-50.0)

    async def test_一時停止を抜ければ再び効く(self) -> None:
        fx = _Fixture()
        with fx.guard.suspend_sensors([_SENSOR]):
            pass
        await fx.latch()

        assert await fx.command(-50.0) == pytest.approx(0.0)


class TestAxisHandleFactory:
    """**軸ハンドルの生成口を 1 つに絞る。**

    保護を各生成箇所へ引数で配る形にすると、渡し忘れが「その経路だけ保護が黙って
    効かない」形でしか現れない —— config にもログにも画面にも出ない。
    """

    _ROOT = pathlib.Path(__file__).resolve().parent.parent
    #: 本番コードのうち、モータへ指令を出しうるもの
    _SOURCES = ("main.py", "lib", "sequences")
    #: 生成そのものを持つ唯一のモジュール
    _FACTORY = pathlib.Path("lib/sequence/motors.py")

    def _production_files(self) -> list[pathlib.Path]:
        files: list[pathlib.Path] = []
        for entry in self._SOURCES:
            path = self._ROOT / entry
            files.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
        return files

    def test_本番コードは軸ハンドルを直に組み立てない(self) -> None:
        direct: list[str] = []
        for path in self._production_files():
            if path.relative_to(self._ROOT) == self._FACTORY:
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            direct.extend(
                f"{path.relative_to(self._ROOT)}:{node.lineno}"
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AxisHandle"
            )

        assert direct == [], (
            f"AxisHandle を直に組み立てている本番コード: {', '.join(direct)}"
            " (build_axis_handle を使う。直接生成はリミット保護を落とす)"
        )

    async def test_生成口はモータ群に結ばれた保護を渡す(self) -> None:
        fx = _Fixture(bind=False)
        clamp = _RecordingClamp(axis_names=("y_axis",))
        fx.group.bind_limit_guard(clamp)

        await build_axis_handle(fx.spec, fx.group).set_target_value(fx.spec.to_commands(-50.0))

        assert [call[0] for call in clamp.calls] == ["y_axis"]

    def test_保護を二重に結べない(self) -> None:
        """後から結んだほうだけが効く形を残さない (症状が画面にもログにも出ない)。"""
        fx = _Fixture()

        with pytest.raises(RuntimeError, match="二重"):
            fx.group.bind_limit_guard(_RecordingClamp())
