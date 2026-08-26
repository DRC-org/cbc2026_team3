from __future__ import annotations

import struct

import can
import pytest

from lib.drivers.base import ControlMode
from lib.drivers.generic import CommandType, GenericDriver


class TestBuildCanId:
    def test_build_can_id(self):
        assert GenericDriver.build_can_id(CommandType.SET_TARGET, 0x01) == 0x001
        assert GenericDriver.build_can_id(CommandType.FEEDBACK, 0x01) == 0x101
        assert GenericDriver.build_can_id(CommandType.SET_MODE, 0x10) == 0x210
        assert GenericDriver.build_can_id(CommandType.E_STOP, 0xFF) == 0x7FF


class TestParseCanId:
    def test_parse_can_id(self):
        cmd, dev = GenericDriver.parse_can_id(0x001)
        assert cmd == CommandType.SET_TARGET
        assert dev == 0x01

        cmd, dev = GenericDriver.parse_can_id(0x101)
        assert cmd == CommandType.FEEDBACK
        assert dev == 0x01

        cmd, dev = GenericDriver.parse_can_id(0x7FF)
        assert cmd == CommandType.E_STOP
        assert dev == 0xFF


class TestEncodeTarget:
    def setup_method(self):
        self.drv = GenericDriver("test_motor", 0x01)

    def test_encode_target_position(self):
        msg = self.drv.encode_target(ControlMode.POSITION, 90.0)
        assert msg.arbitration_id == 0x001
        assert msg.is_extended_id is False
        assert msg.data[0] == 0  # position
        assert msg.data[1] == 0x00
        value = struct.unpack_from("<f", msg.data, 2)[0]
        assert value == pytest.approx(90.0)
        assert msg.data[6] == 0x00
        assert msg.data[7] == 0x00

    def test_encode_target_velocity(self):
        msg = self.drv.encode_target(ControlMode.VELOCITY, -100.5)
        assert msg.arbitration_id == 0x001
        assert msg.data[0] == 1  # velocity
        value = struct.unpack_from("<f", msg.data, 2)[0]
        assert value == pytest.approx(-100.5)

    def test_encode_target_duty(self):
        msg = self.drv.encode_target(ControlMode.DUTY, 0.75)
        assert msg.arbitration_id == 0x001
        assert msg.data[0] == 2  # duty
        value = struct.unpack_from("<f", msg.data, 2)[0]
        assert value == pytest.approx(0.75)


class TestDecodeFeedback:
    def setup_method(self):
        self.drv = GenericDriver("test_motor", 0x01)

    def test_decode_feedback(self):
        data = bytearray(8)
        struct.pack_into("<h", data, 0, 1800)  # 180.0 deg
        struct.pack_into("<h", data, 2, 300)  # 300 rpm
        struct.pack_into("<h", data, 4, 1500)  # 1500 mA
        data[6] = 45  # 45℃
        data[7] = 0x00

        msg = can.Message(arbitration_id=0x101, data=bytes(data), is_extended_id=False)
        state = self.drv.decode_feedback(msg)

        assert state.position == pytest.approx(180.0)
        assert state.velocity == pytest.approx(300.0)
        assert state.current == pytest.approx(1500.0)
        assert state.temperature == pytest.approx(45.0)
        assert state.reached is False

    def test_decode_feedback_with_flags(self):
        data = bytearray(8)
        struct.pack_into("<h", data, 0, 0)
        struct.pack_into("<h", data, 2, 0)
        struct.pack_into("<h", data, 4, 0)
        data[6] = 80
        data[7] = 0b00000001  # reached=True

        msg = can.Message(arbitration_id=0x101, data=bytes(data), is_extended_id=False)
        state = self.drv.decode_feedback(msg)
        assert state.reached is True

        data[7] = 0b00000101  # reached=True, overheat=True
        msg = can.Message(arbitration_id=0x101, data=bytes(data), is_extended_id=False)
        state = self.drv.decode_feedback(msg)
        assert state.reached is True


class TestMatchesFeedback:
    def setup_method(self):
        self.drv = GenericDriver("test_motor", 0x01)

    def test_matches_feedback(self):
        msg = can.Message(arbitration_id=0x101, data=bytes(8), is_extended_id=False)
        assert self.drv.matches_feedback(msg) is True

    def test_matches_feedback_wrong_device(self):
        msg = can.Message(arbitration_id=0x102, data=bytes(8), is_extended_id=False)
        assert self.drv.matches_feedback(msg) is False

        msg_target = can.Message(arbitration_id=0x001, data=bytes(8), is_extended_id=False)
        assert self.drv.matches_feedback(msg_target) is False


