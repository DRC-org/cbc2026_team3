from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, MagicMock

import can
import pytest
import yaml

from lib.drivers.base import ControlMode, MotorDriver, MotorState
from lib.sequence.engine import Sequence
from lib.sequence.motors import MotorGroup, MotorHandle
from lib.sequence.positions import load_position_table
from robots.main_hand import MainHandSequence
from robots.sub_hand import SubHandSequence

_CONFIG_DIR = pathlib.Path(__file__).resolve().parent.parent / "config"


class _RecordingDriver(MotorDriver):
    """指令を共有リストに記録し、即座に到達したことにするテスト用ドライバ。"""

    def __init__(self, name: str, sink: list[tuple[str, float]]) -> None:
        super().__init__(name, 1)
        self._sink = sink

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        assert mode is ControlMode.POSITION
        self._sink.append((self.name, value))
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


def _wire(seq: Sequence, position_config: dict) -> list[tuple[str, float]]:
    table = load_position_table(position_config)
    group, sink = _recording_group(list(table.axes))
    seq.bind_motors(group)
    seq.bind_positions(table)
    return sink


def _axes(names: list[str]) -> dict:
    # 換算を通したことを検証する目的ではないので、この試験では scale=1 の素通しにする
    return {name: {"unit": "test", "command_unit": "test", "timeout_s": 0.2} for name in names}


_MAIN_POSITIONS = {
    "axes": _axes(["lift_motor", "arm_joint", "gripper"]),
    "positions": {
        "lift_motor": {"home": 0.0, "work_3": 13.0, "approach": 14.0, "place": 15.0},
        "arm_joint": {"home": 0.0, "extended": 21.0, "retracted": 22.0},
        "gripper": {"open": 31.0, "closed": 32.0},
    },
}

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
            (
                "move_to_home",
                [("lift_motor", 0.0), ("arm_joint", 0.0), ("gripper", 31.0)],
            ),
            ("move_to_work_3", [("lift_motor", 13.0)]),
            ("approach_work", [("lift_motor", 14.0)]),
            ("extend_arm", [("arm_joint", 21.0)]),
            ("grip_work", [("gripper", 32.0)]),
            ("retract_arm", [("arm_joint", 22.0)]),
            ("carry_to_target", [("lift_motor", 15.0)]),
            ("release_work", [("gripper", 31.0)]),
            (
                "return_home",
                [("lift_motor", 0.0), ("arm_joint", 0.0), ("gripper", 31.0)],
            ),
        ],
    )
    async def test_step_sends_expected_targets(
        self, method_name: str, expected: list[tuple[str, float]]
    ) -> None:
        seq = MainHandSequence()
        sink = _wire(seq, _MAIN_POSITIONS)

        await getattr(seq, method_name)()

        assert sink == expected

    def test_step_labels_unchanged(self) -> None:
        seq = MainHandSequence()

        assert [s["label"] for s in seq.steps_info] == [
            "初期位置へ移動",
            "自陣ワーク 3 列目まで前進",
            "ワーク前まで前進",
            "アーム展開",
            "ハンド閉じる (ワーク把持)",
            "アーム引き戻し",
            "配置位置へ搬送",
            "ハンド開く (リリース)",
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
        sink = _wire(seq, _SUB_POSITIONS)

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


class TestShippedPositionYaml:
    """同梱の位置定数 yaml が、シーケンスの参照する軸・位置をすべて持つことを確認する。"""

    @pytest.mark.parametrize(
        ("yaml_name", "robot_config", "sequence_cls"),
        [
            ("main_hand_positions.yaml", "main_hand.yaml", MainHandSequence),
            ("sub_hand_positions.yaml", "sub_hand.yaml", SubHandSequence),
        ],
    )
    async def test_all_steps_run_against_shipped_yaml(
        self, yaml_name: str, robot_config: str, sequence_cls: type[Sequence]
    ) -> None:
        data = yaml.safe_load((_CONFIG_DIR / yaml_name).read_text())
        table = load_position_table(data, source=yaml_name)
        seq = sequence_cls()
        group, _ = _recording_group(list(table.axes))
        seq.bind_motors(group)
        seq.bind_positions(table)

        for info in seq.steps_info:
            method_name = seq._steps[info["index"]].method_name
            await getattr(seq, method_name)()

    @pytest.mark.parametrize(
        ("yaml_name", "robot_config"),
        [
            ("main_hand_positions.yaml", "main_hand.yaml"),
            ("sub_hand_positions.yaml", "sub_hand.yaml"),
        ],
    )
    def test_axes_match_configured_motors(self, yaml_name: str, robot_config: str) -> None:
        """位置定数の軸名は実機 config のモータ名と一致していること。"""
        table = load_position_table(
            yaml.safe_load((_CONFIG_DIR / yaml_name).read_text()), source=yaml_name
        )
        motors = yaml.safe_load((_CONFIG_DIR / robot_config).read_text())["motors"]

        assert set(table.axes) <= set(motors)
