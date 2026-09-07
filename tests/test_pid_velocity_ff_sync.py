"""`motion.velocity_ff` と `pid.kd` の一致を機械的に守る。

CLAUDE.md の不変条件:

> **`motion.velocity_ff` は `pid.kd` と同値に保つ。2 つの config ファイルにまたがる対である。**

`kd` の単位は counts/(deg/s) なので巡航速度がそのまま制動として出力に乗る
(200mm/s = 11003deg/s では D 項だけで -11003counts になり output_limit を
超えて逆向きに飽和する)。参照速度へ `velocity_ff` と同じ係数を掛けて足すことで
その制動をちょうど打ち消す設計であり、片方だけ動かすと「飽和率だけ上がって
速くならない」という症状になる。今どの値で動いているかを読めるのは起動ログ
だけなので (`motion.velocity_ff` は実行中に変更できず UI にも配信されない)、
ここで機械的に守る。先行事例は `tests/test_firmware_version_sync.py`。

**現状この対を実際に持つのは `config/main_hand_positions.yaml` の `y_axis` 1 組だけ**
(`velocity_ff` を書いている軸がそこにしか無い)。守る対が 1 組しか無いことは
承知のうえで、将来の編集がここを黙って崩さないように置く。

**`motion:` を持たない軸は対象外にする。** `config/bench/y_axis_tuning/main_hand.yaml` は
`pid.kd: 1.0` を持つが、対になる `config/bench/y_axis_tuning/main_hand_positions.yaml` の
`y_axis` には `motion:` が無い (台形プロファイルを意図的に持たせていないベンチ構成)。
「`kd` があれば `velocity_ff` と一致しろ」と素直に書くとこのベンチセットが即座に
誤検出で赤くなる —— 参照速度が存在しないので `velocity_ff` はそもそも定義され得ない。
このスコープの絞り込みは実在するこの反例に立っている (test_config_schema.py の
_BENCH_USES_PRODUCTION_CONFIG まわりで確認済み)。`motion:` を持つ軸だけを対象にすれば、
「対を書くなら両方書け」という以上のことは要求しない。
"""

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

#: 本番 (机上ベンチではない) ロボット。robot yaml と positions yaml は
#: config/ 直下に <robot_name>.yaml / <robot_name>_positions.yaml として並ぶ
#: (main.py の _positions_path と同じ規則)。
_PRODUCTION_ROBOTS = ("main_hand", "sub_hand")


def _load_yaml(path: pathlib.Path) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{path}: トップレベルが dict ではない"
    return doc


def _config_sets() -> list[tuple[str, pathlib.Path, pathlib.Path]]:
    """(表示名, robot yaml, positions yaml) の一覧。本番 2 つ + bench 8 セット。"""
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
    """`motion:` を持つ軸 1 本につき (表示名, robot yaml, positions yaml, 軸名, モータ名) を 1 件。

    `motion:` を持たない軸 (velocity_ff が定義され得ない軸) は集めない —
    docstring の y_axis_tuning がその実例。
    """
    pairs: list[tuple[str, pathlib.Path, pathlib.Path, str, str]] = []
    for label, robot_yaml, positions_path in _config_sets():
        positions = _load_yaml(positions_path)
        axes = positions.get("axes") or {}
        for axis_name, axis in axes.items():
            if not isinstance(axis, dict) or not isinstance(axis.get("motion"), dict):
                continue
            # PC 側 PID を持つモータだけが対を成す。EDULITE 05 / DM3520 はドライバが
            # 位置ループを内蔵するので config が `pid: null` で、`main._attach_motion_profiles`
            # も「位置制御ループに載らないモータへ書いても無害に無視される」と明記している。
            # 絞らないと、その軸へ `motion:` を書いた瞬間に本番コードが無害と保証している
            # 構成でこのテストだけが赤くなる。
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
        # 収集が空振りしたまま緑になるのを防ぐ (glob の書き間違い・config 移動)。
        # 現状 y_axis (motors: y_axis_r / y_axis_l) の 1 軸 2 モータぶんしか無い。
        assert len(_PAIRS) >= 2

    @pytest.mark.parametrize("entry", _PAIRS, ids=_case_id)
    def test_velocity_ff_matches_kd(self, entry) -> None:
        label, robot_yaml, positions_path, axis_name, motor_name = entry

        positions = _load_yaml(positions_path)
        # `lib/sequence/positions.py` は `velocity_ff` を既定 0.0 で許す (必須キーは
        # max_velocity / max_acceleration だけ)。添字で読むと、書き忘れた構成が
        # KeyError で落ちて下の説明文に到達しない
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
