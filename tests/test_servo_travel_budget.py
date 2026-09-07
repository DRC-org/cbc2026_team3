"""サーボ軸の `timeout_s` が、ファームのスルーレートで到達できる時間を持つか。

**この 2 つがずれると、機構が完全に正常でもシーケンスが必ず止まる。** サーボ基板は
`ServoMotion` が `travel = slewRate * elapsed` で角度を補間するので、移動にかかる時間は
`移動量 / スルーレート` で決まる。PC 側の `AxisHandle.wait_reached` は位置定数 yaml の
`axes.<軸>.timeout_s` で待つので、これが移動時間より短い位置の組を書いた瞬間、
その `move_to` は **毎回・決定的に** `SequenceTimeoutError` になる。

到達判定に早出しは無い —— `kDefaultServoReachedToleranceDeg` は 0.0deg で、`reached` は
補間が終わってから 100Hz の `FEEDBACK` に乗って届く。PC はスルーレートを実行時に変える
経路を持たない (`SET_PARAM` はどこからも送っていない) ので、**config に書いた値と
ファームに焼いた値がそのまま動いている値**である。

実際に `wall_f` (`initial: 270` ↔ `open: 90` = 180deg) が `timeout_s: 2.0` に対して
ちょうど 2.000 秒かかる状態で入っており、試合シーケンスの `move_work_3_to_conveyor` と
動作確認の「メインハンド 壁 前後」が両方とも止まる状態だった。規則は yaml のコメントに
書いてあっても守るのは人の注意力だけなので、ここで機械的に突き合わせる。
"""

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

# 仕様書 §2.2: Bit7..6 が基板種別。1 = サーボ基板 (0x40-0x7F)。
_SERVO_BOARD_KIND = 1

# `constexpr motorcan::ServoLimits kProvisionalLimits{0.0f, 270.0f, 90.0f};`
_LIMITS_RE = re.compile(
    r"constexpr\s+motorcan::ServoLimits\s+(\w+)\s*\{"
    r"\s*([\d.]+)f\s*,\s*([\d.]+)f\s*,\s*([\d.]+)f\s*\}\s*;"
)

# `{SlotRole::Servo, 5, 270.0f, kProvisionalLimits, kServoPulse270, false},`
_SERVO_SLOT_RE = re.compile(r"\{\s*SlotRole::Servo\s*,[^}]*?,\s*(\w*Limits\w*)\s*,")

# 補間が終わってからの `FEEDBACK` (100Hz) と `wait_reached` の polling、機構負荷による
# 遅れを見込む係数。1.0 ちょうどでは「計算上は間に合うが実機では毎回落ちる」値が通る。
_MARGIN = 1.2


def _servo_slew_rate_deg_per_s() -> float:
    """サーボ基板が全 Servo スロットへ適用しているスルーレート [deg/s]。

    **全スロットが同じ `ServoLimits` を使っていることまで確かめる。** スロットごとに
    別のレートを持てる構造なので、種類が増えたらこの検査は「どのレートで割るべきか」に
    答えられない —— そのまま素通しすると、検査が生きているように見えて実は嘘をつく。
    """
    text = _SERVO_CONFIG_H.read_text(encoding="utf-8")

    limits = {name: (float(lo), float(hi), float(rate)) for name, lo, hi, rate in _LIMITS_RE.findall(text)}
    assert limits, f"{_SERVO_CONFIG_H}: ServoLimits の定義を 1 つも読めなかった"

    used = set(_SERVO_SLOT_RE.findall(text))
    assert used, f"{_SERVO_CONFIG_H}: SlotRole::Servo の行を 1 つも読めなかった"
    assert len(used) == 1, f"{_SERVO_CONFIG_H}: Servo スロットが複数の ServoLimits を使っている: {sorted(used)}"

    name = used.pop()
    assert name in limits, f"{_SERVO_CONFIG_H}: Servo スロットが使う {name} の定義が読めない"
    rate = limits[name][2]
    assert rate > 0.0, f"{_SERVO_CONFIG_H}: {name} のスルーレートが {rate}"
    return rate


def _servo_motor_names(doc: dict) -> set[str]:
    """robot yaml の `motors` から、サーボ基板に載っている位置制御モータを拾う。"""
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
    """その軸に定義された位置定数の実値。コート別に書かれていれば両コートとも拾う。"""
    values: list[float] = []
    for name in table.names(axis):
        try:
            values.append(table.raw(axis, name))
        except PositionLookupError:
            values.extend(table.raw(axis, name, court=court) for court in Court)
    return values


def _servo_axis_cases() -> list[tuple[pathlib.Path, str, float, float]]:
    """同梱の全 config から (positions のパス, 軸名, 最大移動量 [deg], timeout_s)。

    robot yaml と同じディレクトリの `<robot>_positions.yaml` を対にする。bench で本番
    config をそのまま使うセット (`config/bench/main_hand/`) は positions を持たないので
    ここには現れないが、本番側の同じ軸が検査されるので取りこぼしにはならない。
    """
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
                # 位置が 1 つ以下なら軸内の移動が起きない
                continue
            cases.append((positions_path, axis, max(values) - min(values), spec.timeout_s))
    return cases


_CASES = _servo_axis_cases()


def _case_id(case: tuple[pathlib.Path, str, float, float]) -> str:
    path, axis, _, _ = case
    return f"{path.relative_to(_REPO_ROOT)}::{axis}"


def test_servo_axes_are_found() -> None:
    """検査対象が 0 件のまま緑を返さない (パーサが壊れたら気付けるように)。"""
    assert _CASES, "サーボ軸を 1 つも拾えなかった。robot yaml か positions の対応を確認すること"


@pytest.mark.parametrize("case", _CASES, ids=_case_id)
def test_timeout_covers_the_longest_travel(case: tuple[pathlib.Path, str, float, float]) -> None:
    """定義された位置のうち最も遠い組み合わせでも `timeout_s` 内に到達できること。

    見るのは「シーケンスが実際に踏む組み合わせ」ではなく**定義されている全体の幅**。
    動作確認 (`sequences/motor_check.py`) は 3 状態すべてを一巡するので、片道の最大が
    そのまま 1 回の `move_to` になる。
    """
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