class TestEncodeEStop:
    def test_encode_e_stop(self):
        msg = GenericDriver.encode_e_stop()
        assert msg.arbitration_id == 0x7FF
        assert msg.is_extended_id is False
        assert msg.data == bytes(8)


class TestEncodeSetMode:
    def setup_method(self):
        self.drv = GenericDriver("test_motor", 0x01)

    def test_encode_set_mode(self):
        msg = self.drv.encode_set_mode(ControlMode.POSITION)
        assert msg.arbitration_id == GenericDriver.build_can_id(CommandType.SET_MODE, 0x01)
        assert msg.data[0] == 0
        assert msg.data[1:] == bytes(7)

        msg = self.drv.encode_set_mode(ControlMode.VELOCITY)
        assert msg.data[0] == 1

        msg = self.drv.encode_set_mode(ControlMode.DUTY)
        assert msg.data[0] == 2


class TestHealth:
    """ヘルスチェック判定 (Phase 6 段階②)。"""

    def setup_method(self):
        self.drv = GenericDriver("test_motor", 0x01)

    def _feed(self, *, temp: int = 25, flags: int = 0x00) -> None:
        data = bytearray(8)
        struct.pack_into("<h", data, 0, 0)
        struct.pack_into("<h", data, 2, 0)
        struct.pack_into("<h", data, 4, 0)
        data[6] = temp
        data[7] = flags
        msg = can.Message(arbitration_id=0x101, data=bytes(data), is_extended_id=False)
        self.drv.update_state(msg)

    def test_initial_flags_are_clear(self):
        # 初期化直後はどのフラグも立っていない
        assert self.drv.has_overcurrent_warning() is False
        assert self.drv.is_fault() is False

    def test_thermal_warning_via_temperature_byte(self):
        self._feed(temp=70, flags=0x00)
        assert self.drv.has_thermal_warning(temp_warning_c=65.0) is True

    def test_overcurrent_flag_bit1(self):
        # bit1 = 過電流警告
        self._feed(flags=0b00000010)
        assert self.drv.has_overcurrent_warning() is True
        assert self.drv.is_fault() is False

    def test_overheat_flag_bit2_is_fault(self):
        # bit2 = 過熱 (FAULT 扱い)
        self._feed(flags=0b00000100)
        assert self.drv.is_fault() is True
        assert self.drv.has_overcurrent_warning() is False

    def test_combined_flags_reached_overcurrent_overheat(self):
        # bit0 (到達) + bit1 (過電流) + bit2 (過熱) 同時セット
        self._feed(flags=0b00000111)
        assert self.drv.state.reached is True
        assert self.drv.has_overcurrent_warning() is True
        assert self.drv.is_fault() is True

    def test_flags_clear_on_recovery(self):
        # 一度立ったフラグが新しいフレームで降りることを確認 (復帰挙動)
        self._feed(flags=0b00000110)
        assert self.drv.has_overcurrent_warning() is True
        assert self.drv.is_fault() is True

        self._feed(flags=0b00000000)
        assert self.drv.has_overcurrent_warning() is False
        assert self.drv.is_fault() is False

    def test_reserved_high_bits_ignored(self):
        # bit6-7 は予約。ヘルス判定に影響してはならない (仕様書 §3.2)
        self._feed(flags=0b11000000)
        assert self.drv.has_overcurrent_warning() is False
        assert self.drv.is_fault() is False


