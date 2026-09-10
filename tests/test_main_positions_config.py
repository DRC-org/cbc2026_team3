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

    def test_align_step_is_coarser_than_the_search_step(self, homing) -> None:
        """整列段は片側 1 台で押すので、探索段 (左右 2 台) と同じ刻みでは押し切れない。"""
        assert homing.align_step is not None, "整列段の刻みが探索段に縛られている"
        assert homing.align_step > homing.step
        assert homing.align_distance is not None
        assert homing.align_step < homing.align_distance

    def test_retreats_before_the_next_axis_is_homed(self, homing) -> None:
        """`y_axis = 0` と `rotate = 0` は機構が干渉する。

        `rotate` も零点確定を持つ以上、`y_axis` は原点を確定した直後に干渉域を
        抜けていなければならない (抜けないと `rotate` が 1 歩も動けない)。
        """
        table = _load_position_table_file(_CONFIG_DIR / "main_hand_positions.yaml")

        assert table.axis("rotate").homing is not None, "rotate の零点確定が無効"
        assert homing.retreat_position is not None
        # 原点は - 側 (homing.direction: -1) なので、退避先は + 側にある
        assert table.raw("y_axis", homing.retreat_position) > 0.0

    def test_retreat_position_does_not_share_a_value(self, homing) -> None:
        """偶然の同値は「どちらの位置に居るのか」を読めなくする。"""
        table = _load_position_table_file(_CONFIG_DIR / "main_hand_positions.yaml")
        name = homing.retreat_position
        assert name is not None
        value = table.raw("y_axis", name)

        duplicates = [
            other
            for other in table.names("y_axis")
            if other != name and table.raw("y_axis", other) == pytest.approx(value)
        ]
        assert duplicates == []

    def test_search_distance_matches_the_manual_span(self, homing) -> None:
        manual = (
            _load_position_table_file(_CONFIG_DIR / "main_hand_positions.yaml")
            .axis("y_axis")
            .manual
        )

        assert manual is not None
        assert homing.search_distance == pytest.approx(manual.max_value - manual.min_value)


class TestShippedRotateTravel:
    """`rotate` の機械的可動域。**回転数の一意化が成り立つ唯一の根拠**である。

    半回転がちょうど可動端なので、可動域が無いと電源断のあいだに端から端へ
    動かされた軸を -180deg と読み、次の指令で逆向きに 180deg 回る
    (`docs/history/incidents.md` 2026-09-10)。
    """

    @pytest.fixture
    def table(self):
        return _load_position_table_file(_CONFIG_DIR / "main_hand_positions.yaml")

    def test_可動域を宣言している(self, table) -> None:
        travel = table.axis("rotate").travel

        assert travel is not None
        assert (travel.min_value, travel.max_value) == (0.0, 200.0)

    def test_可動域は1回転未満(self, table) -> None:
        """1 回転以上あると等価表現が 2 つ以上になり、一意化そのものが成り立たない。"""
        travel = table.axis("rotate").travel

        assert travel is not None
        assert 0.0 < travel.span < 360.0

    def test_手動操縦の範囲を機械的可動域が含む(self, table) -> None:
        """**同じ値である必要は無いが、含まれてはいる。**

        手動で行ける先が機械的に到達しないなら、どちらかの値が実機と違う。
        """
        spec = table.axis("rotate")

        assert spec.travel is not None and spec.manual is not None
        assert spec.travel.min_value <= spec.manual.min_value
        assert spec.manual.max_value <= spec.travel.max_value

    def test_他の軸には書かない(self, table) -> None:
        """一意化を使うのは EDULITE 05 だけである。"""
        for axis in table.axes:
            if axis == "rotate":
                continue
            assert table.axis(axis).travel is None, axis

    def test_ベンチ構成には書かない(self) -> None:
        """出力軸に何も繋がらないので「到達しうる範囲」が存在しない。

        `manual` の -15〜90deg を流用すると、手で回した軸の論理角をその範囲へ
        引き寄せて読む。
        """
        bench = _load_position_table_file(
            _CONFIG_DIR / "bench" / "edulite" / "main_hand_positions.yaml"
        )

        assert bench.axis("rotate").travel is None


class TestShippedRotateHome:
    """`rotate` の `home` が零点確定の原点そのものではないこと。

    **零点確定を持つ軸の一般則ではない。** `y_axis` の `home` は 0.0 のままでよく、
    干渉するのは `rotate` の側である —— `y_axis = 0` と `rotate = 0` の組が
    `sequences/main_hand.py` の `HOME` そのものなので、どちらか一方を原点から離す
    必要があり、離すと決めたのが `rotate` だから、この検査も `rotate` にだけ効く。
    """

    @pytest.fixture
    def table(self):
        return _load_position_table_file(_CONFIG_DIR / "main_hand_positions.yaml")

    def test_home_is_clear_of_the_origin_switch(self, table) -> None:
        """到達帯 (`home` ± `tolerance`) が原点センサの ON 区間へ食い込まないこと。

        原点は `-` 側 (`homing.direction: -1`) なので、到達帯の下限が正であれば
        0.0 の側へ食い込まない。食い込むと、可動端インターロック
        (`guard.limits.minus: rotate_origin_sensor`) が ON 区間の内側からの指令を拒む。
        """
        spec = table.axis("rotate")

        assert spec.homing is not None, "rotate の零点確定が無効になっている"
        assert spec.tolerance is not None, "tolerance が無く到達帯を決められない"
        assert table.raw("rotate", "home") - spec.tolerance > 0.0


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


class TestShippedMainHandRetreats:
    """コンベアへ寄せる退避点 (`*_after_*`) が両軸で対になり、可動範囲に収まっていること。

    値はどれも実機で干渉を見ながら詰める仮値である。**始点から終点へ単調に進むことは
    検査しない** —— 干渉を避けるために途中で一旦戻す経路を取る余地を残すため。

    `after_place` は `rotate` だけが持つ 1 点なので対の検査の対象外である
    (`_retreat_names` が見るのは軸をまたいで対になる `*_after_<n>` だけ)。
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
    def _retreat_names(table, axis: str) -> set[str]:
        return {name for name in table.names(axis) if "_after_" in name}

    def test_退避点は両軸で同じ名前が揃っている(self, table) -> None:
        """シーケンスは同じ位置名を `y_axis` と `rotate` の両方へ引く。

        片方にしか無い名前は、実行時に `PositionLookupError` が出るまで分からない。
        """
        y_axis_retreats = self._retreat_names(table, "y_axis")
        rotate_retreats = self._retreat_names(table, "rotate")

        assert y_axis_retreats, "y_axis に退避点が 1 つも無い"
        assert y_axis_retreats == rotate_retreats, (
            f"退避点の対が崩れている: y_axis のみ={sorted(y_axis_retreats - rotate_retreats)}, "
            f"rotate のみ={sorted(rotate_retreats - y_axis_retreats)}"
        )

    @pytest.mark.parametrize("axis", ["y_axis", "rotate"])
    def test_退避点は手動操縦の可動範囲に収まっている(self, table, axis: str) -> None:
        """範囲の判定は loader が持つ (`positions` 全件を `axes.<軸>.manual` と突き合わせる)。

        しきい値をここへ写すと対を機械的に守れなくなるので、同梱ファイルをその
        検証へ通すこと自体が検査であり、ここでは空振りでないことだけを確かめる。
        """
        assert table.axis(axis).manual is not None, f"{axis} に manual が無く範囲検証が効かない"
        assert self._retreat_names(table, axis), f"{axis} に退避点が 1 つも無い"
