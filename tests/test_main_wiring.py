from __future__ import annotations

import struct
import time

import can
import pytest

from lib.drivers.base import ControlMode
from lib.drivers.edulite05 import Edulite05Driver
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import CURRENT_MAX, M3508Driver
from lib.sequence.engine import Sequence
from lib.sequence.motors import EStopActiveError
from lib.server import RobotServer
from main import (
    _DEFAULT_PID,
    _build_position_loops,
    _build_position_pid,
    _load_pid_config,
    _wire_robot_motors,
)


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


def _feed(driver: M3508Driver, deg: float) -> None:
    """M3508 のフィードバックフレームを 1 通流し込む。"""
    angle_raw = round(deg / 360.0 * 8192) % 8192
    data = struct.pack(">HhhBB", angle_raw, 0, 0, 25, 0)
    driver.update_state(can.Message(arbitration_id=0x200 + driver.can_id, data=data))


class _DummySequence(Sequence):
    pass


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
        result = _load_pid_config("lift_motor", {"driver": "m3508", "bus": "b", "can_id": 1})

        assert result == _DEFAULT_PID

    def test_uses_yaml_values(self) -> None:
        motor_cfg = {
            "driver": "m3508",
            "bus": "b",
            "can_id": 1,
            "pid": {
                "kp": 5.5,
                "ki": 0.25,
                "kd": 0.75,
                "integral_limit": 1200,
                "dead_band": 2.5,
                "output_limit": 3000,
            },
        }

        result = _load_pid_config("lift_motor", motor_cfg)

        assert result["kp"] == 5.5
        assert result["ki"] == 0.25
        assert result["kd"] == 0.75
        assert result["integral_limit"] == 1200.0
        assert result["dead_band"] == 2.5
        assert result["output_limit"] == 3000.0

    def test_partial_override_fills_defaults(self) -> None:
        result = _load_pid_config("lift_motor", {"pid": {"kp": 4.0}})

        assert result["kp"] == 4.0
        assert result["ki"] == _DEFAULT_PID["ki"]
        assert result["kd"] == _DEFAULT_PID["kd"]
        assert result["dead_band"] == _DEFAULT_PID["dead_band"]
        assert result["output_limit"] == _DEFAULT_PID["output_limit"]

    def test_null_integral_limit_is_allowed(self) -> None:
        result = _load_pid_config("lift_motor", {"pid": {"integral_limit": None}})

        assert result["integral_limit"] is None

    def test_null_numeric_key_falls_back_to_default(self) -> None:
        """書きかけの yaml (null) で起動を壊さず、安全側の既定値を使う。"""
        result = _load_pid_config("lift_motor", {"pid": {"kp": None, "output_limit": None}})

        assert result["kp"] == _DEFAULT_PID["kp"]
        assert result["output_limit"] == _DEFAULT_PID["output_limit"]

    def test_unknown_key_is_ignored(self) -> None:
        result = _load_pid_config("lift_motor", {"pid": {"kp": 1.0, "kf": 9.0}})

        assert "kf" not in result
        assert result["kp"] == 1.0


class TestBuildPositionPid:
    def test_gains_are_applied(self) -> None:
        pid = _build_position_pid(
            "lift_motor", _m3508_config(kp=3.0, ki=0.5, kd=0.1)["motors"]["lift_motor"]
        )

        assert pid.kp == 3.0
        assert pid.ki == 0.5
        assert pid.kd == 0.1

    def test_output_range_narrowed_by_output_limit(self) -> None:
        """機構未確定のうちは電流上限を絞る。C620 のフルスケールは使わない。"""
        cfg = _m3508_config(output_limit=2000)["motors"]["lift_motor"]

        pid = _build_position_pid("lift_motor", cfg)

        assert pid.output_max == 2000.0
        assert pid.output_min == -2000.0

    def test_output_limit_capped_at_current_max(self) -> None:
        """config で C620 の範囲外を指定してもハード上限を超えない。"""
        cfg = _m3508_config(output_limit=999999)["motors"]["lift_motor"]

        pid = _build_position_pid("lift_motor", cfg)

        assert pid.output_max == float(CURRENT_MAX)
        assert pid.output_min == -float(CURRENT_MAX)

    def test_default_output_limit_is_conservative(self) -> None:
        pid = _build_position_pid("lift_motor", {"driver": "m3508"})

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
            config,
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
            config,
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
            config,
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
            config,
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
        _feed(driver, 0.0)
        # 0.3 秒前のフィードバックを最後の受信とする
        manager.feedback_at["lift_motor"] = time.time() - 0.3

        strict = _build_position_loops(
            config,
            manager,
            {"lift_motor": driver},
            feedback_timeout_ms=100.0,
            is_estop_active=lambda: False,
        )["m3508_bus"]
        await strict.set_target("lift_motor", ControlMode.POSITION, 100.0)
        await strict.step()
        assert manager.last_currents == (0, 0, 0, 0)

        lenient = _build_position_loops(
            config,
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
            config,
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

        await seq.motors.lift_motor.set_position(42.0)

        assert loops[0].target("lift_motor") == 42.0
        assert loops[0].mode("lift_motor") is ControlMode.POSITION
        assert manager.sent_by_motor == []

    async def test_non_m3508_still_sends_directly(self) -> None:
        _, manager, _, seq, _ = self._wire([False])

        await seq.motors.gripper.set_position(1.0)

        assert [name for name, _ in manager.sent_by_motor] == ["gripper"]

    async def test_estop_checker_blocks_motor_group(self) -> None:
        """緊急停止中はシーケンスからモータへ指令を出せない。"""
        flag = [False]
        _, _, _, seq, _ = self._wire(flag)

        flag[0] = True

        with pytest.raises(EStopActiveError):
            await seq.motors.lift_motor.set_position(10.0)
        with pytest.raises(EStopActiveError):
            await seq.motors.gripper.set_position(10.0)

    async def test_estop_checker_reaches_position_loop(self) -> None:
        """実行中ステップが出した目標も、緊急停止で位置制御ループ側が破棄する。"""
        flag = [False]
        _, manager, _, seq, loops = self._wire(flag)
        loop = loops[0]

        await seq.motors.lift_motor.set_position(100.0)
        assert loop.target("lift_motor") == 100.0

        flag[0] = True
        await loop.step()

        assert loop.target("lift_motor") is None
        assert manager.last_currents == (0, 0, 0, 0)


class TestServerEStopProperty:
    def test_e_stop_active_property_reflects_state(self) -> None:
        """main.py から private 属性を触らずに緊急停止状態を読めること。"""
        server = RobotServer()

        assert server.e_stop_active is False

        server._e_stop_active = True
        assert server.e_stop_active is True

    def test_e_stop_active_is_read_only(self) -> None:
        server = RobotServer()

        with pytest.raises(AttributeError):
            server.e_stop_active = True  # type: ignore[misc]
