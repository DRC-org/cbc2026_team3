"""実機と同じフィードバックフレームを組み立て、``update_state`` で流し込むヘルパ。

テストが ``driver._state`` を直接書き換えると、フレーム解釈 (decode_feedback) と
``update_state`` の副作用 (M3508 の多回転アンラップ、自作モタドラのフラグ保持、
EDULITE 05 の fault ビット取り込み) を丸ごと迂回する。到達判定や偏差監視は
その副作用の結果を読むので、そこを飛ばしたテストは「デコード側を壊しても緑」に
なり、安全網として機能しない。

各ドライバのフレーム組み立てがテストごとに写されていたため、フォーマット
(M3508 の角度が符号付きか無しか等) が箇所ごとにずれていた。ここを唯一の
組み立て場所にして、プロトコルが変わったら 1 箇所だけが赤くなるようにする。
"""

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

# M3508 のエンコーダ 1 回転あたりのカウント数 (C620 フィードバックの角度レンジ)
_M3508_COUNTS_PER_REV = 8192


def m3508_counts_for_deg(deg: float) -> int:
    """出力角 [deg] を M3508 フィードバックの生カウントへ換算する。"""
    return round(deg / 360.0 * _M3508_COUNTS_PER_REV)


def m3508_feedback(
    driver: M3508Driver,
    *,
    angle_raw: int = 0,
    rpm: int = 0,
    current: int = 0,
    temp: int = 25,
) -> can.Message:
    """C620 フィードバックフレーム (0x200 + can_id / 8 byte)。"""
    return can.Message(
        arbitration_id=0x200 + driver.can_id,
        data=struct.pack(">HhhBB", angle_raw & 0xFFFF, rpm, current, temp, 0),
        is_extended_id=False,
    )


def feed_m3508(
    driver: M3508Driver,
    *,
    angle_raw: int | None = None,
    deg: float | None = None,
    rpm: int = 0,
    current: int = 0,
    temp: int = 25,
) -> None:
    """M3508 へフィードバックを 1 フレーム流す。

    ``deg`` を渡すと単回転角から生カウントへ換算する。多回転の累積は
    ``update_state`` 側が前回値との差分で行うため、連続して呼ぶ順序に意味がある。
    """
    if (angle_raw is None) == (deg is None):
        raise ValueError("angle_raw と deg のどちらか一方を指定すること")
    raw = m3508_counts_for_deg(deg) if angle_raw is None else angle_raw
    driver.update_state(m3508_feedback(driver, angle_raw=raw, rpm=rpm, current=current, temp=temp))


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
    """自作モータドライバの FEEDBACK フレーム (仕様書 §3.2)。

    Byte0=状態フラグ / Byte1-2=位置。**DLC は可変**で、位置を持たない基板
    (DC・センサ) は状態フラグ 1 バイトだけを送る。``position`` を省くとその形になる。

    **状態フラグはビット位置ではなく名前で指定する。** 生の 2 進リテラルを
    テストに書くと、ビットの割り当てを詰め直したときに全テストを手で書き換える
    ことになり、しかも 1 つ間違えても「別のフラグを見ている」だけで緑のまま通る。
    ``flags`` は「予約ビットに値が載っていても影響しないこと」のように
    *ビット位置そのもの* が検証対象のときだけ使う。

    ``reserved`` は Byte3 以降に載せる余分なバイト。予約領域を素通しにしていない
    ことを見るテストで使う。
    """
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
    """自作モータドライバの INFO フレーム (仕様書 §3.4)。

    Byte0=版 / Byte1=基板種別 / Byte2=スロット役割。**DLC は可変**で、サーボスロット
    だけが Byte3-4 に可動レンジ [0.1deg] を足す。``angle_range_deg`` を省くと
    「レンジを申告しない基板」—— DC・電磁弁・センサ、および**可動レンジ以前の
    サーボファーム** —— の形になる。この 2 つは PC 側で区別できないので、
    サーボ軸では「申告なし」自体が焼き忘れの証拠として扱われる。
    """
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
    """EDULITE 05 のフィードバックフレーム (拡張 ID / 8 byte)。

    ``host_id`` を明示できるのは「宛先が自分でないフレームを無視する」ことを
    確かめるため。既定はドライバ自身の host_id なので通常は指定しない。
    """
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
    """実数 → 固定小数点。``Dm3520Driver.uint_to_float`` の逆変換。

    逆変換をここに置くのは、ドライバ本体には要らない (PC は受信するだけで
    固定小数点を組み立てない) ため。**本体側へ生やすと、テストの都合で作った
    関数が本番コードに常駐する。**
    """
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
    """DM3520 のフィードバックフレーム (標準 ID = MST_ID / 8 byte)。

    ``error`` の既定は 1 (Enabled)。``master_id`` / ``can_id_nibble`` を明示できるのは
    「自分宛でないフレームを無視する」ことを確かめるため。
    """
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
