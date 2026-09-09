from __future__ import annotations

import logging
import math
import struct

import can
import pytest

from lib.drivers.base import ControlMode, MotorState
from lib.drivers.edulite05 import (
    Edulite05Driver,
    Edulite05Fault,
    Edulite05RunMode,
)
from tests.feedback_frames import edulite_feedback, edulite_read_param_response, feed_edulite


def messages_of(steps: list[tuple[can.Message, float]]) -> list[can.Message]:
    return [message for message, _delay in steps]


def steps_as_frames(
    steps: list[tuple[can.Message, float]],
) -> list[tuple[int, bytes, float]]:
    return [(msg.arbitration_id, bytes(msg.data), delay) for msg, delay in steps]


def test_protocol_ranges_and_default_host_id() -> None:
    driver = Edulite05Driver("m1", can_id=5)

    assert driver.host_id == 0xFD
    assert (driver.POS_MIN, driver.POS_MAX) == (-12.57, 12.57)
    assert (driver.VEL_MIN, driver.VEL_MAX) == (-50.0, 50.0)
    assert (driver.TORQUE_MIN, driver.TORQUE_MAX) == (-6.0, 6.0)


def test_float_encoding_clamps_out_of_range_values() -> None:
    assert Edulite05Driver.float_to_uint16(-7.0, -6.0, 6.0) == 0
    assert Edulite05Driver.float_to_uint16(7.0, -6.0, 6.0) == 65535


def test_enable_disable_and_zero_use_host_id_in_can_id() -> None:
    driver = Edulite05Driver("m1", can_id=5, host_id=0xFD)

    enable = driver.encode_enable()
    disable = driver.encode_disable()
    zero = driver.encode_set_zero()

    assert enable.arbitration_id == driver.build_can_id(driver.COMM_TYPE_ENABLE, 0xFD, 5)
    assert disable.arbitration_id == driver.build_can_id(driver.COMM_TYPE_DISABLE, 0xFD, 5)
    assert zero.arbitration_id == driver.build_can_id(driver.COMM_TYPE_SET_ZERO, 0xFD, 5)
    assert enable.data == bytes(8)
    assert disable.data == bytes(8)
    assert zero.data == b"\x01" + bytes(7)
    assert enable.is_extended_id and disable.is_extended_id and zero.is_extended_id


def test_fault_clear_requires_explicit_request() -> None:
    driver = Edulite05Driver("m1", can_id=5)
    assert driver.encode_disable(clear_fault=True).data == b"\x01" + bytes(7)


def test_set_id_carries_the_new_id_above_the_host_id() -> None:
    driver = Edulite05Driver("m1", can_id=0x7F, host_id=0xFD)

    msg = driver.encode_set_id(0x01)

    assert msg.arbitration_id == driver.build_can_id(driver.COMM_TYPE_SET_ID, 0x01FD, 0x7F)
    assert msg.data == b"\x01" + bytes(7)
    assert msg.is_extended_id


def test_set_id_rejects_ids_that_do_not_fit_the_id_field() -> None:
    driver = Edulite05Driver("m1", can_id=0x7F)

    with pytest.raises(ValueError):
        driver.encode_set_id(0x100)
    with pytest.raises(ValueError):
        driver.encode_set_id(-1)


def test_set_id_is_not_part_of_any_automatic_startup_path() -> None:
    driver = Edulite05Driver("m1", can_id=5, set_zero_on_start=True)

    steps = driver.initialization_steps() + driver.activation_steps()
    comm_types = [driver.parse_can_id(msg.arbitration_id)[0] for msg in messages_of(steps)]

    assert driver.COMM_TYPE_SET_ID not in comm_types


def test_write_parameter_uses_little_endian_parameter_and_float() -> None:
    driver = Edulite05Driver("m1", can_id=5)
    msg = driver.encode_write_param_float(driver.PARAM_LOC_REF, 1.0)

    assert msg.arbitration_id == driver.build_can_id(driver.COMM_TYPE_WRITE_PARAM, 0xFD, 5)
    assert msg.data == struct.pack("<Hxxf", 0x7016, 1.0)


def test_run_mode_uses_u8_payload() -> None:
    driver = Edulite05Driver("m1", can_id=5)
    msg = driver.encode_run_mode(Edulite05RunMode.POSITION)
    assert msg.data == struct.pack("<HxxBxxx", driver.PARAM_RUN_MODE, 1)


