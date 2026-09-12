"""軸どうしの干渉 (`interlocks:`) が手動操縦とシーケンスの両方で効くこと。

禁止したいのは 1 つの姿勢 —— `sub_pitch` が close なのに `sub_offset` が close でない ——
で、そこへ入る指令は入口が 2 つ (ピッチを close へ / オフセットを close から離す) ある。
"""

from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, MagicMock

import can
import pytest
import yaml

from lib.drivers.base import ControlMode
from lib.manual import ManualControlError, ManualController
from lib.match_state import Court
from lib.sequence.engine import Sequence, step
from lib.sequence.interlock import AxisInterlock, InterlockSpec, InterlockViolation
from lib.sequence.motors import MotorGroup, MotorHandle, build_axis_state_reader
from lib.sequence.positions import PositionTable, load_position_table
from sequences.sub_hand import SubHandSequence
from tests.fake_drivers import StubFeedbackDriver

_CONFIG_DIR = pathlib.Path(__file__).resolve().parent.parent / "config"

# 出荷の config/sub_hand_positions.yaml と同じ値 (2026-09-10 実測)
_PITCH = {"open": 260.0, "close": 80.0}
_OFFSET = {"open": 0.0, "close": 180.0}


def _config(**overrides: object) -> dict:
    config: dict = {
        "axes": {
            "sub_pitch": {
                "unit": "deg",
                "command_unit": "deg",
                "tolerance": 2.0,
                "motors": {
                    "sub_pitch_r": {"scale": 1.0, "offset": 0.0},
                    "sub_pitch_l": {"scale": -1.0, "offset": 270.0},
                },
            },
            "sub_offset": {
                "unit": "deg",
                "command_unit": "deg",
                "tolerance": 2.0,
                "manual": {"min": 0.0, "max": 180.0, "steps": [5.0]},
            },
            "sub_lift": {"unit": "mm", "command_unit": "rad", "tolerance": 1.0},
        },
        "positions": {
            "sub_pitch": dict(_PITCH),
            "sub_offset": dict(_OFFSET),
            "sub_lift": {"top": -140.0},
        },
        "interlocks": [{"when": {"sub_pitch": "close"}, "require": {"sub_offset": "close"}}],
    }
    config.update(overrides)
    return config


class _EchoDriver(StubFeedbackDriver):
    def __init__(self, name: str) -> None:
        super().__init__(name, 1)
        self.commands: list[float] = []

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        self.commands.append(value)
        self.set_observed(position=value)
        return super().encode_target(mode, value)


def _group(*names: str) -> tuple[MotorGroup, dict[str, _EchoDriver]]:
    mgr = MagicMock()
    mgr.send = AsyncMock()
    group = MotorGroup()
    drivers: dict[str, _EchoDriver] = {}
    for name in names:
        drivers[name] = _EchoDriver(name)
        group.add(MotorHandle(name, drivers[name], mgr, poll_interval=0.001))
    return group, drivers


_MOTORS = ("sub_pitch_r", "sub_pitch_l", "sub_offset", "sub_lift")


def _build(config: dict | None = None) -> tuple[AxisInterlock, MotorGroup, dict[str, _EchoDriver]]:
    table = load_position_table(config or _config(), source="<test>")
    group, drivers = _group(*_MOTORS)
    return AxisInterlock(table), group, drivers


async def _park(group: MotorGroup, table: PositionTable, **targets: str) -> None:
    """軸を位置名へ「指令済み」にする (最後に指令した目標として残る)。"""
    for axis, name in targets.items():
        for motor, command in table.commands(axis, name, court=Court.RED).items():
            await group[motor].set_target(ControlMode.POSITION, command)


