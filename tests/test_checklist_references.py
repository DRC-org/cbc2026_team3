from __future__ import annotations

import pathlib
import re

import pytest
import yaml

from lib.sequence.positions import PositionTable, load_position_table
from tests.test_config_schema import _BENCH_USES_PRODUCTION_CONFIG

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_CONFIG_DIR = _REPO_ROOT / "config"

_COMMAND_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*に\s*([A-Za-z_][A-Za-z0-9_]*)\s*を送り")


def _robot_yamls(directory: pathlib.Path) -> list[pathlib.Path]:
    production_robot = _BENCH_USES_PRODUCTION_CONFIG.get(directory.name)
    if production_robot is not None:
        return [_CONFIG_DIR / f"{production_robot}.yaml"]

    return [
        path
        for path in sorted(directory.glob("*.yaml"))
        if isinstance(doc := yaml.safe_load(path.read_text(encoding="utf-8")), dict)
        and "motors" in doc
    ]


def _position_table(directory: pathlib.Path) -> PositionTable:
    tables = []
    for robot_yaml in _robot_yamls(directory):
        positions = robot_yaml.with_name(f"{robot_yaml.stem}_positions.yaml")
        if not positions.exists():
            continue
        raw = yaml.safe_load(positions.read_text(encoding="utf-8"))
        tables.append(load_position_table(raw, source=str(positions)))
    return PositionTable.merged(tables) if tables else PositionTable.empty()


def _checklist_cases() -> list[tuple[pathlib.Path, str, str, str]]:
    cases: list[tuple[pathlib.Path, str, str, str]] = []
    for path in sorted(_CONFIG_DIR.rglob("checklist.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            continue
        for entries in (doc.get("checklists") or {}).values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                label = entry.get("label")
                if not isinstance(label, str):
                    continue
                for axis, position in _COMMAND_RE.findall(label):
                    cases.append((path, str(entry.get("id")), axis, position))
    return cases


_CASES = _checklist_cases()


def _case_id(case: tuple[pathlib.Path, str, str, str]) -> str:
    path, item_id, axis, position = case
    return f"{path.parent.name}::{item_id}::{axis}.{position}"


def test_some_commands_are_detected() -> None:
    assert _CASES, "『<軸> に <位置> を送り』の形の項目を 1 つも拾えなかった"


@pytest.mark.parametrize("case", _CASES, ids=_case_id)
def test_referenced_axis_and_position_exist(case: tuple[pathlib.Path, str, str, str]) -> None:
    path, item_id, axis, position = case
    table = _position_table(path.parent)
    where = f"{path.relative_to(_REPO_ROOT)} の '{item_id}'"

    assert axis in table.axes, (
        f"{where}: 軸 '{axis}' はこの構成に存在しない"
        f" (定義済み: {', '.join(table.axes) or '(なし)'})。"
        "操縦者は画面に無い軸を探すことになる"
    )
    assert position in table.names(axis), (
        f"{where}: 位置 '{axis}.{position}' はこの構成に存在しない"
        f" (定義済み: {', '.join(table.names(axis)) or '(なし)'})"
    )
