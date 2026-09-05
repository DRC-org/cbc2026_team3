"""main.py の組み立て (composition root) が正しく配線されているかを検証する。

``main`` の関数はすべて ``_`` 付きで、公開されているのは ``main()`` だけ。テストが
private を掴んでいるのはカプセル化の破りではなく、モジュール全体が非公開だから。
ここで確かめる事実 —— M3508 の載っていないバスに位置制御ループを作らない、
同期グループをループへ結び付ける、未知の control_type を起動時に弾く —— は
どれも取り違えると機構が壊れるものなので、``main()`` の起動 (実バスと実 config が
要る) を通してしか触れない状態にはできない。
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import logging
import pathlib
import socket
import struct
import time
import types
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import can
import pytest
import yaml

import main
from lib.axis_sync import MotorSpec, SyncGroup
from lib.can_manager import CANManager
from lib.config_schema import (
    MotorConfig,
    RobotConfig,
    SystemConfig,
    TuningSettings,
    load_robot_config,
)
from lib.control.position_loop import M3508PositionLoop
from lib.control.sync_monitor import SyncMonitor
from lib.drivers.base import ControlMode
from lib.drivers.dm3520 import Dm3520Driver
from lib.drivers.edulite05 import Edulite05Driver
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import CURRENT_MAX, M3508Driver
from lib.sequence.engine import Sequence
from lib.sequence.motors import EStopActiveError
from lib.sequence.positions import PositionTable, load_position_table
from lib.server import RobotContext, RobotServer
from main import (
    _DEFAULT_PID,
    _attach_motion_profiles,
    _attach_sync_groups,
    _build_manual_controller,
    _build_position_loops,
    _build_position_pid,
    _build_sync_groups,
    _build_target_refreshers,
    _create_motor,
    _load_all_configs,
    _load_pid_config,
    _wire_robot_motors,
)
from tests.fake_can import direct_runner, mark_feedback_at, mock_bus
from tests.fake_clock import FakeClock
from tests.feedback_frames import feed_m3508

_CONFIG_DIR = pathlib.Path(__file__).resolve().parent.parent / "config"


class _StubCANManager:
    """MotorHandle / M3508PositionLoop が触る API だけを実装したスタブ。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, can.Message]] = []
        self.sent_by_motor: list[tuple[str, can.Message]] = []
        self.feedback_at: dict[str, float] = {}

    async def send(self, motor_name: str, msg: can.Message) -> None:
        self.sent_by_motor.append((motor_name, msg))

    async def send_to_bus(self, bus_name: str, msg: can.Message) -> None:
        self.sent.append((bus_name, msg))

    def last_feedback_at(self, motor_name: str) -> float | None:
        return self.feedback_at.get(motor_name)

    @property
    def last_currents(self) -> tuple[int, int, int, int]:
        assert self.sent, "CAN フレームが 1 つも送信されていない"
        return struct.unpack(">hhhh", self.sent[-1][1].data)


class _DummySequence(Sequence):
    pass


def _robot(config: dict) -> RobotConfig:
    """検証済み設定へ通す。main.py 側は生 dict を受け取らない (誤記は起動時に弾く)。"""
    return load_robot_config(config, source="test.yaml")


def _motor(name: str, motor_cfg: dict) -> MotorConfig:
    return _robot({"robot_name": "r", "motors": {name: motor_cfg}}).motors[name]


def _m3508_config(**pid_overrides: object) -> dict:
    motor_cfg: dict = {"driver": "m3508", "bus": "m3508_bus", "can_id": 1}
    if pid_overrides:
        motor_cfg["pid"] = dict(pid_overrides)
    return {
        "robot_name": "main_hand",
        "motors": {"lift_motor": motor_cfg},
    }


class TestLoadPidConfig:
    def test_defaults_when_pid_section_missing(self) -> None:
        """pid セクションが無い M3508 は既定ゲインで動く (起動失敗にはしない)。"""
        result = _load_pid_config("lift_motor", None)

        assert result == _DEFAULT_PID

    def test_uses_yaml_values(self) -> None:
        pid_cfg = {
            "kp": 5.5,
            "ki": 0.25,
            "kd": 0.75,
            "integral_limit": 1200,
            "dead_band": 2.5,
            "output_limit": 3000,
        }

        result = _load_pid_config("lift_motor", pid_cfg)

        assert result["kp"] == 5.5
        assert result["ki"] == 0.25
        assert result["kd"] == 0.75
        assert result["integral_limit"] == 1200.0
        assert result["dead_band"] == 2.5
        assert result["output_limit"] == 3000.0

    def test_partial_override_fills_defaults(self) -> None:
        result = _load_pid_config("lift_motor", {"kp": 4.0})

        assert result["kp"] == 4.0
        assert result["ki"] == _DEFAULT_PID["ki"]
        assert result["kd"] == _DEFAULT_PID["kd"]
        assert result["dead_band"] == _DEFAULT_PID["dead_band"]
        assert result["output_limit"] == _DEFAULT_PID["output_limit"]

    def test_null_integral_limit_is_allowed(self) -> None:
        result = _load_pid_config("lift_motor", {"integral_limit": None})

        assert result["integral_limit"] is None

    def test_null_integral_limit_does_not_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        """integral_limit の null は「制限なし」という正当な指定なので警告しない。"""
        with caplog.at_level(logging.WARNING):
            _load_pid_config("lift_motor", {"integral_limit": None})

        assert caplog.records == []

    def test_null_numeric_key_falls_back_to_default(self) -> None:
        """書きかけの yaml (null) で起動を壊さず、安全側の既定値を使う。"""
        result = _load_pid_config("lift_motor", {"kp": None, "output_limit": None})

        assert result["kp"] == _DEFAULT_PID["kp"]
        assert result["output_limit"] == _DEFAULT_PID["output_limit"]

    def test_unknown_key_is_ignored(self) -> None:
        result = _load_pid_config("lift_motor", {"kp": 1.0, "kf": 9.0})

        assert "kf" not in result
        assert result["kp"] == 1.0


class TestBuildPositionPid:
    def test_gains_are_applied(self) -> None:
        pid = _build_position_pid(
            _motor("lift_motor", _m3508_config(kp=3.0, ki=0.5, kd=0.1)["motors"]["lift_motor"])
        )

        assert pid.kp == 3.0
        assert pid.ki == 0.5
        assert pid.kd == 0.1

    def test_output_range_narrowed_by_output_limit(self) -> None:
        """機構未確定のうちは電流上限を絞る。C620 のフルスケールは使わない。"""
        cfg = _motor("lift_motor", _m3508_config(output_limit=2000)["motors"]["lift_motor"])

        pid = _build_position_pid(cfg)

        assert pid.output_max == 2000.0
        assert pid.output_min == -2000.0

    def test_output_limit_capped_at_current_max(self) -> None:
        """config で C620 の範囲外を指定してもハード上限を超えない。"""
        cfg = _motor("lift_motor", _m3508_config(output_limit=999999)["motors"]["lift_motor"])

        pid = _build_position_pid(cfg)

        assert pid.output_max == float(CURRENT_MAX)
        assert pid.output_min == -float(CURRENT_MAX)

    def test_default_output_limit_is_conservative(self) -> None:
        pid = _build_position_pid(
            _motor("lift_motor", {"driver": "m3508", "bus": "b", "can_id": 1})
        )

        assert 0 < pid.output_max < float(CURRENT_MAX)


