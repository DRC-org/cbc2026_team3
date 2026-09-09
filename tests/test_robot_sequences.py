from __future__ import annotations

import collections
import pathlib
from collections.abc import Mapping
from unittest.mock import AsyncMock, MagicMock

import can
import pytest
import yaml

from lib.drivers.base import ControlMode
from lib.match_state import ROLE_PRE_MATCH, Court, load_checklist_definitions
from lib.motion_guard import MotionGuardSpec
from lib.sequence.engine import Sequence, StepInfo
from lib.sequence.motors import MotorGroup, MotorHandle
from lib.sequence.positions import PositionTable, load_position_table
from lib.suction import SuctionSelectionError
from sequences.main_hand import MainHandSequence
from sequences.sub_hand import SubHandSequence
from tests.fake_drivers import StubFeedbackDriver

_CONFIG_DIR = pathlib.Path(__file__).resolve().parent.parent / "config"

_ROBOTS = [
    ("main_hand_positions.yaml", "main_hand.yaml", MainHandSequence),
    ("sub_hand_positions.yaml", "sub_hand.yaml", SubHandSequence),
]


class _RecordingDriver(StubFeedbackDriver):
    def __init__(self, name: str, sink: list[tuple[str, float]]) -> None:
        super().__init__(name, 1)
        self._sink = sink

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        self._sink.append((self.name, value))
        if mode is ControlMode.POSITION:
            self.set_observed(position=value)
        return super().encode_target(mode, value)


def _no_switch_pressed(_name: str) -> bool:
    """機構がストロークの途中に居る状態 (どの可動端スイッチも押されていない)。

    **読み口を配線しないと三値の `None` (読めていない) しか返らず、`guard:` を
    書いた軸は 1 歩も動けない。** それが正しい既定であることは
    `tests/test_motion_guard.py` が別に固定しているので、同梱シーケンスを
    通しで回すここでは「押されていない機体」を明示して与える。
    """
    return False


def _recording_group(
    names: list[str], *, sensor_active=_no_switch_pressed
) -> tuple[MotorGroup, list[tuple[str, float]]]:
    mgr = MagicMock()
    mgr.send = AsyncMock()
    sink: list[tuple[str, float]] = []
    group = MotorGroup(sensor_active=sensor_active)
    for name in names:
        group.add(MotorHandle(name, _RecordingDriver(name, sink), mgr, poll_interval=0.001))
    return group, sink


def _motor_names(table: PositionTable) -> list[str]:
    names: list[str] = []
    for axis in table.axes:
        for name in table.axis(axis).motor_names:
            if name not in names:
                names.append(name)
    return names


def _wire(seq: Sequence, position_config: dict) -> tuple[list[tuple[str, float]], MotorGroup]:
    table = load_position_table(position_config)
    group, sink = _recording_group(_motor_names(table))
    seq.bind_motors(group)
    seq.bind_positions(table)
    return sink, group


def _axis(**overrides: object) -> dict:
    axis: dict = {"unit": "test", "command_unit": "test", "timeout_s": 0.2}
    axis.update(overrides)
    return axis


def _paired_axis(*motors: tuple[str, float], **overrides: object) -> dict:
    return _axis(
        motors={name: {"scale": scale} for name, scale in motors},
        sync_tolerance=overrides.pop("sync_tolerance", 100.0),
        **overrides,
    )