@pytest.mark.parametrize(
    ("mode", "param_id"),
    [
        (ControlMode.POSITION, Edulite05Driver.PARAM_LOC_REF),
        (ControlMode.VELOCITY, Edulite05Driver.PARAM_SPD_REF),
        (ControlMode.CURRENT, Edulite05Driver.PARAM_IQ_REF),
    ],
)
def test_encode_target_maps_control_mode_to_parameter(mode: ControlMode, param_id: int) -> None:
    driver = Edulite05Driver("m1", can_id=5)
    msg = driver.encode_target(mode, 1.25)
    assert msg.data == struct.pack("<Hxxf", param_id, 1.25)


def test_encode_target_rejects_duty_mode() -> None:
    driver = Edulite05Driver("m1", can_id=5)
    with pytest.raises(ValueError):
        driver.encode_target(ControlMode.DUTY, 0.5)


@pytest.mark.parametrize(
    ("mode", "value", "expected"),
    [
        (ControlMode.POSITION, 99.0, Edulite05Driver.POS_MAX),
        (ControlMode.VELOCITY, -99.0, -2.0),
        (ControlMode.CURRENT, 99.0, 5.0),
    ],
)
def test_encode_target_clamps_to_configured_limits(
    mode: ControlMode, value: float, expected: float
) -> None:
    driver = Edulite05Driver("m1", can_id=5, limit_speed=2.0, limit_current=5.0)
    msg = driver.encode_target(mode, value)
    assert struct.unpack("<f", msg.data[4:])[0] == pytest.approx(expected)


def test_initialization_messages_apply_configuration_in_safe_order() -> None:
    driver = Edulite05Driver(
        "m1",
        can_id=5,
        mode="position",
        limit_speed=2.0,
        limit_current=5.0,
        position_kp=30.0,
        set_zero_on_start=True,
    )
    messages = messages_of(driver.initialization_steps())
    comm_types = [driver.parse_can_id(msg.arbitration_id)[0] for msg in messages]

    assert comm_types == [4, 18, 18, 18, 18, 6]
    assert messages[1].data == struct.pack("<HxxBxxx", driver.PARAM_RUN_MODE, 1)
    assert messages[2].data == struct.pack("<Hxxf", driver.PARAM_LIMIT_SPD, 2.0)
    assert messages[3].data == struct.pack("<Hxxf", driver.PARAM_LIMIT_CUR, 5.0)
    assert messages[4].data == struct.pack("<Hxxf", driver.PARAM_LOC_KP, 30.0)

    assert [delay for _message, delay in driver.initialization_steps()] == [
        0.05,
        0.05,
        0.05,
        0.05,
        0.05,
        0.2,
    ]


def test_initialization_does_not_set_zero_by_default() -> None:
    driver = Edulite05Driver("m1", can_id=5)
    comm_types = [
        driver.parse_can_id(msg.arbitration_id)[0]
        for msg in messages_of(driver.initialization_steps())
    ]
    assert driver.COMM_TYPE_SET_ZERO not in comm_types
    assert driver.COMM_TYPE_ENABLE not in comm_types


@pytest.mark.parametrize("limit_current", [-1.0, math.inf, math.nan])
def test_current_limit_rejects_negative_or_non_finite_values(limit_current: float) -> None:
    with pytest.raises(ValueError):
        Edulite05Driver("m1", can_id=5, limit_current=limit_current)


def test_current_limit_is_not_clamped_to_torque_range() -> None:
    driver = Edulite05Driver("m1", can_id=5, limit_current=12.0)
    assert driver.limit_current == 12.0


def test_feedback_decode_updates_status_and_faults() -> None:
    driver = Edulite05Driver("m1", can_id=5)
    msg = edulite_feedback(
        driver,
        position=1.0,
        velocity=2.0,
        torque=0.5,
        temperature=25.0,
        mode_state=2,
        fault_bits=int(Edulite05Fault.OVERCURRENT | Edulite05Fault.HALL),
    )

    state = driver.update_state(msg)

    assert isinstance(state, MotorState)
    assert state.position == pytest.approx(1.0, abs=0.01)
    assert state.velocity == pytest.approx(2.0, abs=0.01)
    assert state.current == pytest.approx(0.5, abs=0.01)
    assert state.temperature == pytest.approx(25.0)
    assert driver.mode_state == 2
    assert driver.fault_bits == Edulite05Fault.OVERCURRENT | Edulite05Fault.HALL
    assert driver.has_overcurrent_warning() is True
    assert driver.is_fault() is True