class TestMotorCheck:
    """アクチュエータ動作確認 API (Phase 6 段階⑦)。"""

    def _feed(
        self,
        drv: GenericDriver,
        *,
        position_dg: int = 0,
        velocity_rpm: int = 0,
        current_ma: int = 0,
        temp: int = 25,
        flags: int = 0x00,
    ) -> None:
        # フィードバック byte0-1 は 0.1deg 単位 (raw_pos * 0.1 = position)
        data = bytearray(8)
        struct.pack_into("<h", data, 0, position_dg)
        struct.pack_into("<h", data, 2, velocity_rpm)
        struct.pack_into("<h", data, 4, current_ma)
        data[6] = temp
        data[7] = flags
        msg = can.Message(arbitration_id=0x101, data=bytes(data), is_extended_id=False)
        drv.update_state(msg)

    def test_check_command_default_position(self):
        drv = GenericDriver("test_motor", 0x01)
        msg, context = drv.check_command(magnitude=0.1)
        # control_type デフォルトは POSITION
        assert msg.data[0] == 0  # position
        value = struct.unpack_from("<f", msg.data, 2)[0]
        assert value == pytest.approx(0.1)
        assert context.target == pytest.approx(0.1)
        assert context.mode is ControlMode.POSITION

    def test_check_command_velocity_mode(self):
        drv = GenericDriver("test_motor", 0x01, control_type=ControlMode.VELOCITY)
        msg, context = drv.check_command(magnitude=50.0)
        assert msg.data[0] == 1  # velocity
        value = struct.unpack_from("<f", msg.data, 2)[0]
        assert value == pytest.approx(50.0)
        assert context.mode is ControlMode.VELOCITY

    def test_check_command_duty_mode(self):
        drv = GenericDriver("test_motor", 0x01, control_type=ControlMode.DUTY)
        msg, _context = drv.check_command(magnitude=0.3)
        assert msg.data[0] == 2  # duty
        value = struct.unpack_from("<f", msg.data, 2)[0]
        assert value == pytest.approx(0.3)

    def test_evaluate_position_passed_when_reached_and_within_tolerance(self):
        drv = GenericDriver("test_motor", 0x01)
        _, context = drv.check_command(magnitude=10.0)
        # position=10.0deg, reached フラグ立ち上がり
        self._feed(drv, position_dg=100, flags=0x01)
        passed, detail = drv.evaluate_check_result(context)
        assert passed is True
        assert detail is None

    def test_evaluate_position_failed_when_not_reached(self):
        drv = GenericDriver("test_motor", 0x01)
        _, context = drv.check_command(magnitude=10.0)
        # 目標 10.0deg に対して 5.0deg しか動いていない (許容 1.0 超え)
        self._feed(drv, position_dg=50, flags=0x00)
        passed, detail = drv.evaluate_check_result(context)
        assert passed is False
        assert detail is not None

    def test_evaluate_velocity_passed(self):
        drv = GenericDriver("test_motor", 0x01, control_type=ControlMode.VELOCITY)
        _, context = drv.check_command(magnitude=100.0)
        # velocity=100rpm (許容 5)
        self._feed(drv, velocity_rpm=98)
        passed, detail = drv.evaluate_check_result(context)
        assert passed is True
        assert detail is None

    def test_evaluate_velocity_failed(self):
        drv = GenericDriver("test_motor", 0x01, control_type=ControlMode.VELOCITY)
        _, context = drv.check_command(magnitude=100.0)
        self._feed(drv, velocity_rpm=20)
        passed, detail = drv.evaluate_check_result(context)
        assert passed is False
        assert detail is not None

    def test_evaluate_duty_passed_when_rotation_detected(self):
        drv = GenericDriver("test_motor", 0x01, control_type=ControlMode.DUTY)
        _, context = drv.check_command(magnitude=0.3)
        # 何らかの回転が観測されれば PASSED (|velocity| > 10)
        self._feed(drv, velocity_rpm=50)
        passed, _ = drv.evaluate_check_result(context)
        assert passed is True

    def test_evaluate_duty_failed_when_no_rotation(self):
        drv = GenericDriver("test_motor", 0x01, control_type=ControlMode.DUTY)
        _, context = drv.check_command(magnitude=0.3)
        self._feed(drv, velocity_rpm=2)
        passed, detail = drv.evaluate_check_result(context)
        assert passed is False
        assert detail is not None

    def test_evaluate_passed_with_overcurrent_flag_adds_detail(self):
        drv = GenericDriver("test_motor", 0x01)
        _, context = drv.check_command(magnitude=10.0)
        # 過電流フラグつき + 目標到達 → PASSED だが detail に注釈
        self._feed(drv, position_dg=100, flags=0b00000011)
        passed, detail = drv.evaluate_check_result(context)
        assert passed is True
        assert detail is not None
        assert "過電流" in detail

    def test_reset_after_check_sends_zero(self):
        drv = GenericDriver("test_motor", 0x01)
        msg = drv.reset_after_check()
        assert msg.data[0] == 0  # POSITION
        value = struct.unpack_from("<f", msg.data, 2)[0]
        assert value == pytest.approx(0.0)

    def test_reset_after_check_velocity_mode_sends_zero(self):
        drv = GenericDriver("test_motor", 0x01, control_type=ControlMode.VELOCITY)
        msg = drv.reset_after_check()
        assert msg.data[0] == 1  # VELOCITY
        value = struct.unpack_from("<f", msg.data, 2)[0]
        assert value == pytest.approx(0.0)


