from __future__ import annotations

import math

import pytest

from lib.drivers.base import ControlMode
from lib.match_state import Court
from lib.sequence.positions import (
    DEFAULT_TIMEOUT_S,
    MotorSpec,
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


def _command(table: PositionTable, axis: str, name: str, *, court: Court | None = None) -> float:
    """単一モータ軸 (モータ名 = 軸名) の指令値。本番と同じ commands() 経由で引く。"""
    return table.commands(axis, name, court=court)[axis]


class TestUnitConversion:
    def test_scale_is_applied(self) -> None:
        """人間の単位 (mm) からモータ指令 (M3508 モータ軸 deg) へ換算される。"""
        table = _table()

        assert _command(table, "lift_motor", "work") == pytest.approx(10.0 * 864.0)

    def test_offset_is_applied(self) -> None:
        table = load_position_table(
            {
                "axes": {"lift_motor": {"scale": 2.0, "offset": 100.0}},
                "positions": {"lift_motor": {"home": 5.0}},
            }
        )

        assert _command(table, "lift_motor", "home") == pytest.approx(5.0 * 2.0 + 100.0)

    def test_deg_to_rad_conversion(self) -> None:
        """EDULITE 05 の指令は rad。deg で書いた値が rad に換算される。"""
        table = _table()

        assert _command(table, "arm_joint", "extended") == pytest.approx(math.radians(15.0))

    def test_scale_and_offset_default_to_identity(self) -> None:
        table = load_position_table(
            {
                "axes": {"gripper": {"unit": "deg", "command_unit": "deg"}},
                "positions": {"gripper": {"open": 12.0}},
            }
        )

        assert _command(table, "gripper", "open") == pytest.approx(12.0)

    def test_raw_returns_human_unit_value(self) -> None:
        table = _table()

        assert table.raw("lift_motor", "work") == pytest.approx(10.0)


class TestTimeoutAndTolerance:
    def test_timeout_defaults(self) -> None:
        table = _table()

        assert table.axis("lift_motor").timeout_s == pytest.approx(DEFAULT_TIMEOUT_S)

    def test_timeout_from_yaml(self) -> None:
        table = load_position_table(
            {
                "axes": {"lift_motor": {"timeout_s": 2.5}},
                "positions": {"lift_motor": {"home": 0.0}},
            }
        )

        assert table.axis("lift_motor").timeout_s == pytest.approx(2.5)

    def test_tolerance_defaults_to_none(self) -> None:
        """未指定ならドライバ既定の許容差を使う (None を返す)。"""
        table = _table()

        assert table.axis("lift_motor").tolerance is None

    def test_tolerance_is_converted_by_scale(self) -> None:
        table = load_position_table(
            {
                "axes": {"lift_motor": {"scale": -864.0, "tolerance": 0.5}},
                "positions": {"lift_motor": {"home": 0.0}},
            }
        )

        # 許容差は向きを持たないため scale の符号は無視する。到達待ちは
        # AxisHandle がモータごとに換算するので、その経路と同じ API で確かめる
        spec = table.axis("lift_motor")
        assert spec.tolerance == pytest.approx(0.5)
        assert spec.motors[0].to_tolerance(spec.tolerance) == pytest.approx(0.5 * 864.0)


class TestCourtVariants:
    def test_scalar_value_is_court_independent(self) -> None:
        table = _table()

        assert _command(table, "lift_motor", "work", court=Court.RED) == pytest.approx(8640.0)
        assert _command(table, "lift_motor", "work", court=Court.BLUE) == pytest.approx(8640.0)

    def test_mapping_value_resolves_per_court(self) -> None:
        table = load_position_table(
            {
                "axes": {"lift_motor": {"scale": 1.0}},
                "positions": {"lift_motor": {"place": {"red": 10.0, "blue": -10.0}}},
            }
        )

        assert _command(table, "lift_motor", "place", court=Court.RED) == pytest.approx(10.0)
        assert _command(table, "lift_motor", "place", court=Court.BLUE) == pytest.approx(-10.0)

    def test_mapping_value_without_court_raises(self) -> None:
        table = load_position_table(
            {
                "axes": {"lift_motor": {}},
                "positions": {"lift_motor": {"place": {"red": 10.0, "blue": -10.0}}},
            }
        )

        with pytest.raises(PositionLookupError):
            _command(table, "lift_motor", "place")

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
            _command(table, "no_such_axis", "home")

        assert "lift_motor" in str(excinfo.value)

    def test_unknown_position_lists_available_names(self) -> None:
        table = _table()

        with pytest.raises(PositionLookupError) as excinfo:
            _command(table, "lift_motor", "no_such_position")

        assert "work" in str(excinfo.value)

    def test_source_is_included_in_error(self) -> None:
        table = _table()

        with pytest.raises(PositionLookupError) as excinfo:
            _command(table, "lift_motor", "no_such_position")

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

        assert table.axes == ()

    def test_empty_helper(self) -> None:
        table = PositionTable.empty(source="missing.yaml")

        assert table.axes == ()
        with pytest.raises(PositionLookupError, match=r"missing\.yaml"):
            _command(table, "lift_motor", "home")


class TestIntrospection:
    def test_axes_and_names(self) -> None:
        table = _table()

        assert set(table.axes) == {"lift_motor", "arm_joint"}
        assert set(table.names("lift_motor")) == {"home", "work"}

    def test_axis_spec_exposes_units(self) -> None:
        spec = _table().axis("lift_motor")

        assert spec.unit == "mm"
        assert spec.command_unit == "deg"


_PAIRED_CONFIG: dict = {
    "axes": {
        "y_axis": {
            "unit": "mm",
            "command_unit": "deg",
            "tolerance": 1.0,
            "sync_tolerance": 2.0,
            "motors": {
                "y_axis_r": {"scale": 864.15, "offset": 0.0},
                "y_axis_l": {"scale": -864.15, "offset": 0.0},
            },
        },
        "gripper": {"unit": "state", "command_unit": "deg", "scale": 1.0},
    },
    "positions": {
        "y_axis": {"home": 0.0, "work": 10.0},
        "gripper": {"open": 30.0, "closed": 0.0},
    },
}


class TestMotorSpec:
    def test_to_value_is_inverse_of_to_command(self) -> None:
        motor = MotorSpec(name="y_axis_l", scale=-864.15, offset=12.5)

        assert motor.to_value(motor.to_command(7.5)) == pytest.approx(7.5)

    def test_to_command_applies_scale_and_offset(self) -> None:
        motor = MotorSpec(name="y_axis_r", scale=2.0, offset=100.0)

        assert motor.to_command(5.0) == pytest.approx(110.0)


class TestPairedAxis:
    def test_commands_applies_per_motor_scale(self) -> None:
        """逆回転ペアは scale の符号で表すため、左右で符号が反転した指令になる。"""
        table = load_position_table(_PAIRED_CONFIG, source="<test>")

        assert table.commands("y_axis", "work") == {
            "y_axis_r": pytest.approx(10.0 * 864.15),
            "y_axis_l": pytest.approx(-10.0 * 864.15),
        }

    def test_commands_for_single_motor_axis_is_keyed_by_axis_name(self) -> None:
        table = load_position_table(_PAIRED_CONFIG, source="<test>")

        assert table.commands("gripper", "open") == {"gripper": pytest.approx(30.0)}

    def test_motor_names(self) -> None:
        table = load_position_table(_PAIRED_CONFIG, source="<test>")

        assert table.axis("y_axis").motor_names == ("y_axis_r", "y_axis_l")
        assert table.axis("gripper").motor_names == ("gripper",)

    def test_sync_tolerance_and_paired_axes(self) -> None:
        table = load_position_table(_PAIRED_CONFIG, source="<test>")

        assert table.sync_tolerance("y_axis") == pytest.approx(2.0)
        assert table.sync_tolerance("gripper") is None
        assert table.paired_axes() == ("y_axis",)

    def test_commands_resolves_court_variants(self) -> None:
        table = load_position_table(
            {
                "axes": {
                    "y_axis": {
                        "motors": {
                            "y_axis_r": {"scale": 2.0},
                            "y_axis_l": {"scale": -2.0},
                        }
                    }
                },
                "positions": {"y_axis": {"place": {"red": 10.0, "blue": -10.0}}},
            }
        )

        assert table.commands("y_axis", "place", court=Court.BLUE) == {
            "y_axis_r": pytest.approx(-20.0),
            "y_axis_l": pytest.approx(20.0),
        }


class TestCommandMode:
    def test_defaults_to_position(self) -> None:
        table = load_position_table(_PAIRED_CONFIG, source="<test>")

        assert table.axis("gripper").command_mode is ControlMode.POSITION
        assert table.axis("gripper").settle_s == pytest.approx(0.0)

    def test_duty_mode_with_settle(self) -> None:
        table = load_position_table(
            {
                "axes": {
                    "conveyor": {
                        "unit": "duty",
                        "command_unit": "duty",
                        "command_mode": "duty",
                        "settle_s": 0.3,
                    }
                },
                "positions": {"conveyor": {"run": 0.6, "stop": 0.0}},
            }
        )

        assert table.axis("conveyor").command_mode is ControlMode.DUTY
        assert table.axis("conveyor").settle_s == pytest.approx(0.3)

    def test_velocity_mode(self) -> None:
        table = load_position_table(
            {
                "axes": {"conveyor": {"command_mode": "velocity"}},
                "positions": {"conveyor": {"run": 100.0}},
            }
        )

        assert table.axis("conveyor").command_mode is ControlMode.VELOCITY

    def test_on_off_mode_with_settle(self) -> None:
        """電磁弁のような離散状態アクチュエータの軸 (仕様書 §9.2 / §9.3)。

        基板が弁の開閉を観測できないので到達判定を持てない。position 以外の軸は
        settle_s の固定待ちへ落ちるため、ここを書き忘れると指令の直後に次の
        ステップへ進む (弁が開き切る前に機体が動く)。
        """
        table = load_position_table(
            {
                "axes": {
                    "valve_1": {
                        "unit": "on_off",
                        "command_unit": "on_off",
                        "command_mode": "on_off",
                        "settle_s": 0.2,
                    }
                },
                "positions": {"valve_1": {"open": 1.0, "closed": 0.0}},
            }
        )

        assert table.axis("valve_1").command_mode is ControlMode.ON_OFF
        assert table.axis("valve_1").settle_s == pytest.approx(0.2)
        assert table.commands("valve_1", "open") == {"valve_1": 1.0}
        assert table.commands("valve_1", "closed") == {"valve_1": 0.0}

    def test_on_off_axis_rejects_manual_range(self) -> None:
        """on_off 軸に manual: は書けないこと。

        弁は開か閉のどちらかしか取らないので「可動範囲」が存在しない。書けてしまうと
        UI がジョグ行を描き、押しても何も起きない操作面ができる。
        """
        with pytest.raises(ValueError, match="command_mode: position"):
            load_position_table(
                {
                    "axes": {
                        "valve_1": {
                            "unit": "on_off",
                            "command_unit": "on_off",
                            "command_mode": "on_off",
                            "manual": {"min": 0.0, "max": 1.0},
                        }
                    },
                    "positions": {"valve_1": {"open": 1.0, "closed": 0.0}},
                }
            )

    def test_current_mode_is_rejected(self) -> None:
        """電流指令は位置定数から出す用途が無く、誤記のまま機構へ流すと危険なため拒否する。"""
        with pytest.raises(ValueError, match="command_mode"):
            load_position_table({"axes": {"conveyor": {"command_mode": "current"}}})

    def test_unknown_mode_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="command_mode"):
            load_position_table({"axes": {"conveyor": {"command_mode": "torque"}}})