def test_torque_value_is_not_compared_with_current_limit() -> None:
    driver = Edulite05Driver("m1", can_id=5, limit_current=1.0)
    driver.update_state(edulite_feedback(driver, torque=5.0))
    assert driver.has_overcurrent_warning() is False


def test_matches_feedback_validates_frame_type_motor_and_host() -> None:
    driver = Edulite05Driver("m1", can_id=5)
    valid = edulite_feedback(driver)
    standard = can.Message(arbitration_id=0x205, data=valid.data, is_extended_id=False)
    wrong_host = edulite_feedback(driver, host_id=0)
    wrong_motor = Edulite05Driver("other", can_id=6)

    assert driver.matches_feedback(valid) is True
    assert driver.matches_feedback(standard) is False
    assert driver.matches_feedback(wrong_host) is False
    assert driver.matches_feedback(edulite_feedback(wrong_motor)) is False


def test_read_param_request_asks_the_motor_and_names_the_host() -> None:
    driver = Edulite05Driver("m1", can_id=5, host_id=0xFD)

    msg = driver.encode_read_param(driver.PARAM_LOC_KP)

    assert msg.arbitration_id == driver.build_can_id(driver.COMM_TYPE_READ_PARAM, 0xFD, 5)
    assert msg.data == struct.pack("<Hxxxxxx", driver.PARAM_LOC_KP)
    assert msg.is_extended_id


def test_read_param_response_matches_only_own_motor_and_host() -> None:
    driver = Edulite05Driver("m1", can_id=5, host_id=0xFD)
    payload = struct.pack("<f", 30.0)
    valid = edulite_read_param_response(driver, param_id=driver.PARAM_LOC_KP, payload=payload)
    other_motor = edulite_read_param_response(
        driver, param_id=driver.PARAM_LOC_KP, payload=payload, motor_id=6
    )
    other_host = edulite_read_param_response(
        driver, param_id=driver.PARAM_LOC_KP, payload=payload, host_id=0x00
    )
    standard = can.Message(arbitration_id=0x205, data=valid.data, is_extended_id=False)
    short = can.Message(arbitration_id=valid.arbitration_id, data=bytes(4), is_extended_id=True)

    assert driver.matches_read_param(valid) is True
    assert driver.matches_read_param(other_motor) is False
    assert driver.matches_read_param(other_host) is False
    assert driver.matches_read_param(standard) is False
    assert driver.matches_read_param(short) is False


def test_read_param_response_is_not_taken_for_feedback() -> None:
    driver = Edulite05Driver("m1", can_id=5)
    response = edulite_read_param_response(
        driver, param_id=driver.PARAM_LIMIT_SPD, payload=struct.pack("<f", 2.0)
    )

    assert driver.matches_feedback(response) is False


def test_decode_read_param_reads_float_parameters() -> None:
    driver = Edulite05Driver("m1", can_id=5)
    response = edulite_read_param_response(
        driver, param_id=driver.PARAM_LIMIT_SPD, payload=struct.pack("<f", 2.5)
    )

    assert driver.decode_read_param(response) == (driver.PARAM_LIMIT_SPD, pytest.approx(2.5))


def test_decode_read_param_reads_run_mode_as_uint8() -> None:
    driver = Edulite05Driver("m1", can_id=5)
    response = edulite_read_param_response(
        driver,
        param_id=driver.PARAM_RUN_MODE,
        payload=bytes([int(Edulite05RunMode.POSITION), 0, 0, 0]),
    )

    assert driver.decode_read_param(response) == (
        driver.PARAM_RUN_MODE,
        int(Edulite05RunMode.POSITION),
    )


def test_decode_read_param_rejects_another_motors_response() -> None:
    driver = Edulite05Driver("m1", can_id=5)
    response = edulite_read_param_response(
        driver, param_id=driver.PARAM_LOC_KP, payload=struct.pack("<f", 30.0), motor_id=6
    )

    with pytest.raises(ValueError):
        driver.decode_read_param(response)


def test_decode_rejects_unrelated_frame() -> None:
    driver = Edulite05Driver("m1", can_id=5)
    with pytest.raises(ValueError):
        driver.decode_feedback(edulite_feedback(driver, host_id=0))


def test_emergency_stop_uses_extended_disable_without_fault_clear() -> None:
    driver = Edulite05Driver("m1", can_id=5)
    message = driver.emergency_stop_message()
    assert message.is_extended_id is True
    assert driver.parse_can_id(message.arbitration_id)[0] == driver.COMM_TYPE_DISABLE
    assert message.data == bytes(8)


