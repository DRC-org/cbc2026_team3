from __future__ import annotations

import pathlib
from collections.abc import Collection, Mapping
from unittest.mock import AsyncMock, MagicMock

import can
import pytest
import yaml

import sequences.main_hand as main_hand
import sequences.sub_hand as sub_hand
from lib.drivers.base import ControlMode
from lib.match_state import Court
from lib.sequence.engine import Sequence
from lib.sequence.motors import AxisHandle, MotorGroup, MotorHandle, build_axis_state_reader
from lib.sequence.positions import AxisSpec, PositionTable, load_position_table
from sequences.motor_check import MAIN_HOME, SUB_HOME, VALVE_AXES, MotorCheckSequence
from tests.fake_drivers import StubFeedbackDriver

_CONFIG_DIR = pathlib.Path(__file__).resolve().parent.parent / "config"


async def _collect(
    available: Collection[str] | None = None, *, only: str | None = None
) -> list[dict[str, str]]:
    """全ステップ (`only` を渡せばその 1 ステップ) が出す指令を順に集める。"""
    seq = MotorCheckSequence(available_axes=available)
    calls: list[dict[str, str]] = []

    async def _record(
        _self: Sequence, targets: Mapping[str, str], *, timeout: float | None = None
    ) -> None:
        calls.append(dict(targets))

    original = Sequence.move_to
    Sequence.move_to = _record  # type: ignore[method-assign, assignment]
    try:
        assert seq.steps, "ステップが 1 つも無い (収集方法が壊れている)"
        for info in seq.steps:
            if only is None or info.method_name == only:
                await getattr(seq, info.method_name)()
    finally:
        Sequence.move_to = original  # type: ignore[method-assign]
    return calls


def _shipped_table() -> PositionTable:
    return _table_of(sorted(_CONFIG_DIR.glob("*_positions.yaml")))


def _table_of(paths: list[pathlib.Path]) -> PositionTable:
    tables = [
        load_position_table(yaml.safe_load(path.read_text()) or {}, source=path.name)
        for path in paths
    ]
    return PositionTable.merged(tables)


def _main_hand_axes() -> tuple[str, ...]:
    return _table_of([_CONFIG_DIR / "main_hand_positions.yaml"]).axes


class TestShippedConfig:
    async def test_全ての指令が実_config_の位置定数で引ける(self) -> None:
        table = _shipped_table()

        for targets in await _collect():
            for axis, position in targets.items():
                for court in (Court.RED, Court.BLUE):
                    table.raw(axis, position, court=court)

    async def test_実_config_の全アクチュエータ軸を一度は動かす(self) -> None:
        table = _shipped_table()
        touched = {axis for targets in await _collect() for axis in targets}

        assert set(table.axes) - touched == set()

    async def test_全ての軸を初期姿勢以外の位置へも動かす(self) -> None:
        commanded: dict[str, set[str]] = {}
        for targets in await _collect():
            for axis, position in targets.items():
                commanded.setdefault(axis, set()).add(position)

        stuck = sorted(axis for axis, names in commanded.items() if len(names) < 2)

        assert not stuck, f"1 つの位置へしか指令していない軸がある: {', '.join(stuck)}"


class TestPairedAxes:
    async def test_左右直結ペアは軸名で指令する(self) -> None:
        touched = {axis for targets in await _collect() for axis in targets}

        assert "y_axis" in touched
        assert "rotate" in touched
        assert not ({"y_axis_r", "y_axis_l", "rotate_r", "rotate_l"} & touched)


class TestFinalPosture:
    async def test_最後は両ハンドを初期姿勢へ戻す(self) -> None:
        posture: dict[str, str] = {}
        for targets in await _collect(only="restore_home"):
            posture.update(targets)

        assert posture == {**MAIN_HOME, **SUB_HOME, "conveyor": "stop"}

    async def test_駆動しっぱなしの軸を残さない(self) -> None:
        last: dict[str, str] = {}
        for targets in await _collect():
            last.update(targets)

        assert last["conveyor"] == "stop"
        assert last["pump_vac"] == "stop"
        for axis in VALVE_AXES:
            assert last[axis] == "closed"


class TestSubHandIsCommandedOneAxisAtATime:
    """サブハンドの初期姿勢を 1 通にまとめると干渉の宣言に引っかかる。

    `sub_y_axis` は `sub_lift` が `top` に居るあいだしか動かせず (`requires`)、
    `sub_pitch` と `sub_offset` は同じ指令で動かせない (`not_with`)。**段は 1 つの
    まま**で、本体だけを分ける。
    """

    @pytest.mark.parametrize("method_name", ["sub_home", "restore_home"])
    async def test_サブハンドは_1_軸ずつ宣言の順で送る(self, method_name: str) -> None:
        calls = await _collect(only=method_name)
        sub = [targets for targets in calls if set(targets) <= set(SUB_HOME)]

        assert sub == [{axis: position} for axis, position in SUB_HOME.items()]

    async def test_昇降を上げてから前後_ピッチとオフセットは別々(self) -> None:
        order = list(SUB_HOME)

        assert order.index("sub_lift") < order.index("sub_y_axis")
        assert order.index("sub_pitch") < order.index("sub_offset")

    async def test_メインハンドは_1_通のまま送る(self) -> None:
        calls = await _collect(only="restore_home")

        assert calls[0] == {**MAIN_HOME, "conveyor": "stop"}

    def test_段の数は変わらない(self) -> None:
        labels = [info.label for info in MotorCheckSequence("x").steps]

        assert labels.count("サブハンド 初期姿勢へ") == 1
        assert labels.count("両ハンドを初期姿勢へ戻す") == 1


