from __future__ import annotations

import struct

import can

from lib.drivers.dm3520 import Dm3520Driver
from lib.drivers.edulite05 import Edulite05Driver
from lib.drivers.generic import (
    _FLAG_E_STOP,
    _FLAG_NEVER_COMMANDED,
    _FLAG_REACHED,
    _FLAG_SENSOR,
    _FLAG_UNCONFIGURED_ID,
    _FLAG_WATCHDOG,
    CommandType,
    GenericDriver,
)
from lib.drivers.m3508 import M3508Driver

_M3508_COUNTS_PER_REV = 8192


def m3508_counts_for_deg(deg: float) -> int:
    return round(deg / 360.0 * _M3508_COUNTS_PER_REV) % _M3508_COUNTS_PER_REV


def m3508_feedback(
    driver: M3508Driver,
    *,
    angle_raw: int = 0,
    rpm: int = 0,
    current: int = 0,
    temp: int = 25,
    timestamp: float = 0.0,
) -> can.Message:
    return can.Message(
        arbitration_id=0x200 + driver.can_id,
        data=struct.pack(">HhhBB", angle_raw & 0xFFFF, rpm, current, temp, 0),
        is_extended_id=False,
        timestamp=timestamp,
    )


def feed_m3508(
    driver: M3508Driver,
    *,
    angle_raw: int | None = None,
    deg: float | None = None,
    rpm: int = 0,
    current: int = 0,
    temp: int = 25,
    timestamp: float = 0.0,
) -> None:
    if (angle_raw is None) == (deg is None):
        raise ValueError("angle_raw と deg のどちらか一方を指定すること")
    raw = m3508_counts_for_deg(deg) if angle_raw is None else angle_raw
    driver.update_state(
        m3508_feedback(
            driver, angle_raw=raw, rpm=rpm, current=current, temp=temp, timestamp=timestamp
        )
    )


def generic_feedback(
    driver: GenericDriver,
    *,
    position: float | None = None,
    reached: bool = False,
    e_stop: bool = False,
    watchdog: bool = False,
    unconfigured_id: bool = False,
    sensor: bool = False,
    never_commanded: bool = False,
    flags: int = 0x00,
    reserved: bytes = b"",
) -> can.Message:
    named = 0
    for enabled, bit in (
        (reached, _FLAG_REACHED),
        (e_stop, _FLAG_E_STOP),
        (watchdog, _FLAG_WATCHDOG),
        (unconfigured_id, _FLAG_UNCONFIGURED_ID),
        (sensor, _FLAG_SENSOR),
        (never_commanded, _FLAG_NEVER_COMMANDED),
    ):
        if enabled:
            named |= bit

    data = bytearray([named | flags])
    if position is not None:
        data.extend(struct.pack("<h", round(position * 10)))
    data.extend(reserved)
    return can.Message(
        arbitration_id=GenericDriver.build_can_id(CommandType.FEEDBACK, driver.can_id),
        data=bytes(data),
        is_extended_id=False,
    )


def feed_generic(
    driver: GenericDriver,
    *,
    position: float | None = None,
    reached: bool = False,
    e_stop: bool = False,
    watchdog: bool = False,
    unconfigured_id: bool = False,
    sensor: bool = False,
    never_commanded: bool = False,
    flags: int = 0x00,
    reserved: bytes = b"",
) -> None:
    driver.update_state(
        generic_feedback(
            driver,
            position=position,
            reached=reached,
            e_stop=e_stop,
            watchdog=watchdog,
            unconfigured_id=unconfigured_id,
            sensor=sensor,
            never_commanded=never_commanded,
            flags=flags,
            reserved=reserved,
        )
    )


def generic_info(
    driver: GenericDriver,
    *,
    firmware_version: int = 1,
    board_kind: int = 1,
    slot_kind: int = 0,
    angle_range_deg: float | None = None,
) -> can.Message:
    data = bytearray([firmware_version, board_kind, slot_kind])
    if angle_range_deg is not None:
        data.extend(struct.pack("<h", round(angle_range_deg * 10)))
    return can.Message(
        arbitration_id=GenericDriver.build_can_id(CommandType.INFO, driver.can_id),
        data=bytes(data),
        is_extended_id=False,
    )


def feed_generic_info(
    driver: GenericDriver,
    *,
    firmware_version: int = 1,
    board_kind: int = 1,
    slot_kind: int = 0,
    angle_range_deg: float | None = None,
) -> None:
    driver.update_info(
        generic_info(
            driver,
            firmware_version=firmware_version,
            board_kind=board_kind,
            slot_kind=slot_kind,
            angle_range_deg=angle_range_deg,
        )
    )


def edulite_feedback(
    driver: Edulite05Driver,
    *,
    position: float = 0.0,
    velocity: float = 0.0,
    torque: float = 0.0,
    temperature: float = 25.0,
    mode_state: int = 2,
    fault_bits: int = 0,
    host_id: int | None = None,
) -> can.Message:
    data_area2 = (mode_state << 14) | (fault_bits << 8) | driver.can_id
    arbitration_id = driver.build_can_id(
        driver.COMM_TYPE_FEEDBACK,
        data_area2,
        driver.host_id if host_id is None else host_id,
    )
    data = struct.pack(
        ">HHHH",
        driver.float_to_uint16(position, driver.POS_MIN, driver.POS_MAX),
        driver.float_to_uint16(velocity, driver.VEL_MIN, driver.VEL_MAX),
        driver.float_to_uint16(torque, driver.TORQUE_MIN, driver.TORQUE_MAX),
        int(temperature * 10),
    )
    return can.Message(arbitration_id=arbitration_id, data=data, is_extended_id=True)


def feed_edulite(driver: Edulite05Driver, **kwargs: float | int | None) -> None:
    driver.update_state(edulite_feedback(driver, **kwargs))  # type: ignore[arg-type]


def _dm3520_to_raw(value: float, max_abs: float, bits: int) -> int:
    span = (1 << bits) - 1
    clamped = min(max(value, -max_abs), max_abs)
    return round((clamped + max_abs) * span / (2.0 * max_abs))


def dm3520_feedback(
    driver: Dm3520Driver,
    *,
    position: float = 0.0,
    velocity: float = 0.0,
    torque: float = 0.0,
    t_mos: int = 25,
    t_rotor: int = 25,
    error: int = 1,
    master_id: int | None = None,
    can_id_nibble: int | None = None,
) -> can.Message:
    pos = _dm3520_to_raw(position, driver.p_max, driver._POS_BITS)
    vel = _dm3520_to_raw(velocity, driver.v_max, driver._VEL_BITS)
    trq = _dm3520_to_raw(torque, driver.t_max, driver._TORQUE_BITS)
    nibble = driver.can_id & 0x0F if can_id_nibble is None else can_id_nibble
    data = bytes(
        [
            ((error & 0x0F) << 4) | (nibble & 0x0F),
            (pos >> 8) & 0xFF,
            pos & 0xFF,
            (vel >> 4) & 0xFF,
            ((vel & 0x0F) << 4) | ((trq >> 8) & 0x0F),
            trq & 0xFF,
            t_mos & 0xFF,
            t_rotor & 0xFF,
        ]
    )
    return can.Message(
        arbitration_id=driver.master_id if master_id is None else master_id,
        data=data,
        is_extended_id=False,
    )


def feed_dm3520(driver: Dm3520Driver, **kwargs: float | int | None) -> None:
    driver.update_state(dm3520_feedback(driver, **kwargs))  # type: ignore[arg-type]