def test_activation_writes_current_position_before_enable() -> None:
    driver = Edulite05Driver("m1", can_id=5)
    driver.update_state(edulite_feedback(driver, position=0.8))
    current = driver.state.position

    steps = driver.activation_steps()
    comm_types = [driver.parse_can_id(msg.arbitration_id)[0] for msg, _delay in steps]

    assert comm_types == [driver.COMM_TYPE_WRITE_PARAM, driver.COMM_TYPE_ENABLE]
    assert steps[0][0].data == struct.pack("<Hxxf", driver.PARAM_LOC_REF, current)
    assert steps[1][0].data == bytes(8)


def test_activation_requires_fresh_feedback_only_in_position_mode() -> None:
    position = Edulite05Driver("m1", can_id=5, mode="position")
    velocity = Edulite05Driver("m2", can_id=6, mode="velocity")

    assert position.requires_fresh_feedback_for_activation() is True
    assert velocity.requires_fresh_feedback_for_activation() is False


def test_activation_in_velocity_mode_holds_zero_speed() -> None:
    driver = Edulite05Driver("m1", can_id=5, mode="velocity")
    driver.update_state(edulite_feedback(driver, velocity=3.0))

    steps = driver.activation_steps()

    assert steps[0][0].data == struct.pack("<Hxxf", driver.PARAM_SPD_REF, 0.0)
    assert driver.parse_can_id(steps[1][0].arbitration_id)[0] == driver.COMM_TYPE_ENABLE


def test_initialization_steps_never_contain_enable() -> None:
    driver = Edulite05Driver("m1", can_id=5, set_zero_on_start=True)
    comm_types = [
        driver.parse_can_id(msg.arbitration_id)[0]
        for msg in messages_of(driver.initialization_steps())
    ]
    assert driver.COMM_TYPE_ENABLE not in comm_types


def test_feedback_probe_is_disable_without_fault_clear() -> None:
    driver = Edulite05Driver("m1", can_id=5)
    probe = driver.feedback_probe_message()

    assert probe is not None
    assert driver.parse_can_id(probe.arbitration_id)[0] == driver.COMM_TYPE_DISABLE
    assert probe.data == bytes(8)


def target_value_of(msg: can.Message) -> float:
    return struct.unpack("<f", msg.data[4:])[0]


