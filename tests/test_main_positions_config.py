from __future__ import annotations

import logging
import pathlib

import pytest
import yaml

from lib.sequence.positions import PositionLookupError, load_position_table
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


class TestShippedMainHandGuard:
    """メインハンド 3 本のスイッチが可動端の歯止めの入力になっていること。

    スイッチと歯止めはセンサ名の文字列でしか繋がっていないので、書き忘れも
    取り違えも yaml は読めてしまう。**`y_axis` は左右 2 本とも原点側に付く**ので、
    1 本だけ書くと守りが半分になり、しかもどちらが落ちているかは機構が壊れるまで
    分からない (向きの取り違えは `AxisSpec` が起動時に落とす)。
    """

    @pytest.fixture
    def table(self):
        return _load_position_table_file(_CONFIG_DIR / "main_hand_positions.yaml")

    def test_y_axis_は左右_2_本とも原点側に宣言する(self, table) -> None:
        spec = table.axis("y_axis")

        assert spec.guard is not None
        assert spec.guard.limits is not None
        assert spec.guard.limits.minus == (
            "y_axis_r_origin_sensor",
            "y_axis_l_origin_sensor",
        )
        assert spec.guard.limits.plus == ()

    def test_rotate_は原点側の_1_本を宣言する(self, table) -> None:
        spec = table.axis("rotate")

        assert spec.guard is not None
        assert spec.guard.limits is not None
        assert spec.guard.limits.minus == ("rotate_origin_sensor",)
        assert spec.guard.limits.plus == ()

    def test_零点確定のセンサが漏れなく歯止めに載っている(self, table) -> None:
        for axis in ("y_axis", "rotate"):
            spec = table.axis(axis)
            assert spec.homing is not None
            assert spec.guard is not None and spec.guard.limits is not None
            assert set(spec.homing.sensor_names) <= set(spec.guard.limits.minus), axis

    def test_跳躍量とトルクは未実測なので書かない(self, table) -> None:
        """**省略は「その守りが無い」ことを意味する。** 実測が入るまで埋めない。

        埋めると、効いている値なのか仮値なのかが config から読めなくなる
        (`max_step` を狭く取ると正常な移動が拒否され、`stall_torque` は
        M3508 が Nm を返さないので桁が合わない)。
        """
        for axis in ("y_axis", "rotate"):
            spec = table.axis(axis)
            assert spec.guard is not None
            assert spec.guard.max_step is None, axis
            assert spec.guard.stall_torque is None, axis


class TestShippedMainHandVias:
    """n 列目ワークへ寄せる経由点 (`*_via_*`) が両軸で対になり、可動範囲に収まっていること。

    値はどれも実機で干渉を見ながら詰める仮値である。**始点から終点へ単調に進むことは
    検査しない** —— 干渉を避けるために途中で一旦戻す経路を取る余地を残すため。
    """

    @pytest.fixture
    def table(self):
        """同梱ファイルを **例外を握り潰さない** loader で読む。

        `_load_position_table_file` は読み込み失敗を空表へ変えて起動を続けるため、
        値の誤りが「軸が定義されていない」に化けて、何がどう外れたのかが読めない。
        """
        path = _CONFIG_DIR / "main_hand_positions.yaml"
        return load_position_table(yaml.safe_load(path.read_text()), source=str(path))

    @staticmethod
    def _via_names(table, axis: str) -> set[str]:
        return {name for name in table.names(axis) if "_via_" in name}

    def test_経由点は両軸で同じ名前が揃っている(self, table) -> None:
        """シーケンスは同じ位置名を `y_axis` と `rotate` の両方へ引く。

        片方にしか無い名前は、実行時に `PositionLookupError` が出るまで分からない。
        """
        y_axis_vias = self._via_names(table, "y_axis")
        rotate_vias = self._via_names(table, "rotate")

        assert y_axis_vias, "y_axis に経由点が 1 つも無い"
        assert y_axis_vias == rotate_vias, (
            f"経由点の対が崩れている: y_axis のみ={sorted(y_axis_vias - rotate_vias)}, "
            f"rotate のみ={sorted(rotate_vias - y_axis_vias)}"
        )

    @pytest.mark.parametrize("axis", ["y_axis", "rotate"])
    def test_経由点は手動操縦の可動範囲に収まっている(self, table, axis: str) -> None:
        """範囲の判定は loader が持つ (`positions` 全件を `axes.<軸>.manual` と突き合わせる)。

        しきい値をここへ写すと対を機械的に守れなくなるので、同梱ファイルをその
        検証へ通すこと自体が検査であり、ここでは空振りでないことだけを確かめる。
        """
        assert table.axis(axis).manual is not None, f"{axis} に manual が無く範囲検証が効かない"
        assert self._via_names(table, axis), f"{axis} に経由点が 1 つも無い"
