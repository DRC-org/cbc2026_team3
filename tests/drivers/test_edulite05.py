from __future__ import annotations

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
from tests.feedback_frames import edulite_feedback


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


def test_mit_frame_clamps_command_and_uses_big_endian_words() -> None:
    driver = Edulite05Driver("m1", can_id=5)
    msg = driver.encode_mit(99.0, -99.0, 999.0, -1.0, 99.0)

    assert msg.arbitration_id == driver.build_can_id(driver.COMM_TYPE_MIT, 65535, 5)
    assert msg.data == struct.pack(">HHHH", 65535, 0, 65535, 0)


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


def test_decode_rejects_unrelated_frame() -> None:
    driver = Edulite05Driver("m1", can_id=5)
    with pytest.raises(ValueError):
        driver.decode_feedback(edulite_feedback(driver, host_id=0))


def test_check_uses_position_parameter_and_current_position() -> None:
    driver = Edulite05Driver("m1", can_id=5)
    driver.update_state(edulite_feedback(driver, position=0.5))
    target = driver.state.position + math.radians(5.0)

    msg, context = driver.check_command(magnitude=5.0)

    assert msg.data == struct.pack("<Hxxf", driver.PARAM_LOC_REF, target)
    assert context.mode is ControlMode.POSITION
    assert context.target == pytest.approx(target)
    # 指令直前の実測角を持たないと「既に目標角に居るのに動いた」と判定できてしまう
    assert context.reference == pytest.approx(driver.state.position)


def test_prepare_check_writes_hold_target_before_enable() -> None:
    """保持目標を書かずに励磁すると、動作確認の瞬間にアームが原点へ飛ぶ。"""
    driver = Edulite05Driver("m1", can_id=5)
    driver.update_state(edulite_feedback(driver, position=0.8))
    messages = messages_of(driver.prepare_check_steps())
    comm_types = [driver.parse_can_id(msg.arbitration_id)[0] for msg in messages]

    enable_index = comm_types.index(driver.COMM_TYPE_ENABLE)
    hold_index = next(
        i
        for i, msg in enumerate(messages)
        if comm_types[i] == driver.COMM_TYPE_WRITE_PARAM
        and struct.unpack("<H", msg.data[:2])[0] == driver.PARAM_LOC_REF
    )

    assert hold_index < enable_index
    # 実測角は 16bit 量子化を経た値なので、書き戻す保持目標も厳密一致はしない
    assert struct.unpack("<f", messages[hold_index].data[4:])[0] == pytest.approx(0.8, abs=0.01)
    assert comm_types == [4, 18, 18, 18, 18, 18, 3]
    assert [delay for _message, delay in driver.prepare_check_steps()] == [
        0.05,
        0.05,
        0.05,
        0.05,
        0.05,
        0.05,
        0.1,
    ]
    assert messages[0].data == bytes(8)


def test_prepare_check_is_initialization_plus_activation() -> None:
    """励磁手順が動作確認経路に複製されると、片方だけ直すたびに S6 が再発する。"""
    driver = Edulite05Driver("m1", can_id=5)
    driver.update_state(edulite_feedback(driver, position=0.3))

    assert steps_as_frames(driver.prepare_check_steps()) == steps_as_frames(
        driver.initialization_steps() + driver.activation_steps()
    )


def test_prepare_check_after_set_zero_holds_new_origin() -> None:
    """set_zero で原点が付け替わった後の保持目標は、付け替え前の実測角であってはならない。"""
    driver = Edulite05Driver("m1", can_id=5, set_zero_on_start=True)
    driver.update_state(edulite_feedback(driver, position=0.8))

    messages = messages_of(driver.prepare_check_steps())
    comm_types = [driver.parse_can_id(msg.arbitration_id)[0] for msg in messages]

    set_zero_index = comm_types.index(driver.COMM_TYPE_SET_ZERO)
    hold_index = next(
        i
        for i, msg in enumerate(messages)
        if comm_types[i] == driver.COMM_TYPE_WRITE_PARAM
        and struct.unpack("<H", msg.data[:2])[0] == driver.PARAM_LOC_REF
    )
    enable_index = comm_types.index(driver.COMM_TYPE_ENABLE)

    assert set_zero_index < hold_index < enable_index
    assert messages[hold_index].data == struct.pack("<Hxxf", driver.PARAM_LOC_REF, 0.0)