_MAIN_POSITIONS = {
    "axes": {
        "y_axis": _paired_axis(("y_axis_r", 1.0), ("y_axis_l", -1.0)),
        "rotate": _paired_axis(("rotate_r", 1.0), ("rotate_l", -1.0)),
        "gripper": _axis(),
        "conveyor": _axis(command_mode="duty", settle_s=0.0),
        "wall_f": _axis(),
        "wall_r": _axis(),
    },
    "positions": {
        # 経由点は経路の順序をそのまま指令値で読むための目印なので、軸をまたいで
        # 1 つも重複しない値を与える (重複すると入れ替わっても検査が通ってしまう)
        "y_axis": {
            "home": 11.0,
            "work_1": 12.0,
            "work_2": 12.5,
            "work_3": 13.0,
            "work_shared": 16.0,
            "approach": 14.0,
            "place": 15.0,
            "work_1_via_1": 101.0,
            "work_1_via_2": 102.0,
            "work_1_via_3": 103.0,
            "work_2_via_1": 201.0,
            "work_2_via_2": 202.0,
            "work_2_via_3": 203.0,
            "work_3_via_1": 301.0,
            "work_3_via_2": 302.0,
            "work_3_via_3": 303.0,
            "work_shared_via_1": 401.0,
            "work_shared_via_2": 402.0,
            "work_shared_via_3": 403.0,
        },
        "rotate": {
            "home": 20.0,
            "pick": 21.0,
            "place": 22.0,
            "work_1_via_1": 501.0,
            "work_1_via_2": 502.0,
            "work_1_via_3": 503.0,
            "work_2_via_1": 601.0,
            "work_2_via_2": 602.0,
            "work_2_via_3": 603.0,
            "work_3_via_1": 701.0,
            "work_3_via_2": 702.0,
            "work_3_via_3": 703.0,
            "work_shared_via_1": 801.0,
            "work_shared_via_2": 802.0,
            "work_shared_via_3": 803.0,
        },
        "gripper": {"open": 31.0, "closed": 32.0},
        "conveyor": {"stop": 0.0, "run": 0.4},
        "wall_f": {"initial": 41.0, "closed": 42.0, "open": 43.0},
        "wall_r": {"initial": 44.0, "closed": 45.0, "open": 46.0},
    },
}

_MAIN_HOME_TARGETS = [
    ("y_axis_r", 11.0),
    ("y_axis_l", -11.0),
    ("rotate_r", 20.0),
    ("rotate_l", -20.0),
    ("gripper", 31.0),
    ("wall_f", 41.0),
    ("wall_r", 44.0),
    # HOME 姿勢はコンベアを回したまま待つ。止めるのは復帰の最後だけ
    ("conveyor", 0.4),
]

_VALVE_AXES = [f"valve_{i}" for i in range(1, 7)]

_SUB_POSITIONS = {
    "axes": {
        **{name: _axis(command_mode="on_off", settle_s=0.0) for name in _VALVE_AXES},
        "pump_vac": _axis(command_mode="duty", settle_s=0.0),
    },
    "positions": {
        **{name: {"open": 1.0, "closed": 0.0} for name in _VALVE_AXES},
        "pump_vac": {"stop": 0.0, "run": 0.61},
    },
}


async def _run_each_step(
    seq: Sequence, position_config: dict
) -> tuple[list[tuple[StepInfo, list[tuple[str, float]]]], MotorGroup]:
    sink, group = _wire(seq, position_config)
    per_step: list[tuple[StepInfo, list[tuple[str, float]]]] = []
    for info in seq.steps:
        first = len(sink)
        await getattr(seq, info.method_name)()
        per_step.append((info, sink[first:]))
    return per_step, group


def _single_motor_value(table: PositionTable, axis: str, position: str) -> float:
    (value,) = table.commands(axis, position).values()
    return value


def _paired_motor_names(table: PositionTable) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for axis in table.axes:
        names = table.axis(axis).motor_names
        if len(names) == 2:
            pairs.append((names[0], names[1]))
    return pairs


# n 列目へ寄せるステップと、そのステップが向かうワーク名。
_MAIN_APPROACH_STEPS = [
    ("move_to_work_3", "work_3"),
    ("move_to_work_shared", "work_shared"),
    ("move_to_work_1", "work_1"),
    ("move_to_work_2", "work_2"),
]