class TestBuildPositionLoops:
    def test_loop_created_only_for_buses_with_m3508(self) -> None:
        config = {
            "robot_name": "r",
            "motors": {
                "lift_motor": {"driver": "m3508", "bus": "m3508_bus", "can_id": 1},
                "arm_joint": {"driver": "edulite05", "bus": "edulite_bus", "can_id": 1},
                "gripper": {"driver": "generic", "bus": "generic_bus", "can_id": 1},
            },
        }
        motors = {
            "lift_motor": M3508Driver("lift_motor", can_id=1),
            "arm_joint": Edulite05Driver(name="arm_joint", can_id=1),
            "gripper": GenericDriver("gripper", can_id=1),
        }

        loops = _build_position_loops(
            _robot(config),
            _StubCANManager(),
            motors,
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
        )

        assert set(loops) == {"m3508_bus"}
        assert loops["m3508_bus"].motor_names == ("lift_motor",)

    def test_no_loop_without_m3508(self) -> None:
        config = {
            "robot_name": "sub_hand",
            "motors": {"gripper": {"driver": "generic", "bus": "generic_bus", "can_id": 1}},
        }
        motors = {"gripper": GenericDriver("gripper", can_id=1)}

        loops = _build_position_loops(
            _robot(config),
            _StubCANManager(),
            motors,
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
        )

        assert loops == {}

    def test_same_bus_m3508s_share_single_loop(self) -> None:
        """C620 は 1 フレームに 4 モータ分を載せるため、バスあたり 1 ループでなければならない。"""
        config = {
            "robot_name": "r",
            "motors": {
                "lift_motor": {"driver": "m3508", "bus": "m3508_bus", "can_id": 1},
                "tilt_motor": {"driver": "m3508", "bus": "m3508_bus", "can_id": 2},
            },
        }
        motors = {
            "lift_motor": M3508Driver("lift_motor", can_id=1),
            "tilt_motor": M3508Driver("tilt_motor", can_id=2),
        }

        loops = _build_position_loops(
            _robot(config),
            _StubCANManager(),
            motors,
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
        )

        assert list(loops) == ["m3508_bus"]
        assert set(loops["m3508_bus"].motor_names) == {"lift_motor", "tilt_motor"}

    def test_separate_buses_get_separate_loops(self) -> None:
        config = {
            "robot_name": "r",
            "motors": {
                "lift_motor": {"driver": "m3508", "bus": "bus_a", "can_id": 1},
                "tilt_motor": {"driver": "m3508", "bus": "bus_b", "can_id": 1},
            },
        }
        motors = {
            "lift_motor": M3508Driver("lift_motor", can_id=1),
            "tilt_motor": M3508Driver("tilt_motor", can_id=1),
        }

        loops = _build_position_loops(
            _robot(config),
            _StubCANManager(),
            motors,
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
        )

        assert set(loops) == {"bus_a", "bus_b"}

    async def test_feedback_timeout_is_propagated(self) -> None:
        """config の health.feedback_timeout_ms がループの途絶判定に効いている。"""
        config = _m3508_config()
        driver = M3508Driver("lift_motor", can_id=1)
        manager = _StubCANManager()
        feed_m3508(driver, deg=0.0)
        # 0.3 秒前のフィードバックを最後の受信とする
        manager.feedback_at["lift_motor"] = time.time() - 0.3

        strict = _build_position_loops(
            _robot(config),
            manager,
            {"lift_motor": driver},
            feedback_timeout_ms=100.0,
            is_estop_active=lambda: False,
        )["m3508_bus"]
        await strict.set_target("lift_motor", ControlMode.POSITION, 100.0)
        await strict.step()
        assert manager.last_currents == (0, 0, 0, 0)

        lenient = _build_position_loops(
            _robot(config),
            manager,
            {"lift_motor": driver},
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
        )["m3508_bus"]
        await lenient.set_target("lift_motor", ControlMode.POSITION, 100.0)
        await lenient.step()
        assert manager.last_currents[0] != 0

    async def test_tuning_settings_reach_the_loop(self) -> None:
        """config の tuning が届かないと、波形が 1 本も出ない。

        既定を「記録しない」に倒してあるぶん、配線漏れは例外ではなく沈黙として
        現れる。ここで通し 1 回ぶんを実際に閉じさせて、経路が生きていることを見る。
        """
        config = {
            "robot_name": "main_hand",
            "motors": {"lift_motor": {"driver": "m3508", "bus": "m3508_bus", "can_id": 1}},
        }
        driver = M3508Driver("lift_motor", can_id=1)
        manager = _StubCANManager()
        feed_m3508(driver, deg=0.0)
        manager.feedback_at["lift_motor"] = time.time()
        captures: list[object] = []

        loop = _build_position_loops(
            _robot(config),
            manager,
            {"lift_motor": driver},
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
            tuning=TuningSettings(
                enabled=True,
                window_s=0.02,
                pre_trigger_s=0.0,
                min_step_deg=0.5,
                max_points=300,
            ),
            capture_sink=captures.append,
        )["m3508_bus"]

        await loop.set_target("lift_motor", ControlMode.POSITION, 10.0)
        for _ in range(20):
            manager.feedback_at["lift_motor"] = time.time()
            await loop.step()

        assert captures

    async def test_no_recording_without_tuning_settings(self) -> None:
        config = {
            "robot_name": "main_hand",
            "motors": {"lift_motor": {"driver": "m3508", "bus": "m3508_bus", "can_id": 1}},
        }
        driver = M3508Driver("lift_motor", can_id=1)
        manager = _StubCANManager()
        feed_m3508(driver, deg=0.0)
        manager.feedback_at["lift_motor"] = time.time()
        captures: list[object] = []

        loop = _build_position_loops(
            _robot(config),
            manager,
            {"lift_motor": driver},
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
            capture_sink=captures.append,
        )["m3508_bus"]

        await loop.set_target("lift_motor", ControlMode.POSITION, 10.0)
        for _ in range(20):
            manager.feedback_at["lift_motor"] = time.time()
            await loop.step()

        assert captures == []


class TestWireRobotMotors:
    def _wire(self, estop_flag: list[bool]) -> tuple:
        config = _m3508_config()
        config["motors"]["gripper"] = {
            "driver": "generic",
            "bus": "generic_bus",
            "can_id": 1,
        }
        driver = M3508Driver("lift_motor", can_id=1)
        motors = {"lift_motor": driver, "gripper": GenericDriver("gripper", can_id=1)}
        manager = _StubCANManager()
        seq = _DummySequence("main_hand")

        loops = _wire_robot_motors(
            _robot(config),
            manager,
            motors,
            seq,
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: estop_flag[0],
        )
        return config, manager, motors, seq, loops

    def test_motor_group_is_bound_to_sequence(self) -> None:
        _, _, _, seq, _ = self._wire([False])

        assert seq.has_motors
        assert set(seq.motors.names) == {"lift_motor", "gripper"}

    async def test_m3508_target_goes_through_position_loop(self) -> None:
        """target_sinks 経由になっていれば、set_position は直接 CAN 送信しない。"""
        _, manager, _, seq, loops = self._wire([False])

        await seq.motors.lift_motor.set_target(ControlMode.POSITION, 42.0)

        assert loops[0].target("lift_motor") == 42.0
        assert manager.sent_by_motor == []

    async def test_non_m3508_still_sends_directly(self) -> None:
        _, manager, _, seq, _ = self._wire([False])

        await seq.motors.gripper.set_target(ControlMode.POSITION, 1.0)

        assert [name for name, _ in manager.sent_by_motor] == ["gripper"]

    async def test_estop_checker_blocks_motor_group(self) -> None:
        """緊急停止中はシーケンスからモータへ指令を出せない。"""
        flag = [False]
        _, _, _, seq, _ = self._wire(flag)

        flag[0] = True

        with pytest.raises(EStopActiveError):
            await seq.motors.lift_motor.set_target(ControlMode.POSITION, 10.0)
        with pytest.raises(EStopActiveError):
            await seq.motors.gripper.set_target(ControlMode.POSITION, 10.0)

    async def test_estop_checker_reaches_position_loop(self) -> None:
        """実行中ステップが出した目標も、緊急停止で位置制御ループ側が破棄する。"""
        flag = [False]
        _, manager, _, seq, loops = self._wire(flag)
        loop = loops[0]

        await seq.motors.lift_motor.set_target(ControlMode.POSITION, 100.0)
        assert loop.target("lift_motor") == 100.0

        flag[0] = True
        await loop.step()

        assert loop.target("lift_motor") is None
        assert manager.last_currents == (0, 0, 0, 0)


class TestBuildManualController:
    """手動操縦の指令口が、シーケンスと同じ MotorGroup を共有していること。

    別の MotorGroup を組むと、緊急停止インターロック・M3508 の PID 迂回・
    自作モタドラの再送対象が 2 セットに分かれる。片方の配線を落としても起動でき、
    「そちらから出した指令だけが停止中も通る」形で現れるので気付けない。
    """

    def _build(self, estop_flag: list[bool]):
        config = _m3508_config()
        config["motors"]["gripper"] = {
            "driver": "generic",
            "bus": "generic_bus",
            "can_id": 1,
        }
        motors = {
            "lift_motor": M3508Driver("lift_motor", can_id=1),
            "gripper": GenericDriver("gripper", can_id=1),
        }
        manager = _StubCANManager()
        seq = _DummySequence("main_hand")
        loops = _wire_robot_motors(
            _robot(config),
            manager,
            motors,
            seq,
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: estop_flag[0],
        )
        positions = load_position_table(
            {
                "axes": {
                    "lift": {
                        "unit": "mm",
                        "command_unit": "deg",
                        "manual": {"min": 0.0, "max": 20.0},
                        "motors": {"lift_motor": {"scale": 10.0}},
                    },
                    "gripper": {"unit": "deg", "command_unit": "deg"},
                },
                "positions": {"lift": {"home": 0.0}, "gripper": {"open": 5.0}},
            },
            source="<test>",
        )
        return manager, seq, loops, _build_manual_controller(seq, positions)

    async def test_m3508_への手動指令も位置制御ループを経由する(self) -> None:
        # 別グループを組むと M3508 へ位置指令が直接飛び、C620 に受理されず黙って効かない
        manager, _, loops, manual = self._build([False])

        await manual.set_value("lift", 4.0)

        assert loops[0].target("lift_motor") == pytest.approx(40.0)
        assert manager.sent_by_motor == []

    async def test_緊急停止中は手動指令も遮断される(self) -> None:
        flag = [False]
        _, _, _, manual = self._build(flag)
        flag[0] = True

        with pytest.raises(EStopActiveError):
            await manual.set_value("lift", 4.0)
        with pytest.raises(EStopActiveError):
            await manual.move_to_position("gripper", "open")

    async def test_自作モタドラの手動目標が再送対象へ載る(self) -> None:
        # 再送が効かないと、手動で開いたグリッパが 500ms で戻る
        manager, seq, _, manual = self._build([False])
        refreshers = _build_target_refreshers(
            seq.motors,
            {"gripper": GenericDriver("gripper", can_id=1)},
            manager,
            is_estop_active=lambda: False,
        )
        assert len(refreshers) == 1
        refresher = refreshers[0]

        await manual.move_to_position("gripper", "open")
        manager.sent_by_motor.clear()
        await refresher.step()

        assert [name for name, _ in manager.sent_by_motor] == ["gripper"]


