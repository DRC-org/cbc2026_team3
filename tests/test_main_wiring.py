from __future__ import annotations

import ast
import dataclasses
import inspect
import logging
import math
import pathlib
import socket
import struct
import time
import types
from collections.abc import Callable
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, call, patch

import can
import pytest
import yaml

import main
from lib.axis_sync import MotorSpec, SyncGroup
from lib.can_manager import CANManager
from lib.config_schema import (
    HealthThresholds,
    MotorConfig,
    RobotConfig,
    SystemConfig,
    load_robot_config,
    load_system_config,
)
from lib.control.position_loop import M3508PositionLoop
from lib.control.sync_monitor import SyncMonitor
from lib.control.target_refresh import QueryDrivenTargetRefresher
from lib.drivers.base import ControlMode
from lib.drivers.dm3520 import Dm3520Driver
from lib.drivers.edulite05 import Edulite05Driver
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import CURRENT_MAX, M3508Driver
from lib.health import MotorHealth
from lib.match_state import ChecklistItem
from lib.motion_guard import AxisReading, SensorSuspension
from lib.sequence.engine import Sequence
from lib.sequence.motors import EStopActiveError, MotorGroup, MotorHandle
from lib.sequence.positions import PositionTable, load_position_table
from lib.server import RobotContext, RobotServer
from main import (
    _DEFAULT_PID,
    _attach_motion_profiles,
    _attach_sync_groups,
    _attach_travel_ranges,
    _build_limit_monitors,
    _build_manual_controller,
    _build_position_loops,
    _build_position_pid,
    _build_sync_groups,
    _build_target_refreshers,
    _create_motor,
    _load_all_configs,
    _load_checklist_definitions,
    _load_pid_config,
    _make_sensor_reader,
    _wire_robot_motors,
)
from sequences.motor_check import MotorCheckSequence
from tests.fake_can import (
    deliver_frame,
    direct_runner,
    mark_feedback_at,
    mock_bus,
    mock_can_manager,
)
from tests.fake_clock import FakeClock
from tests.fake_drivers import StubFeedbackDriver
from tests.feedback_frames import edulite_feedback, feed_generic, feed_m3508, generic_info

_CONFIG_DIR = pathlib.Path(__file__).resolve().parent.parent / "config"


class _StubCANManager:
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


def _no_sensors(_name: str) -> bool | None:
    """センサを 1 本も持たない構成の読み口 (可動端インターロックの注入)。

    `guard:` を書いた軸が 1 本も無い config でしか使わないので、返す値は
    判定に現れない。**それでも `False` ではなく `None` を返す** —— 「読めていない」
    が既定であることを、テスト側の書き方でも崩さないため。
    """
    return None


def _no_contacts(_name: str) -> int | None:
    """接触の累計を提供しないセンサ (カウンタを持たないドライバと同じ扱い)。"""
    return None


def _robot(config: dict) -> RobotConfig:
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
        with caplog.at_level(logging.WARNING):
            _load_pid_config("lift_motor", {"integral_limit": None})

        assert caplog.records == []

    def test_null_numeric_key_falls_back_to_default(self) -> None:
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
        cfg = _motor("lift_motor", _m3508_config(output_limit=2000)["motors"]["lift_motor"])

        pid = _build_position_pid(cfg)

        assert pid.output_max == 2000.0
        assert pid.output_min == -2000.0

    def test_output_limit_capped_at_current_max(self) -> None:
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
        config = _m3508_config()
        driver = M3508Driver("lift_motor", can_id=1)
        manager = _StubCANManager()
        feed_m3508(driver, deg=0.0)
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
            PositionTable.empty(),
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: estop_flag[0],
            sensor_active=_no_sensors,
        )
        return config, manager, motors, seq, loops

    def test_motor_group_is_bound_to_sequence(self) -> None:
        _, _, _, seq, _ = self._wire([False])

        assert seq.has_motors
        assert set(seq.motors.names) == {"lift_motor", "gripper"}

    async def test_m3508_target_goes_through_position_loop(self) -> None:
        _, manager, _, seq, loops = self._wire([False])

        await seq.motors.lift_motor.set_target(ControlMode.POSITION, 42.0)

        assert loops[0].target("lift_motor") == 42.0
        assert manager.sent_by_motor == []

    async def test_non_m3508_still_sends_directly(self) -> None:
        _, manager, _, seq, _ = self._wire([False])

        await seq.motors.gripper.set_target(ControlMode.POSITION, 1.0)

        assert [name for name, _ in manager.sent_by_motor] == ["gripper"]

    async def test_estop_checker_blocks_motor_group(self) -> None:
        flag = [False]
        _, _, _, seq, _ = self._wire(flag)

        flag[0] = True

        with pytest.raises(EStopActiveError):
            await seq.motors.lift_motor.set_target(ControlMode.POSITION, 10.0)
        with pytest.raises(EStopActiveError):
            await seq.motors.gripper.set_target(ControlMode.POSITION, 10.0)

    async def test_estop_checker_reaches_position_loop(self) -> None:
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
            PositionTable.empty(),
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: estop_flag[0],
            sensor_active=_no_sensors,
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
            PositionTable.empty(),
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
            sensor_active=_no_sensors,
        )
        return manager, motors, seq

    @staticmethod
    def _slide_config() -> dict:
        return {"sub_slide": {"driver": "dm3520", "bus": "generic_bus", "can_id": 1}}

    def test_only_generic_motors_are_refreshed(self) -> None:
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
            PositionTable.empty(),
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
            sensor_active=_no_sensors,
        )

        assert (
            _build_target_refreshers(seq.motors, motors, manager, is_estop_active=lambda: False)
            == []
        )

    def test_dm3520_gets_its_own_refresher(self) -> None:
        manager, motors, seq = self._wire(
            extra_motors={"sub_slide": Dm3520Driver("sub_slide", can_id=1)},
            extra_config=self._slide_config(),
        )

        refreshers = _build_target_refreshers(
            seq.motors, motors, manager, is_estop_active=lambda: False
        )

        assert [r.motor_names for r in refreshers] == [("gripper",), ("sub_slide",)]

    def test_edulite_gets_a_refresher_too(self) -> None:
        manager, motors, seq = self._wire(
            extra_motors={"rotate_r": Edulite05Driver("rotate_r", can_id=1)},
            extra_config={"rotate_r": {"driver": "edulite05", "bus": "generic_bus", "can_id": 1}},
        )

        refreshers = _build_target_refreshers(
            seq.motors, motors, manager, is_estop_active=lambda: False
        )

        assert ("rotate_r",) in [r.motor_names for r in refreshers]

    async def test_edulite_is_polled_even_without_a_target(self) -> None:
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
        server = RobotServer()

        assert server.e_stop_active is False

        await server.activate_e_stop(reason="配線確認")
        assert server.e_stop_active is True

    def test_e_stop_active_is_read_only(self) -> None:
        server = RobotServer()

        with pytest.raises(AttributeError):
            server.e_stop_active = True  # type: ignore[misc]