class TestMainHandSteps:
    @pytest.mark.parametrize(("method_name", "work"), _MAIN_APPROACH_STEPS)
    async def test_列へは経由点_3_つを順に踏んでから寄せる(
        self, method_name: str, work: str
    ) -> None:
        """`y_axis` と `rotate` を一息に動かすと機構が干渉する (docs/invariants.md §4)。

        守っているのはこの並びだけなので、**両軸が同じ段数・同じ順序で対になって**
        指令されることまで見る。片軸だけ見ると対が崩れても落ちない。
        """
        table = load_position_table(_MAIN_POSITIONS)
        seq = MainHandSequence()
        sink, _ = _wire(seq, _MAIN_POSITIONS)

        await getattr(seq, method_name)()

        issued: dict[str, list[float]] = collections.defaultdict(list)
        for name, value in sink:
            issued[name].append(value)

        vias = [f"{work}_via_{n}" for n in (1, 2, 3)]
        for axis, path in (("y_axis", [*vias, work]), ("rotate", [*vias, "pick"])):
            for motor in table.axis(axis).motor_names:
                expected = [table.commands(axis, position)[motor] for position in path]
                assert len(set(expected)) == len(expected), (
                    f"{axis}.{motor} の経路に同じ指令値があり順序を検査できない"
                )
                assert issued[motor] == expected, (
                    f"{method_name}: {motor} が {path} の順に動いていない"
                )

    async def test_starts_and_ends_at_home(self) -> None:
        per_step, _ = await _run_each_step(MainHandSequence(), _MAIN_POSITIONS)

        assert dict(per_step[0][1]) == dict(_MAIN_HOME_TARGETS)
        # 復帰だけは HOME 姿勢へ戻したあとコンベアを止めて終わる
        assert dict(per_step[-1][1]) == {**dict(_MAIN_HOME_TARGETS), "conveyor": 0.0}

    async def test_release_is_a_step_of_its_own(self) -> None:
        table = load_position_table(_MAIN_POSITIONS)
        gripper = table.axis("gripper").motor_names[0]
        opened = _single_motor_value(table, "gripper", "open")
        closed = _single_motor_value(table, "gripper", "closed")
        per_step, _ = await _run_each_step(MainHandSequence(), _MAIN_POSITIONS)

        holding = False
        releases = 0
        for info, commands in per_step:
            issued = dict(commands)
            if issued.get(gripper) == closed:
                holding = True
                continue
            if issued.get(gripper) != opened or not holding:
                continue
            releases += 1
            holding = False
            assert set(issued) == {gripper}, f"{info.method_name} がリリースと同時に他軸を動かす"

        assert releases > 0, "ワークを持ったまま開くステップが 1 つも無い"

    async def test_grab_and_release_require_trigger(self) -> None:
        table = load_position_table(_MAIN_POSITIONS)
        gripper = table.axis("gripper").motor_names[0]
        opened = _single_motor_value(table, "gripper", "open")
        closed = _single_motor_value(table, "gripper", "closed")
        per_step, _ = await _run_each_step(MainHandSequence(), _MAIN_POSITIONS)

        holding = False
        checked = 0
        for info, commands in per_step:
            issued = dict(commands)
            if issued.get(gripper) == closed:
                holding = True
            elif issued.get(gripper) == opened and holding:
                holding = False
            else:
                continue
            checked += 1
            assert info.require_trigger is True, f"{info.method_name} がトリガー待ちを持たない"

        assert checked >= 2

    async def test_paired_axes_are_commanded_with_opposite_signs(self) -> None:
        table = load_position_table(_MAIN_POSITIONS)
        pairs = _paired_motor_names(table)
        assert pairs, "ペア軸が 1 つも無い位置定数では検査にならない"
        per_step, _ = await _run_each_step(MainHandSequence(), _MAIN_POSITIONS)

        checked = 0
        for info, commands in per_step:
            issued = dict(commands)
            for right, left in pairs:
                if right not in issued or left not in issued:
                    continue
                checked += 1
                assert issued[right] != 0.0, (
                    f"{info.method_name}: {right} が 0 で符号を検査できない"
                )
                assert issued[left] == pytest.approx(-issued[right]), (
                    f"{info.method_name}: {right} と {left} が同符号"
                )

        assert checked > 0

    async def test_conveyor_is_commanded_as_duty(self) -> None:
        seq = MainHandSequence()
        per_step, group = await _run_each_step(seq, _MAIN_POSITIONS)

        run = _single_motor_value(load_position_table(_MAIN_POSITIONS), "conveyor", "run")
        commanded = [
            value for _, commands in per_step for name, value in commands if name == "conveyor"
        ]

        assert group["conveyor"].mode is ControlMode.DUTY
        assert run in commanded, "シーケンス中にコンベアを一度も回していない"


