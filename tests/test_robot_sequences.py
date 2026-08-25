from __future__ import annotations

import collections
import pathlib
from unittest.mock import AsyncMock, MagicMock

import can
import pytest
import yaml

from lib.drivers.base import ControlMode, MotorDriver, MotorState
from lib.sequence.engine import Sequence
from lib.sequence.motors import MotorGroup, MotorHandle
from lib.sequence.positions import PositionTable, load_position_table
from robots.main_hand import MainHandSequence
from robots.sub_hand import SubHandSequence

_CONFIG_DIR = pathlib.Path(__file__).resolve().parent.parent / "config"

# 位置定数 yaml とロボット config の対応。両方を突き合わせる試験で共有する
_ROBOTS = [
    ("main_hand_positions.yaml", "main_hand.yaml", MainHandSequence),
    ("sub_hand_positions.yaml", "sub_hand.yaml", SubHandSequence),
]


class _RecordingDriver(MotorDriver):
    """指令を共有リストに記録し、即座に到達したことにするテスト用ドライバ。"""

    def __init__(self, name: str, sink: list[tuple[str, float]]) -> None:
        super().__init__(name, 1)
        self._sink = sink

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        # コンベアのような duty 指令の軸もあるため、モードは限定せずそのまま記録する
        self._sink.append((self.name, value))
        if mode is ControlMode.POSITION:
            self._state = MotorState(position=value)
        return can.Message(arbitration_id=0x100, data=bytes(8), is_extended_id=False)

    def decode_feedback(self, msg: can.Message) -> MotorState:  # pragma: no cover
        return self._state

    def matches_feedback(self, msg: can.Message) -> bool:  # pragma: no cover
        return False


def _recording_group(names: list[str]) -> tuple[MotorGroup, list[tuple[str, float]]]:
    mgr = MagicMock()
    mgr.send = AsyncMock()
    sink: list[tuple[str, float]] = []
    group = MotorGroup()
    for name in names:
        group.add(MotorHandle(name, _RecordingDriver(name, sink), mgr, poll_interval=0.001))
    return group, sink


def _motor_names(table: PositionTable) -> list[str]:
    """位置定数表が参照する全モータ名 (論理軸ではモータ名 != 軸名になる)。"""
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
    # 換算を通したことを検証する目的ではないので、この試験では scale=1 の素通しにする
    axis: dict = {"unit": "test", "command_unit": "test", "timeout_s": 0.2}
    axis.update(overrides)
    return axis


def _axes(names: list[str]) -> dict:
    return {name: _axis() for name in names}


def _paired_axis(*motors: tuple[str, float], **overrides: object) -> dict:
    """左右 2 台で 1 動作をする軸。逆回転は scale の符号で表す (本番 yaml と同じ書き方)。"""
    return _axis(
        motors={name: {"scale": scale} for name, scale in motors},
        # 実機の値ではなく「偏差判定が働いても素通しになる」幅。指令値の検証が目的のため
        sync_tolerance=overrides.pop("sync_tolerance", 100.0),
        **overrides,
    )


_MAIN_POSITIONS = {
    "axes": {
        "y_axis": _paired_axis(("y_axis_r", 1.0), ("y_axis_l", -1.0)),
        "rotate": _paired_axis(("rotate_r", 1.0), ("rotate_l", -1.0)),
        "gripper": _axis(),
        # duty 軸は到達判定を持たない。試験では固定待ちを入れない
        "conveyor": _axis(command_mode="duty", settle_s=0.0),
        "wall_f": _axis(),
        "wall_r": _axis(),
    },
    "positions": {
        "y_axis": {"home": 11.0, "work_3": 13.0, "approach": 14.0, "place": 15.0},
        "rotate": {"home": 20.0, "pick": 21.0, "place": 22.0},
        "gripper": {"open": 31.0, "closed": 32.0},
        "conveyor": {"stop": 0.0, "run": 0.4},
        "wall_f": {"initial": 41.0, "closed": 42.0, "open": 43.0},
        "wall_r": {"initial": 44.0, "closed": 45.0, "open": 46.0},
    },
}

# 初期位置は 2 ステップ (先頭と末尾) で共有されるため、期待値も 1 か所にまとめる
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

_SUB_POSITIONS = {
    "axes": _axes(["sub_arm_joint", "sub_gripper"]),
    "positions": {
        "sub_arm_joint": {"home": 0.0, "extended": 21.0, "handoff": 23.0, "place": 24.0},
        "sub_gripper": {"open": 31.0, "closed": 32.0},
    },
}