class TestAxisSchemaValidation:
    def test_motors_with_axis_level_scale_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="motors"):
            load_position_table(
                {"axes": {"y_axis": {"scale": 2.0, "motors": {"y_axis_r": {"scale": 2.0}}}}}
            )

    def test_motors_with_axis_level_offset_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="motors"):
            load_position_table(
                {"axes": {"y_axis": {"offset": 1.0, "motors": {"y_axis_r": {"scale": 2.0}}}}}
            )

    def test_empty_motors_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="motors"):
            load_position_table({"axes": {"y_axis": {"motors": {}}}})

    def test_unknown_key_under_motor_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="invert"):
            load_position_table(
                {"axes": {"y_axis": {"motors": {"y_axis_r": {"scale": 2.0, "invert": True}}}}}
            )

    def test_zero_scale_on_any_motor_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="scale"):
            load_position_table(
                {
                    "axes": {
                        "y_axis": {
                            "motors": {
                                "y_axis_r": {"scale": 2.0},
                                "y_axis_l": {"scale": 0.0},
                            }
                        }
                    }
                }
            )

    def test_sync_tolerance_on_single_motor_axis_is_rejected(self) -> None:
        """1 台の軸に書くと防護が効かないまま「書いたつもり」になるため拒否する。"""
        with pytest.raises(ValueError, match="sync_tolerance"):
            load_position_table({"axes": {"gripper": {"scale": 1.0, "sync_tolerance": 2.0}}})

    def test_negative_sync_tolerance_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="sync_tolerance"):
            load_position_table(
                {
                    "axes": {
                        "y_axis": {
                            "sync_tolerance": -1.0,
                            "motors": {"a": {"scale": 1.0}, "b": {"scale": -1.0}},
                        }
                    }
                }
            )

    def test_negative_settle_s_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="settle_s"):
            load_position_table({"axes": {"conveyor": {"settle_s": -0.1}}})

    def test_motors_must_be_mapping(self) -> None:
        with pytest.raises(ValueError, match="motors"):
            load_position_table({"axes": {"y_axis": {"motors": ["y_axis_r"]}}})

    def test_motor_entry_must_be_mapping(self) -> None:
        with pytest.raises(ValueError, match="y_axis_r"):
            load_position_table({"axes": {"y_axis": {"motors": {"y_axis_r": 2.0}}}})