# ワーク 1 個ぶん 16 ステップ x 4 個 + 初期位置 + 復帰。
_SUB_CYCLE_LABELS = (
    "棚へ寄せる",
    "吸着高さへ下降",
    "ワーク吸着",
    "持ち上げ",
    "回転可能位置へ後退",
    "搬送姿勢へ",
    "箱 {n} の上へ",
    "オフセットを閉じる",
    "ピッチを閉じる",
    "箱へ下降",
    "ワーク解放 (配置)",
    "上昇",
    "ピッチを開く",
    "オフセットを開く",
    "回転可能位置へ後退",
    "受け取り姿勢へ",
)

# 16 ステップのうち操縦者のトリガー待ちを持つ位置 (0 始まり)。
_SUB_TRIGGER_OFFSETS = (0, 2, 9, 10)

# 前端スイッチ (動作点 0.0mm) からこれより内側に居るあいだ sub_rotate を回すと機構が
# 干渉する (docs/invariants.md §4)。守っているのはステップの並びだけである。
_ROTATE_CLEARANCE_MM = 150.0

_SUB_GRIP_METHODS = tuple(f"work_{n}_grip" for n in range(1, 5))
_SUB_RELEASE_METHODS = tuple(f"work_{n}_release" for n in range(1, 5))


def _expected_sub_labels() -> list[str]:
    labels = ["初期位置へ移動"]
    for n in range(1, 5):
        labels += [f"{n} 個目: {label.format(n=n)}" for label in _SUB_CYCLE_LABELS]
    labels.append("初期位置へ復帰")
    return labels


async def _collect_moves(seq: Sequence) -> list[tuple[int, dict[str, str]]]:
    """各ステップが出した `move_to` を、ステップ番号付きで出た順に集める。

    軸をまたぐ順序 (どの姿勢で何を動かすか) は `move_to` 1 回ごとの単位でしか
    見えない。ステップ単位で束ねると「同じステップの中で順に動かした」のか
    「同時に動かした」のかが区別できず、干渉の検査にならない。
    """
    calls: list[tuple[int, dict[str, str]]] = []
    current = 0

    async def _record(
        _self: Sequence, targets: Mapping[str, str], *, timeout: float | None = None
    ) -> None:
        calls.append((current, dict(targets)))

    original = Sequence.move_to
    Sequence.move_to = _record  # type: ignore[method-assign, assignment]
    try:
        for index, info in enumerate(seq.steps):
            current = index
            await getattr(seq, info.method_name)()
    finally:
        Sequence.move_to = original  # type: ignore[method-assign]
    return calls