class TestBuildTargetRefresher:
    """周期送信が要るモータだけを、種別ごとに別のタスクへ束ねる配線。"""

    def _wire(self, extra_motors: dict | None = None, extra_config: dict | None = None) -> tuple:
        config = _m3508_config()
        config["motors"]["gripper"] = {
            "driver": "generic",
            "bus": "generic_bus",
            "can_id": 1,
        }
        config["motors"].update(extra_config or {})
        motors = {
            "lift_motor": M3508Driver("lift_motor", can_id=1),
            "gripper": GenericDriver("gripper", can_id=1),
            **(extra_motors or {}),
        }
        manager = _StubCANManager()
        seq = _DummySequence("main_hand")
        _wire_robot_motors(
            _robot(config),
            manager,
            motors,
            seq,
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
        )
        return manager, motors, seq

    @staticmethod
    def _slide_config() -> dict:
        return {"sub_slide": {"driver": "dm3520", "bus": "generic_bus", "can_id": 1}}

    def test_only_generic_motors_are_refreshed(self) -> None:
        """M3508 は位置制御ループが 200Hz で送り続けるので再送対象ではない。"""
        manager, motors, seq = self._wire()

        refreshers = _build_target_refreshers(
            seq.motors, motors, manager, is_estop_active=lambda: False
        )

        assert [r.motor_names for r in refreshers] == [("gripper",)]

    def test_empty_without_periodic_motors(self) -> None:
        motors = {"lift_motor": M3508Driver("lift_motor", can_id=1)}
        manager = _StubCANManager()
        seq = _DummySequence("main_hand")
        _wire_robot_motors(
            _robot(_m3508_config()),
            manager,
            motors,
            seq,
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
        )

        assert (
            _build_target_refreshers(seq.motors, motors, manager, is_estop_active=lambda: False)
            == []
        )

    def test_dm3520_gets_its_own_refresher(self) -> None:
        """**自作モタドラと同じタスクに束ねてはならない。**

        目標を持たないモータの扱いが正反対で、自作モタドラは送ってはならず
        (起動直後にコンベアが回り出す)、DM3520 は送らなければならない
        (問い合わせ駆動なのでフィードバックが 1 通も来ない)。
        """
        manager, motors, seq = self._wire(
            extra_motors={"sub_slide": Dm3520Driver("sub_slide", can_id=1)},
            extra_config=self._slide_config(),
        )

        refreshers = _build_target_refreshers(
            seq.motors, motors, manager, is_estop_active=lambda: False
        )

        assert [r.motor_names for r in refreshers] == [("gripper",), ("sub_slide",)]

    def test_edulite_gets_a_refresher_too(self) -> None:
        """**EDULITE 05 も問い合わせ駆動である。** 自発的にはフィードバックを返さない。

        かつて ``_build_target_refreshers`` は「EDULITE は自発的にフィードバックを
        返すので対象外」として除外していたが、実機はそうではなかった —— 励磁したまま
        13 秒放置しても 1 通も届かず、届いたのは起動時に PC が送ったフレームへの
        応答 20 通だけだった。

        再送しないと、操縦していない間じゅう ``MotorHealth.STALE`` になる。症状は
        「手動操縦すると動くのに常に赤い」だけで、配線不良と区別が付かない。
        """
        manager, motors, seq = self._wire(
            extra_motors={"rotate_r": Edulite05Driver("rotate_r", can_id=1)},
            extra_config={"rotate_r": {"driver": "edulite05", "bus": "generic_bus", "can_id": 1}},
        )

        refreshers = _build_target_refreshers(
            seq.motors, motors, manager, is_estop_active=lambda: False
        )

        assert ("rotate_r",) in [r.motor_names for r in refreshers]

    async def test_edulite_is_polled_even_without_a_target(self) -> None:
        """**目標を持たないモータへも送る。** ここが自作モタドラとの決定的な違い。

        送らないと「励磁して待機しているだけの状態」が丸ごと観測できなくなる
        (フィードバックが 1 通も来ないので STALE になる)。
        """
        manager, motors, seq = self._wire(
            extra_motors={"rotate_r": Edulite05Driver("rotate_r", can_id=1)},
            extra_config={"rotate_r": {"driver": "edulite05", "bus": "generic_bus", "can_id": 1}},
        )
        refreshers = _build_target_refreshers(
            seq.motors, motors, manager, is_estop_active=lambda: False
        )
        manager.sent_by_motor.clear()

        for refresher in refreshers:
            await refresher.step()

        assert "rotate_r" in [name for name, _ in manager.sent_by_motor]

    async def test_estop_checker_blocks_resend(self) -> None:
        """再送は停止指令を上書きする。緊急停止中は 1 通も出してはならない。"""
        manager, motors, seq = self._wire()
        flag = [False]
        refreshers = _build_target_refreshers(
            seq.motors, motors, manager, is_estop_active=lambda: flag[0]
        )
        await seq.motors.gripper.set_target(ControlMode.POSITION, 1.0)
        manager.sent_by_motor.clear()

        flag[0] = True
        for refresher in refreshers:
            await refresher.step()

        assert manager.sent_by_motor == []

    async def test_resends_last_target(self) -> None:
        manager, motors, seq = self._wire()
        refreshers = _build_target_refreshers(
            seq.motors, motors, manager, is_estop_active=lambda: False
        )
        await seq.motors.gripper.set_target(ControlMode.POSITION, 1.0)
        manager.sent_by_motor.clear()

        for refresher in refreshers:
            await refresher.step()

        assert [name for name, _ in manager.sent_by_motor] == ["gripper"]


class TestServerEStopProperty:
    async def test_e_stop_active_property_reflects_state(self) -> None:
        """main.py から private 属性を触らずに緊急停止状態を読めること。

        状態を作るのも公開経路 (activate_e_stop) から行う。private へ直接
        代入すると、「停止したのにプロパティが追随しない」配線ミスを
        テストの側が肩代わりして隠してしまう。
        """
        server = RobotServer()

        assert server.e_stop_active is False

        await server.activate_e_stop(reason="配線確認")
        assert server.e_stop_active is True

    def test_e_stop_active_is_read_only(self) -> None:
        server = RobotServer()

        with pytest.raises(AttributeError):
            server.e_stop_active = True  # type: ignore[misc]


class TestCreateMotorControlType:
    """generic ドライバの control_type が config から反映されること。

    duty 指令の DC モータが POSITION で生成されると、動作確認は位置到達を待って
    必ず失敗し、reset も位置指令になる。config と実挙動が食い違う状態を防ぐ。
    """

    def _generic(self, **extra: object) -> GenericDriver:
        cfg: dict = {"driver": "generic", "bus": "generic_bus", "can_id": 1}
        cfg.update(extra)
        motor = _create_motor(_motor("gripper", cfg))
        assert isinstance(motor, GenericDriver)
        return motor

    def test_duty_control_type_is_applied(self) -> None:
        assert self._generic(control_type="duty").control_type is ControlMode.DUTY

    def test_velocity_control_type_is_applied(self) -> None:
        assert self._generic(control_type="velocity").control_type is ControlMode.VELOCITY

    def test_position_control_type_is_applied(self) -> None:
        assert self._generic(control_type="position").control_type is ControlMode.POSITION

    def test_control_type_is_case_insensitive(self) -> None:
        assert self._generic(control_type="DUTY").control_type is ControlMode.DUTY

    def test_missing_control_type_defaults_to_position(self) -> None:
        assert self._generic().control_type is ControlMode.POSITION

    def test_unknown_control_type_aborts_startup(self) -> None:
        """誤記を position へ落として起動を続けない。

        duty 0.3 のつもりの指令が position 0.3deg としてファームへ届き、ファームは
        それを正当なフレームとして受理する。警告ログでは事故を止められない。
        """
        with pytest.raises(ValueError, match="control_type"):
            self._generic(control_type="torque")

    def test_current_control_type_aborts_startup(self) -> None:
        """GenericDriver は CURRENT を送れない。"""
        with pytest.raises(ValueError, match="control_type"):
            self._generic(control_type="current")

    def test_control_type_on_non_generic_driver_aborts_startup(self) -> None:
        """書いても効かないキーを黙って受け取らない。"""
        with pytest.raises(ValueError, match="control_type"):
            _motor(
                "y_axis_r",
                {"driver": "m3508", "bus": "m3508_bus", "can_id": 1, "control_type": "duty"},
            )


