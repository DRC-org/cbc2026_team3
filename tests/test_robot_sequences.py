from __future__ import annotations

import collections
import pathlib
from unittest.mock import AsyncMock, MagicMock

import can
import pytest
import yaml

from lib.drivers.base import ControlMode
from lib.match_state import ROLE_PRE_MATCH, Court, load_checklist_definitions
from lib.sequence.engine import Sequence, StepInfo
from lib.sequence.motors import MotorGroup, MotorHandle
from lib.sequence.positions import PositionTable, load_position_table
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


def _recording_group(names: list[str]) -> tuple[MotorGroup, list[tuple[str, float]]]:
    mgr = MagicMock()
    mgr.send = AsyncMock()
    sink: list[tuple[str, float]] = []
    group = MotorGroup()
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


def _axes(names: list[str]) -> dict:
    return {name: _axis() for name in names}


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
        "y_axis": {
            "home": 11.0,
            "work_1": 12.0,
            "work_2": 12.5,
            "work_3": 13.0,
            "work_shared": 16.0,
            "approach": 14.0,
            "place": 15.0,
        },
        "rotate": {"home": 20.0, "pick": 21.0, "place": 22.0},
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
    ("conveyor", 0.0),
]

_VALVE_AXES = [f"valve_{i}" for i in range(1, 7)]

_SUB_POSITIONS = {
    "axes": {
        **_axes(["sub_arm_joint"]),
        **{name: _axis(command_mode="on_off", settle_s=0.0) for name in _VALVE_AXES},
        "pump_vac": _axis(command_mode="duty", settle_s=0.0),
        "pump_blow": _axis(command_mode="duty", settle_s=0.0),
    },
    "positions": {
        "sub_arm_joint": {"home": 0.0, "extended": 21.0, "handoff": 23.0, "place": 24.0},
        **{name: {"open": 1.0, "closed": 0.0} for name in _VALVE_AXES},
        "pump_vac": {"stop": 0.0, "run": 0.61},
        "pump_blow": {"stop": 0.0, "run": 0.62},
    },
}


def _valves(value: float) -> list[tuple[str, float]]:
    return [(name, value) for name in _VALVE_AXES]


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


class TestMainHandSteps:
    async def test_starts_and_ends_at_home(self) -> None:
        per_step, _ = await _run_each_step(MainHandSequence(), _MAIN_POSITIONS)

        assert dict(per_step[0][1]) == dict(_MAIN_HOME_TARGETS)
        assert dict(per_step[-1][1]) == dict(_MAIN_HOME_TARGETS)

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


class TestSubHandSteps:
    @pytest.mark.parametrize(
        ("method_name", "expected"),
        [
            (
                "move_to_home",
                [
                    *_valves(0.0),
                    ("pump_blow", 0.0),
                    ("sub_arm_joint", 0.0),
                    ("pump_vac", 0.61),
                ],
            ),
            ("extend_sub_arm", [("sub_arm_joint", 21.0)]),
            ("move_to_handoff", [("sub_arm_joint", 23.0)]),
            ("grip_by_suction", _valves(1.0)),
            ("move_to_place", [("sub_arm_joint", 24.0)]),
            (
                "release_at_place",
                [
                    *_valves(0.0),
                    ("pump_blow", 0.62),
                    ("pump_blow", 0.0),
                ],
            ),
            (
                "return_home",
                [
                    *_valves(0.0),
                    ("pump_blow", 0.0),
                    ("sub_arm_joint", 0.0),
                ],
            ),
        ],
    )
    async def test_step_sends_expected_targets(
        self, method_name: str, expected: list[tuple[str, float]]
    ) -> None:
        seq = SubHandSequence()
        sink, _ = _wire(seq, _SUB_POSITIONS)

        await getattr(seq, method_name)()

        assert sink == expected

    def test_step_labels_unchanged(self) -> None:
        seq = SubHandSequence()

        assert [s["label"] for s in seq.steps_info] == [
            "初期位置へ移動",
            "補助ハンド展開",
            "ワーク受け取り位置へ",
            "ワーク吸着",
            "配置位置へ移動",
            "ワーク解放 (配置)",
            "初期位置へ復帰",
        ]

    def test_suction_requires_trigger(self) -> None:
        seq = SubHandSequence()
        suction = next(s for s in seq.steps_info if s["label"] == "ワーク吸着")

        assert suction["require_trigger"] is True

    async def test_release_does_not_stop_vacuum_pump(self) -> None:
        seq = SubHandSequence()
        sink, _ = _wire(seq, _SUB_POSITIONS)

        await seq.release_at_place()

        assert "pump_vac" not in [name for name, _ in sink]


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
                required |= {
                    name for name in (guard.limits.plus, guard.limits.minus) if name is not None
                }
            if required - registered:
                missing[str(positions_path)] = required - registered

        assert missing == {}

    def test_本番とベンチの_guard_が一致する(self) -> None:
        """**歯止めは対である。片方だけ動かしてはならない。**

        ベンチ側だけ緩めると、本番では止まる構成がベンチでだけ端を踏み越える
        (`homing.search_distance` を対で持たせているのと同じ理由)。
        """
        for name in ("sub_hand_positions.yaml",):
            production = _load_shipped(name)
            bench = load_position_table(
                yaml.safe_load((_CONFIG_DIR / "bench" / "sub_hand_homing" / name).read_text()),
                source=name,
            )
            for axis in sorted(set(production.axes) & set(bench.axes)):
                assert production.axis(axis).guard == bench.axis(axis).guard, axis