class TestDeclaration:
    def test_出荷のサブハンドはピッチとオフセットの干渉を宣言している(self) -> None:
        table = load_position_table(
            yaml.safe_load((_CONFIG_DIR / "sub_hand_positions.yaml").read_text()),
            source="sub_hand_positions.yaml",
        )
        assert table.interlocks == (
            InterlockSpec(when=(("sub_pitch", "close"),), require=(("sub_offset", "close"),)),
        )

    def test_書かなければ歯止めは無い(self) -> None:
        config = _config()
        del config["interlocks"]
        assert load_position_table(config).interlocks == ()

    def test_存在しない軸名は起動拒否(self) -> None:
        config = _config(
            interlocks=[{"when": {"pitch": "close"}, "require": {"sub_offset": "close"}}]
        )
        with pytest.raises(ValueError, match="軸 'pitch' が axes にありません"):
            load_position_table(config)

    def test_存在しない位置名は起動拒否(self) -> None:
        config = _config(
            interlocks=[{"when": {"sub_pitch": "close"}, "require": {"sub_offset": "closed"}}]
        )
        with pytest.raises(ValueError, match=r"'closed' が positions\.sub_offset にありません"):
            load_position_table(config)

    def test_tolerance_の無い軸は起動拒否(self) -> None:
        config = _config()
        del config["axes"]["sub_offset"]["tolerance"]
        with pytest.raises(ValueError, match=r"axes\.sub_offset\.tolerance が必要"):
            load_position_table(config)

    def test_同じ軸を両側に書くのは起動拒否(self) -> None:
        config = _config(
            interlocks=[{"when": {"sub_pitch": "close"}, "require": {"sub_pitch": "open"}}]
        )
        with pytest.raises(ValueError, match="同じ軸 sub_pitch"):
            load_position_table(config)

    def test_未知のキーと欠けたキーは起動拒否(self) -> None:
        with pytest.raises(ValueError, match="未知のキー: unless"):
            load_position_table(
                _config(
                    interlocks=[
                        {
                            "when": {"sub_pitch": "close"},
                            "require": {"sub_offset": "close"},
                            "unless": 1,
                        }
                    ]
                )
            )
        with pytest.raises(ValueError, match="必須キーがありません: require"):
            load_position_table(_config(interlocks=[{"when": {"sub_pitch": "close"}}]))

    def test_位置指令でない軸は起動拒否(self) -> None:
        config = _config()
        config["axes"]["pump"] = {"unit": "duty", "command_unit": "duty", "command_mode": "duty"}
        config["positions"]["pump"] = {"run": 1.0}
        config["interlocks"] = [{"when": {"pump": "run"}, "require": {"sub_offset": "close"}}]
        with pytest.raises(ValueError, match="位置指令の軸ではありません"):
            load_position_table(config)

    def test_統合した表は干渉の宣言を引き継ぐ(self) -> None:
        merged = PositionTable.merged([load_position_table(_config()), PositionTable.empty()])
        assert len(merged.interlocks) == 1


