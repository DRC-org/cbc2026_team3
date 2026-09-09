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
from lib.sequence.motors import AxisHandle, MotorGroup, MotorHandle
from lib.sequence.positions import AxisSpec, PositionTable, load_position_table
from sequences.motor_check import MAIN_HOME, SUB_HOME, VALVE_AXES, MotorCheckSequence
from tests.fake_drivers import StubFeedbackDriver

_CONFIG_DIR = pathlib.Path(__file__).resolve().parent.parent / "config"


async def _collect(available: Collection[str] | None = None) -> list[dict[str, str]]:
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
        calls = await _collect()

        assert calls[-1] == {**MAIN_HOME, **SUB_HOME}

    async def test_駆動しっぱなしの軸を残さない(self) -> None:
        last: dict[str, str] = {}
        for targets in await _collect():
            last.update(targets)

        assert last["conveyor"] == "stop"
        assert last["pump_vac"] == "stop"
        for axis in VALVE_AXES:
            assert last[axis] == "closed"


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
            "サブハンド ピッチ (左右直結ペア)": ("sub_pitch",),
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

        assert calls[-1] == {axis: pos for axis, pos in MAIN_HOME.items() if axis in available}
        assert calls[-1] == MAIN_HOME, "メインハンドの初期姿勢が欠けている"

    def test_軸が一本も無ければ指令するステップが残らない(self) -> None:
        seq = MotorCheckSequence(available_axes=[])

        assert not any(info.axes for info in seq.steps)
