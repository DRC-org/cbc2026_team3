from __future__ import annotations

import pathlib
import re

import pytest
import yaml

from lib.match_state import Court
from lib.sequence.positions import PositionLookupError, load_position_table

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_CONFIG_DIR = _REPO_ROOT / "config"
_SERVO_CONFIG_H = _REPO_ROOT / "firmware" / "servo" / "include" / "config.h"

_SERVO_BOARD_KIND = 1

_LIMITS_RE = re.compile(
    r"constexpr\s+motorcan::ServoLimits\s+(\w+)\s*\{"
    r"\s*([\d.]+)f\s*,\s*([\d.]+)f\s*,\s*([\d.]+)f\s*\}\s*;"
)

_SERVO_SLOT_RE = re.compile(r"\{\s*SlotRole::Servo\s*,[^}]*?,\s*(\w*Limits\w*)\s*,")

# 実測: `wall_f` (270 ↔ 90 = 180deg) は `timeout_s: 2.0` に対してちょうど 2.000 秒かかる。
# 1.0 ちょうどの係数では「計算上は間に合うが実機では毎回落ちる」値が通る。
_MARGIN = 1.2


def _servo_slew_rate_deg_per_s() -> float:
    text = _SERVO_CONFIG_H.read_text(encoding="utf-8")

    limits = {
        name: (float(lo), float(hi), float(rate)) for name, lo, hi, rate in _LIMITS_RE.findall(text)
    }
    assert limits, f"{_SERVO_CONFIG_H}: ServoLimits の定義を 1 つも読めなかった"

    used = set(_SERVO_SLOT_RE.findall(text))
    assert used, f"{_SERVO_CONFIG_H}: SlotRole::Servo の行を 1 つも読めなかった"

    missing = sorted(used - limits.keys())
    assert not missing, f"{_SERVO_CONFIG_H}: Servo スロットが使う {missing} の定義が読めない"

    rates = {limits[name][2] for name in used}
    assert len(rates) == 1, (
        f"{_SERVO_CONFIG_H}: Servo スロットのスルーレートが分かれている:"
        f" {sorted((name, limits[name][2]) for name in used)}"
    )

    rate = rates.pop()
    assert rate > 0.0, f"{_SERVO_CONFIG_H}: Servo スロットのスルーレートが {rate}"
    return rate


def _servo_motor_names(doc: dict) -> set[str]:
    motors = doc.get("motors")
    if not isinstance(motors, dict):
        return set()
    found = set()
    for name, motor in motors.items():
        if not isinstance(motor, dict) or motor.get("driver") != "generic":
            continue
        if motor.get("control_type") != "position":
            continue
        can_id = motor.get("can_id")
        if isinstance(can_id, int) and (can_id >> 6) == _SERVO_BOARD_KIND:
            found.add(name)
    return found


def _axis_values(table, axis: str) -> list[float]:
    values: list[float] = []
    for name in table.names(axis):
        try:
            values.append(table.raw(axis, name))
        except PositionLookupError:
            values.extend(table.raw(axis, name, court=court) for court in Court)
    return values


def _servo_axis_cases() -> list[tuple[pathlib.Path, str, float, float]]:
    cases: list[tuple[pathlib.Path, str, float, float]] = []
    for robot_path in sorted(_CONFIG_DIR.rglob("*.yaml")):
        doc = yaml.safe_load(robot_path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            continue
        servo_motors = _servo_motor_names(doc)
        if not servo_motors:
            continue
        positions_path = robot_path.with_name(f"{robot_path.stem}_positions.yaml")
        if not positions_path.exists():
            continue

        raw = yaml.safe_load(positions_path.read_text(encoding="utf-8"))
        table = load_position_table(raw, source=str(positions_path))
        for axis in table.axes:
            spec = table.axis(axis)
            if not (set(spec.motor_names) & servo_motors):
                continue
            values = _axis_values(table, axis)
            if len(values) < 2:
                continue
            cases.append((positions_path, axis, max(values) - min(values), spec.timeout_s))
    return cases


_CASES = _servo_axis_cases()


def _case_id(case: tuple[pathlib.Path, str, float, float]) -> str:
    path, axis, _, _ = case
    return f"{path.relative_to(_REPO_ROOT)}::{axis}"


def test_servo_axes_are_found() -> None:
    assert _CASES, "サーボ軸を 1 つも拾えなかった。robot yaml か positions の対応を確認すること"


@pytest.mark.parametrize("case", _CASES, ids=_case_id)
def test_timeout_covers_the_longest_travel(case: tuple[pathlib.Path, str, float, float]) -> None:
    path, axis, travel_deg, timeout_s = case
    slew = _servo_slew_rate_deg_per_s()
    required_s = travel_deg / slew

    assert required_s * _MARGIN <= timeout_s, (
        f"{path.relative_to(_REPO_ROOT)} の '{axis}': 最大移動 {travel_deg:g}deg は"
        f" スルーレート {slew:g}deg/s で {required_s:.3f}s かかるのに"
        f" timeout_s={timeout_s:g}s しかない。"
        f" 到達判定に早出しは無く FEEDBACK (100Hz) の遅延も乗るので、"
        f" 少なくとも {required_s * _MARGIN:.3f}s は要る"
    )