def _paired_table() -> PositionTable:
    return load_position_table(
        {
            "axes": {
                "y_axis": {
                    "unit": "mm",
                    "command_unit": "deg",
                    "tolerance": 1.0,
                    "sync_tolerance": 2.0,
                    "motors": {
                        "y_axis_r": {"scale": 55.02, "offset": 1.0},
                        "y_axis_l": {"scale": -55.02, "offset": -1.0},
                    },
                },
                "gripper": {"unit": "deg", "command_unit": "deg", "scale": 1.0},
            }
        },
        source="<test>",
    )


def _paired_motors() -> dict[str, object]:
    return {
        "y_axis_r": M3508Driver("y_axis_r", can_id=1),
        "y_axis_l": M3508Driver("y_axis_l", can_id=2),
        "gripper": GenericDriver("gripper", can_id=1),
    }


class TestBuildSyncGroups:
    def test_paired_axis_becomes_group(self) -> None:
        groups = _build_sync_groups(_paired_table(), _paired_motors())

        assert [g.name for g in groups] == ["y_axis"]
        group = groups[0]
        assert group.tolerance == 2.0
        # 逆回転ペアは scale の符号で表す。符号が落ちると偏差が常に過大に見えて誤発報する
        assert [(m.name, m.scale, m.offset) for m in group.members] == [
            ("y_axis_r", 55.02, 1.0),
            ("y_axis_l", -55.02, -1.0),
        ]

    def test_single_motor_axis_is_not_included(self) -> None:
        groups = _build_sync_groups(_paired_table(), _paired_motors())

        assert all(g.name != "gripper" for g in groups)

    def test_axis_with_missing_motor_is_skipped_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """config の書き間違いで監視が黙って無効になるのを防ぐ。"""
        motors = {"y_axis_r": M3508Driver("y_axis_r", can_id=1)}

        with caplog.at_level(logging.WARNING):
            groups = _build_sync_groups(_paired_table(), motors)

        assert groups == []
        assert any("y_axis_l" in record.getMessage() for record in caplog.records)


class TestAttachSyncGroups:
    def _loops(self) -> dict:
        config = {
            "robot_name": "main_hand",
            "motors": {
                "y_axis_r": {"driver": "m3508", "bus": "m3508_bus", "can_id": 1},
                "y_axis_l": {"driver": "m3508", "bus": "m3508_bus", "can_id": 2},
                "other": {"driver": "m3508", "bus": "other_bus", "can_id": 1},
            },
        }
        motors = {
            "y_axis_r": M3508Driver("y_axis_r", can_id=1),
            "y_axis_l": M3508Driver("y_axis_l", can_id=2),
            "other": M3508Driver("other", can_id=1),
        }
        return _build_position_loops(
            _robot(config),
            _StubCANManager(),
            motors,
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
        )

    def test_group_on_single_loop_is_registered(self) -> None:
        loops = self._loops()
        group = SyncGroup(
            "y_axis",
            (MotorSpec("y_axis_r", 55.02, 0.0), MotorSpec("y_axis_l", -55.02, 0.0)),
            tolerance=2.0,
        )

        _attach_sync_groups([group], list(loops.values()))

        assert loops["m3508_bus"].sync_group_names == ("y_axis",)
        assert loops["other_bus"].sync_group_names == ()

    def test_group_outside_position_loops_is_skipped(self) -> None:
        """EDULITE のペアは PC 側常駐ループを持たないので SyncMonitor だけで見る。"""
        loops = self._loops()
        group = SyncGroup(
            "rotate",
            (MotorSpec("rotate_r", 1.0, 0.0), MotorSpec("rotate_l", -1.0, 0.0)),
            tolerance=3.0,
        )

        _attach_sync_groups([group], list(loops.values()))

        assert all(loop.sync_group_names == () for loop in loops.values())

    def test_group_split_across_loops_is_skipped(self) -> None:
        """別バスに分かれたペアは 1 フレームで同時指令できないため登録しない。"""
        loops = self._loops()
        group = SyncGroup(
            "mixed",
            (MotorSpec("y_axis_r", 1.0, 0.0), MotorSpec("other", -1.0, 0.0)),
            tolerance=1.0,
        )

        _attach_sync_groups([group], list(loops.values()))

        assert all(loop.sync_group_names == () for loop in loops.values())


#: y_axis の実測換算 [deg/mm]
_Y_SCALE = 55.0131


def _motion_table() -> PositionTable:
    """``motion`` を書いた逆回転ペアと、位置制御ループに載らないペアを持つ表。"""
    return load_position_table(
        {
            "axes": {
                "y_axis": {
                    "unit": "mm",
                    "command_unit": "deg",
                    "timeout_s": 4.0,
                    "tolerance": 1.0,
                    "sync_tolerance": 2.0,
                    "motion": {
                        "max_velocity": 10.0,
                        "max_acceleration": 50.0,
                        "velocity_ff": 0.5,
                    },
                    "motors": {
                        "y_axis_r": {"scale": _Y_SCALE, "offset": 0.0},
                        "y_axis_l": {"scale": -_Y_SCALE, "offset": 0.0},
                    },
                },
                # ドライバが位置ループを内蔵する軸 (EDULITE)。位置制御ループには載らない
                "rotate": {
                    "unit": "deg",
                    "command_unit": "rad",
                    "tolerance": 2.0,
                    "motion": {"max_velocity": 30.0, "max_acceleration": 100.0},
                    "motors": {
                        "rotate_r": {"scale": 0.017, "offset": 0.0},
                        "rotate_l": {"scale": -0.017, "offset": 0.0},
                    },
                },
                # motion を書かない軸。従来どおり最終目標をステップで入れる
                "gripper": {"unit": "deg", "command_unit": "deg", "scale": 1.0},
            }
        },
        source="<test>",
    )