class TestManualSpec:
    """手動操縦の可動範囲 (axes.<軸>.manual)。

    通常運用の ``move_to`` は位置名でしか値を引けないため「定義した状態以外を
    送れない」ことが構造的に保証されている。手動はその保証を外す経路なので、
    代わりの境界が config に無ければ連続操作そのものを許さない。
    """

    def test_manual_を書かない軸は連続操作の対象外(self) -> None:
        table = _table()
        assert table.axis("lift_motor").manual is None
        assert table.manual_axes() == ()

    def test_manual_を書いた軸だけが連続操作の対象になる(self) -> None:
        table = _table(
            axes={
                "lift_motor": {
                    "unit": "mm",
                    "command_unit": "deg",
                    "scale": 864.0,
                    "manual": {"min": -2.0, "max": 20.0, "steps": [0.5, 2.0]},
                },
                "arm_joint": {"unit": "deg", "command_unit": "rad", "scale": math.pi / 180.0},
            }
        )
        manual = table.axis("lift_motor").manual
        assert manual is not None
        assert (manual.min_value, manual.max_value) == (-2.0, 20.0)
        assert manual.steps == (0.5, 2.0)
        assert table.manual_axes() == ("lift_motor",)

    def test_steps_を省くと既定のジョグ量が入る(self) -> None:
        table = _table(
            axes={
                "lift_motor": {
                    "unit": "mm",
                    "command_unit": "deg",
                    "scale": 864.0,
                    "manual": {"min": 0.0, "max": 20.0},
                },
                "arm_joint": {"unit": "deg", "command_unit": "rad", "scale": math.pi / 180.0},
            }
        )
        manual = table.axis("lift_motor").manual
        assert manual is not None
        assert len(manual.steps) >= 1
        assert all(step > 0 for step in manual.steps)

    def test_clamp_は範囲内へ丸める(self) -> None:
        table = _table(
            axes={
                "lift_motor": {
                    "unit": "mm",
                    "command_unit": "deg",
                    "scale": 864.0,
                    "manual": {"min": -2.0, "max": 20.0},
                },
                "arm_joint": {"unit": "deg", "command_unit": "rad", "scale": math.pi / 180.0},
            }
        )
        manual = table.axis("lift_motor").manual
        assert manual is not None
        assert manual.clamp(-99.0) == -2.0
        assert manual.clamp(99.0) == 20.0
        assert manual.clamp(5.0) == 5.0

    def test_min_が_max_以上なら起動を拒否する(self) -> None:
        with pytest.raises(ValueError, match="min は max より小さい"):
            _table(
                axes={
                    "lift_motor": {
                        "unit": "mm",
                        "scale": 864.0,
                        "manual": {"min": 10.0, "max": 10.0},
                    },
                    "arm_joint": {"unit": "deg", "scale": 1.0},
                }
            )

    def test_duty_軸への_manual_は起動を拒否する(self) -> None:
        # duty に「可動範囲」は存在しない。書けると押しても位置決めされない操作面が出る
        with pytest.raises(ValueError, match="command_mode: position"):
            load_position_table(
                {
                    "axes": {
                        "conveyor": {
                            "unit": "duty",
                            "command_mode": "duty",
                            "scale": 1.0,
                            "manual": {"min": -1.0, "max": 1.0},
                        }
                    },
                    "positions": {"conveyor": {"stop": 0.0}},
                },
                source="<test>",
            )

    def test_steps_に非正の値があれば起動を拒否する(self) -> None:
        with pytest.raises(ValueError, match="正の値"):
            _table(
                axes={
                    "lift_motor": {
                        "unit": "mm",
                        "scale": 864.0,
                        "manual": {"min": 0.0, "max": 20.0, "steps": [1.0, 0.0]},
                    },
                    "arm_joint": {"unit": "deg", "scale": 1.0},
                }
            )

    def test_範囲外のプリセット位置があれば起動を拒否する(self) -> None:
        # 「シーケンスは行ける場所へ手動では行けない」軸を config の時点で塞ぐ
        with pytest.raises(ValueError, match="manual の範囲"):
            _table(
                axes={
                    "lift_motor": {
                        "unit": "mm",
                        "scale": 864.0,
                        "manual": {"min": 0.0, "max": 5.0},
                    },
                    "arm_joint": {"unit": "deg", "scale": 1.0},
                }
            )

    def test_コート別のプリセットも両方が範囲内であることを見る(self) -> None:
        with pytest.raises(ValueError, match="manual の範囲"):
            load_position_table(
                {
                    "axes": {
                        "lift_motor": {
                            "unit": "mm",
                            "scale": 1.0,
                            "manual": {"min": 0.0, "max": 10.0},
                        }
                    },
                    "positions": {"lift_motor": {"work": {"red": 5.0, "blue": 50.0}}},
                },
                source="<test>",
            )


