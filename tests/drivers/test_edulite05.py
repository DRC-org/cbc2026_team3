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

    assert comm_types == [4, 18, 18, 18, 18]
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
    ]


@pytest.mark.parametrize("set_zero_on_start", [False, True])
def test_initialization_never_sets_zero(set_zero_on_start: bool) -> None:
    """**どの設定でも 1 通も送らない。** 原点は PC 側のオフセットだけが持つ。

    `SET_ZERO` を混ぜると生座標そのものが付け替わり、PC 側のオフセットと二重定義に
    なる (`docs/invariants.md` §3)。
    """
    driver = Edulite05Driver("m1", can_id=5, set_zero_on_start=set_zero_on_start)
    comm_types = [
        driver.parse_can_id(msg.arbitration_id)[0]
        for msg in messages_of(driver.initialization_steps())
    ]
    assert driver.COMM_TYPE_SET_ZERO not in comm_types
    assert driver.COMM_TYPE_ENABLE not in comm_types


@pytest.mark.parametrize("set_zero_on_start", [False, True])
def test_initialization_steps_と_reinitialization_steps_は同一(set_zero_on_start: bool) -> None:
    """分けて書くと、片方だけ直された状態が作れる。"""
    driver = Edulite05Driver("m1", can_id=5, set_zero_on_start=set_zero_on_start)

    assert steps_as_frames(driver.initialization_steps()) == steps_as_frames(
        driver.reinitialization_steps()
    )


class TestReinitialization:
    """電源断で失われる設定は再励磁のたびに書き直す。

    `run_mode` / `limit_spd` / `limit_cur` / `loc_kp` はどれも `WRITE_PARAM`(0x12)
    で、マニュアルの type 18 は "lost after power failure"。物理非常停止は本機の
    電源を数秒落とすので、書き直さないと復帰した個体は位置モードですらない。
    """

    @staticmethod
    def _comm_types(driver: Edulite05Driver) -> list[int]:
        return [
            driver.parse_can_id(msg.arbitration_id)[0]
            for msg in messages_of(driver.reinitialization_steps())
        ]

    def test_無励磁化してから設定を書き直す(self) -> None:
        driver = Edulite05Driver(
            "m1",
            can_id=5,
            mode="position",
            limit_speed=2.0,
            limit_current=5.0,
            position_kp=30.0,
        )

        messages = messages_of(driver.reinitialization_steps())

        assert self._comm_types(driver) == [
            driver.COMM_TYPE_DISABLE,
            driver.COMM_TYPE_WRITE_PARAM,
            driver.COMM_TYPE_WRITE_PARAM,
            driver.COMM_TYPE_WRITE_PARAM,
            driver.COMM_TYPE_WRITE_PARAM,
        ]
        assert messages[1].data == struct.pack("<HxxBxxx", driver.PARAM_RUN_MODE, 1)
        assert messages[2].data == struct.pack("<Hxxf", driver.PARAM_LIMIT_SPD, 2.0)
        assert messages[3].data == struct.pack("<Hxxf", driver.PARAM_LIMIT_CUR, 5.0)
        assert messages[4].data == struct.pack("<Hxxf", driver.PARAM_LOC_KP, 30.0)

    def test_位置モード以外は位置ゲインを書かない(self) -> None:
        driver = Edulite05Driver("m1", can_id=5, mode="velocity")

        params = [
            struct.unpack_from("<H", msg.data)[0]
            for msg in messages_of(driver.reinitialization_steps())[1:]
        ]

        assert driver.PARAM_LOC_KP not in params
        assert driver.PARAM_RUN_MODE in params

    def test_SET_ZEROを含めない(self) -> None:
        """再励磁のたびに送ると、零点確定で合わせた原点をその場の姿勢へ書き換える。"""
        driver = Edulite05Driver("m1", can_id=5, set_zero_on_start=True)

        assert driver.COMM_TYPE_SET_ZERO not in self._comm_types(driver)
        assert driver.COMM_TYPE_ENABLE not in self._comm_types(driver)


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

    def test_原点を控えていなくても実測角を書く(self) -> None:
        """生座標は原点を控え直しても動かないので、`after_set_zero` で分岐しない。

        分岐を残すと、控える前と後で保持目標の意味が変わる経路が 1 本増える。
        """
        driver = Edulite05Driver("m1", can_id=5)
        feed_edulite(driver, position=1.5)
        raw = driver.state.position

        steps = driver.activation_steps(after_set_zero=True)

        assert target_value_of(steps[0][0]) == pytest.approx(raw, abs=1e-6)

    def test_控え直した原点の移動量をINFOで残す(self, caplog: pytest.LogCaptureFixture) -> None:
        """機械ゼロが電源断でどれだけ動いたかを知る唯一の材料になる。"""
        driver = Edulite05Driver("m1", can_id=5)
        feed_edulite(driver, position=1.0)
        driver.capture_origin_here()
        feed_edulite(driver, position=1.5)

        with caplog.at_level(logging.INFO, logger="lib.drivers.edulite05"):
            caplog.clear()
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