class TestHomingComesFirst:
    def test_零点確定が最初のステップ(self) -> None:
        first = MotorCheckSequence("x").steps[0]

        assert "零点" in first.label

    async def test_零点確定は実行口が無ければ素通りする(self) -> None:
        seq = MotorCheckSequence()
        await seq.home_axes()

    async def test_homing_を持つ軸だけを対象にする(self) -> None:
        table = _shipped_table()
        homing_axes = [name for name in table.axes if table.axis(name).homing is not None]

        assert homing_axes == ["y_axis", "rotate", "sub_y_axis", "sub_lift"]


class _EchoDriver(StubFeedbackDriver):
    def __init__(self, name: str) -> None:
        super().__init__(name, 1)
        self.commands: list[float] = []

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        self.commands.append(value)
        return super().encode_target(mode, value)


class _FirstStepHoming:
    """探索 1 歩目だけを打つ零点確定の代役。渡された spec と handle をそのまま使う。"""

    async def home(self, spec: AxisSpec, handle: AxisHandle) -> float:
        homing = spec.homing
        assert homing is not None
        await handle.set_target_value(spec.to_commands(homing.direction * homing.step))
        return homing.step


_COURT_CONFIG = {
    "axes": {
        "lift": {
            "unit": "mm",
            "command_unit": "rad",
            "scale": {"blue": 2.0, "red": -2.0},
            "homing": {"sensor": "s", "direction": 1, "search_distance": 5.0, "step": 1.0},
        }
    },
    "positions": {"lift": {"home": -1.0}},
}


class TestHomingUsesCourt:
    @pytest.mark.parametrize(("court", "expected"), [(Court.BLUE, 2.0), (Court.RED, -2.0)])
    async def test_first_search_step_uses_court_sign(self, court: Court, expected: float) -> None:
        seq = MotorCheckSequence()
        driver = _EchoDriver("lift")
        group = MotorGroup()
        mgr = MagicMock()
        mgr.send = AsyncMock()
        group.add(MotorHandle("lift", driver, mgr))
        seq.bind_motors(group)
        seq.bind_positions(load_position_table(_COURT_CONFIG, source="<test>"))
        seq.bind_homing(_FirstStepHoming())  # type: ignore[arg-type]
        seq.set_court(court)

        await seq.home_axes()

        assert driver.commands == [pytest.approx(expected)]


class TestStepShape:
    def test_操縦者のトリガー待ちを持たない(self) -> None:
        assert all(not info.require_trigger for info in MotorCheckSequence("x").steps)

    @pytest.mark.parametrize("axis", VALVE_AXES)
    async def test_電磁弁は一個ずつ開閉する(self, axis: str) -> None:
        opened = [targets for targets in await _collect() if targets.get(axis) == "open"]

        assert len(opened) == 1
        assert set(opened[0]) == {axis}


class TestConstantsHaveASingleOwner:
    def test_メインハンドの初期姿勢は_main_hand_が持つ(self) -> None:
        assert MAIN_HOME is main_hand.HOME

    def test_電磁弁の軸名は_sub_hand_が持つ(self) -> None:
        assert VALVE_AXES is sub_hand.VALVE_AXES

    async def test_メインハンドは同じ初期姿勢へ往復する(self) -> None:
        """行きと帰りで同じ MAIN_HOME を使う。MAIN_HOME 自身がコンベアを止めて待つ。"""
        seq = main_hand.MainHandSequence()
        calls: list[dict[str, str]] = []

        async def _record(targets, *, timeout: float | None = None) -> None:
            calls.append(dict(targets))

        seq.move_to = _record  # type: ignore[method-assign]
        await seq.move_to_home()
        await seq.return_home()

        assert calls == [MAIN_HOME, MAIN_HOME]

    def test_ステップの宣言軸は初期姿勢と電磁弁から導かれる(self) -> None:
        declared = {axis for info in MotorCheckSequence("x").steps for axis in info.axes}

        assert declared == {*MAIN_HOME, *SUB_HOME, *VALVE_AXES}