class TestCreateMotorControlType:
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
        with pytest.raises(ValueError, match="control_type"):
            self._generic(control_type="torque")

    def test_current_control_type_aborts_startup(self) -> None:
        with pytest.raises(ValueError, match="control_type"):
            self._generic(control_type="current")

    def test_control_type_on_non_generic_driver_aborts_startup(self) -> None:
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
        loops = self._loops()
        group = SyncGroup(
            "rotate",
            (MotorSpec("rotate_r", 1.0, 0.0), MotorSpec("rotate_l", -1.0, 0.0)),
            tolerance=3.0,
        )

        _attach_sync_groups([group], list(loops.values()))

        assert all(loop.sync_group_names == () for loop in loops.values())

    def test_group_split_across_loops_is_skipped(self) -> None:
        loops = self._loops()
        group = SyncGroup(
            "mixed",
            (MotorSpec("y_axis_r", 1.0, 0.0), MotorSpec("other", -1.0, 0.0)),
            tolerance=1.0,
        )

        _attach_sync_groups([group], list(loops.values()))

        assert all(loop.sync_group_names == () for loop in loops.values())


_Y_SCALE = 55.0131


def _motion_table() -> PositionTable:
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
                "gripper": {"unit": "deg", "command_unit": "deg", "scale": 1.0},
            }
        },
        source="<test>",
    )