class TestSubHandSteps:
    def test_ワーク_4_個ぶんの_66_ステップである(self) -> None:
        labels = [s["label"] for s in SubHandSequence().steps_info]

        assert labels == _expected_sub_labels()
        assert len(labels) == 1 + 16 * 4 + 1

    def test_トリガー待ちは仕様どおりの位置だけに付く(self) -> None:
        steps = SubHandSequence().steps_info

        waiting = {s["index"] for s in steps if s["require_trigger"]}

        assert waiting == {1 + 16 * n + offset for n in range(4) for offset in _SUB_TRIGGER_OFFSETS}

    async def test_回転の直前は前端スイッチから_150mm_以上離れている(self) -> None:
        table = _load_shipped("sub_hand_positions.yaml")
        at: str | None = None
        turns = 0

        for index, targets in await _collect_moves(SubHandSequence()):
            if "sub_y_axis" in targets:
                at = targets["sub_y_axis"]
            if "sub_rotate" not in targets:
                continue
            turns += 1
            assert at is not None, f"ステップ {index}: 前後軸の位置が定まらないまま回している"
            # 前端スイッチの動作点が 0.0mm なので、位置の絶対値がそのまま隙間になる
            assert abs(table.raw("sub_y_axis", at)) >= _ROTATE_CLEARANCE_MM, (
                f"ステップ {index}: sub_y_axis が '{at}' に居るまま sub_rotate を回している"
            )
            if index > 0:
                assert at == "clear", (
                    f"ステップ {index}: 回転可能位置 (clear) を経由せずに回している"
                )

        assert turns == 1 + 4 * 2

    async def test_前後に動かす直前の昇降は必ず_top(self) -> None:
        lift: str | None = None
        moves = 0

        for index, targets in await _collect_moves(SubHandSequence()):
            if "sub_lift" in targets:
                lift = targets["sub_lift"]
            if "sub_y_axis" not in targets:
                continue
            moves += 1
            assert lift == "top", f"ステップ {index}: sub_lift が '{lift}' のまま前後に動かしている"

        assert moves == 1 + 4 * 4 + 1

    async def test_ピッチとオフセットを同じ指令で動かさない(self) -> None:
        for index, targets in await _collect_moves(SubHandSequence()):
            assert not {"sub_pitch", "sub_offset"} <= set(targets), (
                f"ステップ {index}: ピッチとオフセットを同時に動かしている"
            )

    async def test_ピッチとオフセットは別々のステップである(self) -> None:
        axes_by_step: dict[int, set[str]] = collections.defaultdict(set)
        for index, targets in await _collect_moves(SubHandSequence()):
            axes_by_step[index] |= set(targets)

        together = sorted(
            index for index, axes in axes_by_step.items() if {"sub_pitch", "sub_offset"} <= axes
        )

        # 例外は初期位置へ戻すステップだけ。そこも 1 指令ずつ順に送っている
        assert together == [0]

    async def test_閉じる順はオフセット先_開く順はピッチ先(self) -> None:
        moves = [
            (axis, targets[axis])
            for _, targets in await _collect_moves(SubHandSequence())
            for axis in ("sub_offset", "sub_pitch")
            if axis in targets
        ]

        assert (
            moves
            == [("sub_pitch", "open"), ("sub_offset", "open")]
            + [
                ("sub_offset", "close"),
                ("sub_pitch", "close"),
                ("sub_pitch", "open"),
                ("sub_offset", "open"),
            ]
            * 4
        )

    async def test_初期位置は弁を閉じてポンプを回してから姿勢を戻す(self) -> None:
        moves = await _collect_moves(SubHandSequence())

        calls = [targets for index, targets in moves if index == 0]

        assert calls == [
            {**dict.fromkeys(_VALVE_AXES, "closed"), "pump_vac": "run"},
            {"sub_lift": "top"},
            {"sub_y_axis": "retracted"},
            {"sub_pitch": "open"},
            {"sub_offset": "open"},
            {"sub_rotate": "receive"},
        ]

    async def test_復帰は収納位置へ戻すだけ(self) -> None:
        seq = SubHandSequence()
        last = len(seq.steps) - 1

        calls = [targets for index, targets in await _collect_moves(seq) if index == last]

        assert calls == [{"sub_y_axis": "retracted"}]

    def test_default_suction_covers_every_valve(self) -> None:
        seq = SubHandSequence()

        assert seq.suction.axes == tuple(_VALVE_AXES)
        assert seq.suction.enabled() == tuple(_VALVE_AXES)

    @pytest.mark.parametrize("method_name", _SUB_GRIP_METHODS)
    async def test_吸着は選ばれたパッドだけ開き残りを閉じ直す(self, method_name: str) -> None:
        seq = SubHandSequence()
        sink, _ = _wire(seq, _SUB_POSITIONS)
        assert seq.suction.select(["valve_2", "valve_5"]) is None

        await getattr(seq, method_name)()

        assert dict(sink) == {
            name: (1.0 if name in ("valve_2", "valve_5") else 0.0) for name in _VALVE_AXES
        }

    @pytest.mark.parametrize("method_name", _SUB_GRIP_METHODS)
    async def test_吸着はパッドが選ばれていなければ失敗する(self, method_name: str) -> None:
        seq = SubHandSequence()
        sink, _ = _wire(seq, _SUB_POSITIONS)
        assert seq.suction.select([]) is None

        with pytest.raises(SuctionSelectionError):
            await getattr(seq, method_name)()

        assert sink == []

    @pytest.mark.parametrize("method_name", _SUB_RELEASE_METHODS)
    async def test_解放は弁を閉じるだけでポンプを止めない(self, method_name: str) -> None:
        seq = SubHandSequence()
        sink, _ = _wire(seq, _SUB_POSITIONS)

        await getattr(seq, method_name)()

        assert dict(sink) == dict.fromkeys(_VALVE_AXES, 0.0)
        assert "pump_vac" not in [name for name, _ in sink]