class TestPartialConfiguration:
    def test_サブハンド系のステップが登録から外れる(self) -> None:
        seq = MotorCheckSequence(available_axes=_main_hand_axes())
        labels = [info.label for info in seq.steps]

        assert not [label for label in labels if "サブハンド" in label]
        assert [label for label in labels if "メインハンド" in label] == [
            "メインハンド 初期姿勢へ",
            "メインハンド y 軸 (左右直結ペア)",
            "メインハンド エンドエフェクタ回転 (左右直結ペア)",
            "メインハンド グリッパ",
            "メインハンド 壁 前後",
            "メインハンド コンベア (目視確認)",
        ]

    def test_零点確定は軸を宣言しないので構成に依らず残る(self) -> None:
        seq = MotorCheckSequence(available_axes=_main_hand_axes())

        assert "零点" in seq.steps[0].label

    def test_除外したステップと欠けている軸が読める(self) -> None:
        seq = MotorCheckSequence(available_axes=_main_hand_axes())
        excluded = {info.label: info.missing_axes for info in seq.excluded_steps}

        assert excluded == {
            "サブハンド 初期姿勢へ": tuple(sorted(SUB_HOME)),
            "サブハンド 前後スライド (Y 方向)": ("sub_y_axis",),
            "サブハンド 昇降": ("sub_lift",),
            "サブハンド 回転 (左右直結ペア)": ("sub_rotate",),
            # ピッチを閉じる前にオフセットを閉じるので、この段は 2 軸を要求する
            "サブハンド ピッチ (左右直結ペア)": ("sub_offset", "sub_pitch"),
            "サブハンド オフセット": ("sub_offset",),
            "サブハンド 電磁弁 6 個 (打音・目視確認)": tuple(sorted(VALVE_AXES)),
            "サブハンド 吸気ポンプ (聴音確認)": ("pump_vac",),
        }

    def test_本番構成では一つも除外されない(self) -> None:
        seq = MotorCheckSequence(available_axes=_shipped_table().axes)

        assert seq.excluded_steps == ()
        assert len(seq.steps) == len(MotorCheckSequence("x").steps)

    async def test_存在しない軸へは一度も指令しない(self) -> None:
        available = set(_main_hand_axes())
        touched = {axis for targets in await _collect(available) for axis in targets}

        assert touched <= available

    async def test_最後は存在する軸だけを初期姿勢へ戻す(self) -> None:
        available = set(_main_hand_axes())
        calls = await _collect(available)
        restored = {**MAIN_HOME, "conveyor": "stop"}

        assert calls[-1] == {axis: pos for axis, pos in restored.items() if axis in available}
        assert calls[-1] == restored, "メインハンドの初期姿勢が欠けている"

    def test_軸が一本も無ければ指令するステップが残らない(self) -> None:
        seq = MotorCheckSequence(available_axes=[])

        assert not any(info.axes for info in seq.steps)


class _ReachingDriver(StubFeedbackDriver):
    """指令どおりに動く機体。到達模擬なので `move_to` が待たずに進む。"""

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        if mode is ControlMode.POSITION:
            self.set_observed(position=value)
        return super().encode_target(mode, value)


class _NoOpHoming:
    """零点確定の代役。**軸を動かさない** (寄せる段が居ることをこの通しで見る)。

    原点を書いたことにはする —— 確定していない軸は寄せる段が信用しない。
    """

    def __init__(self, group: MotorGroup) -> None:
        self.homed: list[str] = []
        self._group = group

    async def home(self, spec: AxisSpec, _handle: AxisHandle) -> float:
        self.homed.append(spec.name)
        for name in spec.motor_names:
            self._group[name].driver.mark_origin_confirmed()
        return 0.0


@pytest.mark.usefixtures("instant_settle")
class TestShippedRunThrough:
    """同梱の位置定数で**全ステップを到達模擬で通し、歯止めが 1 度も拒否しない**。

    層ごとのテストは 1 枚ずつしか見ないので、「宣言が動作確認を壊さない」を
    担保するのはこの通しだけである。零点確定は軸を動かさない代役なので、
    `sub_lift` を `top` へ寄せているのは `home_axes` の 2 段目そのもの。
    """

    async def test_全ステップが拒否されずに通る(self) -> None:
        table = _shipped_table()
        seq = MotorCheckSequence(available_axes=table.axes)
        seq.set_court(Court.RED)
        mgr = MagicMock()
        mgr.send = AsyncMock()
        group = MotorGroup(sensor_active=lambda _name: False)
        for axis in table.axes:
            for name in table.axis(axis).motor_names:
                group.add(MotorHandle(name, _ReachingDriver(name, 1), mgr, poll_interval=0.001))
        group.bind_axis_state(
            build_axis_state_reader(
                table, group, court=lambda: seq.court, is_stale=lambda _name: False
            )
        )
        seq.bind_motors(group)
        seq.bind_positions(table)
        homing = _NoOpHoming(group)
        seq.bind_homing(homing)  # type: ignore[arg-type]

        for info in seq.steps:
            await getattr(seq, info.method_name)()

        # 宣言が効いていないと、この通しは何も見ていないことになる
        assert table.axis("sub_y_axis").guard is not None
        assert table.axis("sub_y_axis").guard.requires  # type: ignore[union-attr]
        assert homing.homed[0] == "sub_lift", "参照される軸を先に確定していない"
