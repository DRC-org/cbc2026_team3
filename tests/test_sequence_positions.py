from __future__ import annotations

import math

import pytest

from lib.match_state import Court
from lib.sequence.positions import (
    DEFAULT_TIMEOUT_S,
    PositionLookupError,
    PositionTable,
    load_position_table,
)


def _table(**overrides: object) -> PositionTable:
    data: dict = {
        "axes": {
            "lift_motor": {
                "unit": "mm",
                "command_unit": "deg",
                "scale": 864.0,
                "offset": 0.0,
            },
            "arm_joint": {
                "unit": "deg",
                "command_unit": "rad",
                "scale": math.pi / 180.0,
            },
        },
        "positions": {
            "lift_motor": {"home": 0.0, "work": 10.0},
            "arm_joint": {"home": 0.0, "extended": 15.0},
        },
    }
    data.update(overrides)
    return load_position_table(data, source="<test>")


class TestUnitConversion:
    def test_scale_is_applied(self) -> None:
        """人間の単位 (mm) からモータ指令 (M3508 モータ軸 deg) へ換算される。"""
        table = _table()

        assert table.command("lift_motor", "work") == pytest.approx(10.0 * 864.0)

    def test_offset_is_applied(self) -> None:
        table = load_position_table(
            {
                "axes": {"lift_motor": {"scale": 2.0, "offset": 100.0}},
                "positions": {"lift_motor": {"home": 5.0}},
            }
        )

        assert table.command("lift_motor", "home") == pytest.approx(5.0 * 2.0 + 100.0)

    def test_deg_to_rad_conversion(self) -> None:
        """EDULITE 05 の指令は rad。deg で書いた値が rad に換算される。"""
        table = _table()

        assert table.command("arm_joint", "extended") == pytest.approx(math.radians(15.0))

    def test_scale_and_offset_default_to_identity(self) -> None:
        table = load_position_table(
            {
                "axes": {"gripper": {"unit": "deg", "command_unit": "deg"}},
                "positions": {"gripper": {"open": 12.0}},
            }
        )

        assert table.command("gripper", "open") == pytest.approx(12.0)

    def test_raw_returns_human_unit_value(self) -> None:
        table = _table()

        assert table.raw("lift_motor", "work") == pytest.approx(10.0)


class TestTimeoutAndTolerance:
    def test_timeout_defaults(self) -> None:
        table = _table()

        assert table.timeout("lift_motor") == pytest.approx(DEFAULT_TIMEOUT_S)

    def test_timeout_from_yaml(self) -> None:
        table = load_position_table(
            {
                "axes": {"lift_motor": {"timeout_s": 2.5}},
                "positions": {"lift_motor": {"home": 0.0}},
            }
        )

        assert table.timeout("lift_motor") == pytest.approx(2.5)

    def test_tolerance_defaults_to_none(self) -> None:
        """未指定ならドライバ既定の許容差を使う (None を返す)。"""
        table = _table()

        assert table.tolerance("lift_motor") is None

    def test_tolerance_is_converted_by_scale(self) -> None:
        table = load_position_table(
            {
                "axes": {"lift_motor": {"scale": -864.0, "tolerance": 0.5}},
                "positions": {"lift_motor": {"home": 0.0}},
            }
        )

        # 許容差は向きを持たないため scale の符号は無視する
        assert table.tolerance("lift_motor") == pytest.approx(0.5 * 864.0)


class TestCourtVariants:
    def test_scalar_value_is_court_independent(self) -> None:
        table = _table()

        assert table.command("lift_motor", "work", court=Court.RED) == pytest.approx(8640.0)
        assert table.command("lift_motor", "work", court=Court.BLUE) == pytest.approx(8640.0)

    def test_mapping_value_resolves_per_court(self) -> None:
        table = load_position_table(
            {
                "axes": {"lift_motor": {"scale": 1.0}},
                "positions": {"lift_motor": {"place": {"red": 10.0, "blue": -10.0}}},
            }
        )

        assert table.command("lift_motor", "place", court=Court.RED) == pytest.approx(10.0)
        assert table.command("lift_motor", "place", court=Court.BLUE) == pytest.approx(-10.0)

    def test_mapping_value_without_court_raises(self) -> None:
        table = load_position_table(
            {
                "axes": {"lift_motor": {}},
                "positions": {"lift_motor": {"place": {"red": 10.0, "blue": -10.0}}},
            }
        )

        with pytest.raises(PositionLookupError):
            table.command("lift_motor", "place")

    def test_mapping_missing_court_key_raises_at_load(self) -> None:
        with pytest.raises(ValueError, match="blue"):
            load_position_table(
                {
                    "axes": {"lift_motor": {}},
                    "positions": {"lift_motor": {"place": {"red": 10.0}}},
                }
            )


class TestLookupErrors:
    def test_unknown_axis_lists_available_axes(self) -> None:
        table = _table()

        with pytest.raises(PositionLookupError) as excinfo:
            table.command("no_such_axis", "home")

        assert "lift_motor" in str(excinfo.value)

    def test_unknown_position_lists_available_names(self) -> None:
        table = _table()

        with pytest.raises(PositionLookupError) as excinfo:
            table.command("lift_motor", "no_such_position")

        assert "work" in str(excinfo.value)

    def test_source_is_included_in_error(self) -> None:
        table = _table()

        with pytest.raises(PositionLookupError) as excinfo:
            table.command("lift_motor", "no_such_position")

        assert "<test>" in str(excinfo.value)


class TestLoadValidation:
    def test_positions_for_undefined_axis_raises(self) -> None:
        """換算係数が無い軸に生値を送ると単位事故になるため、読み込み時に弾く。"""
        with pytest.raises(ValueError, match="unknown_axis"):
            load_position_table(
                {
                    "axes": {"lift_motor": {}},
                    "positions": {"unknown_axis": {"home": 0.0}},
                }
            )

    def test_non_numeric_value_raises(self) -> None:
        with pytest.raises(ValueError):
            load_position_table(
                {
                    "axes": {"lift_motor": {}},
                    "positions": {"lift_motor": {"home": "abc"}},
                }
            )

    def test_empty_config_yields_empty_table(self) -> None:
        table = load_position_table({})

        assert table.is_empty is True
        assert table.axes == ()

    def test_empty_helper(self) -> None:
        table = PositionTable.empty(source="missing.yaml")

        assert table.is_empty is True
        with pytest.raises(PositionLookupError, match=r"missing\.yaml"):
            table.command("lift_motor", "home")


class TestIntrospection:
    def test_axes_and_names(self) -> None:
        table = _table()

        assert set(table.axes) == {"lift_motor", "arm_joint"}
        assert set(table.names("lift_motor")) == {"home", "work"}

    def test_axis_spec_exposes_units(self) -> None:
        spec = _table().axis("lift_motor")

        assert spec.unit == "mm"
        assert spec.command_unit == "deg"
