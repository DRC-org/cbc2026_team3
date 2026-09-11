from __future__ import annotations

import logging
import struct
import time
from collections.abc import Callable

import can

from lib.drivers.base import ControlMode, MotorDriver, MotorState

CURRENT_MIN = -16384
CURRENT_MAX = 16384

_TX_ARBITRATION_ID = 0x200
_FEEDBACK_BASE_ID = 0x200
_ANGLE_MAX = 8191

# 単回転角の換算は API 互換のため _ANGLE_MAX で割るが、多回転累積は 1 回転ごとに
# 0.04deg ずれるので実分解能 8192 を使う。
_COUNTS_PER_REV = 8192
# 半周を超える差分は 0 跨ぎの折り返しとみなす (1kHz に対し半周回るには 3600rpm 超が要る)。
_COUNTS_HALF_REV = _COUNTS_PER_REV // 2

_GAP_SPEED_MARGIN = 2.0

_MAX_TRUSTED_GAP_S = 0.1

logger = logging.getLogger(__name__)

GEAR_RATIO = 3591 / 187

_OVERCURRENT_THRESHOLD_MA = 18000


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


class M3508Driver(MotorDriver):
    def __init__(
        self,
        name: str,
        can_id: int,
        *,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= can_id <= 4:
            raise ValueError(f"can_id は 1〜4 の範囲: {can_id}")
        super().__init__(name, can_id)

        self._prev_angle_raw: int | None = None
        self._accumulated_counts: int = 0
        self._origin_counts: int = 0

        self._time_source = time_source
        self._prev_at: float | None = None
        self._prev_stamp: float | None = None
        self._prev_rpm: int = 0

        self._reanchor_count: int = 0
        self._last_reanchor_gap_s: float | None = None
        self._origin_trusted: bool = True

    def encode_target(self, mode: ControlMode, value: float) -> can.Message:
        if mode is not ControlMode.CURRENT:
            raise ValueError(f"M3508 は CURRENT モードのみサポート (受け取った: {mode.name})")

        clamped = _clamp(int(value), CURRENT_MIN, CURRENT_MAX)
        currents = [0, 0, 0, 0]
        currents[self.can_id - 1] = clamped

        return can.Message(
            arbitration_id=_TX_ARBITRATION_ID,
            data=struct.pack(">hhhh", *currents),
            is_extended_id=False,
        )

    def decode_feedback(self, msg: can.Message) -> MotorState:
        angle_raw, rpm, current, temp = struct.unpack(">hhhB", msg.data[:7])
        position_deg = (angle_raw & 0xFFFF) / _ANGLE_MAX * 360.0

        return MotorState(
            position=position_deg,
            velocity=float(rpm),
            current=float(current),
            temperature=float(temp),
        )

    def matches_feedback(self, msg: can.Message) -> bool:
        return msg.arbitration_id == _FEEDBACK_BASE_ID + self.can_id

    def update_state(self, msg: can.Message) -> MotorState:
        angle_raw, rpm = struct.unpack(">Hh", msg.data[:4])
        now = self._time_source()

        if self._prev_angle_raw is not None:
            gap_s = self._elapsed_since_previous(msg, now)
            if self._can_trust_wrap(gap_s, rpm):
                diff = angle_raw - self._prev_angle_raw
                if diff > _COUNTS_HALF_REV:
                    diff -= _COUNTS_PER_REV
                elif diff < -_COUNTS_HALF_REV:
                    diff += _COUNTS_PER_REV
                self._accumulated_counts += diff
            else:
                self._reanchor(gap_s, rpm)

        self._prev_angle_raw = angle_raw
        self._prev_rpm = rpm
        self._prev_at = now
        self._prev_stamp = self._frame_stamp(msg)

        return super().update_state(msg)

    @staticmethod
    def _frame_stamp(msg: can.Message) -> float | None:
        stamp = getattr(msg, "timestamp", None)
        if stamp is None:
            return None
        stamp = float(stamp)
        return stamp if stamp > 0.0 else None

    def _elapsed_since_previous(self, msg: can.Message, now: float) -> float:
        stamp = self._frame_stamp(msg)
        if stamp is not None and self._prev_stamp is not None:
            gap = stamp - self._prev_stamp
            if gap >= 0.0:
                return gap
        if self._prev_at is None:
            return 0.0
        return now - self._prev_at

    def _can_trust_wrap(self, gap_s: float, rpm_now: int) -> bool:
        if gap_s > _MAX_TRUSTED_GAP_S:
            return False

        max_rpm = max(abs(self._prev_rpm), abs(rpm_now))
        plausible_rev = gap_s * max_rpm / 60.0 * _GAP_SPEED_MARGIN
        return plausible_rev < 0.5

    def _reanchor(self, gap_s: float, rpm_now: int) -> None:
        self._reanchor_count += 1
        self._last_reanchor_gap_s = gap_s
        self._origin_trusted = False
        self._origin_confirmed = False
        logger.warning(
            "フィードバックが %.0fms 途切れたため累積角の折り返し推定を中止しました "
            "(motor=%s, 前後の rpm=%d/%d)。原点がずれている可能性があります",
            gap_s * 1000.0,
            self.name,
            self._prev_rpm,
            rpm_now,
        )

    @property
    def multi_turn_position(self) -> float:
        return (self._accumulated_counts - self._origin_counts) / _COUNTS_PER_REV * 360.0

    @property
    def origin_trusted(self) -> bool:
        return self._origin_trusted

    @property
    def reanchor_count(self) -> int:
        return self._reanchor_count

    def health_detail(self) -> str | None:
        if self._origin_trusted:
            return None
        gap_ms = (self._last_reanchor_gap_s or 0.0) * 1000.0
        return (
            f"フィードバック途切れ ({gap_ms:.0f}ms) で累積角を再アンカーしました。"
            f"原点がずれている可能性があります (計 {self._reanchor_count} 回)。"
            "原点を確定し直してください"
        )

    def reset_multi_turn_origin(self) -> None:
        self._origin_counts = self._accumulated_counts
        self._origin_trusted = True
        self.mark_origin_confirmed()

    def default_tolerance(self, mode: ControlMode) -> float:
        if mode is ControlMode.POSITION:
            return super().default_tolerance(mode) * GEAR_RATIO
        return super().default_tolerance(mode)

    def feedback_position(self) -> float:
        return self.multi_turn_position

    def has_overcurrent_warning(self) -> bool:
        return abs(self._state.current) > _OVERCURRENT_THRESHOLD_MA

    @staticmethod
    def encode_current_frame(currents: list[int]) -> can.Message:
        clamped = [_clamp(c, CURRENT_MIN, CURRENT_MAX) for c in currents]
        return can.Message(
            arbitration_id=_TX_ARBITRATION_ID,
            data=struct.pack(">hhhh", *clamped),
            is_extended_id=False,
        )