class TestPcSideOrigin:
    """原点をモータ側の `SET_ZERO` ではなく PC 側のオフセットで持つ。

    `SET_ZERO` の零点は電源断で失われるので、物理非常停止のたびに原点が
    フラッシュの機械ゼロへ戻る (`docs/invariants.md` §3)。
    """

    def test_控えるまでは論理位置が電文どおり(self) -> None:
        driver = Edulite05Driver("m1", can_id=5)
        feed_edulite(driver, position=1.25)

        assert driver.origin_offset == 0.0
        assert driver.feedback_position() == pytest.approx(driver.state.position)

    def test_控えた瞬間の論理位置は0で生値は動かない(self) -> None:
        driver = Edulite05Driver("m1", can_id=5)
        feed_edulite(driver, position=3.0)
        raw = driver.state.position

        driver.capture_origin_here()

        assert driver.feedback_position() == pytest.approx(0.0, abs=1e-9)
        assert driver.state.position == pytest.approx(raw)
        assert driver.origin_offset == pytest.approx(raw)

    def test_論理0の指令は控えた生角度を書く(self) -> None:
        driver = Edulite05Driver("m1", can_id=5)
        feed_edulite(driver, position=3.0)
        raw = driver.state.position
        driver.capture_origin_here()

        msg = driver.encode_target(ControlMode.POSITION, 0.0)

        assert struct.unpack_from("<H", msg.data)[0] == driver.PARAM_LOC_REF
        assert target_value_of(msg) == pytest.approx(raw, abs=1e-6)

    def test_頭打ちは生値のレンジで掛かる(self) -> None:
        """`POS_MIN`/`POS_MAX` は uint16 の写像レンジで、機構の可動域ではない。

        論理値でクランプすると、オフセットを足した生値がレンジ外へ出て折り返す。
        """
        driver = Edulite05Driver("m1", can_id=5)
        feed_edulite(driver, position=3.0)
        driver.capture_origin_here()

        msg = driver.encode_target(ControlMode.POSITION, 10.0)

        assert target_value_of(msg) == pytest.approx(driver.POS_MAX, abs=1e-5)

    def test_保持目標を往復させても今の生角度のまま(self) -> None:
        """20Hz の再送が `idle_target_value()` を `encode_target()` へ通す。

        生のまま返すと `生値 + オフセット` を書き続け、機構がオフセットぶん走る。
        """
        driver = Edulite05Driver("m1", can_id=5)
        feed_edulite(driver, position=2.0)
        driver.capture_origin_here()
        feed_edulite(driver, position=2.5)
        raw = driver.state.position

        msg = driver.encode_target(driver.mode, driver.idle_target_value())

        assert target_value_of(msg) == pytest.approx(raw, abs=1e-6)

    @pytest.mark.parametrize("after_set_zero", [False, True])
    def test_励磁の保持目標は今の生角度(self, after_set_zero: bool) -> None:
        driver = Edulite05Driver("m1", can_id=5)
        feed_edulite(driver, position=2.0)
        driver.capture_origin_here()
        feed_edulite(driver, position=2.5)
        raw = driver.state.position

        steps = driver.activation_steps(after_set_zero=after_set_zero)

        assert struct.unpack_from("<H", steps[0][0].data)[0] == driver.PARAM_LOC_REF
        assert target_value_of(steps[0][0]) == pytest.approx(raw, abs=1e-6)

    def test_モータ側で切り直した直後だけ0を書く(self) -> None:
        """PC 側の控えが無いなら、零点はまだモータ側にある。

        `SET_ZERO` は生座標そのものを付け替えるので、旧原点で測られた在庫の
        フィードバックを書くと enable した瞬間に新旧の原点差だけ機構が動く。
        """
        driver = Edulite05Driver("m1", can_id=5)
        feed_edulite(driver, position=1.5)

        steps = driver.activation_steps(after_set_zero=True)

        assert target_value_of(steps[0][0]) == pytest.approx(0.0, abs=1e-9)

    def test_控え直した原点の移動量をINFOで残す(self, caplog: pytest.LogCaptureFixture) -> None:
        """機械ゼロが電源断でどれだけ動いたかを知る唯一の材料になる。"""
        driver = Edulite05Driver("m1", can_id=5)
        feed_edulite(driver, position=1.0)
        driver.capture_origin_here()
        feed_edulite(driver, position=1.5)

        with caplog.at_level(logging.INFO, logger="lib.drivers.edulite05"):
            driver.capture_origin_here()

        assert [record.levelno for record in caplog.records] == [logging.INFO]
        record = caplog.records[0]
        assert "m1" in record.getMessage()
        assert any(
            isinstance(arg, float) and arg == pytest.approx(0.5, abs=0.01)
            for arg in record.args or ()
        )

    def test_頭打ちになった指令はWARNINGで残す(self, caplog: pytest.LogCaptureFixture) -> None:
        driver = Edulite05Driver("m1", can_id=5)
        feed_edulite(driver, position=3.0)
        driver.capture_origin_here()

        with caplog.at_level(logging.WARNING, logger="lib.drivers.edulite05"):
            driver.encode_target(ControlMode.POSITION, 10.0)

        assert [record.levelno for record in caplog.records] == [logging.WARNING]
        assert "m1" in caplog.records[0].getMessage()

    def test_端に張り付いている間は繰り返し出さない(self, caplog: pytest.LogCaptureFixture) -> None:
        """20Hz の再送で同じ指令が流れ続けるので、変化した瞬間だけ残す。"""
        driver = Edulite05Driver("m1", can_id=5)
        feed_edulite(driver, position=3.0)
        driver.capture_origin_here()

        with caplog.at_level(logging.WARNING, logger="lib.drivers.edulite05"):
            for _ in range(5):
                driver.encode_target(ControlMode.POSITION, 10.0)

        assert len(caplog.records) == 1


class TestIsEnergized:
    def test_未受信では判定しない(self) -> None:
        assert Edulite05Driver("m1", can_id=5).is_energized() is None

    def test_モータモードなら励磁されている(self) -> None:
        driver = Edulite05Driver("m1", can_id=5)
        driver.update_state(edulite_feedback(driver, mode_state=2))

        assert driver.is_energized() is True

    @pytest.mark.parametrize("mode_state", [0, 1])
    def test_リセット中と校正中は無励磁(self, mode_state: int) -> None:
        driver = Edulite05Driver("m1", can_id=5)
        driver.update_state(edulite_feedback(driver, mode_state=mode_state))

        assert driver.is_energized() is False