class TestAxisToValue:
    """指令値・フィードバックを人間の単位へ戻す逆換算 (手動の現在値表示に使う)。"""

    def test_単一モータ軸は_to_commands_の逆になる(self) -> None:
        spec = _table().axis("lift_motor")
        commands = spec.to_commands(12.5)
        assert spec.to_value(commands) == pytest.approx(12.5)

    def test_逆回転ペアでも符号を落とさず同じ値へ戻る(self) -> None:
        table = load_position_table(
            {
                "axes": {
                    "y_axis": {
                        "unit": "mm",
                        "motors": {
                            "y_axis_r": {"scale": 55.02},
                            "y_axis_l": {"scale": -55.02},
                        },
                    }
                },
                "positions": {"y_axis": {"home": 0.0}},
            },
            source="<test>",
        )
        spec = table.axis("y_axis")
        commands = spec.to_commands(8.0)
        # 逆回転ペアは指令値の符号が逆。人間の単位へ戻せば両方 8.0 になる
        assert commands["y_axis_r"] == pytest.approx(-commands["y_axis_l"])
        assert spec.to_value(commands) == pytest.approx(8.0)

    def test_値が揃わなければ例外にする(self) -> None:
        spec = _table().axis("lift_motor")
        with pytest.raises(PositionLookupError):
            spec.to_value({})