_POS_LSB = (Edulite05Driver.POS_MAX - Edulite05Driver.POS_MIN) / 65535.0


class TestRawAngleUnwrap:
    """電源投入で [0, 360) へ畳まれる電文値を、PC 側で連続化して読む。

    畳まれた側と畳まれない側が対になっている `rotate` では、畳みが左右へ
    360deg の差を作り `SyncMonitor` が緊急停止を掛け直す
    (`docs/invariants.md` §2 / `docs/history/incidents.md` 2026-09-09)。
    """

    def test_正回りの折り返しは進んだぶんだけ進む(self) -> None:
        driver = Edulite05Driver("m1", can_id=5)
        feed_edulite(driver, position=math.radians(179.0))
        feed_edulite(driver, position=math.radians(-179.0))

        assert driver.feedback_position() == pytest.approx(math.radians(181.0), abs=2 * _POS_LSB)

    def test_逆回りの折り返しも戻ったぶんだけ戻る(self) -> None:
        driver = Edulite05Driver("m1", can_id=5)
        feed_edulite(driver, position=math.radians(-179.0))
        feed_edulite(driver, position=math.radians(179.0))

        assert driver.feedback_position() == pytest.approx(math.radians(-181.0), abs=2 * _POS_LSB)

    def test_電源断で畳まれても動いていないと読む(self) -> None:
        """2026-09-09 の実測値。`rotate_l` の電文値が +360deg 弱だけ飛んだ。

        機構は 1LSB も動いていないので、論理位置も動いてはならない。
        """
        driver = Edulite05Driver("m1", can_id=5)
        feed_edulite(driver, position=math.radians(-181.7142))
        before = driver.feedback_position()

        feed_edulite(driver, position=math.radians(178.2634))

        # 実測の差 1LSB に、電文へ載せるときの量子化 1LSB が重なる。
        assert driver.feedback_position() - before == pytest.approx(0.0, abs=2 * _POS_LSB)

    def test_畳まれた後の指令は今の電文座標に続く値を書く(self) -> None:
        """読み側だけ直すと、指令が 360deg の移動になって軸が 1 回転する。"""
        driver = Edulite05Driver("m1", can_id=5)
        feed_edulite(driver, position=math.radians(-181.7142))
        driver.capture_origin_here()
        feed_edulite(driver, position=math.radians(178.2634))
        raw = driver.state.position

        msg = driver.encode_target(ControlMode.POSITION, 0.0)

        assert target_value_of(msg) == pytest.approx(raw, abs=2 * _POS_LSB)

    def test_保持目標を往復させても畳まれる前の座標へ戻らない(self) -> None:
        driver = Edulite05Driver("m1", can_id=5)
        feed_edulite(driver, position=math.radians(-181.7142))
        driver.capture_origin_here()
        feed_edulite(driver, position=math.radians(178.2634))
        raw = driver.state.position

        msg = driver.encode_target(driver.mode, driver.idle_target_value())

        assert target_value_of(msg) == pytest.approx(raw, abs=2 * _POS_LSB)

    def test_畳まれた後に控えた原点も連続化した座標で持つ(self) -> None:
        """原点を電文座標で控えると、控えた瞬間に回転数ぶんの論理位置が生える。"""
        driver = Edulite05Driver("m1", can_id=5)
        feed_edulite(driver, position=math.radians(-181.7142))
        feed_edulite(driver, position=math.radians(178.2634))
        raw = driver.state.position

        driver.capture_origin_here()

        assert driver.feedback_position() == pytest.approx(0.0, abs=1e-9)
        assert target_value_of(driver.encode_target(ControlMode.POSITION, 0.0)) == pytest.approx(
            raw, abs=1e-6
        )

    def test_最初のフレームは補正しない(self) -> None:
        """前回値が無いので、既定値 0 との差を折り返しと読んではならない。"""
        driver = Edulite05Driver("m1", can_id=5)
        feed_edulite(driver, position=math.radians(700.0))

        assert driver.feedback_position() == pytest.approx(driver.state.position)

    def test_電文の値は生のまま残す(self) -> None:
        """診断の生値カラムが読む。連続化した値で上書きしてはならない。"""
        driver = Edulite05Driver("m1", can_id=5)
        feed_edulite(driver, position=math.radians(179.0))

        state = driver.update_state(edulite_feedback(driver, position=math.radians(-179.0)))

        assert state.position == pytest.approx(math.radians(-179.0), abs=_POS_LSB)
        assert driver.state.position == pytest.approx(math.radians(-179.0), abs=_POS_LSB)


