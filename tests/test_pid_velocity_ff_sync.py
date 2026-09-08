from __future__ import annotations

import pathlib

import pytest
import yaml

from tests.test_config_schema import (
    _BENCH_DIRS,
    _bench_positions_path,
    _bench_robot_yaml_path,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_CONFIG_DIR = _REPO_ROOT / "config"

_PRODUCTION_ROBOTS = ("main_hand", "sub_hand")


def _load_yaml(path: pathlib.Path) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{path}: トップレベルが dict ではない"
    return doc


def _config_sets() -> list[tuple[str, pathlib.Path, pathlib.Path]]:
    sets: list[tuple[str, pathlib.Path, pathlib.Path]] = []

    for robot_name in _PRODUCTION_ROBOTS:
        sets.append(
            (
                robot_name,
                _CONFIG_DIR / f"{robot_name}.yaml",
                _CONFIG_DIR / f"{robot_name}_positions.yaml",
            )
        )

    for bench in _BENCH_DIRS:
        bench_dir = _CONFIG_DIR / "bench" / bench
        robot_yaml = _bench_robot_yaml_path(bench, bench_dir)
        robot_name = _load_yaml(robot_yaml)["robot_name"]
        positions_path = _bench_positions_path(bench, bench_dir, robot_name)
        sets.append((f"bench/{bench}", robot_yaml, positions_path))

    return sets


def _velocity_ff_pairs() -> list[tuple[str, pathlib.Path, pathlib.Path, str, str]]:
    pairs: list[tuple[str, pathlib.Path, pathlib.Path, str, str]] = []
    for label, robot_yaml, positions_path in _config_sets():
        positions = _load_yaml(positions_path)
        axes = positions.get("axes") or {}
        for axis_name, axis in axes.items():
            if not isinstance(axis, dict) or not isinstance(axis.get("motion"), dict):
                continue
            robot = _load_yaml(robot_yaml)
            for motor_name in axis.get("motors") or {}:
                motor = (robot.get("motors") or {}).get(motor_name)
                if not isinstance(motor, dict) or not isinstance(motor.get("pid"), dict):
                    continue
                pairs.append((label, robot_yaml, positions_path, axis_name, motor_name))
    return pairs


_PAIRS = _velocity_ff_pairs()


def _case_id(entry: tuple[str, pathlib.Path, pathlib.Path, str, str]) -> str:
    label, _, _, axis_name, motor_name = entry
    return f"{label}::{axis_name}::{motor_name}"


class TestPidVelocityFfSync:
    def test_at_least_one_pair_is_covered(self) -> None:
        assert len(_PAIRS) >= 2

    @pytest.mark.parametrize("entry", _PAIRS, ids=_case_id)
    def test_velocity_ff_matches_kd(self, entry) -> None:
        label, robot_yaml, positions_path, axis_name, motor_name = entry

        positions = _load_yaml(positions_path)
        velocity_ff = positions["axes"][axis_name]["motion"].get("velocity_ff", 0.0)

        robot = _load_yaml(robot_yaml)
        motor = robot["motors"][motor_name]
        assert "pid" in motor and "kd" in motor["pid"], (
            f"{robot_yaml}: {motor_name} に pid.kd が無い "
            f"({positions_path} の axes.{axis_name}.motion.velocity_ff と対になるはず)"
        )
        kd = motor["pid"]["kd"]

        assert kd == velocity_ff, (
            f"{label}: axes.{axis_name}.motion.velocity_ff={velocity_ff} ({positions_path}) が "
            f"motors.{motor_name}.pid.kd={kd} ({robot_yaml}) と食い違っている。"
            "片方だけ動かすと巡航中に D 項が出力を食い潰し、"
            "症状は「飽和率だけ上がって速くならない」"
        )