class TestMatchesFeedbackRobustness:
    """受信ループを殺さないための頑健性 (仕様書 §2.1)。"""

    def setup_method(self):
        self.drv = GenericDriver("test_motor", 0x01)

    @pytest.mark.parametrize("command_type", [0b100, 0b101, 0b110])
    def test_reserved_command_type_returns_false(self, command_type: int):
        # 予約コマンド種別は CommandType enum に無いが、例外を投げてはならない
        msg = can.Message(
            arbitration_id=(command_type << 8) | 0x01,
            data=bytes(8),
            is_extended_id=False,
        )
        assert self.drv.matches_feedback(msg) is False

    def test_extended_frame_returns_false(self):
        # 同一バスに Extended Frame の他プロトコルが相乗りしても壊れないこと
        msg = can.Message(arbitration_id=0x101, data=bytes(8), is_extended_id=True)
        assert self.drv.matches_feedback(msg) is False

    def test_try_parse_can_id_returns_none_for_reserved(self):
        assert GenericDriver.try_parse_can_id((0b101 << 8) | 0x01) is None
        assert GenericDriver.try_parse_can_id(0x101) == (CommandType.FEEDBACK, 0x01)

    def test_parse_can_id_still_raises_for_reserved(self):
        # 後方互換: 明示的に解析する経路では従来どおり例外を投げる
        with pytest.raises(ValueError):
            GenericDriver.parse_can_id((0b100 << 8) | 0x01)


class TestEncodeEStopClear:
    """緊急停止の解除フレーム (仕様書 §3.5)。"""

    def test_encode_e_stop_clear_broadcast(self):
        msg = GenericDriver.encode_e_stop_clear()
        assert msg.arbitration_id == 0x7FF
        assert msg.is_extended_id is False
        assert msg.data[0] == 0x01
        assert msg.data[1] == 0x5A
        assert msg.data[2] == 0xA5
        assert msg.data[3:] == bytes(5)

    def test_encode_e_stop_clear_specific_device(self):
        msg = GenericDriver.encode_e_stop_clear(0x03)
        assert msg.arbitration_id == GenericDriver.build_can_id(CommandType.E_STOP, 0x03)
        assert msg.data[0] == 0x01

    def test_encode_e_stop_is_unchanged(self):
        # 停止フレームは Byte0=0x00 のまま (仕様書 §3.5)
        msg = GenericDriver.encode_e_stop()
        assert msg.arbitration_id == 0x7FF
        assert msg.data == bytes(8)


class TestActivationSteps:
    def test_activation_steps_sends_e_stop_clear_to_own_device(self):
        drv = GenericDriver("test_motor", 0x05)
        steps = drv.activation_steps()

        assert len(steps) == 1
        msg, delay = steps[0]
        assert msg.arbitration_id == GenericDriver.build_can_id(CommandType.E_STOP, 0x05)
        assert msg.data[0] == 0x01
        assert msg.data[1] == 0x5A
        assert msg.data[2] == 0xA5
        assert delay == pytest.approx(0.0)

    def test_activation_does_not_require_fresh_feedback(self):
        # 解除フレームは目標値を変えず、ファームは解除後 target=0 から始める
        drv = GenericDriver("test_motor", 0x05)
        assert drv.requires_fresh_feedback_for_activation() is False


class TestStatusFlagsBit3To5:
    """FEEDBACK Byte7 bit3-5 (仕様書 §3.2)。"""

    def setup_method(self):
        self.drv = GenericDriver("test_motor", 0x01)

    def _feed(self, *, flags: int) -> None:
        data = bytearray(8)
        data[6] = 25
        data[7] = flags
        msg = can.Message(arbitration_id=0x101, data=bytes(data), is_extended_id=False)
        self.drv.update_state(msg)

    def test_initial_flags_are_clear(self):
        assert self.drv.e_stop_active is False
        assert self.drv.watchdog_active is False
        assert self.drv.device_id_unconfigured is False

    def test_e_stop_flag_bit3(self):
        self._feed(flags=0b00001000)
        assert self.drv.e_stop_active is True
        # 緊急停止中は異常ではないので FAULT にはしない
        assert self.drv.is_fault() is False
        assert "緊急停止" in (self.drv.check_safety_error() or "")

    def test_watchdog_flag_bit4(self):
        self._feed(flags=0b00010000)
        assert self.drv.watchdog_active is True
        assert self.drv.is_fault() is False
        assert "ウォッチドッグ" in (self.drv.check_safety_error() or "")

    def test_unconfigured_device_id_bit5_is_fault(self):
        self._feed(flags=0b00100000)
        assert self.drv.device_id_unconfigured is True
        assert self.drv.is_fault() is True
        assert "デバイス ID" in (self.drv.check_safety_error() or "")

    def test_no_safety_error_when_flags_clear(self):
        self._feed(flags=0b00000001)
        assert self.drv.check_safety_error() is None

    def test_flags_clear_on_recovery(self):
        self._feed(flags=0b00111000)
        assert self.drv.is_fault() is True

        self._feed(flags=0b00000000)
        assert self.drv.e_stop_active is False
        assert self.drv.watchdog_active is False
        assert self.drv.device_id_unconfigured is False
        assert self.drv.is_fault() is False
        assert self.drv.check_safety_error() is None