class TestJudgment:
    """入力は「指令が通った後の状態」。他方の軸は最後に指令した目標 → 無ければフィードバック。"""

    async def test_オフセットが_open_のままピッチを_close_へは拒否(self) -> None:
        interlock, group, _ = _build()
        await _park(group, interlock._table, sub_offset="open")

        with pytest.raises(InterlockViolation) as excinfo:
            interlock.check({"sub_pitch": _PITCH["close"]}, court=Court.RED, motors=group)

        message = str(excinfo.value)
        assert "sub_pitch が close のあいだ sub_offset は close でなければなりません" in message
        assert "sub_pitch が close (80deg) になり" in message
        assert "sub_offset は open (0deg、最後に指令した目標) のまま" in message
        assert "先に sub_offset を close にしてください" in message

    async def test_ピッチが_close_のままオフセットを_close_から離すのは拒否(self) -> None:
        interlock, group, _ = _build()
        await _park(group, interlock._table, sub_offset="close", sub_pitch="close")

        with pytest.raises(InterlockViolation) as excinfo:
            interlock.check({"sub_offset": 175.0}, court=Court.RED, motors=group)

        message = str(excinfo.value)
        assert "sub_offset が 定義位置の外 (175deg) になり" in message
        assert "sub_pitch は close (80deg、最後に指令した目標) のまま" in message
        assert "先に sub_pitch を close から外してください" in message

    async def test_許される順序は通る(self) -> None:
        interlock, group, _ = _build()
        table = interlock._table
        await _park(group, table, sub_pitch="open", sub_offset="open")

        interlock.check({"sub_offset": _OFFSET["close"]}, court=Court.RED, motors=group)
        await _park(group, table, sub_offset="close")
        interlock.check({"sub_pitch": _PITCH["close"]}, court=Court.RED, motors=group)
        await _park(group, table, sub_pitch="close")
        interlock.check({"sub_pitch": _PITCH["open"]}, court=Court.RED, motors=group)
        await _park(group, table, sub_pitch="open")
        interlock.check({"sub_offset": _OFFSET["open"]}, court=Court.RED, motors=group)

    async def test_中間の角は_close_ではないと扱う(self) -> None:
        interlock, group, _ = _build()
        for motor, command in interlock._table.axis("sub_pitch").to_commands(100.0).items():
            await group[motor].set_target(ControlMode.POSITION, command)

        interlock.check({"sub_offset": _OFFSET["open"]}, court=Court.RED, motors=group)

    async def test_close_かどうかは_tolerance_で決まる(self) -> None:
        interlock, group, _ = _build()
        await _park(group, interlock._table, sub_offset="open")

        interlock.check({"sub_pitch": _PITCH["close"] + 2.5}, court=Court.RED, motors=group)
        with pytest.raises(InterlockViolation):
            interlock.check({"sub_pitch": _PITCH["close"] + 1.5}, court=Court.RED, motors=group)

    async def test_目標が無ければフィードバックを使い文面にそう書く(self) -> None:
        interlock, group, drivers = _build()
        drivers["sub_offset"].set_observed(position=_OFFSET["open"])

        with pytest.raises(InterlockViolation, match=r"open \(0deg、フィードバック\) のまま"):
            interlock.check({"sub_pitch": _PITCH["close"]}, court=Court.RED, motors=group)

    async def test_緊急停止で目標が消えた後もフィードバックで守る(self) -> None:
        interlock, group, _ = _build()
        await _park(group, interlock._table, sub_offset="open")
        for handle in group.handles:
            handle.clear_target()

        with pytest.raises(InterlockViolation, match="フィードバック"):
            interlock.check({"sub_pitch": _PITCH["close"]}, court=Court.RED, motors=group)

    async def test_同じ指令に両軸が入っていれば通った後の組で判定する(self) -> None:
        interlock, group, _ = _build()
        await _park(group, interlock._table, sub_pitch="open", sub_offset="open")

        interlock.check(
            {"sub_pitch": _PITCH["close"], "sub_offset": _OFFSET["close"]},
            court=Court.RED,
            motors=group,
        )
        with pytest.raises(InterlockViolation, match="sub_offset が open \\(0deg\\) になります"):
            interlock.check(
                {"sub_pitch": _PITCH["close"], "sub_offset": _OFFSET["open"]},
                court=Court.RED,
                motors=group,
            )

    async def test_干渉に無関係な軸は他方の軸を読まない(self) -> None:
        interlock, group, _ = _build()
        interlock.check({"sub_lift": -140.0}, court=Court.RED, motors=group)

    async def test_他方の軸のモータが構成に無ければ判定できないとして拒否(self) -> None:
        table = load_position_table(_config(), source="<test>")
        group, _ = _group("sub_offset")

        with pytest.raises(InterlockViolation, match="sub_pitch_r, sub_pitch_l が構成にありません"):
            AxisInterlock(table).check({"sub_offset": 0.0}, court=Court.RED, motors=group)