class TestMainHandSteps:
    @pytest.mark.parametrize(
        ("method_name", "expected"),
        [
            ("move_to_home", _MAIN_HOME_TARGETS),
            ("move_to_work_3", [("y_axis_r", 13.0), ("y_axis_l", -13.0)]),
            ("approach_work", [("y_axis_r", 14.0), ("y_axis_l", -14.0)]),
            ("rotate_to_pick", [("rotate_r", 21.0), ("rotate_l", -21.0)]),
            ("grip_work", [("gripper", 32.0)]),
            ("close_walls", [("wall_f", 42.0), ("wall_r", 45.0)]),
            ("rotate_to_home", [("rotate_r", 20.0), ("rotate_l", -20.0)]),
            ("carry_to_target", [("y_axis_r", 15.0), ("y_axis_l", -15.0)]),
            ("open_walls", [("wall_f", 43.0), ("wall_r", 46.0)]),
            ("start_conveyor", [("conveyor", 0.4)]),
            ("release_work", [("gripper", 31.0)]),
            ("stop_conveyor", [("conveyor", 0.0)]),
            ("return_home", _MAIN_HOME_TARGETS),
        ],
    )
    async def test_step_sends_expected_targets(
        self, method_name: str, expected: list[tuple[str, float]]
    ) -> None:
        seq = MainHandSequence()
        sink, _ = _wire(seq, _MAIN_POSITIONS)

        await getattr(seq, method_name)()

        assert sink == expected

    @pytest.mark.parametrize(
        ("method_name", "right", "left"),
        [
            ("move_to_work_3", "y_axis_r", "y_axis_l"),
            ("rotate_to_pick", "rotate_r", "rotate_l"),
        ],
    )
    async def test_paired_axis_commands_are_opposite_signs(
        self, method_name: str, right: str, left: str
    ) -> None:
        """左右直結のペア軸は逆回転で同一動作するため、指令は符号が反転していること。"""
        seq = MainHandSequence()
        sink, _ = _wire(seq, _MAIN_POSITIONS)

        await getattr(seq, method_name)()

        commands = dict(sink)
        assert commands[right] != 0.0
        assert commands[left] == pytest.approx(-commands[right])

    async def test_conveyor_is_commanded_as_duty(self) -> None:
        """コンベアは DC モータで位置の概念がないため duty で指令されること。"""
        seq = MainHandSequence()
        _, group = _wire(seq, _MAIN_POSITIONS)

        await seq.start_conveyor()

        assert group["conveyor"].mode is ControlMode.DUTY

    def test_step_labels(self) -> None:
        seq = MainHandSequence()

        assert [s["label"] for s in seq.steps_info] == [
            "初期位置へ移動",
            "自陣ワーク 3 列目まで前進",
            "ワーク前まで前進",
            "エンドエフェクタを把持姿勢へ",
            "ハンド閉じる (ワーク把持)",
            "壁を閉じる (ワーク保持)",
            "エンドエフェクタを戻す",
            "配置位置へ搬送",
            "壁を開く",
            "コンベア稼働",
            "ハンド開く (リリース)",
            "コンベア停止",
            "初期位置へ復帰",
        ]

    def test_grip_stops_even_in_full_auto(self) -> None:
        """把持は失敗すると機構破損に直結するため全自動でも目視確認を要求する。"""
        seq = MainHandSequence()
        grip = next(s for s in seq.steps_info if s["label"].startswith("ハンド閉じる"))

        assert grip["require_trigger"] is True
        assert grip["auto_stop"] is True

    def test_release_requires_trigger_but_does_not_block_full_auto(self) -> None:
        seq = MainHandSequence()
        release = next(s for s in seq.steps_info if s["label"].startswith("ハンド開く"))

        assert release["require_trigger"] is True
        assert release["auto_stop"] is False