class TestAttachMotionProfiles:
    def _rig(self) -> tuple[_StubCANManager, dict[str, M3508Driver], dict]:
        config = {
            "robot_name": "main_hand",
            "motors": {
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
        manager, _, loops = self._rig()

        _attach_motion_profiles(_motion_table(), list(loops.values()))

        loop = loops["m3508_bus"]
        await loop.set_target("y_axis_r", ControlMode.POSITION, 15.0 * _Y_SCALE)
        await loop.set_target("y_axis_l", ControlMode.POSITION, -15.0 * _Y_SCALE)
        await loop.step()

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
        _, _, loops = self._rig()

        with caplog.at_level(logging.INFO):
            _attach_motion_profiles(_motion_table(), list(loops.values()))

        assert any("rotate_r" in record.getMessage() for record in caplog.records)

    def test_起動ログに3つのつまみが全部出る(self, caplog: pytest.LogCaptureFixture) -> None:
        _, _, loops = self._rig()

        with caplog.at_level(logging.INFO):
            _attach_motion_profiles(_motion_table(), list(loops.values()))

        messages = [
            record.getMessage()
            for record in caplog.records
            if "y_axis_r" in record.getMessage() and "台形プロファイル" in record.getMessage()
        ]
        assert messages
        assert all("10.0" in message for message in messages)
        assert all("50.0" in message for message in messages)
        assert all("velocity_ff=0.5" in message for message in messages)

    def test_左右ペアは軸ごと1行に畳む(self, caplog: pytest.LogCaptureFixture) -> None:
        _, _, loops = self._rig()

        with caplog.at_level(logging.INFO):
            _attach_motion_profiles(_motion_table(), list(loops.values()))

        applied = [
            record.getMessage()
            for record in caplog.records
            if record.getMessage().startswith("台形プロファイル: y_axis (")
        ]
        assert len(applied) == 1
        assert "y_axis_r" in applied[0]
        assert "y_axis_l" in applied[0]

    async def test_velocity_ff_が位置制御ループまで届く(self) -> None:
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

        assert manager.last_currents[0] == pytest.approx(0.5 * 10.0 * _Y_SCALE, abs=2)


class TestAttachHelpersAreCalledFromTheCompositionRoot:
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

    @pytest.mark.parametrize(
        "helper", ["_attach_sync_groups", "_attach_motion_profiles", "_attach_travel_ranges"]
    )
    def test_wire_one_robot_calls_the_helper(self, helper: str) -> None:
        assert helper in self._called_names("_wire_one_robot")


class TestTravelRangesReachTheDrivers:
    """軸の可動域はモータ座標へ直して渡す。**`scale` が負の側は整列してから。**

    整列を外すと `set_travel_range` が min >= max を受け取り、起動が落ちるか
    (落ちなければ) 回転数の一意化が可動域の外側を中心に選ぶ。
    """

    def _table(self) -> PositionTable:
        return load_position_table(
            {
                "axes": {
                    "rotate": {
                        "unit": "deg",
                        "command_unit": "rad",
                        "travel": {"min": 0.0, "max": 180.0},
                        "motors": {
                            "rotate_r": {"scale": math.radians(1.0), "offset": 0.0},
                            "rotate_l": {"scale": -math.radians(1.0), "offset": 0.0},
                        },
                    }
                },
                "positions": {"rotate": {"home": 0.0}},
            },
            source="<test>",
        )

    def _drivers(self) -> dict[str, Edulite05Driver]:
        return {
            "rotate_r": Edulite05Driver("rotate_r", can_id=0x11),
            "rotate_l": Edulite05Driver("rotate_l", can_id=0x12),
        }

    def test_scale_が正のモータへはそのまま渡る(self) -> None:
        drivers = self._drivers()

        _attach_travel_ranges(self._table(), drivers)

        assert drivers["rotate_r"].travel_range == pytest.approx((0.0, math.pi))

    def test_scale_が負のモータには整列してから渡る(self) -> None:
        drivers = self._drivers()

        _attach_travel_ranges(self._table(), drivers)

        assert drivers["rotate_l"].travel_range == pytest.approx((-math.pi, 0.0))

    def test_travel_を書かない軸へは渡さない(self) -> None:
        table = load_position_table(
            {
                "axes": {"lift": {"unit": "mm", "scale": 1.0}},
                "positions": {"lift": {"home": 0.0}},
            },
            source="<test>",
        )
        drivers = {"lift": Edulite05Driver("lift", can_id=0x21)}

        _attach_travel_ranges(table, drivers)

        assert drivers["lift"].travel_range is None

    def test_このロボットに居ないモータは警告して飛ばす(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="main"):
            _attach_travel_ranges(self._table(), {})

        assert len(caplog.records) == 2


class TestShippedMainHandConfig:
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
    NOT_FOR_SERVER: ClassVar[dict[str, str]] = {
        "can_buses": "CANManager の生成に使う (サーバーはバスを直接触らない)",
        "source": "エラーメッセージへ出すファイル名",
    }

    def _server_call_keywords(self) -> str:
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
        names = {f.name for f in dataclasses.fields(SystemConfig)}
        assert set(self.NOT_FOR_SERVER) <= names


class TestRobotContextReachesTheServer:
    NOT_A_PARAMETER: ClassVar[dict[str, str]] = {
        "mode": "サーバーが持つ実行時状態 (起動時は必ず SEQUENCE から始まる)",
        "court_dependent_axes": "位置定数から導く (書き写すと宣言と食い違う)",
        "court_dependent_position_axes": "位置定数から導く (書き写すと宣言と食い違う)",
    }

    def _add_robot_call_arguments(self) -> set[str]:
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
        system_path = self._system(tmp_path)

        with caplog.at_level(logging.WARNING):
            _, loaded = _load_all_configs(system_path, [tmp_path / "absent.yaml"])

        assert loaded == []
        assert any("absent.yaml" in record.getMessage() for record in caplog.records)

    def test_invalid_checklist_aborts_with_a_message(self, tmp_path: pathlib.Path) -> None:
        path = self._write(
            tmp_path,
            "checklist.yaml",
            "checklists:\n  main_hand:\n    - id: a\n      label: A\n",
        )

        with pytest.raises(SystemExit) as exc:
            _load_checklist_definitions(path)

        message = str(exc.value)
        assert "設定を読み込めません" in message
        assert "main_hand" in message


class TestSequenceClassSelection:
    def _module(self, source: str) -> types.ModuleType:
        module = types.ModuleType("fakerobots.r1")
        exec(compile(source, "<fakerobots.r1>", "exec"), module.__dict__)
        return module

    _PREAMBLE = "from lib.sequence.engine import Sequence\n"

    def test_単一のサブクラスをそのまま返す(self) -> None:
        module = self._module(self._PREAMBLE + "class OnlyOne(Sequence):\n    pass\n")

        assert main._sequence_class_defined_in(module).__name__ == "OnlyOne"

    def test_複数定義されていたら起動を拒否する(self) -> None:
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
        module = self._module(
            self._PREAMBLE
            + "from sequences.motor_check import MotorCheckSequence\n"
            + "class ZzzOwn(Sequence):\n    pass\n"
        )

        assert main._sequence_class_defined_in(module).__name__ == "ZzzOwn"

    def test_サブクラスが無ければ_None(self) -> None:
        assert main._sequence_class_defined_in(self._module("x = 1\n")) is None


class TestRobotBusSelection:
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

    def test_setup_robot_は別名ではなくインタフェース名で開く(self) -> None:
        robot = _robot(
            {
                "robot_name": "r",
                "motors": {"conveyor": {"driver": "generic", "bus": "generic_bus", "can_id": 1}},
            }
        )

        with patch("main._create_bus") as create_bus:
            main._setup_robot(robot, self._BUSES, dry_run=True)

        assert create_bus.call_args_list == [call("can_generic", dry_run=True)]

    def test_バスを開けなければ一行のメッセージで落とす(self) -> None:
        with (
            patch("main.can.Bus", side_effect=OSError(19, "No such device")),
            pytest.raises(SystemExit) as exc,
        ):
            main._create_bus("can_dm3520", dry_run=False)

        message = str(exc.value)
        assert "can_dm3520" in message
        assert "setup_can.sh" in message


class TestSensorExpectedFirmware:
    _SENSOR_ID = 0x44

    def _setup(self, expected_firmware: int | None) -> tuple[CANManager, GenericDriver]:
        sensor_cfg: dict = {"bus": "generic_bus", "can_id": self._SENSOR_ID}
        if expected_firmware is not None:
            sensor_cfg["expected_firmware"] = expected_firmware
        robot = _robot(
            {
                "robot_name": "r",
                "motors": {"conveyor": {"driver": "generic", "bus": "generic_bus", "can_id": 1}},
                "sensors": {"origin_sensor": sensor_cfg},
            }
        )
        can_manager, _motors = main._setup_robot(
            robot, {"generic_bus": "can_generic"}, dry_run=True
        )
        sensor = can_manager.sensors["origin_sensor"]
        assert isinstance(sensor, GenericDriver)
        return can_manager, sensor

    def _sensor_health(self, can_manager: CANManager):
        snapshot = can_manager.health()
        return next(info for info in snapshot.motors if info.name == "origin_sensor")

    def test_申告値が食い違うセンサは_fault(self) -> None:
        can_manager, sensor = self._setup(expected_firmware=6)
        deliver_frame(can_manager, "generic_bus", generic_info(sensor, firmware_version=5))

        assert self._sensor_health(can_manager).state is MotorHealth.FAULT
        assert sensor.info_mismatch is not None
        assert "焼き忘れ" in sensor.info_mismatch

    def test_申告値が一致するセンサは_fault_にならない(self) -> None:
        can_manager, sensor = self._setup(expected_firmware=6)
        deliver_frame(can_manager, "generic_bus", generic_info(sensor, firmware_version=6))

        assert self._sensor_health(can_manager).state is not MotorHealth.FAULT

    def test_期待値を書かないセンサは照合しない(self) -> None:
        can_manager, sensor = self._setup(expected_firmware=None)
        deliver_frame(can_manager, "generic_bus", generic_info(sensor, firmware_version=5))

        assert self._sensor_health(can_manager).state is not MotorHealth.FAULT


class TestReadOperstate:
    def _write(self, root: pathlib.Path, channel: str, text: str) -> None:
        (root / channel).mkdir()
        (root / channel / "operstate").write_text(text)

    def test_down_を読む(self, tmp_path: pathlib.Path) -> None:
        self._write(tmp_path, "can_m3508", "down\n")

        assert main._read_operstate("can_m3508", root=tmp_path) == "down"

    def test_up_を読む(self, tmp_path: pathlib.Path) -> None:
        self._write(tmp_path, "can_m3508", "up\n")

        assert main._read_operstate("can_m3508", root=tmp_path) == "up"

    @pytest.mark.skipif(
        not pathlib.Path("/sys/class/net/lo").is_dir(),
        reason="sysfs のネットワークインタフェースが無い環境",
    )
    def test_既定の根は実在するインタフェースを読める(self) -> None:
        assert main._read_operstate("lo") is not None

    def test_実体が無ければ判定できないとしてNoneを返す(self, tmp_path: pathlib.Path) -> None:
        assert main._read_operstate("can_m3508", root=tmp_path) is None


class TestCreateBusOperstate:
    def test_down_なら起動ログにERRORでインタフェース名を残す(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(main, "_read_operstate", lambda _channel: "down")

        with (
            patch("main.can.Bus", return_value=MagicMock()),
            caplog.at_level(logging.ERROR),
        ):
            main._create_bus("can_m3508", dry_run=False)

        assert any(
            "can_m3508" in record.getMessage() and record.levelno == logging.ERROR
            for record in caplog.records
        )

    def test_down_でも起動は止めない(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(main, "_read_operstate", lambda _channel: "down")

        with patch("main.can.Bus", return_value=MagicMock()) as bus_ctor:
            bus = main._create_bus("can_m3508", dry_run=False)

        assert bus is bus_ctor.return_value

    def test_up_ならログを出さない(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(main, "_read_operstate", lambda _channel: "up")

        with (
            patch("main.can.Bus", return_value=MagicMock()),
            caplog.at_level(logging.DEBUG),
        ):
            main._create_bus("can_m3508", dry_run=False)

        assert caplog.records == []

    def test_unknown_ではログを出さない(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(main, "_read_operstate", lambda _channel: "unknown")

        with (
            patch("main.can.Bus", return_value=MagicMock()),
            caplog.at_level(logging.DEBUG),
        ):
            main._create_bus("can_m3508", dry_run=False)

        assert caplog.records == []

    def test_判定できなければログを出さない(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(main, "_read_operstate", lambda _channel: None)

        with (
            patch("main.can.Bus", return_value=MagicMock()),
            caplog.at_level(logging.DEBUG),
        ):
            main._create_bus("can_m3508", dry_run=False)

        assert caplog.records == []

    def test_dry_run_では判定しない(self, monkeypatch: pytest.MonkeyPatch) -> None:

        def _fail(_channel: str) -> str | None:
            raise AssertionError("dry_run では _read_operstate を呼んではならない")

        monkeypatch.setattr(main, "_read_operstate", _fail)

        bus = main._create_bus("can_m3508", dry_run=True)
        bus.shutdown()


class TestEnsurePortAvailable:
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
        source = inspect.getsource(main.main)
        assert source.index("_ensure_port_available") < source.index("_wire_one_robot")


class TestStartAll:
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
            limit_monitors=[],
            target_refreshers=[],
            motor_group=None,
        )

    async def test_起動時に励磁できなかったモータをサーバーへ渡す(self) -> None:
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


class TestLimitMonitorWiring:
    """移動中の可動端監視を回す配線。

    配線し忘れると歯止めは**指令を書く瞬間しか効かない**ままになり、遠い目標を
    1 回書いた移動が途中でスイッチを踏んでも誰も止めない。
    """

    def _table(self, *, guard: dict | None) -> PositionTable:
        axis: dict = {"unit": "mm", "command_unit": "rad", "scale": 2.0}
        if guard is not None:
            axis["guard"] = guard
        return load_position_table(
            {"axes": {"sub_y_axis": axis}, "positions": {"sub_y_axis": {"home": 0.0}}},
            source="<test>",
        )

    def _sequence(self) -> Sequence:
        sequence = _DummySequence("sub_hand")
        group = MotorGroup(sensor_active=_no_sensors)
        group.add(
            MotorHandle("sub_y_axis", StubFeedbackDriver("sub_y_axis", 1), mock_can_manager())
        )
        sequence.bind_motors(group)
        return sequence

    def test_limits_を書いた軸を監視する(self) -> None:
        monitors = _build_limit_monitors(
            self._table(guard={"limits": {"minus": "sub_y_rear"}}),
            self._sequence(),
            sensor_active=_no_sensors,
            sensor_contact_count=_no_contacts,
        )

        assert [monitor.axis_names for monitor in monitors] == [("sub_y_axis",)]

    def test_対象の軸が無ければ回さない(self) -> None:
        assert (
            _build_limit_monitors(
                self._table(guard=None),
                self._sequence(),
                sensor_active=_no_sensors,
                sensor_contact_count=_no_contacts,
            )
            == []
        )

    async def test_止めた回数を_move_to_へ配線する(self) -> None:
        """配線しないと、保護に曲げられた移動が「到達した」として次のステップへ進む。"""
        sequence = self._sequence()
        monitors = _build_limit_monitors(
            self._table(guard={"limits": {"minus": "sub_y_rear"}}),
            sequence,
            sensor_active=_no_sensors,
            sensor_contact_count=_no_contacts,
        )
        await sequence.motors["sub_y_axis"].set_target(ControlMode.POSITION, -10.0)
        await monitors[0].step()

        read = main._make_limit_interventions(monitors)

        assert read("sub_y_axis").count == 1
        assert read("居ない軸").count == 0

    async def test_起動で回し_終了で止める(self) -> None:
        monitor = MagicMock()
        monitor.stop = AsyncMock()
        can_manager = MagicMock()
        can_manager.run = AsyncMock(return_value=[])
        can_manager.shutdown = AsyncMock()
        wiring = main._RobotWiring(
            name="sub_hand",
            sequence=_DummySequence("sub_hand"),
            can_manager=can_manager,
            positions=PositionTable.empty(),
            position_loops=[],
            sync_monitors=[],
            limit_monitors=[monitor],
            target_refreshers=[],
            motor_group=None,
        )
        server = MagicMock()
        server.start = AsyncMock()
        server.cleanup = AsyncMock()

        await main._start_all(server, [wiring])
        monitor.start.assert_called_once_with()

        await main._shutdown_all(server, [wiring])
        monitor.stop.assert_awaited_once_with()


class TestLimitMonitorSensorSuspensionWiring:
    """可動端監視へ渡す読み口は、**現在値も接触の累計も**整列段の覆いを通す。

    片方を生のまま渡すと、覆ったはずのセンサでも接触を数えた周期だけ「押されている」と
    判定され、整列段の最中に目標が実測位置へ書き直される (覆う目的そのものが果たせない)。
    零点確定自身へ渡すぶんは生のまま —— 探索も離脱も「今 ON か」で進む。
    """

    def _wired_contact_reader(
        self,
        suspension: SensorSuspension,
        contact_count: Callable[[str], int | None],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: pathlib.Path,
    ) -> Callable[[str], int | None]:
        captured: dict[str, Callable[[str], int | None]] = {}

        def _capture(_positions, _sequence, *, sensor_active, sensor_contact_count):
            captured["count"] = sensor_contact_count
            return []

        monkeypatch.setattr(main, "_setup_robot", lambda *_a, **_k: (CANManager(), {}))
        monkeypatch.setattr(main, "_make_sensor_contact_reader", lambda _managers: contact_count)
        monkeypatch.setattr(main, "_build_limit_monitors", _capture)

        main._wire_one_robot(
            MagicMock(),
            tmp_path / "bench.yaml",
            _robot(
                {
                    "robot_name": "bench",
                    "motors": {
                        "bench_axis": {"driver": "generic", "bus": "bench_bus", "can_id": 1}
                    },
                }
            ),
            load_system_config({"can_buses": {"bench_bus": "can0"}}, source="<test>"),
            dry_run=True,
            is_estop_active=lambda: False,
            e_stop_tasks=set(),
            sensor_suspension=suspension,
        )
        return captured["count"]

    def test_接触の累計も覆いを通して渡す(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        contacts = {"y_axis_l_origin_sensor": 3}
        suspension = SensorSuspension()

        read = self._wired_contact_reader(suspension, contacts.get, monkeypatch, tmp_path)

        assert read("y_axis_l_origin_sensor") == 3
        with suspension.suspend(["y_axis_l_origin_sensor"]):
            contacts["y_axis_l_origin_sensor"] = 9
            assert read("y_axis_l_origin_sensor") == 3
        assert read("y_axis_l_origin_sensor") == 9

    def test_零点確定へは生の累計を渡す(self) -> None:
        """覆った値を零点確定自身が読むと、到達判定が自分の目を塞ぐ。"""
        tree = ast.parse(pathlib.Path(main.__file__).read_text(encoding="utf-8"))
        runner = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "HomingRunner"
        )
        passed = {kw.arg: ast.unparse(kw.value) for kw in runner.keywords}

        assert passed["sensor_contact_count"] == "_make_sensor_contact_reader(can_managers)"


class _RecordingLimitMonitor:
    """`LimitMonitor` のうち解決器と入口が触る口だけ。伝えられた軸とその時点の実測を控える。"""

    axis_names: tuple[str, ...] = ()

    def __init__(self, observe: Callable[[], float] | None = None) -> None:
        self.replaced: list[tuple[str, float | None]] = []
        self._observe = observe
        self.directions: dict[str, int] = {}

    def origin_replaced(self, axis: str) -> None:
        self.replaced.append((axis, None if self._observe is None else self._observe()))

    def pressed_toward(self, name: str) -> int | None:
        return self.directions.get(name)


class TestOriginResolver:
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
        motors = {
            "y_axis_r": M3508Driver("y_axis_r", can_id=1),
            "y_axis_l": M3508Driver("y_axis_l", can_id=2),
        }
        loop = self._loop(motors)
        for driver, deg in ((motors["y_axis_r"], 30.0), (motors["y_axis_l"], -30.0)):
            feed_m3508(driver, deg=0.0)
            feed_m3508(driver, deg=deg)
        assert motors["y_axis_r"].multi_turn_position != 0.0
        assert motors["y_axis_l"].multi_turn_position != 0.0

        capture = main._make_origin_resolver([loop], self._table(), is_estop_active=lambda: False)(
            "y_axis"
        )

        assert capture is not None
        await capture()

        assert motors["y_axis_r"].multi_turn_position == pytest.approx(0.0)
        assert motors["y_axis_l"].multi_turn_position == pytest.approx(0.0)

    async def test_確定した後に可動端監視へ付け替えを伝える(self) -> None:
        """伝えないと、前の座標で覚えた接触位置が付け替え後の実測と比べられる。"""
        motors = {
            "y_axis_r": M3508Driver("y_axis_r", can_id=1),
            "y_axis_l": M3508Driver("y_axis_l", can_id=2),
        }
        loop = self._loop(motors)
        for driver in motors.values():
            feed_m3508(driver, deg=0.0)
            feed_m3508(driver, deg=30.0)
        guard = _RecordingLimitMonitor(lambda: motors["y_axis_r"].multi_turn_position)

        capture = main._make_origin_resolver(
            [loop], self._table(), limit_monitors=[guard], is_estop_active=lambda: False
        )("y_axis")

        assert capture is not None
        await capture()

        # 付け替えた後に伝える (伝えた時点で実測は新しい座標)
        assert guard.replaced == [("y_axis", pytest.approx(0.0))]

    def test_位置制御ループにも_set_zero_にも載らない軸は手段が無い(self) -> None:
        mgr = CANManager(run_blocking=direct_runner())
        mgr.add_bus("can_generic", mock_bus())
        for name, can_id in (("rotate_r", 0x41), ("rotate_l", 0x42)):
            mgr.add_motor("can_generic", GenericDriver(name, can_id=can_id))

        resolve = main._make_origin_resolver(
            [self._loop()], self._table(), can_managers=[mgr], is_estop_active=lambda: False
        )

        assert resolve("rotate") is None


class TestOriginResolverViaDriver:
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
        sent: list[tuple[str, can.Message]] = []
        mgr = CANManager(run_blocking=direct_runner())
        mgr.add_bus("can_edulite", mock_bus())
        for name, can_id in (("rotate_r", 0x11), ("rotate_l", 0x12)):
            mgr.add_motor("can_edulite", Edulite05Driver(name, can_id=can_id))
            # 原点を控えるには鮮度が要る (未受信の 0.0 を現在位置と信じない)
            deliver_frame(mgr, "can_edulite", edulite_feedback(mgr.motors[name], position=1.5))

        async def _send(motor_name: str, msg: can.Message) -> None:
            sent.append((motor_name, msg))
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

    async def test_edulite_のペア軸は_PC_側の原点で確定する(self) -> None:
        """**`SET_ZERO` と PC 側オフセットを併用してはならない。**

        どちらも生の座標系そのものを動かす操作なので、同時に効かせると原点が
        二重定義になる (`docs/invariants.md` §3)。
        """
        mgr, sent = self._manager()

        capture = main._make_origin_resolver(
            [], self._table(), can_managers=[mgr], is_estop_active=lambda: False
        )("rotate")

        assert capture is not None
        await capture()

        assert sent == [], "原点は CAN の往復なしで確定する"
        for name in ("rotate_r", "rotate_l"):
            assert mgr.motors[name].feedback_position() == pytest.approx(0.0, abs=1e-9)

    async def test_ドライバ経路でも確定した後に可動端監視へ伝える(self) -> None:
        mgr, _sent = self._manager()
        guard = _RecordingLimitMonitor(lambda: mgr.motors["rotate_r"].feedback_position())

        capture = main._make_origin_resolver(
            [],
            self._table(),
            can_managers=[mgr],
            limit_monitors=[guard],
            is_estop_active=lambda: False,
        )("rotate")

        assert capture is not None
        await capture()

        assert guard.replaced == [("rotate", pytest.approx(0.0, abs=1e-9))]

    async def test_原点の持ち方が違うモータが混ざった軸は手段が無い(self) -> None:
        """片方の経路へ流すと、もう片方は原点が動かないまま「成功した」と返る。"""
        mgr = CANManager(run_blocking=direct_runner())
        mgr.add_bus("can_edulite", mock_bus())
        mgr.add_bus("can_dm3520", mock_bus())
        mgr.add_motor("can_edulite", Edulite05Driver("rotate_r", can_id=0x11))
        mgr.add_motor("can_dm3520", Dm3520Driver("rotate_l", can_id=0x01, master_id=0x11))

        resolve = main._make_origin_resolver(
            [], self._table(), can_managers=[mgr], is_estop_active=lambda: False
        )

        assert resolve("rotate") is None

    async def test_緊急停止インターロックを零点確定へ渡す(self) -> None:
        """**渡し忘れると「緊急停止を見ない零点確定」が黙って通る。**

        探索が完遂していない姿勢を原点にすると、黙って成功した誤った原点が残り、
        以後の `move_to` が全部そのぶんずれた場所へ動く。
        """
        mgr, _sent = self._manager()

        capture = main._make_origin_resolver(
            [], self._table(), can_managers=[mgr], is_estop_active=lambda: True
        )("rotate")

        assert capture is not None
        with pytest.raises(RuntimeError, match="緊急停止"):
            await capture()

        for name in ("rotate_r", "rotate_l"):
            assert mgr.motors[name].origin_offset == 0.0

    async def test_原点付け替え中は同期監視を止める(self) -> None:
        mgr, _sent = self._manager()
        monitor = self._monitor()
        suspended_during: list[bool] = []

        original = mgr.capture_origin_in_place

        async def _spy(names, **kwargs):
            suspended_during.append(monitor.is_suspended("rotate"))
            await original(names, **kwargs)

        mgr.capture_origin_in_place = _spy  # type: ignore[method-assign]

        capture = main._make_origin_resolver(
            [],
            self._table(),
            can_managers=[mgr],
            sync_monitors=[monitor],
            is_estop_active=lambda: False,
        )("rotate")

        assert capture is not None
        await capture()

        assert suspended_during == [True]
        assert monitor.is_suspended("rotate") is False

    async def test_付け替えの前後で古い目標を捨てる(self) -> None:
        """**`SET_ZERO` は「生値 0 が指す物理位置」を付け替える。**

        探索がヒットした直後に `HomingRunner` が「その場で止める」ために書いた
        目標は旧原点基準の生値で、その値は零点確定で補正しようとしていたズレ
        そのものである。捨てずに再励磁すると、20Hz の再送がそのぶんだけ離れた
        位置へ押し続ける (`activation_steps(after_set_zero=True)` が保持目標へ
        0 を書く手当ては、再送 1 通で上書きされて効かない)。
        """
        mgr, _sent = self._manager()
        table = self._table()
        handles = [MotorHandle(name, mgr.motors[name], mgr) for name in ("rotate_r", "rotate_l")]
        refresher = QueryDrivenTargetRefresher(handles, mgr)
        for handle in handles:
            await handle.set_target(ControlMode.POSITION, 1.25)
        assert all(h.has_target for h in handles)

        capture = main._make_origin_resolver(
            [],
            table,
            can_managers=[mgr],
            target_refreshers=[refresher],
            is_estop_active=lambda: False,
        )("rotate")

        assert capture is not None
        await capture()

        # 捨てた後なので、次の再送は「今の姿勢を保て」を新しい原点で取り直す
        assert [h.has_target for h in handles] == [False, False]
        assert refresher.is_paused is False

    async def test_付け替えのあいだ再送を黙らせる(self) -> None:
        """捨てるだけでは足りない —— 再送は目標が無ければラッチを取り直すので、
        `SET_ZERO` の直前に取ったラッチが直後には別の位置を指す。
        """
        mgr, _sent = self._manager()
        table = self._table()
        handles = [MotorHandle(name, mgr.motors[name], mgr) for name in ("rotate_r", "rotate_l")]
        refresher = QueryDrivenTargetRefresher(handles, mgr)
        paused_during: list[bool] = []

        original = mgr.capture_origin_in_place

        async def _spy(names, **kwargs):
            paused_during.append(refresher.is_paused)
            await original(names, **kwargs)

        mgr.capture_origin_in_place = _spy  # type: ignore[method-assign]

        capture = main._make_origin_resolver(
            [],
            table,
            can_managers=[mgr],
            target_refreshers=[refresher],
            is_estop_active=lambda: False,
        )("rotate")

        assert capture is not None
        await capture()

        assert paused_during == [True]
        # **必ず戻す。** 戻し忘れると以後この軸へ 1 通も再送されず、
        # 問い合わせ駆動の DM3520 / EDULITE 05 は永久に STALE になる
        assert refresher.is_paused is False

    async def test_付け替えが失敗しても再送を戻す(self) -> None:
        mgr, _sent = self._manager()
        table = self._table()
        handles = [MotorHandle(name, mgr.motors[name], mgr) for name in ("rotate_r", "rotate_l")]
        refresher = QueryDrivenTargetRefresher(handles, mgr)

        async def _boom(_names, **_kwargs):
            raise RuntimeError("再励磁できません")

        mgr.capture_origin_in_place = _boom  # type: ignore[method-assign]

        capture = main._make_origin_resolver(
            [],
            table,
            can_managers=[mgr],
            target_refreshers=[refresher],
            is_estop_active=lambda: False,
        )("rotate")

        assert capture is not None
        with pytest.raises(RuntimeError):
            await capture()

        assert refresher.is_paused is False

    async def test_付け替えが失敗しても同期監視を戻す(self) -> None:
        """例外で抜ける経路が `finally` を通らないと、監視が死んだままになる。"""
        mgr, _sent = self._manager()
        monitor = self._monitor()

        async def _boom(_names, **_kwargs):
            raise RuntimeError("再励磁できません")

        mgr.capture_origin_in_place = _boom  # type: ignore[method-assign]

        capture = main._make_origin_resolver(
            [],
            self._table(),
            can_managers=[mgr],
            sync_monitors=[monitor],
            is_estop_active=lambda: False,
        )("rotate")

        assert capture is not None
        with pytest.raises(RuntimeError):
            await capture()

        assert monitor.is_suspended("rotate") is False

    async def test_dm3520_も_set_zero_で確定できる(self) -> None:
        # 特殊コマンドは 3 つとも同じ CAN ID なので、順序は末尾バイトでしか読めない
        table = load_position_table(
            {
                "axes": {
                    "sub_y_axis": {
                        "unit": "mm",
                        "command_unit": "rad",
                        "motors": {"sub_y_axis_m": {"scale": 1.0}},
                    }
                },
                "positions": {"sub_y_axis": {"home": 0.0}},
            },
            source="<test>",
        )
        sent: list[can.Message] = []
        mgr = CANManager(run_blocking=direct_runner())
        mgr.add_bus("can_dm3520", mock_bus())
        driver = Dm3520Driver("sub_y_axis_m", can_id=0x01, master_id=0x11)
        mgr.add_motor("can_dm3520", driver)

        async def _send(motor_name: str, msg: can.Message) -> None:
            sent.append(msg)
            # 問い合わせへの応答としてフィードバックが届く状況を模す
            mark_feedback_at(mgr, motor_name, time.time())
            if msg.arbitration_id == Dm3520Driver.CONFIG_FRAME_ID:
                # 固定小数点レンジの読み返しにも応答する。応答が無いモータは
                # 励磁を拒否される (`activation_block_reason`) ので、実機と同じく
                # 「読めている」状態にしておかないと再励磁の順序を見られない
                register = msg.data[3]
                driver.matches_feedback(
                    can.Message(
                        arbitration_id=driver.master_id,
                        data=struct.pack(
                            "<HBBf",
                            driver.can_id,
                            Dm3520Driver.CONFIG_READ,
                            register,
                            {
                                Dm3520Driver.REG_P_MAX: driver.p_max,
                                Dm3520Driver.REG_V_MAX: driver.v_max,
                                Dm3520Driver.REG_T_MAX: driver.t_max,
                            }.get(register, 0.0),
                        ),
                        is_extended_id=False,
                    )
                )

        mgr.send = _send  # type: ignore[method-assign]

        capture = main._make_origin_resolver(
            [], table, can_managers=[mgr], is_estop_active=lambda: False
        )("sub_y_axis")

        assert capture is not None
        await capture()

        # 特殊コマンドだけを抜き出す (目標書き込みは 0xFF が 7 つ並ばない)
        specials = [msg.data[7] for msg in sent if msg.data[:7] == bytes([0xFF] * 7)]
        zero = specials.index(Dm3520Driver.SPECIAL_SET_ZERO)
        # SET_ZERO の前に必ず disable がある (励磁したまま原点を動かすと機構が飛ぶ)
        assert Dm3520Driver.SPECIAL_DISABLE in specials[:zero]
        # SET_ZERO の後に必ず enable がある (無励磁のまま残さない)
        assert Dm3520Driver.SPECIAL_ENABLE in specials[zero:]


class TestMotorCheckWiring:
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

    def _wire(self, tables: dict[str, PositionTable]) -> MagicMock:
        server = MagicMock()
        main._wire_motor_check_sequence(
            server,
            [],
            tables,
            loops=[],
            can_managers=[],
            sync_monitors=[],
            limit_monitors=[],
            target_refreshers=[],
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
            sensor_suspension=SensorSuspension(),
        )
        return server

    def test_メインハンドだけの構成でも登録する(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            server = self._wire({"main_hand": self._table("main_hand_positions.yaml")})

        sequence = server.set_motor_check_sequence.call_args.args[0]
        sub_labels = {
            info.label for info in MotorCheckSequence("x").steps if "サブハンド" in info.label
        }

        assert not [info for info in sequence.steps if "サブハンド" in info.label]
        assert {info.label for info in sequence.excluded_steps} == sub_labels

    def test_除外したステップを起動ログに出す(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            self._wire({"main_hand": self._table("main_hand_positions.yaml")})

        excluded_logs = [rec.getMessage() for rec in caplog.records if "除外" in rec.getMessage()]
        assert any("サブハンド 昇降" in msg and "sub_lift" in msg for msg in excluded_logs)

    def test_出荷構成では一つも除外しない(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            server = self._wire(
                {"both": self._table("main_hand_positions.yaml", "sub_hand_positions.yaml")}
            )

        sequence = server.set_motor_check_sequence.call_args.args[0]
        assert sequence.excluded_steps == ()

    def test_指令できる軸が無ければ登録しない(self, caplog: pytest.LogCaptureFixture) -> None:
        empty = load_position_table({"axes": {}, "positions": {}}, source="<test>")
        with caplog.at_level(logging.WARNING):
            server = self._wire({"main_hand": empty})

        server.set_motor_check_sequence.assert_not_called()


class TestStartupSummaryLines:
    def test_しきい値は4つとも読める形で並ぶ(self) -> None:
        text = main._describe_thresholds(
            HealthThresholds(
                feedback_timeout_ms=250.0,
                temp_warning_c=60.0,
                temp_critical_c=75.0,
                tx_error_threshold=32,
            )
        )

        assert "250" in text
        assert "60" in text
        assert "75" in text
        assert "32" in text
        assert "feedback_timeout_ms" not in text

    def test_整数で表せる値から小数点以下を落とす(self) -> None:
        assert main._format_number(180.0) == "180"
        assert main._format_number(180.5) == "180.5"

    def test_ロールが1つなら件数だけを出す(self) -> None:
        items = [ChecklistItem(id=f"i{n}", label=f"項目{n}") for n in range(3)]

        assert main._describe_checklist({"pre_match": items}) == "3 項目"

    def test_ロールが複数ならロール名を添える(self) -> None:
        text = main._describe_checklist(
            {
                "main_hand": [ChecklistItem(id="a", label="A")],
                "sub_hand": [ChecklistItem(id="b", label="B"), ChecklistItem(id="c", label="C")],
            }
        )

        assert "main_hand 1 項目" in text
        assert "sub_hand 2 項目" in text


class TestSensorReader:
    """可動端インターロックが読むセンサ状態 (`_make_sensor_reader`)。**三値である。**

    `True` / `False` / **`None` (読めていない)** を区別する。最後の 1 つは
    「`sensors:` に居ない」と「フィードバックが途絶している」の両方で返り、
    どちらも `False` へ丸めてはならない —— 丸めた瞬間に**配線が抜けたセンサが
    「押されていない = 進んでよい」に化ける** (2026-09-09 の事故はスイッチが
    1 スロットずれていて PC へ届いていなかった)。
    """

    def _manager(self, *, active: bool, age_ms: float = 0.0) -> _StubCANManager:
        manager = _StubCANManager()
        sensor = GenericDriver("front_switch", can_id=0x49)
        feed_generic(sensor, sensor=active)
        manager.sensors = {"front_switch": sensor}  # type: ignore[attr-defined]
        manager.feedback_at["front_switch"] = time.time() - age_ms / 1000.0
        return manager

    def test_接触は_True_で返る(self) -> None:
        read = _make_sensor_reader([self._manager(active=True)], feedback_timeout_ms=500.0)

        assert read("front_switch") is True

    def test_非接触は_False_で返る(self) -> None:
        read = _make_sensor_reader([self._manager(active=False)], feedback_timeout_ms=500.0)

        assert read("front_switch") is False

    def test_未登録のセンサは_None(self) -> None:
        """`sensors:` に居ない名前を「押されていない」と答えてはならない。"""
        read = _make_sensor_reader([self._manager(active=False)], feedback_timeout_ms=500.0)

        assert read("rear_switch") is None

    def test_途絶したセンサは_None(self) -> None:
        """届かなくなったセンサの最後の値を信じ続けると、抜けた配線が素通りする。"""
        read = _make_sensor_reader(
            [self._manager(active=False, age_ms=5000.0)], feedback_timeout_ms=500.0
        )

        assert read("front_switch") is None


class TestPressedTowardWiring:
    """端センサが当たった向きの記憶は **2 つの `MotorGroup` の両方**へ配線する。

    入口が記憶を見ないと、宣言と食い違う端で監視は退避を通すのに入口が宣言された側として
    拒み、その軸は両向きとも動かせない。配線先は `bind_axis_state` と同じ 2 箇所。
    """

    _CONFIG_DIR: ClassVar[pathlib.Path] = pathlib.Path(__file__).resolve().parent.parent / "config"

    def test_ロボットごとの束に配線されている(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        guard = _RecordingLimitMonitor()
        guard.directions["bench_rear"] = -1
        monkeypatch.setattr(main, "_setup_robot", lambda *_a, **_k: (CANManager(), {}))
        monkeypatch.setattr(main, "_build_limit_monitors", lambda *_a, **_k: [guard])

        wiring = main._wire_one_robot(
            MagicMock(),
            tmp_path / "bench.yaml",
            _robot(
                {
                    "robot_name": "bench",
                    "motors": {
                        "bench_axis": {"driver": "generic", "bus": "bench_bus", "can_id": 1}
                    },
                }
            ),
            load_system_config({"can_buses": {"bench_bus": "can0"}}, source="<test>"),
            dry_run=True,
            is_estop_active=lambda: False,
            e_stop_tasks=set(),
            sensor_suspension=SensorSuspension(),
        )

        read = wiring.sequence.motors.pressed_toward
        assert read is not None
        assert read("bench_rear") == -1
        assert read("bench_front") is None

    def test_統合動作確認が組む束にも配線されている(self) -> None:
        server = MagicMock()
        table = load_position_table(
            yaml.safe_load((self._CONFIG_DIR / "sub_hand_positions.yaml").read_text()) or {},
            source="sub_hand_positions.yaml",
        )
        guard = _RecordingLimitMonitor()
        guard.directions["sub_lift_b_limit_sensor"] = -1

        main._wire_motor_check_sequence(
            server,
            [],
            {"sub_hand": table},
            loops=[],
            can_managers=[],
            sync_monitors=[],
            limit_monitors=[guard],  # type: ignore[list-item]
            target_refreshers=[],
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
            sensor_suspension=SensorSuspension(),
        )

        read = server.set_motor_check_sequence.call_args.args[0].motors.pressed_toward
        assert read is not None
        assert read("sub_lift_b_limit_sensor") == -1


class TestAxisStateWiring:
    """軸間干渉の読み口は **2 つの `MotorGroup` の両方**へ配線する。

    ロボットごとの束と、統合動作確認が自前で組む束は別物なので、片方の配線は
    もう片方へ引き継がれない。忘れると**その経路だけが「条件の軸が読めていない」
    で全軸拒否**になる —— 動作確認は毎セッション回すので必ず踏む。
    """

    _CONFIG_DIR: ClassVar[pathlib.Path] = pathlib.Path(__file__).resolve().parent.parent / "config"

    def _positions(self) -> PositionTable:
        return load_position_table(
            {
                "axes": {
                    "lift": {
                        "unit": "mm",
                        "command_unit": "deg",
                        "tolerance": 1.0,
                        "motors": {"lift_motor": {"scale": 10.0}},
                    }
                },
                "positions": {"lift": {"top": -1.0}},
            },
            source="<test>",
        )

    def test_ロボットごとの束に配線されている(self) -> None:
        manager = _StubCANManager()
        driver = M3508Driver("lift_motor", can_id=1)
        # 1 通目は多回転の基準になるので、動いたことを表すには 2 通要る
        feed_m3508(driver, deg=0.0)
        feed_m3508(driver, deg=-20.0)
        manager.feedback_at["lift_motor"] = time.time()
        seq = _DummySequence("main_hand")

        _wire_robot_motors(
            _robot(_m3508_config()),
            manager,
            {"lift_motor": driver},
            seq,
            self._positions(),
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
            sensor_active=_no_sensors,
        )

        read = seq.motors.axis_state
        assert read is not None
        assert read("lift") == AxisReading(value=pytest.approx(-2.0, abs=1e-3), target=None)

    def test_途絶した軸は読めていないとして返る(self) -> None:
        """既定の 0.0 を運び続ける軸が「原点に居る」として条件を満たすのを防ぐ。"""
        manager = _StubCANManager()
        driver = M3508Driver("lift_motor", can_id=1)
        feed_m3508(driver, deg=0.0)
        feed_m3508(driver, deg=-20.0)
        manager.feedback_at["lift_motor"] = time.time() - 5.0
        seq = _DummySequence("main_hand")

        _wire_robot_motors(
            _robot(_m3508_config()),
            manager,
            {"lift_motor": driver},
            seq,
            self._positions(),
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
            sensor_active=_no_sensors,
        )

        read = seq.motors.axis_state
        assert read is not None
        assert read("lift") == AxisReading(value=None, target=None)

    def test_統合動作確認が組む束にも配線されている(self) -> None:
        server = MagicMock()
        table = load_position_table(
            yaml.safe_load((self._CONFIG_DIR / "sub_hand_positions.yaml").read_text()) or {},
            source="sub_hand_positions.yaml",
        )

        main._wire_motor_check_sequence(
            server,
            [],
            {"sub_hand": table},
            loops=[],
            can_managers=[],
            sync_monitors=[],
            limit_monitors=[],
            target_refreshers=[],
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
            sensor_suspension=SensorSuspension(),
        )

        sequence = server.set_motor_check_sequence.call_args.args[0]
        assert sequence.motors.axis_state is not None

    def test_零点合わせパネルに寄せる口が配線されている(self) -> None:
        """配線しないとパネルは寄せず、前後の零点確定が毎回 1 軸失敗で終わる。

        寄せる口は**動作確認と同じ `move_to`** でなければならない。別に組むと
        パネル経由だけが可動端保護や到達判定の外側に出る。
        """
        server = MagicMock()
        table = load_position_table(
            yaml.safe_load((self._CONFIG_DIR / "sub_hand_positions.yaml").read_text()) or {},
            source="sub_hand_positions.yaml",
        )

        main._wire_motor_check_sequence(
            server,
            [],
            {"sub_hand": table},
            loops=[],
            can_managers=[],
            sync_monitors=[],
            limit_monitors=[],
            target_refreshers=[],
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
            sensor_suspension=SensorSuspension(),
        )

        sequence = server.set_motor_check_sequence.call_args.args[0]
        source = server.set_homing_source.call_args.args[0]
        assert source.move_to == sequence.move_to