class TestMerged:
    """統合動作確認シーケンスは両ハンドの軸を 1 つの表から引く。

    軸名がロボット横断に一意であることに依存しているので、崩れたら起動時に
    弾けなければならない。
    """

    @staticmethod
    def _one(axis: str, position: str) -> PositionTable:
        return load_position_table(
            {
                "axes": {axis: {"unit": "deg", "command_unit": "deg"}},
                "positions": {axis: {position: 1.0}},
            },
            source=f"<{axis}>",
        )

    def test_両方の軸を引ける(self) -> None:
        merged = PositionTable.merged([self._one("y_axis", "home"), self._one("valve_1", "open")])

        assert set(merged.axes) == {"y_axis", "valve_1"}
        assert merged.raw("y_axis", "home") == 1.0
        assert merged.raw("valve_1", "open") == 1.0

    def test_軸名が衝突したら拒否する(self) -> None:
        # 後勝ちで上書きすると、動作確認が意図した側とは別の機体の軸を動かす。
        # 「指令したのに動かない機構」と「触っていないのに動く機構」が同時に出る
        with pytest.raises(ValueError, match="gripper"):
            PositionTable.merged([self._one("gripper", "open"), self._one("gripper", "closed")])

    def test_出どころを引き継ぐ(self) -> None:
        # 位置が見つからないときの例外メッセージは source を頼りに config を探す
        merged = PositionTable.merged([self._one("y_axis", "home")])
        assert "y_axis" in merged.source

    def test_空でも成立する(self) -> None:
        assert PositionTable.merged([]).axes == ()
