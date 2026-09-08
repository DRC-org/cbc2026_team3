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
    """yaml が無くても起動はできる。動かそうとした時点で初めて明示的に失敗する。"""
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
    """換算係数の無い軸が混ざった yaml は、誤った生値を送るより空で起動する方が安全。"""
    path = tmp_path / "broken_positions.yaml"
    path.write_text("axes: {}\npositions:\n  lift_motor:\n    home: 1.0\n")

    with caplog.at_level(logging.ERROR):
        table = _load_position_table_file(path)

    assert table.axes == ()
    assert "broken_positions.yaml" in caplog.text


class TestShippedYAxisHoming:
    """同梱の `config/main_hand_positions.yaml` の `y_axis.homing` を突き合わせる。

    この軸は左右にスイッチが 1 本ずつ付き、探索の後に**整列段**が押されていない側の
    モータだけを進める。整列段の指令先を決めるのは `homing.sensors` の対応表だけ
    なので、そこと `sensors:` 登録・`sync_tolerance`・`manual` の全幅は
    **どれも別の場所に書かれた値と対でしか意味を持たない**。片方だけ動かされたことを
    機械的に検出できるのはここしかない。
    """

    @pytest.fixture
    def homing(self):
        table = _load_position_table_file(_CONFIG_DIR / "main_hand_positions.yaml")
        spec = table.axis("y_axis").homing
        assert spec is not None, "y_axis の零点確定が無効になっている"
        return spec

    def test_sensor_map_covers_the_axis_motors(self, homing) -> None:
        """対応表のキーが `y_axis` のモータと過不足なく一致すること。

        1 つ書き忘れるとそのモータだけが整列段の対象から静かに外れ、片側が押されて
        いないまま原点が確定する (機構の遊びぶんが原点のずれとして焼き付く)。
        """
        table = _load_position_table_file(_CONFIG_DIR / "main_hand_positions.yaml")

        assert homing.sensors is not None
        assert set(homing.sensors) == set(table.axis("y_axis").motor_names)

    def test_sensors_are_registered_in_the_robot_config(self, homing) -> None:
        """対応表の値が `config/main_hand.yaml` の `sensors:` に登録済みであること。

        登録漏れは「原点センサが応答していません」で 1 歩も動かずに落ちるだけなので、
        配線不良と区別が付かない。
        """
        config = yaml.safe_load((_CONFIG_DIR / "main_hand.yaml").read_text())
        registered = set(config.get("sensors") or {})

        assert set(homing.sensor_names) <= registered

    def test_align_distance_is_inside_the_sync_tolerance(self, homing) -> None:
        """整列段の移動量上限が偏差許容差より**真に小さい**こと。

        届くと零点確定の最中に 3 層の保護 (位置制御ループ 200Hz / SyncMonitor 50Hz /
        move_to 完了時) が発報し、機体はラッチしたまま止まる。操縦者からは
        「動作確認の最初のステップでいつも緊急停止する」としか見えない。

        **強制しているのは `AxisSpec` の起動時検証なので、破った config はここまで
        読み込めずに落ちる** (この class の fixture が `homing is None` で止まる)。
        この試験の値は、その拒否を**会場での起動失敗ではなく config の試験として**
        受け止める点にある。
        """
        spec = _load_position_table_file(_CONFIG_DIR / "main_hand_positions.yaml").axis("y_axis")

        assert spec.sync_tolerance is not None
        assert homing.align_distance is not None
        assert homing.align_distance < spec.sync_tolerance

    def test_search_distance_matches_the_manual_span(self, homing) -> None:
        """探索距離が `manual` の全幅 (max - min) と一致すること。

        `manual` は「この軸を動かしてよい範囲」の唯一の宣言なので、そこを超えて動かす
        歯止めを置くと、宣言と歯止めのどちらが正なのか config から読めなくなる。
        全幅に揃えてあれば**可動範囲の上端から始めても下端までは必ず届き、下端より
        先へは行かない**。**対は 2 箇所に分かれて書かれているので、片方だけ動かされた
        ことを機械的に検出できるのはこの試験だけである。**
        """
        manual = (
            _load_position_table_file(_CONFIG_DIR / "main_hand_positions.yaml")
            .axis("y_axis")
            .manual
        )

        assert manual is not None
        assert homing.search_distance == pytest.approx(manual.max_value - manual.min_value)