def _guard_declaration(guard: MotionGuardSpec | None) -> object:
    """歯止めを、**位置の値に依らない宣言**へ落とす (本番とベンチの突き合わせ用)。"""
    if guard is None:
        return None
    return (
        guard.limits,
        guard.max_step,
        guard.stall_torque,
        tuple((required.axis, required.label) for required in guard.requires),
        guard.not_with,
    )


def _load_shipped(yaml_name: str) -> PositionTable:
    return load_position_table(
        yaml.safe_load((_CONFIG_DIR / yaml_name).read_text()), source=yaml_name
    )


def _load_config(robot_config: str) -> dict:
    return yaml.safe_load((_CONFIG_DIR / robot_config).read_text())


class TestShippedPositionYaml:
    @pytest.mark.parametrize(("yaml_name", "robot_config", "sequence_cls"), _ROBOTS)
    async def test_all_steps_run_against_shipped_yaml(
        self, yaml_name: str, robot_config: str, sequence_cls: type[Sequence]
    ) -> None:
        table = _load_shipped(yaml_name)
        seq = sequence_cls()
        group, _ = _recording_group(_motor_names(table))
        seq.bind_motors(group)
        seq.bind_positions(table)

        for info in seq.steps:
            await getattr(seq, info.method_name)()

    @pytest.mark.parametrize(("yaml_name", "robot_config", "sequence_cls"), _ROBOTS)
    def test_axis_motors_exist_in_robot_config(
        self, yaml_name: str, robot_config: str, sequence_cls: type[Sequence]
    ) -> None:
        table = _load_shipped(yaml_name)
        motors = _load_config(robot_config)["motors"]

        assert set(_motor_names(table)) <= set(motors)

    async def test_main_hand_paired_axes_command_opposite_signs(self) -> None:
        table = _load_shipped("main_hand_positions.yaml")

        for axis, position in (("y_axis", "work_shared"), ("rotate", "pick")):
            commands = list(table.commands(axis, position).values())
            assert len(commands) == 2
            assert commands[0] != 0.0
            assert commands[1] == pytest.approx(-commands[0])

    def test_conveyor_run_is_reversed_between_courts(self) -> None:
        table = _load_shipped("main_hand_positions.yaml")

        red = table.raw("conveyor", "run", court=Court.RED)
        blue = table.raw("conveyor", "run", court=Court.BLUE)

        assert red != 0.0 and blue != 0.0, "搬送 duty が 0 では回転方向を持たない"
        assert red * blue < 0.0, f"赤 ({red}) と青 ({blue}) の搬送方向が逆になっていない"


