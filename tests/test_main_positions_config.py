from __future__ import annotations

import logging
import pathlib

import pytest
import yaml

from lib.sequence.positions import PositionLookupError
from main import _load_position_table_file, _positions_path

_CONFIG_DIR = pathlib.Path(__file__).resolve().parent.parent / "config"

_VALID_YAML = """
axes:
  lift_motor:
    unit: mm
    command_unit: deg
    scale: 2.0
positions:
  lift_motor:
    home: 3.0
"""


def test_positions_path_is_sibling_of_robot_config() -> None:
    path = _positions_path(pathlib.Path("/etc/cbc/main_hand.yaml"), "main_hand")

    assert path == pathlib.Path("/etc/cbc/main_hand_positions.yaml")


def test_loads_valid_file(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "main_hand_positions.yaml"
    path.write_text(_VALID_YAML)

    table = _load_position_table_file(path)

    assert table.commands("lift_motor", "home") == {"lift_motor": pytest.approx(6.0)}


def test_missing_file_warns_and_returns_empty_table(
    tmp_path: pathlib.Path, caplog: logging.LogCaptureFixture
) -> None:
    path = tmp_path / "absent_positions.yaml"

    with caplog.at_level(logging.WARNING):
        table = _load_position_table_file(path)

    assert table.axes == ()
    assert "absent_positions.yaml" in caplog.text
    with pytest.raises(PositionLookupError):
        table.commands("lift_motor", "home")


def test_invalid_file_logs_error_and_returns_empty_table(
    tmp_path: pathlib.Path, caplog: logging.LogCaptureFixture
) -> None:
    path = tmp_path / "broken_positions.yaml"
    path.write_text("axes: {}\npositions:\n  lift_motor:\n    home: 1.0\n")

    with caplog.at_level(logging.ERROR):
        table = _load_position_table_file(path)

    assert table.axes == ()
    assert "broken_positions.yaml" in caplog.text


class TestShippedYAxisHoming:
    @pytest.fixture
    def homing(self):
        table = _load_position_table_file(_CONFIG_DIR / "main_hand_positions.yaml")
        spec = table.axis("y_axis").homing
        assert spec is not None, "y_axis の零点確定が無効になっている"
        return spec

    def test_sensor_map_covers_the_axis_motors(self, homing) -> None:
        table = _load_position_table_file(_CONFIG_DIR / "main_hand_positions.yaml")

        assert homing.sensors is not None
        assert set(homing.sensors) == set(table.axis("y_axis").motor_names)

    def test_sensors_are_registered_in_the_robot_config(self, homing) -> None:
        config = yaml.safe_load((_CONFIG_DIR / "main_hand.yaml").read_text())
        registered = set(config.get("sensors") or {})

        assert set(homing.sensor_names) <= registered

    def test_align_distance_is_inside_the_sync_tolerance(self, homing) -> None:
        spec = _load_position_table_file(_CONFIG_DIR / "main_hand_positions.yaml").axis("y_axis")

        assert spec.sync_tolerance is not None
        assert homing.align_distance is not None
        assert homing.align_distance < spec.sync_tolerance

    def test_search_distance_matches_the_manual_span(self, homing) -> None:
        manual = (
            _load_position_table_file(_CONFIG_DIR / "main_hand_positions.yaml")
            .axis("y_axis")
            .manual
        )

        assert manual is not None
        assert homing.search_distance == pytest.approx(manual.max_value - manual.min_value)
