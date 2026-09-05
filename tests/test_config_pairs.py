"""2 つの config ファイルにまたがる「対」を機械的に守る。

同じ 1 つの物理量が `config/<robot>.yaml` と `config/<robot>_positions.yaml` の
両方に現れる箇所がある。**片方だけ動かしても起動は通り、症状も出ない** ——
その形の設定は、突き合わせる仕組みが無ければ誰も気付けないまま試合へ持ち込まれる。

同じ発想でファーム側の版番号を守っているのが `tests/test_firmware_version_sync.py`
で、こちらは PC 側の config どうしを見る。**同一ファイル内で閉じる検証は
`lib/config_schema.py` と `lib/sequence/positions.py` が起動時に行う**ので、
ここへ書くのはファイルをまたぐぶんだけ。
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_CONFIG_DIR = _REPO_ROOT / "config"


def _motion_axes() -> list[tuple[pathlib.Path, pathlib.Path, str, dict, dict]]:
    """同梱の全 config セット (bench を含む) から `motion` を持つ軸を拾う。

    返すのは (位置定数ファイル, モータ定義ファイル, 軸名, motion, motors)。
    対応付けは `<robot>_positions.yaml` → 同じディレクトリの `<robot>.yaml` で、
    これは `main.py` が `--config` / `--positions` を組にして読む関係と同じ。
    """
    found: list[tuple[pathlib.Path, pathlib.Path, str, dict, dict]] = []
    for positions_path in sorted(_CONFIG_DIR.rglob("*_positions.yaml")):
        robot_path = positions_path.with_name(
            positions_path.name.removesuffix("_positions.yaml") + ".yaml"
        )
        if not robot_path.exists():
            continue
        positions_doc = yaml.safe_load(positions_path.read_text(encoding="utf-8"))
        robot_doc = yaml.safe_load(robot_path.read_text(encoding="utf-8"))
        if not isinstance(positions_doc, dict) or not isinstance(robot_doc, dict):
            continue
        motors = robot_doc.get("motors")
        if not isinstance(motors, dict):
            continue
        for axis_name, axis in (positions_doc.get("axes") or {}).items():
            if not isinstance(axis, dict):
                continue
            motion = axis.get("motion")
            if isinstance(motion, dict):
                found.append((positions_path, robot_path, axis_name, motion, motors))
    return found


_MOTION_AXES = _motion_axes()


def _case_id(entry: tuple[pathlib.Path, pathlib.Path, str, dict, dict]) -> str:
    positions_path, _, axis_name, _, _ = entry
    return f"{positions_path.relative_to(_REPO_ROOT)}::{axis_name}"


class TestVelocityFeedforwardMatchesKd:
    """`motion.velocity_ff` は対応するモータの `pid.kd` と同値でなければならない。

    `kd` の単位は counts/(deg/s) なので、**巡航速度がそのまま制動として出力に乗る**。
    y_axis の 200mm/s = 11003deg/s では D 項だけで -11003counts になり、
    `output_limit` 5000 を超えて逆向きに飽和する —— 台形プロファイルで消したはずの
    飽和が、速度を上げた途端に別の理由で戻る。定常追従では実測速度 ≒ 参照速度なので、
    参照速度へ同じ係数を掛けて足せばその制動をちょうど打ち消せる。

    **この対は運転中に崩せる。** `velocity_ff` は実行中に変更できず UI にも配信
    されないので、`/pid-tuning` から `kd` だけを動かすと対応が黙って壊れる。
    症状は「飽和率だけ上がって速くならない」で、今どの値で動いているかを読めるのは
    起動ログだけ。せめて config に書かれた初期値どうしはここで固定する。

    **`motion` を持たない軸は対象外。** 参照速度そのものが無いので `velocity_ff` に
    打ち消す相手が居ない (`config/bench/y_axis_tuning` は台形プロファイルを外して
    ステップ応答を測るセットなので、意図的にこの状態にある)。
    """

    def test_shipped_configs_have_motion_axes(self) -> None:
        # 収集が空振りしたまま緑になるのを防ぐ (rglob の書き間違い・config の移動)
        assert len(_MOTION_AXES) >= 2

    @pytest.mark.parametrize("entry", _MOTION_AXES, ids=_case_id)
    def test_axis_motors_exist(self, entry) -> None:
        """突き合わせる相手が居ることを先に確かめる。

        居ない軸を黙って読み飛ばすと、下の検査が「対象 0 件」で緑になる。
        """
        positions_path, robot_path, axis_name, _, motors = entry
        axis_motors = _axis_motor_names(positions_path, axis_name)
        assert axis_motors, f"{positions_path}: axes.{axis_name} に motors が無い"
        missing = [name for name in axis_motors if name not in motors]
        assert not missing, f"{robot_path}: {axis_name} のモータ {missing} が居ない"

    @pytest.mark.parametrize("entry", _MOTION_AXES, ids=_case_id)
    def test_velocity_ff_matches_every_motor_kd(self, entry) -> None:
        positions_path, robot_path, axis_name, motion, motors = entry
        velocity_ff = float(motion.get("velocity_ff", 0.0))

        for motor_name in _axis_motor_names(positions_path, axis_name):
            pid = motors[motor_name].get("pid")
            if not isinstance(pid, dict):
                # PC 側 PID を持たないモータ (pid: null = ドライバ内蔵の位置ループ)。
                # 打ち消す相手が居ないので対象にしない
                continue
            kd = float(pid.get("kd", 0.0))
            assert kd == velocity_ff, (
                f"{positions_path}: axes.{axis_name}.motion.velocity_ff ({velocity_ff}) が "
                f"{robot_path}: motors.{motor_name}.pid.kd ({kd}) と食い違っている。"
                " 巡航速度がそのまま制動として出力に乗るため、片方だけ動かすと"
                " 飽和率だけ上がって速くならない"
            )


def _axis_motor_names(positions_path: pathlib.Path, axis_name: str) -> list[str]:
    """`axes.<軸>.motors` に並ぶモータ名。単一モータ軸は軸名がそのままモータ名。"""
    doc = yaml.safe_load(positions_path.read_text(encoding="utf-8"))
    axis = doc["axes"][axis_name]
    motors = axis.get("motors")
    if isinstance(motors, dict):
        return list(motors)
    return [axis_name]