class TestProvisionalOrigin:
    """起動時の暫定原点。左右の機械ゼロ差 (実機 175.879deg) を消して機体を動かせる形にする。

    ここが効かないと起動直後から偏差が立ち、零点確定にたどり着く手前で詰まる。
    """

    def test_起動時に生角度を控える(self) -> None:
        driver = Edulite05Driver("m1", can_id=5, set_zero_on_start=True)
        feed_edulite(driver, position=1.5)
        raw = driver.state.position

        assert driver.establish_provisional_origin() is True
        assert driver.origin_offset == pytest.approx(raw)
        assert driver.feedback_position() == pytest.approx(0.0, abs=1e-9)

    def test_2度目は効かない(self) -> None:
        """**再励磁のたびに控え直すと、そのときの姿勢が新しい原点になる。**

        物理緊急停止からの復帰は再励磁を通るので、ここが効き続けると原点が
        毎回書き換わり、左右で別々の時刻に控えたぶんが消えない偏差として残る。
        """
        driver = Edulite05Driver("m1", can_id=5, set_zero_on_start=True)
        feed_edulite(driver, position=1.5)
        driver.establish_provisional_origin()
        offset = driver.origin_offset
        feed_edulite(driver, position=2.5)

        assert driver.establish_provisional_origin() is False
        assert driver.origin_offset == pytest.approx(offset)

    def test_零点確定の後は効かない(self) -> None:
        """スイッチで確定した正確な原点を、その後の再励磁が上書きしてはならない。"""
        driver = Edulite05Driver("m1", can_id=5, set_zero_on_start=True)
        feed_edulite(driver, position=1.5)
        driver.capture_origin_here()
        offset = driver.origin_offset
        feed_edulite(driver, position=2.5)

        assert driver.establish_provisional_origin() is False
        assert driver.origin_offset == pytest.approx(offset)

    def test_宣言していなければ1度も効かない(self) -> None:
        driver = Edulite05Driver("m1", can_id=5, set_zero_on_start=False)
        feed_edulite(driver, position=1.5)

        assert driver.establish_provisional_origin() is False
        assert driver.origin_offset == 0.0

    def test_位置モード以外では効かない(self) -> None:
        """論理座標を持つのは位置モードだけで、速度・電流に原点という量が無い。"""
        driver = Edulite05Driver("m1", can_id=5, mode="velocity", set_zero_on_start=True)
        feed_edulite(driver, position=1.5)

        assert driver.establish_provisional_origin() is False
        assert driver.origin_offset == 0.0


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


