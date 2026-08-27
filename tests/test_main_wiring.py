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
import logging
import pathlib
import struct
import time

import can
import pytest
import yaml

from lib.axis_sync import MotorSpec, SyncGroup
from lib.config_schema import MotorConfig, RobotConfig, SystemConfig, load_robot_config
from lib.drivers.base import ControlMode
from lib.drivers.edulite05 import Edulite05Driver
from lib.drivers.generic import GenericDriver
from lib.drivers.m3508 import CURRENT_MAX, M3508Driver
from lib.sequence.engine import Sequence
from lib.sequence.motors import EStopActiveError
from lib.sequence.positions import PositionTable, load_position_table
from lib.server import RobotServer
import main
from main import (
    _DEFAULT_PID,
    _attach_sync_groups,
    _build_position_loops,
    _build_position_pid,
    _build_sync_groups,
    _build_target_refresher,
    _create_motor,
    _load_all_configs,
    _load_pid_config,
    _wire_robot_motors,
)

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


def _feed(driver: M3508Driver, deg: float) -> None:
    """M3508 のフィードバックフレームを 1 通流し込む。"""
    angle_raw = round(deg / 360.0 * 8192) % 8192
    data = struct.pack(">HhhBB", angle_raw, 0, 0, 25, 0)
    driver.update_state(can.Message(arbitration_id=0x200 + driver.can_id, data=data))


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
        _feed(driver, 0.0)
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
        assert loops[0].mode("lift_motor") is ControlMode.POSITION
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


class TestBuildTargetRefresher:
    """自作モタドラのコマンドウォッチドッグ (500ms) 対策の配線。"""

    def _wire(self) -> tuple:
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
        _wire_robot_motors(
            _robot(config),
            manager,
            motors,
            seq,
            feedback_timeout_ms=500.0,
            is_estop_active=lambda: False,
        )
        return manager, motors, seq

    def test_only_generic_motors_are_refreshed(self) -> None:
        """M3508 は位置制御ループが 200Hz で送り続けるので再送対象ではない。"""
        _, motors, seq = self._wire()

        refresher = _build_target_refresher(seq.motors, motors, is_estop_active=lambda: False)

        assert refresher is not None
        assert refresher.motor_names == ("gripper",)

    def test_none_without_generic_motors(self) -> None:
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

        assert _build_target_refresher(seq.motors, motors, is_estop_active=lambda: False) is None

    async def test_estop_checker_blocks_resend(self) -> None:
        """再送は停止指令を上書きする。緊急停止中は 1 通も出してはならない。"""
        manager, motors, seq = self._wire()
        flag = [False]
        refresher = _build_target_refresher(seq.motors, motors, is_estop_active=lambda: flag[0])
        assert refresher is not None
        await seq.motors.gripper.set_target(ControlMode.POSITION, 1.0)
        manager.sent_by_motor.clear()

        flag[0] = True
        await refresher.step()

        assert manager.sent_by_motor == []

    async def test_resends_last_target(self) -> None:
        manager, motors, seq = self._wire()
        refresher = _build_target_refresher(seq.motors, motors, is_estop_active=lambda: False)
        assert refresher is not None
        await seq.motors.gripper.set_target(ControlMode.POSITION, 1.0)
        manager.sent_by_motor.clear()

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
    NOT_FOR_SERVER = {
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

        assert set(system.can_buses) == {"m3508_bus", "edulite_bus", "generic_bus"}
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