class TestAttachMotionProfiles:
    """位置定数の ``motion`` を位置制御ループへ結ぶ配線。

    単位換算 (人間の単位 → 指令単位) を知るのはこの層だけで、位置制御ループは
    指令単位しか扱わない。``add_motor`` の引数ではなく後付けにしてあるのは、
    ``PositionTable`` を持たない呼び出し元 (``scripts/tune_y_axis.py`` 等) を
    壊さないため —— 設定しなければ今までどおり動く。
    """

    def _rig(self) -> tuple[_StubCANManager, dict[str, M3508Driver], dict]:
        config = {
            "robot_name": "main_hand",
            "motors": {
                # 実機の y_axis と同じゲイン。偏差 1.14mm で P 項が上限に届く
                "y_axis_r": {
                    "driver": "m3508",
                    "bus": "m3508_bus",
                    "can_id": 1,
                    "pid": {"kp": 32.0, "output_limit": 2000},
                },
                "y_axis_l": {
                    "driver": "m3508",
                    "bus": "m3508_bus",
                    "can_id": 2,
                    "pid": {"kp": 32.0, "output_limit": 2000},
                },
                "gripper_motor": {"driver": "m3508", "bus": "other_bus", "can_id": 1},
            },
        }
        manager = _StubCANManager()
        motors = {
            "y_axis_r": M3508Driver("y_axis_r", can_id=1),
            "y_axis_l": M3508Driver("y_axis_l", can_id=2),
            "gripper_motor": M3508Driver("gripper_motor", can_id=1),
        }
        loops = _build_position_loops(
            _robot(config),
            manager,
            motors,
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
        )
        now = time.time()
        for name, driver in motors.items():
            feed_m3508(driver, angle_raw=0)
            manager.feedback_at[name] = now
        return manager, motors, loops

    async def test_逆回転ペアの両側に制限が載る(self) -> None:
        """換算は ``abs(scale)`` で行うこと。

        速度・加速度の制限は向きを持たない量なので、符号付きの ``scale`` を掛けると
        逆回転側だけ負値になる。プロファイルは正の上限しか受け取らないため、
        符号を落とすと**起動そのものが落ちる**か、制限として機能しない側が残る。
        """
        manager, _, loops = self._rig()

        _attach_motion_profiles(_motion_table(), list(loops.values()))

        loop = loops["m3508_bus"]
        await loop.set_target("y_axis_r", ControlMode.POSITION, 15.0 * _Y_SCALE)
        await loop.set_target("y_axis_l", ControlMode.POSITION, -15.0 * _Y_SCALE)
        await loop.step()

        # 15mm (825deg) をステップで入れれば kp=32 で必ず飽和する。中間目標が
        # 実測から起きていれば、1 周期目の偏差はほとんど無い
        assert not loop.is_saturated("y_axis_r")
        assert not loop.is_saturated("y_axis_l")
        assert max(abs(current) for current in manager.last_currents) < 100

    async def test_motion_を書かない軸は従来どおりステップ入力(self) -> None:
        _, _, loops = self._rig()

        _attach_motion_profiles(_motion_table(), list(loops.values()))

        loop = loops["other_bus"]
        await loop.set_target("gripper_motor", ControlMode.POSITION, 5000.0)
        await loop.step()

        assert loop.is_saturated("gripper_motor")

    def test_位置制御ループ外の軸はログに残して続行する(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """黙って飛ばすと「制限が効いているつもり」で config を読むことになる。"""
        _, _, loops = self._rig()

        with caplog.at_level(logging.INFO):
            _attach_motion_profiles(_motion_table(), list(loops.values()))

        assert any("rotate_r" in record.getMessage() for record in caplog.records)

    def test_起動ログに3つのつまみが全部出る(self, caplog: pytest.LogCaptureFixture) -> None:
        """``velocity_ff`` は**実行中に変更できず UI にも配信されない**。

        ``pid_gains()`` に相当する読み口が無いので、起動ログが「今どの値で動いて
        いるか」を知る唯一の経路になる。しかも巡航中の出力を最も大きく左右する値
        (``kd`` と釣り合っていないと D 項が出力を食い潰す) なので、落とすと
        「速くならない」原因が画面からもログからも読めなくなる。
        """
        _, _, loops = self._rig()

        with caplog.at_level(logging.INFO):
            _attach_motion_profiles(_motion_table(), list(loops.values()))

        messages = [
            record.getMessage()
            for record in caplog.records
            if "y_axis_r" in record.getMessage() and "台形プロファイル" in record.getMessage()
        ]
        assert messages
        assert all("10.0" in message for message in messages)  # max_velocity
        assert all("50.0" in message for message in messages)  # max_acceleration
        assert all("velocity_ff=0.5" in message for message in messages)

    async def test_velocity_ff_が位置制御ループまで届く(self) -> None:
        """巡航中の出力は ``velocity_ff * 参照速度``。

        速度 FF は config の ``motion.velocity_ff`` にしか無い値なので、配線で
        落とすと「書いたのに効かない設定」になる (症状はどこにも出ない)。
        kp=ki=kd=0 の軸で見れば、出力に残るのは FF だけになる。
        """
        mono = FakeClock()
        wall = FakeClock(start=5000.0)
        manager = _StubCANManager()
        loop = M3508PositionLoop(
            manager,
            "m3508_bus",
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
            time_source=mono,
            feedback_clock=wall,
        )
        driver = M3508Driver("y_axis_r", can_id=1)
        pid_config = _motor(
            "y_axis_r",
            {"driver": "m3508", "bus": "m3508_bus", "can_id": 1, "pid": {"kp": 0.0}},
        )
        loop.add_motor("y_axis_r", driver, _build_position_pid(pid_config))
        _attach_motion_profiles(_motion_table(), [loop])

        await loop.set_target("y_axis_r", ControlMode.POSITION, 15.0 * _Y_SCALE)
        for _ in range(100):
            mono.advance(0.005)
            wall.advance(0.005)
            feed_m3508(driver, angle_raw=0)
            manager.feedback_at["y_axis_r"] = wall.now
            await loop.step()

        # 0.5s で巡航速度 10mm/s (= 550.131deg/s) に達している
        assert manager.last_currents[0] == pytest.approx(0.5 * 10.0 * _Y_SCALE, abs=2)


class TestAttachHelpersAreCalledFromTheCompositionRoot:
    """後付けの配線は、呼び忘れても位置制御ループがそのまま動いてしまう。

    ``add_motor`` の引数ではないので、``_wire_one_robot`` から 1 行落としても
    全テストが緑のまま通る。症状は「config に書いた同期監視 / 速度制限が丸ごと
    効かない」だけで、起動ログにも UI にも現れない。
    """

    def _called_names(self, function: str) -> set[str]:
        tree = ast.parse(pathlib.Path(main.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == function:
                return {
                    call.func.id
                    for call in ast.walk(node)
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                }
        raise AssertionError(f"main.py に {function} の定義が無い")

    @pytest.mark.parametrize("helper", ["_attach_sync_groups", "_attach_motion_profiles"])
    def test_wire_one_robot_calls_the_helper(self, helper: str) -> None:
        assert helper in self._called_names("_wire_one_robot")


class TestShippedMainHandConfig:
    """同梱 config と配線の結合を守る回帰テスト。"""

    def _load(self) -> tuple[RobotConfig, PositionTable, dict]:
        config = load_robot_config(
            yaml.safe_load((_CONFIG_DIR / "main_hand.yaml").read_text()),
            source="main_hand.yaml",
        )
        positions = load_position_table(
            yaml.safe_load((_CONFIG_DIR / "main_hand_positions.yaml").read_text()),
            source="main_hand_positions.yaml",
        )
        motors = {name: _create_motor(motor) for name, motor in config.motors.items()}
        return config, positions, motors

    def test_two_sync_groups_are_built(self) -> None:
        _, positions, motors = self._load()

        groups = _build_sync_groups(positions, motors)

        assert {g.name for g in groups} == {"y_axis", "rotate"}

    def test_y_axis_group_lands_on_the_m3508_loop(self) -> None:
        config, positions, motors = self._load()
        loops = _build_position_loops(
            config,
            _StubCANManager(),
            motors,
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
        )

        _attach_sync_groups(_build_sync_groups(positions, motors), list(loops.values()))

        assert loops["m3508_bus"].sync_group_names == ("y_axis",)

    def test_conveyor_is_created_as_duty_motor(self) -> None:
        _, _, motors = self._load()

        assert motors["conveyor"].control_type is ControlMode.DUTY
        assert motors["gripper"].control_type is ControlMode.POSITION


class TestSystemConfigReachesTheServer:
    """config/system.yaml の各セクションが RobotServer まで届いていること。

    ここが空いていると、`main()` の RobotServer(...) から引数を 1 本落としても
    全テストが緑のままになる (実際に health / motor_check の配線は誰も見ていなかった)。
    症状は「yaml に書いた値どおりに動かない」だけで、ログにも UI にも現れない。

    値ではなく**式の書かれ方**を見る。値の一致だけを見るとリテラルで書き直しても
    通ってしまい、単一情報源から外れたことを検出できない。
    """

    #: RobotServer へ渡さないフィールドと、その渡し先。
    #: 新しいセクションを足した人はここへ書くか、サーバーへ配線するかを選ぶことになる。
    NOT_FOR_SERVER: ClassVar[dict[str, str]] = {
        "can_buses": "CANManager の生成に使う (サーバーはバスを直接触らない)",
        "source": "エラーメッセージへ出すファイル名",
    }

    def _server_call_keywords(self) -> str:
        """main() の中の RobotServer(...) 呼び出しの、キーワード引数の式をすべて連結する。"""
        tree = ast.parse(pathlib.Path(main.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "RobotServer"
            ):
                return " ".join(ast.unparse(kw.value) for kw in node.keywords)
        raise AssertionError("main.py に RobotServer(...) の呼び出しが無い")

    def test_every_section_is_wired(self) -> None:
        wired = self._server_call_keywords()
        # 局所変数へ受けてから渡している経路も追えるよう、代入元まで含めて見る
        source = pathlib.Path(main.__file__).read_text(encoding="utf-8")

        for field in dataclasses.fields(SystemConfig):
            if field.name in self.NOT_FOR_SERVER:
                continue
            reference = f"system.{field.name}"
            assert reference in wired or f"= {reference}" in source, (
                f"config/system.yaml の {field.name} が RobotServer へ配線されていません。"
                f" 渡さないなら {self.__class__.__name__}.NOT_FOR_SERVER に理由を書いてください。"
            )

    def test_exemptions_are_real_fields(self) -> None:
        """存在しないフィールド名で免除を書くと、本物の配線漏れを隠せてしまう。"""
        names = {f.name for f in dataclasses.fields(SystemConfig)}
        assert set(self.NOT_FOR_SERVER) <= names


class TestRobotContextReachesTheServer:
    """ロボット 1 台に紐づく部品が ``main()`` からサーバーまで届いていること。

    ``RobotContext`` にフィールドを足し、``add_robot`` に引数を足したのに
    ``main()`` から渡し忘れる、という抜け方をする。症状はその機能が丸ごと
    無反応になるだけで、起動ログにも UI にも現れない (実際に手動操縦を足したとき、
    ``manual=`` を落としても全テストが緑のままだった)。

    連鎖を 2 本に分けて見る:
      RobotContext のフィールド → add_robot の引数 → main() の呼び出し
    """

    #: add_robot の引数にしないフィールドと、その理由。
    NOT_A_PARAMETER: ClassVar[dict[str, str]] = {
        "mode": "サーバーが持つ実行時状態 (起動時は必ず SEQUENCE から始まる)",
    }

    def _add_robot_call_arguments(self) -> set[str]:
        """main() 内の server.add_robot(...) が渡している引数名。

        位置引数は add_robot のシグネチャ順で名前へ解決する。名前でしか見ないと、
        位置で渡している sequence / can_manager を配線漏れと誤判定する。
        """
        signature = inspect.signature(RobotServer.add_robot)
        positional = [name for name in signature.parameters if name != "self"]

        tree = ast.parse(pathlib.Path(main.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_robot"
            ):
                supplied = {kw.arg for kw in node.keywords if kw.arg is not None}
                supplied |= set(positional[: len(node.args)])
                return supplied
        raise AssertionError("main.py に server.add_robot(...) の呼び出しが無い")

    def test_every_context_field_is_a_parameter(self) -> None:
        parameters = set(inspect.signature(RobotServer.add_robot).parameters)
        for field in dataclasses.fields(RobotContext):
            if field.name in self.NOT_A_PARAMETER:
                continue
            assert field.name in parameters, (
                f"RobotContext.{field.name} を add_robot から渡せません。"
                f" 渡さないなら {self.__class__.__name__}.NOT_A_PARAMETER に理由を書いてください。"
            )

    def test_main_supplies_every_parameter(self) -> None:
        supplied = self._add_robot_call_arguments()
        expected = {
            name for name in inspect.signature(RobotServer.add_robot).parameters if name != "self"
        }
        missing = expected - supplied
        assert not missing, (
            f"main() の add_robot(...) が {sorted(missing)} を渡していません"
            " (その機能が丸ごと無反応になり、ログにも UI にも現れません)"
        )

    def test_exemptions_are_real_fields(self) -> None:
        names = {f.name for f in dataclasses.fields(RobotContext)}
        assert set(self.NOT_A_PARAMETER) <= names


class TestLoadAllConfigs:
    """設定の誤記で起動を止める経路。会場で読むのは操縦者なので traceback は出さない。"""

    def _write(self, tmp_path: pathlib.Path, name: str, body: str) -> pathlib.Path:
        path = tmp_path / name
        path.write_text(body)
        return path

    def _system(self, tmp_path: pathlib.Path) -> pathlib.Path:
        return self._write(tmp_path, "system.yaml", "can_buses:\n  generic_bus: can_generic\n")

    def test_shipped_configs_load(self) -> None:
        system, loaded = _load_all_configs(
            _CONFIG_DIR / "system.yaml",
            [_CONFIG_DIR / "main_hand.yaml", _CONFIG_DIR / "sub_hand.yaml"],
        )

        assert set(system.can_buses) == {
            "m3508_bus",
            "edulite_bus",
            "generic_bus",
            "dm3520_bus",
        }
        assert [robot.robot_name for _, robot in loaded] == ["main_hand", "sub_hand"]

    def test_missing_system_config_aborts(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(SystemExit, match=r"system\.yaml"):
            _load_all_configs(tmp_path / "system.yaml", [])

    def test_invalid_robot_config_aborts(self, tmp_path: pathlib.Path) -> None:
        system_path = self._system(tmp_path)
        robot_path = self._write(
            tmp_path,
            "r.yaml",
            "robot_name: r\nmotors:\n  conveyor:\n"
            "    driver: generic\n    bus: generic_bus\n    can_id: 1\n"
            "    control_type: duy\n",
        )

        with pytest.raises(SystemExit) as exc:
            _load_all_configs(system_path, [robot_path])

        message = str(exc.value)
        assert "conveyor" in message
        assert "control_type" in message
        assert "duy" in message

    def test_missing_robot_config_is_skipped(
        self, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """1 台ぶんの yaml が無いだけなら、もう 1 台の点検はできるようにする。"""
        system_path = self._system(tmp_path)

        with caplog.at_level(logging.WARNING):
            _, loaded = _load_all_configs(system_path, [tmp_path / "absent.yaml"])

        assert loaded == []
        assert any("absent.yaml" in record.getMessage() for record in caplog.records)


class TestSequenceClassSelection:
    """sequences/<name>.py から登録するシーケンスの決め方。

    かつては ``dir()`` の並び (アルファベット順) の先頭を採っていたため、
    モジュールが他機体のシーケンスを import しただけで乗っ取られえた
    (``"MotorCheckSequence" < "SubHandSequence"``)。症状は「sub_hand の
    sequence_start でなぜか両ハンドが動く」だけで、config からもログからも
    理由が読めない。
    """

    def _module(self, source: str) -> types.ModuleType:
        module = types.ModuleType("fakerobots.r1")
        exec(compile(source, "<fakerobots.r1>", "exec"), module.__dict__)
        return module

    _PREAMBLE = "from lib.sequence.engine import Sequence\n"

    def test_単一のサブクラスをそのまま返す(self) -> None:
        module = self._module(self._PREAMBLE + "class OnlyOne(Sequence):\n    pass\n")

        assert main._sequence_class_defined_in(module).__name__ == "OnlyOne"

    def test_複数定義されていたら起動を拒否する(self) -> None:
        """どちらを登録すべきかは構成からしか決まらない。黙って片方を選ばない。"""
        module = self._module(
            self._PREAMBLE
            + "class AaaFirst(Sequence):\n    pass\n"
            + "class ZzzSecond(Sequence):\n    pass\n"
        )

        with pytest.raises(SystemExit) as exc:
            main._sequence_class_defined_in(module)

        message = str(exc.value)
        assert "AaaFirst" in message
        assert "ZzzSecond" in message

    def test_import_しただけのシーケンスは候補にならない(self) -> None:
        """名前順で先頭に来る import 済みクラスに乗っ取られないこと。"""
        module = self._module(
            self._PREAMBLE
            + "from sequences.motor_check import MotorCheckSequence\n"
            + "class ZzzOwn(Sequence):\n    pass\n"
        )

        assert main._sequence_class_defined_in(module).__name__ == "ZzzOwn"

    def test_サブクラスが無ければ_None(self) -> None:
        assert main._sequence_class_defined_in(self._module("x = 1\n")) is None


class TestRobotBusSelection:
    """**そのロボットが使うバスだけを開く。**

    全バスを開くと、メインハンドは DM3520 を 1 台も持たないのに `can_dm3520` を
    開くことになり、CANable が 1 本欠けているだけで**両ハンドとも起動できなくなる**
    (片方だけの運用も動作確認も UI の起動もできない)。受信ループが物理バス 1 本に
    つき 2 本立つのも同じ原因。
    """

    _BUSES: ClassVar[dict[str, str]] = {
        "m3508_bus": "can_m3508",
        "edulite_bus": "can_edulite",
        "generic_bus": "can_generic",
        "dm3520_bus": "can_dm3520",
    }

    def test_出荷_config_で使わないバスを開かない(self) -> None:
        _system, loaded = _load_all_configs(
            _CONFIG_DIR / "system.yaml",
            [_CONFIG_DIR / "main_hand.yaml", _CONFIG_DIR / "sub_hand.yaml"],
        )
        robots = {robot.robot_name: robot for _, robot in loaded}

        main_buses = main._robot_bus_names(robots["main_hand"], self._BUSES)
        sub_buses = main._robot_bus_names(robots["sub_hand"], self._BUSES)

        # メインハンドは DM3520 を 1 台も持たない / サブハンドは M3508 を持たない
        assert "dm3520_bus" not in main_buses
        assert "m3508_bus" not in sub_buses

    def test_モータとセンサが載るバスだけを列挙する(self) -> None:
        robot = _robot(
            {
                "robot_name": "r",
                "motors": {"conveyor": {"driver": "generic", "bus": "generic_bus", "can_id": 1}},
                "sensors": {"origin": {"bus": "edulite_bus", "can_id": 2}},
            }
        )

        # 宣言順を保つ (バス番号の入れ替わりを config の並びで固定するため)
        assert main._robot_bus_names(robot, self._BUSES) == ["edulite_bus", "generic_bus"]

    def test_setup_robot_は使うバスだけを開く(self) -> None:
        robot = _robot(
            {
                "robot_name": "r",
                "motors": {"conveyor": {"driver": "generic", "bus": "generic_bus", "can_id": 1}},
            }
        )

        can_manager, _motors = main._setup_robot(robot, self._BUSES, dry_run=True)

        assert can_manager.bus_names == ("generic_bus",)

    def test_バスを開けなければ一行のメッセージで落とす(self) -> None:
        """down しているインタフェースで生の traceback を出さない。

        この呼び出しは `main()` の try の外にあるので、素通しすると後始末も
        1 段も走らない。会場で読むのは操縦者なので直し方まで書いて止める。
        """
        with (
            patch("main.can.Bus", side_effect=OSError(19, "No such device")),
            pytest.raises(SystemExit) as exc,
        ):
            main._create_bus("can_dm3520", dry_run=False)

        message = str(exc.value)
        assert "can_dm3520" in message
        assert "setup_can.sh" in message


class TestCreateBusOperstate:
    """**down しているインタフェースでも起動は止めない。ログにだけ出す。**

    `--strict` を通していない構成 (片ハンドだけの練習・机上ベンチ・会場での逃げ道) を
    一律に潰さないため、拒否は足さない。価値は「人が最初に見る場所 (起動ログ) に、
    原因をインタフェース名付きで残す」ことだけ。`docs/impl_plan.md` の
    「既知の制約: バス down 時の失敗が分かりにくい」参照。
    """

    def test_down_なら起動ログにERRORでインタフェース名を残す(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            patch("main.can.Bus", return_value=MagicMock()),
            caplog.at_level(logging.ERROR),
        ):
            main._create_bus("can_m3508", dry_run=False, read_operstate=lambda _channel: "down")

        assert any(
            "can_m3508" in record.getMessage() and record.levelno == logging.ERROR
            for record in caplog.records
        )

    def test_down_でも起動は止めない(self) -> None:
        with patch("main.can.Bus", return_value=MagicMock()) as bus_ctor:
            bus = main._create_bus(
                "can_m3508", dry_run=False, read_operstate=lambda _channel: "down"
            )

        assert bus is bus_ctor.return_value

    def test_up_ならログを出さない(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch("main.can.Bus", return_value=MagicMock()),
            caplog.at_level(logging.ERROR),
        ):
            main._create_bus("can_m3508", dry_run=False, read_operstate=lambda _channel: "up")

        assert caplog.records == []

    def test_unknown_は_up_なのでログを出さない(self, caplog: pytest.LogCaptureFixture) -> None:
        """**SocketCAN は up でも `unknown` を返す** (carrier を管理しない)。

        判定を `!= "up"` へ書き換えると健全なバスが毎回起動ログを汚す。
        """
        with (
            patch("main.can.Bus", return_value=MagicMock()),
            caplog.at_level(logging.ERROR),
        ):
            main._create_bus("can_m3508", dry_run=False, read_operstate=lambda _channel: "unknown")

        assert caplog.records == []

    def test_判定できなければログを出さない(self, caplog: pytest.LogCaptureFixture) -> None:
        """`None` (存在しないインタフェース。virtual バスやテスト環境) は「分からない」

        であって異常ではない。判定できなかったこと自体もログに出さない —
        平常時 (virtual バス) に毎回警告が出るとログが埋もれる。
        """
        with (
            patch("main.can.Bus", return_value=MagicMock()),
            caplog.at_level(logging.DEBUG),
        ):
            main._create_bus("can_m3508", dry_run=False, read_operstate=lambda _channel: None)

        assert caplog.records == []

    def test_dry_run_では判定しない(self) -> None:
        """`--dry-run` の virtual バスは `/sys/class/net/` に実体が無い。呼び出しごと省く。

        呼ばれたら `AssertionError` で分かるようにする (呼ばなければ落ちない)。
        """

        def _fail(_channel: str) -> str | None:
            raise AssertionError("dry_run では read_operstate を呼んではならない")

        bus = main._create_bus("can_m3508", dry_run=True, read_operstate=_fail)
        bus.shutdown()

    def test_既定は実際のoperstate読み取り関数(self) -> None:
        assert main._create_bus.__kwdefaults__["read_operstate"] is main._read_operstate

    def test_実体が無ければ判定できないとしてNoneを返す(self) -> None:
        # 実在しないチャンネル名を渡す。/sys/class/net/<channel>/operstate が無いので
        # 「判定できない」= None を返す (異常へ倒さない)
        assert main._read_operstate("does-not-exist-channel-xyz") is None


class TestEnsurePortAvailable:
    """**bind の可否は CAN を開くより前に見る。**

    立ち上げ順は「CAN → 制御ループ → 目標値再送 → サーバー bind」なので、
    ポートが埋まっていると**機体を励磁して 200Hz の制御ループを回し始めた後**に
    落ちる。「起動したか分からず二度叩く」は会場で普通に起きる操作。
    """

    def test_空いていれば通る(self) -> None:
        main._ensure_port_available("127.0.0.1", 0)

    def test_使用中なら起動を拒否する(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]

            with pytest.raises(SystemExit) as exc:
                main._ensure_port_available("127.0.0.1", port)

        assert "使用中" in str(exc.value)

    def test_CAN_を開く前に呼ばれる(self) -> None:
        """`main()` の並び順そのものを固定する。後ろへ移すと意味が無くなる。"""
        source = inspect.getsource(main.main)
        assert source.index("_ensure_port_available") < source.index("_wire_one_robot")


class TestStartAll:
    """起動の順序と、起動時に励磁できなかったモータの受け渡し。"""

    def _wiring(self, name: str, inactive: list[str]) -> main._RobotWiring:
        can_manager = MagicMock()
        can_manager.run = AsyncMock(return_value=inactive)
        return main._RobotWiring(
            name=name,
            sequence=_DummySequence(name),
            can_manager=can_manager,
            positions=PositionTable.empty(),
            position_loops=[],
            sync_monitors=[],
            target_refreshers=[],
            motor_group=None,
        )

    async def test_起動時に励磁できなかったモータをサーバーへ渡す(self) -> None:
        """**捨ててはならない。** 捨てると `safety.unenergized_motors` は緊急停止
        解除の経路でしか埋まらず、起動時の励磁失敗は画面のどこにも出ない。
        """
        server = MagicMock()
        server.start = AsyncMock()

        await main._start_all(server, [self._wiring("main_hand", ["rotate_r"])])

        server.set_initial_inactive_motors.assert_called_once_with("main_hand", ["rotate_r"])

    async def test_ロボットごとに渡す(self) -> None:
        server = MagicMock()
        server.start = AsyncMock()

        await main._start_all(
            server,
            [self._wiring("main_hand", []), self._wiring("sub_hand", ["sub_lift"])],
        )

        assert [call.args for call in server.set_initial_inactive_motors.call_args_list] == [
            ("main_hand", []),
            ("sub_hand", ["sub_lift"]),
        ]


class TestOriginResolver:
    """零点確定の実行手段を、**探索を始める前に**問える形で解決する。

    センサまで押し込んでから「確定できません」で降りると、機構を動かした意味が
    無いまま姿勢だけが変わる。
    """

    def _table(self) -> PositionTable:
        return load_position_table(
            {
                "axes": {
                    "y_axis": {
                        "unit": "mm",
                        "command_unit": "deg",
                        "sync_tolerance": 2.0,
                        "motors": {"y_axis_r": {"scale": 2.0}, "y_axis_l": {"scale": -2.0}},
                    },
                    "rotate": {
                        "unit": "deg",
                        "command_unit": "rad",
                        "sync_tolerance": 3.0,
                        "motors": {"rotate_r": {"scale": 1.0}, "rotate_l": {"scale": -1.0}},
                    },
                },
                "positions": {"y_axis": {"home": 0.0}, "rotate": {"home": 0.0}},
            },
            source="<test>",
        )

    def _loop(self, motors: dict[str, M3508Driver] | None = None) -> M3508PositionLoop:
        config = {
            "robot_name": "main_hand",
            "motors": {
                "y_axis_r": {"driver": "m3508", "bus": "m3508_bus", "can_id": 1},
                "y_axis_l": {"driver": "m3508", "bus": "m3508_bus", "can_id": 2},
            },
        }
        if motors is None:
            motors = {
                "y_axis_r": M3508Driver("y_axis_r", can_id=1),
                "y_axis_l": M3508Driver("y_axis_l", can_id=2),
            }
        loops = _build_position_loops(
            _robot(config),
            _StubCANManager(),
            motors,
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
        )
        loop = loops["m3508_bus"]
        _attach_sync_groups([self._table().axis("y_axis").sync_group], [loop])
        return loop

    async def test_ペア軸はグループ単位で確定する(self) -> None:
        """左右を別々の時刻に確定すると、その間に動いたぶんのオフセットが残る。

        **解決器が何を返したかではなく、実際に両方の原点が動いたかを見る。**
        片方だけへ効く実装 (`set_origin_here` を 1 台へ) でも「解決器が
        callable を返した」ことは変わらないので、そこを見るだけでは噛まない。
        """
        motors = {
            "y_axis_r": M3508Driver("y_axis_r", can_id=1),
            "y_axis_l": M3508Driver("y_axis_l", can_id=2),
        }
        loop = self._loop(motors)
        # 実機と同じフレームで動かす (直接代入はデコード層を丸ごと迂回する)。
        # 1 通目は累積角の起点になるだけなので、動かすには 2 通要る
        for driver, deg in ((motors["y_axis_r"], 30.0), (motors["y_axis_l"], -30.0)):
            feed_m3508(driver, deg=0.0)
            feed_m3508(driver, deg=deg)
        assert motors["y_axis_r"].multi_turn_position != 0.0
        assert motors["y_axis_l"].multi_turn_position != 0.0

        capture = main._make_origin_resolver([loop], self._table())("y_axis")

        assert capture is not None
        await capture()

        assert motors["y_axis_r"].multi_turn_position == pytest.approx(0.0)
        assert motors["y_axis_l"].multi_turn_position == pytest.approx(0.0)

    def test_位置制御ループにも_set_zero_にも載らない軸は手段が無い(self) -> None:
        """自作モタドラのサーボのように原点を切り直す手段が無いドライバでは、
        **「確定したつもり」で先へ進ませない。**
        """
        mgr = CANManager(run_blocking=direct_runner())
        mgr.add_bus("can_generic", mock_bus())
        for name, can_id in (("rotate_r", 0x41), ("rotate_l", 0x42)):
            mgr.add_motor("can_generic", GenericDriver(name, can_id=can_id))

        resolve = main._make_origin_resolver([self._loop()], self._table(), can_managers=[mgr])

        assert resolve("rotate") is None


class TestOriginResolverViaSetZero:
    """原点をドライバ内部に持つモータ (EDULITE 05) は `SET_ZERO` で切り直す。

    `M3508PositionLoop` に載らないので PC 側に累積角が無く、CAN フレームを
    送る以外に「今の位置を 0 と定義し直す」手段が無い。
    """

    def _table(self) -> PositionTable:
        return load_position_table(
            {
                "axes": {
                    "rotate": {
                        "unit": "deg",
                        "command_unit": "rad",
                        "sync_tolerance": 3.0,
                        "motors": {"rotate_r": {"scale": 1.0}, "rotate_l": {"scale": -1.0}},
                    },
                },
                "positions": {"rotate": {"home": 0.0}},
            },
            source="<test>",
        )

    def _manager(self) -> tuple[CANManager, list[tuple[str, can.Message]]]:
        """実 CANManager に EDULITE 2 台を載せ、送信フレームを記録する。"""
        sent: list[tuple[str, can.Message]] = []
        mgr = CANManager(run_blocking=direct_runner())
        mgr.add_bus("can_edulite", mock_bus())
        for name, can_id in (("rotate_r", 0x11), ("rotate_l", 0x12)):
            mgr.add_motor("can_edulite", Edulite05Driver(name, can_id=can_id))

        async def _send(motor_name: str, msg: can.Message) -> None:
            sent.append((motor_name, msg))
            # 問い合わせへの応答としてフィードバックが届く状況を模す
            mark_feedback_at(mgr, motor_name, time.time())

        mgr.send = _send  # type: ignore[method-assign]
        return mgr, sent

    def _monitor(self) -> SyncMonitor:
        group = self._table().axis("rotate").sync_group
        assert group is not None
        return SyncMonitor(
            [group],
            {name: MagicMock() for name in ("rotate_r", "rotate_l")},
            last_feedback_at=lambda _name: None,
        )

    async def test_edulite_のペア軸は_set_zero_で確定できる(self) -> None:
        mgr, sent = self._manager()

        capture = main._make_origin_resolver([], self._table(), can_managers=[mgr])("rotate")

        assert capture is not None
        await capture()

        comm_types = [
            (name, Edulite05Driver.parse_can_id(msg.arbitration_id)[0]) for name, msg in sent
        ]
        # 無励磁 → SET_ZERO → (問い合わせ) → 目標書き込み → enable の順
        assert ("rotate_r", Edulite05Driver.COMM_TYPE_SET_ZERO) in comm_types
        assert ("rotate_l", Edulite05Driver.COMM_TYPE_SET_ZERO) in comm_types
        for name in ("rotate_r", "rotate_l"):
            order = [t for n, t in comm_types if n == name]
            zero = order.index(Edulite05Driver.COMM_TYPE_SET_ZERO)
            # SET_ZERO の前に必ず disable がある (励磁したまま原点を動かすと軸が飛ぶ)
            assert Edulite05Driver.COMM_TYPE_DISABLE in order[:zero]
            # SET_ZERO の後に必ず enable がある (無励磁のまま残さない)
            assert Edulite05Driver.COMM_TYPE_ENABLE in order[zero:]

    async def test_原点付け替え中は同期監視を止める(self) -> None:
        """左右の SET_ZERO のあいだ 2 台の座標系が違うので、偏差という量が
        定義を失う。40ms の debounce に収まる保証は無い。
        """
        mgr, _sent = self._manager()
        monitor = self._monitor()
        suspended_during: list[bool] = []

        original = mgr.capture_origin_via_set_zero

        async def _spy(names):
            suspended_during.append(monitor.is_suspended("rotate"))
            await original(names)

        mgr.capture_origin_via_set_zero = _spy  # type: ignore[method-assign]

        capture = main._make_origin_resolver(
            [], self._table(), can_managers=[mgr], sync_monitors=[monitor]
        )("rotate")

        assert capture is not None
        await capture()

        assert suspended_during == [True]
        # **必ず戻す。** 戻し忘れると以後の試合中ずっと偏差監視が死んだまま残る
        assert monitor.is_suspended("rotate") is False

    async def test_付け替えが失敗しても同期監視を戻す(self) -> None:
        """例外で抜ける経路が `finally` を通らないと、監視が死んだままになる。"""
        mgr, _sent = self._manager()
        monitor = self._monitor()

        async def _boom(_names):
            raise RuntimeError("再励磁できません")

        mgr.capture_origin_via_set_zero = _boom  # type: ignore[method-assign]

        capture = main._make_origin_resolver(
            [], self._table(), can_managers=[mgr], sync_monitors=[monitor]
        )("rotate")

        assert capture is not None
        with pytest.raises(RuntimeError):
            await capture()

        assert monitor.is_suspended("rotate") is False

    async def test_dm3520_は対象外(self) -> None:
        """`SET_ZERO` の安全な順序は disable を要求するが、`sub_lift` は
        disable すると自重で落ちる (保持ブレーキが無い)。
        """
        table = load_position_table(
            {
                "axes": {
                    "sub_lift": {
                        "unit": "mm",
                        "command_unit": "rad",
                        "motors": {"sub_lift_m": {"scale": 1.0}},
                    }
                },
                "positions": {"sub_lift": {"home": 0.0}},
            },
            source="<test>",
        )
        mgr = CANManager(run_blocking=direct_runner())
        mgr.add_bus("can_dm3520", mock_bus())
        mgr.add_motor("can_dm3520", Dm3520Driver("sub_lift_m", can_id=0x01, master_id=0x11))

        resolve = main._make_origin_resolver([], table, can_managers=[mgr])

        assert resolve("sub_lift") is None


class TestMotorCheckWiring:
    """統合動作確認の登録 (`main._wire_motor_check_sequence`)。

    **構成に無い軸のステップを除外しつつ、除外したことを起動ログに出す。**
    機構が未装着のハンドを外して実機を動かす構成 (`config/bench/main_hand`) で、
    残っているハンドの動作確認まで一切できなくなってはならない。一方で除外を
    黙って行うと、本番構成で 1 軸が config から漏れていてもそのステップごと
    消えて全ステップが成功する。
    """

    _CONFIG_DIR: ClassVar[pathlib.Path] = pathlib.Path(__file__).resolve().parent.parent / "config"

    def _table(self, *names: str) -> PositionTable:
        return PositionTable.merged(
            [
                load_position_table(
                    yaml.safe_load((self._CONFIG_DIR / name).read_text()) or {}, source=name
                )
                for name in names
            ]
        )

    def _wire(self, tables: list[PositionTable]) -> MagicMock:
        server = MagicMock()
        main._wire_motor_check_sequence(
            server,
            [],
            tables,
            loops=[],
            can_managers=[],
            sync_monitors=[],
            feedback_timeout_ms=500.0,
        )
        return server

    def test_メインハンドだけの構成でも登録する(self, caplog: pytest.LogCaptureFixture) -> None:
        """サブハンドが不在でも、メインハンド実機の動作確認は使えること。"""
        with caplog.at_level(logging.WARNING):
            server = self._wire([self._table("main_hand_positions.yaml")])

        sequence = server.set_motor_check_sequence.call_args.args[0]
        assert not [info for info in sequence.steps if "サブハンド" in info.label]
        assert len(sequence.excluded_steps) == 7

    def test_除外したステップを起動ログに出す(self, caplog: pytest.LogCaptureFixture) -> None:
        """画面を開かずに構成の食い違いへ気付ける唯一の経路。"""
        with caplog.at_level(logging.WARNING):
            self._wire([self._table("main_hand_positions.yaml")])

        excluded_logs = [rec.getMessage() for rec in caplog.records if "除外" in rec.getMessage()]
        assert any("サブハンド 昇降" in msg and "sub_lift" in msg for msg in excluded_logs)

    def test_出荷構成では一つも除外しない(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            server = self._wire(
                [self._table("main_hand_positions.yaml", "sub_hand_positions.yaml")]
            )

        sequence = server.set_motor_check_sequence.call_args.args[0]
        assert sequence.excluded_steps == ()

    def test_指令できる軸が無ければ登録しない(self, caplog: pytest.LogCaptureFixture) -> None:
        """未登録なら「シーケンスが読み込まれていません」として拒否される。

        零点確定のステップは軸を宣言しないので構成に依らず残る —— ステップ数で
        判定すると、1 本も駆動しない構成が「登録された」状態で通る。
        """
        empty = load_position_table({"axes": {}, "positions": {}}, source="<test>")
        with caplog.at_level(logging.WARNING):
            server = self._wire([empty])

        server.set_motor_check_sequence.assert_not_called()
