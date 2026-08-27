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

from lib.drivers.edulite05 import Edulite05Driver
from lib.drivers.generic import (
    _FLAG_E_STOP,
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
    position: float = 0.0,
    velocity: float = 0.0,
    current_ma: int = 0,
    temp: int = 25,
    reached: bool = False,
    e_stop: bool = False,
    watchdog: bool = False,
    unconfigured_id: bool = False,
    sensor: bool = False,
    flags: int = 0x00,
) -> can.Message:
    """自作モータドライバの FEEDBACK フレーム (仕様書 §3.2)。

    位置は 0.1deg 単位の符号付き 16bit。人間が書く単位 (deg) で受けて換算する。

    **状態フラグはビット位置ではなく名前で指定する。** 生の 2 進リテラルを
    テストに書くと、ビットの割り当てを詰め直したときに全テストを手で書き換える
    ことになり、しかも 1 つ間違えても「別のフラグを見ている」だけで緑のまま通る。
    ``flags`` は「予約ビットに値が載っていても影響しないこと」のように
    *ビット位置そのもの* が検証対象のときだけ使う。

    ``current_ma`` / ``temp`` は Byte5-7 の予約領域に載る値。プロトコルからは
    外れているので (仕様書 §3.2)、素通しにしていないことを見るテストで使う。
    """
    named = 0
    for enabled, bit in (
        (reached, _FLAG_REACHED),
        (e_stop, _FLAG_E_STOP),
        (watchdog, _FLAG_WATCHDOG),
        (unconfigured_id, _FLAG_UNCONFIGURED_ID),
        (sensor, _FLAG_SENSOR),
    ):
        if enabled:
            named |= bit

    data = bytearray(8)
    struct.pack_into("<h", data, 0, round(position * 10))
    struct.pack_into("<h", data, 2, int(velocity))
    data[4] = named | flags
    # Byte5-7 は予約。プロトコルからは外れているので (仕様書 §3.2)、
    # 素通しにしていないことを見るテストのためにここへ載せる
    struct.pack_into("<h", data, 5, int(current_ma))
    data[7] = temp
    return can.Message(
        arbitration_id=GenericDriver.build_can_id(CommandType.FEEDBACK, driver.can_id),
        data=bytes(data),
        is_extended_id=False,
    )


def feed_generic(
    driver: GenericDriver,
    *,
    position: float = 0.0,
    velocity: float = 0.0,
    current_ma: int = 0,
    temp: int = 25,
    reached: bool = False,
    e_stop: bool = False,
    watchdog: bool = False,
    unconfigured_id: bool = False,
    sensor: bool = False,
    flags: int = 0x00,
) -> None:
    driver.update_state(
        generic_feedback(
            driver,
            position=position,
            velocity=velocity,
            current_ma=current_ma,
            temp=temp,
            reached=reached,
            e_stop=e_stop,
            watchdog=watchdog,
            unconfigured_id=unconfigured_id,
            sensor=sensor,
            flags=flags,
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