class TestTravelRangeUniquification:
    """軸の可動域が 1 回転未満なら、回転数は差分を見なくても一意に決まる。

    差分アンラップは「2 つの電文のあいだに軸が半回転以上動かない」ことに立っている。
    `rotate` の可動域はちょうど 0〜180deg で**半回転がその境界そのもの**なので、
    電源が落ちているあいだに人が端から端へ動かすと、量子化した差分が π の
    どちら側へ落ちるかで符号が反転する
    (`docs/invariants.md` §2 / `docs/history/incidents.md` 2026-09-10)。
    """

    #: 電源復帰後の最初の電文の差分を、量子化で確実に π より小さい側へ落とす
    MARGIN = math.degrees(2 * _POS_LSB)

    #: `config/main_hand_positions.yaml` の rotate。scale が負の側は整列で符号が入れ替わる
    R_TRAVEL = (0.0, math.pi)
    L_TRAVEL = (-math.pi, 0.0)

    #: 零点確定したときの電文値。論理 0 がここに乗る
    R_ORIGIN_DEG = 350.0
    L_ORIGIN_DEG = 10.0

    #: 電源断のあいだに人が軸を 180deg 動かした後の電文値 ([0, 360) へ畳まれて届く)
    R_AFTER_DEG = 170.0 + MARGIN
    L_AFTER_DEG = 190.0 - MARGIN

    def _homed(
        self,
        travel: tuple[float, float] | None,
        *,
        name: str = "rotate_r",
        origin_deg: float = R_ORIGIN_DEG,
    ) -> Edulite05Driver:
        driver = Edulite05Driver(name, can_id=0x11)
        if travel is not None:
            driver.set_travel_range(*travel)
        feed_edulite(driver, position=math.radians(origin_deg))
        driver.capture_origin_here()
        return driver

    def test_電源断のあいだに180deg動かされても正の側で読む(self) -> None:
        """**論理 -180deg と読むと、次の論理 0 の指令で逆向きに 180deg 回る。**

        可動域は 0〜180deg なので -180deg は可動域の外であり、+180deg しか有り得ない。
        """
        driver = self._homed(self.R_TRAVEL)

        feed_edulite(driver, position=math.radians(self.R_AFTER_DEG))

        assert driver.feedback_position() == pytest.approx(math.pi, abs=3 * _POS_LSB)

    def test_scaleが負の側も可動域のある側で読む(self) -> None:
        driver = self._homed(self.L_TRAVEL, name="rotate_l", origin_deg=self.L_ORIGIN_DEG)

        feed_edulite(driver, position=math.radians(self.L_AFTER_DEG))

        assert driver.feedback_position() == pytest.approx(-math.pi, abs=3 * _POS_LSB)

    def test_可動域が無ければ従来の差分アンラップのまま(self) -> None:
        driver = self._homed(None)

        feed_edulite(driver, position=math.radians(self.R_AFTER_DEG))

        assert driver.feedback_position() == pytest.approx(-math.pi, abs=3 * _POS_LSB)

    def test_暫定原点しかないあいだは一意化しない(self) -> None:
        """暫定原点はその場の姿勢を論理 0 にするだけで、機構原点とは無関係である。"""
        driver = Edulite05Driver("rotate_r", can_id=0x11, set_zero_on_start=True)
        driver.set_travel_range(*self.R_TRAVEL)
        feed_edulite(driver, position=math.radians(self.R_ORIGIN_DEG))
        assert driver.establish_provisional_origin() is True

        feed_edulite(driver, position=math.radians(self.R_AFTER_DEG))

        assert driver.feedback_position() == pytest.approx(-math.pi, abs=3 * _POS_LSB)

    def test_零点確定の後は一意化が効く(self) -> None:
        driver = Edulite05Driver("rotate_r", can_id=0x11, set_zero_on_start=True)
        driver.set_travel_range(*self.R_TRAVEL)
        feed_edulite(driver, position=math.radians(self.R_ORIGIN_DEG))
        driver.establish_provisional_origin()
        driver.capture_origin_here()

        feed_edulite(driver, position=math.radians(self.R_AFTER_DEG))

        assert driver.feedback_position() == pytest.approx(math.pi, abs=3 * _POS_LSB)

    def test_可動域が1回転以上なら一意化しない(self) -> None:
        """等価表現が 2 つ以上あるので、回転数は可動域からは決まらない。"""
        driver = self._homed((0.0, 3.0 * math.pi))

        feed_edulite(driver, position=math.radians(self.R_AFTER_DEG))

        assert driver.feedback_position() == pytest.approx(-math.pi, abs=3 * _POS_LSB)

    def test_可動域を少し外れた姿勢はそのまま読む(self) -> None:
        """オーバーシュートを -175deg と読むと、指令が可動域の逆端を向く。"""
        driver = self._homed(self.R_TRAVEL)

        feed_edulite(driver, position=math.radians(self.R_ORIGIN_DEG + 185.0 - 360.0))

        assert driver.feedback_position() == pytest.approx(math.radians(185.0), abs=3 * _POS_LSB)

    def test_可動域の外はWARNINGで残す(self, caplog: pytest.LogCaptureFixture) -> None:
        driver = self._homed(self.R_TRAVEL)

        with caplog.at_level(logging.WARNING, logger="lib.drivers.edulite05"):
            feed_edulite(driver, position=math.radians(self.R_ORIGIN_DEG + 185.0 - 360.0))

        assert [record.levelname for record in caplog.records] == ["WARNING"]
        assert "可動域" in caplog.records[0].message

    def test_可動域の外に居るあいだ繰り返し出さない(self, caplog: pytest.LogCaptureFixture) -> None:
        driver = self._homed(self.R_TRAVEL)

        with caplog.at_level(logging.WARNING, logger="lib.drivers.edulite05"):
            for _ in range(5):
                feed_edulite(driver, position=math.radians(self.R_ORIGIN_DEG + 185.0 - 360.0))

        assert len(caplog.records) == 1

    def test_可動域へ戻ってからまた外れれば再び出す(self, caplog: pytest.LogCaptureFixture) -> None:
        driver = self._homed(self.R_TRAVEL)

        with caplog.at_level(logging.WARNING, logger="lib.drivers.edulite05"):
            feed_edulite(driver, position=math.radians(self.R_ORIGIN_DEG + 185.0 - 360.0))
            feed_edulite(driver, position=math.radians(self.R_ORIGIN_DEG + 90.0 - 360.0))
            feed_edulite(driver, position=math.radians(self.R_ORIGIN_DEG + 185.0 - 360.0))

        assert len(caplog.records) == 2

    def test_可動域の内側では1通も出さない(self, caplog: pytest.LogCaptureFixture) -> None:
        driver = self._homed(self.R_TRAVEL)

        with caplog.at_level(logging.WARNING, logger="lib.drivers.edulite05"):
            feed_edulite(driver, position=math.radians(self.R_ORIGIN_DEG + 90.0 - 360.0))

        assert caplog.records == []
        assert driver.feedback_position() == pytest.approx(math.radians(90.0), abs=3 * _POS_LSB)

    def test_一意化した回転数は指令にも効く(self) -> None:
        """読み側だけ直すと、指令がモータの居場所から 360deg 離れた値になる。"""
        driver = self._homed(self.R_TRAVEL)
        feed_edulite(driver, position=math.radians(self.R_AFTER_DEG))
        raw = driver.state.position

        msg = driver.encode_target(ControlMode.POSITION, driver.feedback_position())

        assert target_value_of(msg) == pytest.approx(raw, abs=3 * _POS_LSB)

    def test_可動域は整列した順で受け取る(self) -> None:
        driver = Edulite05Driver("rotate_r", can_id=0x11)

        with pytest.raises(ValueError, match="min < max"):
            driver.set_travel_range(math.pi, 0.0)

    def test_可動域の読み出し口を持つ(self) -> None:
        driver = Edulite05Driver("rotate_r", can_id=0x11)
        assert driver.travel_range is None

        driver.set_travel_range(*self.R_TRAVEL)

        assert driver.travel_range == pytest.approx(self.R_TRAVEL)


