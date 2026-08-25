from __future__ import annotations

import logging
import pathlib

import pytest

from lib.sequence.positions import PositionLookupError
from main import _load_position_table_file, _positions_path

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

    assert table.command("lift_motor", "home") == pytest.approx(6.0)


def test_missing_file_warns_and_returns_empty_table(
    tmp_path: pathlib.Path, caplog: logging.LogCaptureFixture
) -> None:
    """yaml が無くても起動はできる。動かそうとした時点で初めて明示的に失敗する。"""
    path = tmp_path / "absent_positions.yaml"

    with caplog.at_level(logging.WARNING):
        table = _load_position_table_file(path)

    assert table.is_empty is True
    assert "absent_positions.yaml" in caplog.text
    with pytest.raises(PositionLookupError):
        table.command("lift_motor", "home")


def test_invalid_file_logs_error_and_returns_empty_table(
    tmp_path: pathlib.Path, caplog: logging.LogCaptureFixture
) -> None:
    """換算係数の無い軸が混ざった yaml は、誤った生値を送るより空で起動する方が安全。"""
    path = tmp_path / "broken_positions.yaml"
    path.write_text("axes: {}\npositions:\n  lift_motor:\n    home: 1.0\n")

    with caplog.at_level(logging.ERROR):
        table = _load_position_table_file(path)

    assert table.is_empty is True
    assert "broken_positions.yaml" in caplog.text