def test_prepare_check_keeps_configured_run_mode() -> None:
    """動作確認が run_mode を書き換えると、以後その機体の速度指令が効かなくなる。"""
    driver = Edulite05Driver("m1", can_id=5, mode="velocity")
    driver.update_state(edulite_feedback(driver, velocity=3.0))

    messages = messages_of(driver.prepare_check_steps())
    run_mode_frames = [
        msg
        for msg in messages
        if driver.parse_can_id(msg.arbitration_id)[0] == driver.COMM_TYPE_WRITE_PARAM
        and struct.unpack("<H", msg.data[:2])[0] == driver.PARAM_RUN_MODE
    ]

    assert [msg.data for msg in run_mode_frames] == [
        struct.pack("<HxxBxxx", driver.PARAM_RUN_MODE, int(Edulite05RunMode.VELOCITY))
    ]
    # run_mode を書き換えないので、復帰は無励磁化の 1 フレームだけで足りる
    reset = driver.reset_after_check()
    assert driver.parse_can_id(reset.arbitration_id)[0] == driver.COMM_TYPE_DISABLE


def test_check_command_and_evaluation_follow_configured_mode() -> None:
    driver = Edulite05Driver("m1", can_id=5, mode="velocity", limit_speed=2.0)
    driver.update_state(edulite_feedback(driver, velocity=0.0))

    msg, context = driver.check_command(magnitude=5.0)
    expected = 5.0 * 2.0 * math.pi / 60.0

    assert struct.unpack("<H", msg.data[:2])[0] == driver.PARAM_SPD_REF
    assert struct.unpack("<f", msg.data[4:])[0] == pytest.approx(expected)
    assert context.mode is ControlMode.VELOCITY
    assert context.target == pytest.approx(expected)

    driver.update_state(edulite_feedback(driver, velocity=expected))
    passed, _detail = driver.evaluate_check_result(context)
    assert passed is True

    driver.update_state(edulite_feedback(driver, velocity=-expected))
    failed, detail = driver.evaluate_check_result(context)
    assert failed is False
    assert "rpm" in detail


def test_check_safety_error_rejects_current_mode() -> None:
    """電流指令[A]とフィードバックのトルク[Nm]は次元が違い、合否を判定できない。"""
    driver = Edulite05Driver("m1", can_id=5, mode="current")
    assert driver.check_safety_error() is not None


def test_check_safety_rejects_known_fault_and_overtemperature() -> None:
    driver = Edulite05Driver("m1", can_id=5)
    driver.update_state(edulite_feedback(driver, fault_bits=int(Edulite05Fault.UNDERVOLTAGE)))
    assert "fault=0x01" in driver.check_safety_error()

    driver.update_state(edulite_feedback(driver, temperature=60.0))
    assert "過温" in driver.check_safety_error()


def test_emergency_stop_uses_extended_disable_without_fault_clear() -> None:
    driver = Edulite05Driver("m1", can_id=5)
    message = driver.emergency_stop_message()
    assert message.is_extended_id is True
    assert driver.parse_can_id(message.arbitration_id)[0] == driver.COMM_TYPE_DISABLE
    assert message.data == bytes(8)


def test_evaluate_check_result_and_reset() -> None:
    driver = Edulite05Driver("m1", can_id=5)
    driver.update_state(edulite_feedback(driver, position=0.0))
    _msg, context = driver.check_command(magnitude=5.0)
    driver.update_state(edulite_feedback(driver, position=math.radians(4.5)))
    passed, detail = driver.evaluate_check_result(context)

    assert passed is True
    assert detail is None
    reset = driver.reset_after_check()
    assert reset.data == bytes(8)
    assert driver.parse_can_id(reset.arbitration_id)[0] == driver.COMM_TYPE_DISABLE


def test_activation_writes_current_position_before_enable() -> None:
    """enable 直前に現在角を目標へ書かないと、有効化した瞬間に原点へ飛ぶ。"""
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
    """起動フレームだけで enable すると現在角を書く前に励磁されてしまう。"""
    driver = Edulite05Driver("m1", can_id=5, set_zero_on_start=True)
    comm_types = [
        driver.parse_can_id(msg.arbitration_id)[0]
        for msg in messages_of(driver.initialization_steps())
    ]
    assert driver.COMM_TYPE_ENABLE not in comm_types


def test_feedback_probe_is_disable_without_fault_clear() -> None:
    """フィードバックを引き出す問い合わせは、無励磁を保つフレームでなければならない。"""
    driver = Edulite05Driver("m1", can_id=5)
    probe = driver.feedback_probe_message()

    assert probe is not None
    assert driver.parse_can_id(probe.arbitration_id)[0] == driver.COMM_TYPE_DISABLE
    assert probe.data == bytes(8)