class TestShippedRobotConfig:
    def test_can_ids_are_unique_per_bus_across_robots(self) -> None:
        owners: dict[tuple[str, int], list[str]] = collections.defaultdict(list)

        for path in sorted(_CONFIG_DIR.glob("*.yaml")):
            config = yaml.safe_load(path.read_text()) or {}
            motors = config.get("motors")
            if not motors:
                continue
            buses = config.get("can_buses") or {}
            for motor_name, motor in motors.items():
                interface = buses.get(motor["bus"], motor["bus"])
                owners[(interface, int(motor["can_id"]))].append(f"{path.name}:{motor_name}")

        duplicated = {key: names for key, names in owners.items() if len(names) > 1}

        assert duplicated == {}

    def test_motor_names_are_unique_across_robots(self) -> None:
        owners: dict[str, list[str]] = collections.defaultdict(list)

        for path in sorted(_CONFIG_DIR.glob("*.yaml")):
            config = yaml.safe_load(path.read_text()) or {}
            for section in ("motors", "sensors"):
                for name in config.get(section) or {}:
                    owners[name].append(f"{path.name}:{section}")

        duplicated = {name: places for name, places in owners.items() if len(places) > 1}

        assert duplicated == {}

    def test_axis_names_are_unique_across_robots(self) -> None:
        owners: dict[str, list[str]] = collections.defaultdict(list)

        for path in sorted(_CONFIG_DIR.glob("*_positions.yaml")):
            config = yaml.safe_load(path.read_text()) or {}
            for name in config.get("axes") or {}:
                owners[name].append(path.name)

        duplicated = {name: places for name, places in owners.items() if len(places) > 1}

        assert duplicated == {}

    def test_homing_sensors_are_registered(self) -> None:
        sensors: set[str] = set()
        for path in sorted(_CONFIG_DIR.glob("*.yaml")):
            if path.name.endswith("_positions.yaml"):
                continue
            config = yaml.safe_load(path.read_text()) or {}
            sensors |= set(config.get("sensors") or {})

        required: set[str] = set()
        for path in sorted(_CONFIG_DIR.glob("*_positions.yaml")):
            table = _load_shipped(path.name)
            for axis in table.axes:
                homing = table.axis(axis).homing
                if homing is not None:
                    required |= set(homing.sensor_names)

        assert required <= sensors

    def test_paired_axis_motors_agree_on_set_zero_on_start(self) -> None:
        mismatched: dict[str, dict[str, bool]] = {}
        inspected: set[str] = set()

        for positions_path in sorted(_CONFIG_DIR.rglob("*_positions.yaml")):
            robot_path = positions_path.with_name(
                positions_path.name.removesuffix("_positions.yaml") + ".yaml"
            )
            motors = (yaml.safe_load(robot_path.read_text()) or {}).get("motors") or {}
            table = load_position_table(
                yaml.safe_load(positions_path.read_text()), source=str(positions_path)
            )

            for axis in table.axes:
                names = [name for name in table.axis(axis).motor_names if name in motors]
                if len(names) < 2:
                    continue
                key = f"{robot_path.relative_to(_CONFIG_DIR)}:{axis}"
                inspected.add(key)
                flags = {name: bool(motors[name].get("set_zero_on_start", False)) for name in names}
                if len(set(flags.values())) > 1:
                    mismatched[key] = flags

        assert mismatched == {}

        assert {
            "main_hand.yaml:y_axis",
            "main_hand.yaml:rotate",
            "bench/edulite/main_hand.yaml:rotate",
            "bench/m3508/main_hand.yaml:y_axis",
        } <= inspected

    def test_checklist_covers_what_cannot_be_judged_automatically(self) -> None:
        checklist = yaml.safe_load((_CONFIG_DIR / "checklist.yaml").read_text())
        ids = {item["id"] for item in checklist["checklists"][ROLE_PRE_MATCH]}

        assert {
            "y_axis_sync",
            "rotate_sync",
            "wall_initial",
            "conveyor_stop",
            "conveyor_run",
            "origin_sensor_react",
            # 向きの取り違えは反応の確認では見えない (押されている端へ進む指令だけが
            # 通る)。退避できるかどうかを機体ごとに唱えるのが唯一の検出経路
            "limit_escape_main",
            "limit_escape_sub",
            "valves_closed",
            "valves_actuate",
            "pumps_run",
            "suction_hold",
            "suction_release",
        } <= ids


_CHECKLIST_PATHS = sorted(_CONFIG_DIR.rglob("checklist.yaml"))
_CHECKLIST_TEST_IDS = [str(path.relative_to(_CONFIG_DIR)) for path in _CHECKLIST_PATHS]