class TestSubHandSteps:
    @pytest.mark.parametrize(
        ("method_name", "expected"),
        [
            ("move_to_home", [("sub_arm_joint", 0.0), ("sub_gripper", 31.0)]),
            ("extend_sub_arm", [("sub_arm_joint", 21.0)]),
            ("move_to_handoff", [("sub_arm_joint", 23.0)]),
            ("grip_handoff", [("sub_gripper", 32.0)]),
            ("move_to_place", [("sub_arm_joint", 24.0)]),
            ("release_at_place", [("sub_gripper", 31.0)]),
            ("return_home", [("sub_arm_joint", 0.0), ("sub_gripper", 31.0)]),
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
            "ハンド閉じる (受け取り)",
            "配置位置へ移動",
            "ハンド開く (配置)",
            "初期位置へ復帰",
        ]

    def test_grip_stops_even_in_full_auto(self) -> None:
        seq = SubHandSequence()
        grip = next(s for s in seq.steps_info if s["label"].startswith("ハンド閉じる"))

        assert grip["require_trigger"] is True
        assert grip["auto_stop"] is True


def _load_shipped(yaml_name: str) -> PositionTable:
    return load_position_table(
        yaml.safe_load((_CONFIG_DIR / yaml_name).read_text()), source=yaml_name
    )


def _load_config(robot_config: str) -> dict:
    return yaml.safe_load((_CONFIG_DIR / robot_config).read_text())


class TestShippedPositionYaml:
    """同梱の位置定数 yaml が、シーケンスの参照する軸・位置をすべて持つことを確認する。"""

    @pytest.mark.parametrize(("yaml_name", "robot_config", "sequence_cls"), _ROBOTS)
    async def test_all_steps_run_against_shipped_yaml(
        self, yaml_name: str, robot_config: str, sequence_cls: type[Sequence]
    ) -> None:
        table = _load_shipped(yaml_name)
        seq = sequence_cls()
        group, _ = _recording_group(_motor_names(table))
        seq.bind_motors(group)
        seq.bind_positions(table)

        for info in seq.steps_info:
            method_name = seq._steps[info["index"]].method_name
            await getattr(seq, method_name)()

    @pytest.mark.parametrize(("yaml_name", "robot_config", "sequence_cls"), _ROBOTS)
    def test_axis_motors_exist_in_robot_config(
        self, yaml_name: str, robot_config: str, sequence_cls: type[Sequence]
    ) -> None:
        """論理軸を構成するモータ名は、実機 config に定義済みのモータであること。

        軸名はモータ名でなくてよい (y_axis は y_axis_r / y_axis_l の 2 台で駆動する) ため、
        突き合わせるのは軸名ではなく AxisSpec.motor_names。
        """
        table = _load_shipped(yaml_name)
        motors = _load_config(robot_config)["motors"]

        assert set(_motor_names(table)) <= set(motors)

    async def test_main_hand_paired_axes_command_opposite_signs(self) -> None:
        """同梱 yaml でも左右ペア軸が逆符号で指令されること (符号を落とすと機構が壊れる)。"""
        table = _load_shipped("main_hand_positions.yaml")

        for axis, position in (("y_axis", "approach"), ("rotate", "pick")):
            commands = list(table.commands(axis, position).values())
            assert len(commands) == 2
            assert commands[0] != 0.0
            assert commands[1] == pytest.approx(-commands[0])


class TestShippedRobotConfig:
    def test_can_ids_are_unique_per_bus_across_robots(self) -> None:
        """can_id はバス単位でロボット横断に一意であること。

        can_edulite / can_generic はメインハンドとサブハンドで物理的に同じバスを共有する。
        重複すると CANManager._receive_loop が最初にマッチした 1 台で break し、
        もう一方は永久にフィードバックを得られない (EDULITE では無励磁のまま運用に入る)。
        """
        owners: dict[tuple[str, int], list[str]] = collections.defaultdict(list)

        for path in sorted(_CONFIG_DIR.glob("*.yaml")):
            config = yaml.safe_load(path.read_text()) or {}
            motors = config.get("motors")
            if not motors:
                continue
            buses = config.get("can_buses") or {}
            for motor_name, motor in motors.items():
                # バス別名 (m3508_bus 等) ではなく実インタフェース名で束ねる。
                # 別名が違っていても同じ物理バスなら衝突するため
                interface = buses.get(motor["bus"], motor["bus"])
                owners[(interface, int(motor["can_id"]))].append(f"{path.name}:{motor_name}")

        duplicated = {key: names for key, names in owners.items() if len(names) > 1}

        assert duplicated == {}

    @pytest.mark.parametrize(
        ("motor_name", "position_name"),
        [("gripper", "open"), ("wall_f", "open"), ("wall_r", "open")],
    )
    def test_motor_check_magnitude_matches_safe_position(
        self, motor_name: str, position_name: str
    ) -> None:
        """離散状態アクチュエータの動作確認は、実際に使う安全な状態値で行うこと。

        generic 既定の 0.1deg は「どの状態でもない」無意味な指令になる。値がずれると
        動作確認で動く位置と運用で使う位置が別物になり、確認が意味を失う。
        """
        motors = _load_config("main_hand.yaml")["motors"]
        table = _load_shipped("main_hand_positions.yaml")

        magnitude = motors[motor_name]["motor_check"]["magnitude"]

        assert magnitude == pytest.approx(table.raw(motor_name, position_name))

    @pytest.mark.parametrize("motor_name", ["y_axis_r", "y_axis_l", "rotate_r", "rotate_l"])
    def test_paired_motors_are_excluded_from_motor_check(self, motor_name: str) -> None:
        """左右直結のペア軸は 1 台ずつ動かすと機構を壊すため動作確認から除外する。"""
        motors = _load_config("main_hand.yaml")["motors"]

        assert motors[motor_name]["motor_check"]["magnitude"] == 0

    def test_main_hand_checklist_covers_excluded_pairs(self) -> None:
        """動作確認から外したペア軸・離散アクチュエータは目視確認項目で埋めること。"""
        checklist = yaml.safe_load((_CONFIG_DIR / "checklist.yaml").read_text())
        ids = {item["id"] for item in checklist["checklists"]["main_hand"]}

        assert {"y_axis_sync", "rotate_sync", "wall_initial", "conveyor_stop"} <= ids
