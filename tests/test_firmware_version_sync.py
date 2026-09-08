from __future__ import annotations

import pathlib
import re

import pytest
import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_CONFIG_DIR = _REPO_ROOT / "config"
_FIRMWARE_DIR = _REPO_ROOT / "firmware"

_BOARD_KIND_TO_PROJECT = {
    1: "servo",
    2: "dc_motor",
    3: "solenoid",
}

_VERSION_RE = re.compile(r"constexpr\s+uint8_t\s+kFirmwareVersion\s*=\s*(\d+)\s*;")


def _firmware_version(project: str) -> int:
    header = _FIRMWARE_DIR / project / "include" / "config.h"
    matches = _VERSION_RE.findall(header.read_text(encoding="utf-8"))
    assert len(matches) == 1, f"{header}: kFirmwareVersion の定義が {len(matches)} 個"
    return int(matches[0])


def _generic_motors() -> list[tuple[pathlib.Path, str, dict]]:
    found: list[tuple[pathlib.Path, str, dict]] = []
    for path in sorted(_CONFIG_DIR.rglob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            continue
        motors = doc.get("motors")
        if not isinstance(motors, dict):
            continue
        for name, motor in motors.items():
            if isinstance(motor, dict) and motor.get("driver") == "generic":
                found.append((path, name, motor))
    return found


_GENERIC_MOTORS = _generic_motors()


def _case_id(entry: tuple[pathlib.Path, str, dict]) -> str:
    path, name, _ = entry
    return f"{path.relative_to(_REPO_ROOT)}::{name}"


class TestFirmwareVersionSync:
    def test_shipped_configs_have_generic_motors(self):
        assert len(_GENERIC_MOTORS) >= 20

    @pytest.mark.parametrize("entry", _GENERIC_MOTORS, ids=_case_id)
    def test_expected_firmware_is_declared(self, entry):
        path, name, motor = entry
        assert "expected_firmware" in motor, f"{path}: {name} に expected_firmware が無い"

    @pytest.mark.parametrize("entry", _GENERIC_MOTORS, ids=_case_id)
    def test_expected_firmware_matches_header(self, entry):
        path, name, motor = entry
        can_id = motor["can_id"]
        project = _BOARD_KIND_TO_PROJECT[(can_id >> 6) & 0b11]
        expected = _firmware_version(project)
        assert motor["expected_firmware"] == expected, (
            f"{path}: {name} (can_id=0x{can_id:02X}) の expected_firmware が "
            f"firmware/{project}/include/config.h の kFirmwareVersion={expected} と "
            f"食い違っている。片方だけ上げると、正しく焼いた基板が一斉に FAULT になる"
        )