class TestShippedChecklists:
    def test_scan_finds_every_shipped_checklist(self) -> None:
        expected = {pathlib.Path("checklist.yaml")} | {
            pathlib.Path("bench") / bench_dir.name / "checklist.yaml"
            for bench_dir in (_CONFIG_DIR / "bench").iterdir()
            if bench_dir.is_dir()
        }

        assert {path.relative_to(_CONFIG_DIR) for path in _CHECKLIST_PATHS} == expected

    @pytest.mark.parametrize("path", _CHECKLIST_PATHS, ids=_CHECKLIST_TEST_IDS)
    def test_no_entry_is_silently_dropped(self, path: pathlib.Path) -> None:
        raw = yaml.safe_load(path.read_text()) or {}
        written = sum(len(entries) for entries in (raw.get("checklists") or {}).values())

        loaded = load_checklist_definitions(raw)

        assert sum(len(items) for items in loaded.values()) == written

    @pytest.mark.parametrize("path", _CHECKLIST_PATHS, ids=_CHECKLIST_TEST_IDS)
    def test_item_ids_are_unique_within_a_role(self, path: pathlib.Path) -> None:
        loaded = load_checklist_definitions(yaml.safe_load(path.read_text()) or {})

        duplicated: dict[str, list[str]] = {}
        for role, items in loaded.items():
            counts = collections.Counter(item.id for item in items)
            dups = sorted(item_id for item_id, count in counts.items() if count > 1)
            if dups:
                duplicated[role] = dups

        assert duplicated == {}


class TestShippedMotionGuard:
    """`axes.<軸>.guard` が同梱 config の中で閉じていること。

    可動端インターロックはセンサ名の文字列でしか繋がっていない。綴りを間違えても
    yaml は読めてしまい、症状は「その端では止まらない」— つまり**機構を壊すまで
    出ない**。本番と机上ベンチ (config/bench/<対象>/) の全セットを見る。
    """

    def _sensor_names(self, positions_path: pathlib.Path) -> set[str]:
        robot_path = positions_path.with_name(
            positions_path.name.removesuffix("_positions.yaml") + ".yaml"
        )
        if not robot_path.exists():
            # 本番 config をそのまま使うベンチセット (config/bench/main_hand)
            robot_path = _CONFIG_DIR / robot_path.name
        return set((yaml.safe_load(robot_path.read_text()) or {}).get("sensors") or {})

    def test_guard_のセンサ名が_sensors_に登録されている(self) -> None:
        """未登録の名前は三値の `None` (読めていない) にしかならない。

        安全側 (止まる) には倒れるが、**その軸は 1 歩も動かせなくなる**ので
        綴り違いは起動前に潰す。
        """
        missing: dict[str, set[str]] = {}

        for positions_path in sorted(_CONFIG_DIR.rglob("*_positions.yaml")):
            table = load_position_table(
                yaml.safe_load(positions_path.read_text()), source=positions_path.name
            )
            registered = self._sensor_names(positions_path)
            required: set[str] = set()
            for axis in table.axes:
                guard = table.axis(axis).guard
                if guard is None or guard.limits is None:
                    continue
                required |= {*guard.limits.plus, *guard.limits.minus}
            if required - registered:
                missing[str(positions_path)] = required - registered

        assert missing == {}

    def test_本番とベンチの_guard_が一致する(self) -> None:
        """**歯止めは対である。片方だけ動かしてはならない。**

        ベンチ側だけ緩めると、本番では止まる構成がベンチでだけ端を踏み越える
        (`homing.search_distance` を対で持たせているのと同じ理由)。

        干渉条件だけは解決後の数値ではなく**宣言 (参照先の軸と位置名) を**突き合わせる。
        ベンチの位置定数は機構が無いぶん mm の値が本番と違ってよく、数値で比べると
        必ず食い違う。対であるべきなのは「何を条件にしているか」である。
        """
        for name in ("sub_hand_positions.yaml",):
            production = _load_shipped(name)
            bench = load_position_table(
                yaml.safe_load((_CONFIG_DIR / "bench" / "sub_hand_homing" / name).read_text()),
                source=name,
            )
            for axis in sorted(set(production.axes) & set(bench.axes)):
                assert _guard_declaration(production.axis(axis).guard) == _guard_declaration(
                    bench.axis(axis).guard
                ), axis