class TestManual:
    def _build(self) -> tuple[ManualController, MotorGroup, dict[str, _EchoDriver]]:
        table = load_position_table(_config(), source="<test>")
        group, drivers = _group(*_MOTORS)
        return ManualController(group, table), group, drivers

    async def test_ピッチを_close_へは_ManualControlError(self) -> None:
        manual, _, drivers = self._build()
        await manual.move_to_position("sub_offset", "open")

        with pytest.raises(ManualControlError, match="軸どうしの干渉のため拒否") as excinfo:
            await manual.move_to_position("sub_pitch", "close")

        assert "sub_pitch" in str(excinfo.value) and "sub_offset" in str(excinfo.value)
        assert drivers["sub_pitch_r"].commands == []

    async def test_オフセットを_close_から離すのは_3_つの入口とも拒否(self) -> None:
        manual, _, drivers = self._build()
        await manual.move_to_position("sub_offset", "close")
        await manual.move_to_position("sub_pitch", "close")
        sent = list(drivers["sub_offset"].commands)

        with pytest.raises(ManualControlError, match="軸どうしの干渉のため拒否"):
            await manual.move_to_position("sub_offset", "open")
        with pytest.raises(ManualControlError, match="軸どうしの干渉のため拒否"):
            await manual.set_value("sub_offset", 90.0)
        with pytest.raises(ManualControlError, match="軸どうしの干渉のため拒否"):
            await manual.jog("sub_offset", -5.0)

        assert drivers["sub_offset"].commands == sent

    async def test_許される順序は通る(self) -> None:
        manual, _, drivers = self._build()
        await manual.move_to_position("sub_pitch", "open")
        await manual.move_to_position("sub_offset", "open")

        await manual.move_to_position("sub_offset", "close")
        await manual.move_to_position("sub_pitch", "close")
        await manual.move_to_position("sub_pitch", "open")
        await manual.move_to_position("sub_offset", "open")

        assert drivers["sub_offset"].commands == [0.0, 180.0, 0.0]
        assert drivers["sub_pitch_r"].commands == [260.0, 80.0, 260.0]

    async def test_拒否された指令はジョグの起点に残らない(self) -> None:
        manual, _, _ = self._build()
        await manual.move_to_position("sub_offset", "close")
        await manual.move_to_position("sub_pitch", "close")
        with pytest.raises(ManualControlError):
            await manual.set_value("sub_offset", 90.0)

        assert next(a["target"] for a in manual.axes_info() if a["name"] == "sub_offset") == 180.0


class _TwoStep(Sequence):
    @step("閉じる (逆順)")
    async def wrong_order(self) -> None:
        await self.move_to({"sub_pitch": "close"})
        await self.move_to({"sub_offset": "close"})


@pytest.mark.usefixtures("instant_settle")
class TestSequence:
    async def test_逆順に書き換えた段は_move_to_で拒否される(self) -> None:
        table = load_position_table(_config(), source="<test>")
        group, drivers = _group(*_MOTORS)
        seq = _TwoStep("test")
        seq.bind_motors(group)
        seq.bind_positions(table)
        await _park(group, table, sub_pitch="open", sub_offset="open")

        with pytest.raises(InterlockViolation, match="先に sub_offset を close にしてください"):
            await seq.wrong_order()

        assert drivers["sub_pitch_r"].commands == [260.0]

    async def test_出荷のサブハンドは通しで_1_度も発火しない(self, monkeypatch) -> None:
        table = load_position_table(
            yaml.safe_load((_CONFIG_DIR / "sub_hand_positions.yaml").read_text()),
            source="sub_hand_positions.yaml",
        )
        motors = [m for axis in table.axes for m in table.axis(axis).motor_names]
        group, _ = _group(*motors)
        group_with_sensors = MotorGroup(sensor_active=lambda _name: False)
        for handle in group.handles:
            # 通しは零点確定を済ませた後の状態。未確定だと guard.requires が先に拒否する
            handle.driver.mark_origin_confirmed()
            group_with_sensors.add(handle)
        seq = SubHandSequence()
        seq.bind_motors(group_with_sensors)
        seq.bind_positions(table)
        # sub_lift の scale はコート別なので、コートを選ぶまで軸を解決できない
        seq.set_court(Court.RED)
        # guard.requires の読み口。本番と同じ組み立てを使う (鮮度の材料だけ無いので
        # 「常に新しい」を与える)。配線しないと既定は「常に読めていない」で、
        # 干渉インターロックまで届く前に MotionGuard が拒否する
        group_with_sensors.bind_axis_state(
            build_axis_state_reader(
                table, group_with_sensors, court=lambda: seq.court, is_stale=lambda _name: False
            )
        )

        consulted = 0
        original = AxisInterlock.check

        def counting(self: AxisInterlock, *args, **kwargs) -> None:
            nonlocal consulted
            consulted += 1
            original(self, *args, **kwargs)

        monkeypatch.setattr(AxisInterlock, "check", counting)

        for info in seq.steps:
            await getattr(seq, info.method_name)()

        # 判定を素通りしていないこと (1 段に move_to が複数ある段があるので段数以上)
        assert consulted >= len(seq.steps)