class TestTravelRangeFallbackIsLoggedOnce:
    """一意化できない構成は、毎フレームではなく 1 回だけ残す。"""

    def test_可動域が渡されていなければ1回だけ残す(self, caplog: pytest.LogCaptureFixture) -> None:
        driver = Edulite05Driver("rotate_r", can_id=0x11)
        feed_edulite(driver, position=0.5)
        driver.capture_origin_here()

        with caplog.at_level(logging.WARNING, logger="lib.drivers.edulite05"):
            for _ in range(5):
                feed_edulite(driver, position=1.0)

        messages = [record.message for record in caplog.records]
        assert len(messages) == 1
        assert "可動域" in messages[0]

    def test_暫定原点しかないあいだは1通も出さない(self, caplog: pytest.LogCaptureFixture) -> None:
        """一意化そのものが成り立たない段なので、可動域の有無は問題ではない。"""
        driver = Edulite05Driver("rotate_r", can_id=0x11, set_zero_on_start=True)
        feed_edulite(driver, position=0.5)
        driver.establish_provisional_origin()

        with caplog.at_level(logging.WARNING, logger="lib.drivers.edulite05"):
            feed_edulite(driver, position=1.0)

        assert caplog.records == []

    def test_可動域が1回転以上なら受け取った時点で1回残す(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        driver = Edulite05Driver("rotate_r", can_id=0x11)

        with caplog.at_level(logging.WARNING, logger="lib.drivers.edulite05"):
            driver.set_travel_range(0.0, 3.0 * math.pi)
            feed_edulite(driver, position=0.5)
            driver.capture_origin_here()
            feed_edulite(driver, position=1.0)

        assert len(caplog.records) == 1
        assert "1 回転" in caplog.records[0].message

    def test_一意化できる構成では1通も出さない(self, caplog: pytest.LogCaptureFixture) -> None:
        driver = Edulite05Driver("rotate_r", can_id=0x11)

        with caplog.at_level(logging.WARNING, logger="lib.drivers.edulite05"):
            driver.set_travel_range(0.0, math.pi)
            feed_edulite(driver, position=0.5)
            driver.capture_origin_here()
            feed_edulite(driver, position=0.5 + math.radians(90.0))

        assert caplog.records == []
