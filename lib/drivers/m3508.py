from __future__ import annotations

import struct

import can

from lib.drivers.base import CheckContext, ControlMode, MotorDriver, MotorState

# 動作確認時に「回転検出なし」とみなす rpm のしきい値。
# C620 のフィードバックノイズ・微小逆起電力を除去するため小さめに固定。
_CHECK_VELOCITY_DEAD_BAND_RPM = 50.0

# 位置制御ループ (lib/control/position_loop.py) が PID 出力レンジに使うため公開する
CURRENT_MIN = -16384
CURRENT_MAX = 16384

_TX_ARBITRATION_ID = 0x200
_FEEDBACK_BASE_ID = 0x200
_ANGLE_MAX = 8191

# エンコーダ 1 回転あたりのカウント数。単回転角 (0〜360) の換算は既存 API 互換のため
# _ANGLE_MAX で割っているが、多回転累積は 1 回転ごとに 0.04deg ずれるのを避けるため
# 実分解能 8192 を使う
_COUNTS_PER_REV = 8192
_COUNTS_HALF_REV = _COUNTS_PER_REV // 2

# M3508 に内蔵される遊星減速機の減速比 (DJI 公称 3591/187 ≒ 19.2)。
# エンコーダは減速前のロータ側にあるため、フィードバック角・multi_turn_position は
# すべてモータ軸基準であり、出力軸の角度に直すにはこの値で割る
GEAR_RATIO = 3591 / 187

# C620 ESC は明示的な過電流フラグを持たないため、フィードバック電流の絶対値で異常検出する
# しきい値 18000 は連続定格 (約 ±10000 mA) を大きく超え、かつ素子飽和 (16384) より上の値を選定
# 後続フェーズで config 化するが段階② では定数で実装する
_OVERCURRENT_THRESHOLD_MA = 18000


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


class M3508Driver(MotorDriver):
    """DJI M3508 モータドライバ (C620 ESC 経由 CAN 通信)。"""

    def __init__(self, name: str, can_id: int) -> None:
        if not 1 <= can_id <= 4:
            raise ValueError(f"can_id は 1〜4 の範囲: {can_id}")
        super().__init__(name, can_id)

        # 多回転累積 (リフト軸のように 1 回転を超える機構の位置制御に必要)。
        # C620 のフィードバックは単回転角しか持たないため PC 側でアンラップする
        self._prev_angle_raw: int | None = None
        self._accumulated_counts: int = 0
        self._origin_counts: int = 0

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

    # ------------------------------------------------------------------ #
    #  多回転累積角
    # ------------------------------------------------------------------ #

    def update_state(self, msg: can.Message) -> MotorState:
        # decode_feedback の position は 0〜360 のまま保つ規約なので、
        # 累積はここ (副作用を持てる場所) で別管理する
        angle_raw = struct.unpack(">H", msg.data[:2])[0]

        if self._prev_angle_raw is not None:
            diff = angle_raw - self._prev_angle_raw
            # 半周を超える差分は 0 を跨いだ折り返しとみなす。
            # M3508 のフィードバック周期 (1kHz) に対し半周分回るには 3600rpm 超が必要で、
            # 減速機出力側の実回転数では起こり得ない
            if diff > _COUNTS_HALF_REV:
                diff -= _COUNTS_PER_REV
            elif diff < -_COUNTS_HALF_REV:
                diff += _COUNTS_PER_REV
            self._accumulated_counts += diff

        # 初回は差分を取れない。起動姿勢を原点にすることで、目標 0 が
        # 「電源投入時の位置を保持」を意味するようになり、起動直後の暴走を防ぐ
        self._prev_angle_raw = angle_raw

        return super().update_state(msg)

    @property
    def multi_turn_position(self) -> float:
        """原点からの累積回転角 [deg]。複数回転しても折り返さない。"""
        return (self._accumulated_counts - self._origin_counts) / _COUNTS_PER_REV * 360.0

    def reset_multi_turn_origin(self) -> None:
        """現在位置を累積角の原点にする (ホーミング完了後に呼ぶ)。"""
        self._origin_counts = self._accumulated_counts

    # ------------------------------------------------------------------ #
    #  目標到達判定
    # ------------------------------------------------------------------ #

    def default_tolerance(self, mode: ControlMode) -> float:
        # フィードバックはモータ軸基準なので、共通既定値 1deg をそのまま使うと
        # 出力軸では 0.05deg 相当になり PID の定常偏差に埋もれて永久に到達しない。
        # 他ドライバと同じ「出力軸 1deg」の意味になるよう減速比分だけ広げる
        if mode is ControlMode.POSITION:
            return super().default_tolerance(mode) * GEAR_RATIO
        return super().default_tolerance(mode)

    def feedback_position(self) -> float:
        # 位置制御ループ (lib/control/position_loop.py) は累積角を目標値として扱う。
        # 単回転角 (MotorState.position) と比較すると次元が食い違い、
        # 何回転もする軸でラップ角がたまたま目標と一致した瞬間に誤到達する
        return self.multi_turn_position

    def has_overcurrent_warning(self) -> bool:
        return abs(self._state.current) > _OVERCURRENT_THRESHOLD_MA

    # ------------------------------------------------------------------ #
    #  動作確認 (Phase 6 段階⑦)
    # ------------------------------------------------------------------ #
    # M3508 は電流制御専用のため、check は微小電流を 1 投入し
    # フィードバック rpm の符号一致で「指示が伝わって回転した」ことを確認する

    def check_command(self, *, magnitude: float = 500.0) -> tuple[can.Message, CheckContext]:
        msg = self.encode_target(ControlMode.CURRENT, magnitude)
        context = CheckContext(
            mode=ControlMode.CURRENT,
            target=float(magnitude),
            display_unit="mA",
        )
        return msg, context

    def evaluate_check_result(self, context: CheckContext) -> tuple[bool, str | None]:
        # 指令 [mA] とフィードバック [rpm] は次元が違うため追従判定 (evaluate_tracking) は
        # 使えない。回転が出たことと駆動方向だけを見る
        target = context.target
        velocity = self._state.velocity

        if abs(velocity) < _CHECK_VELOCITY_DEAD_BAND_RPM:
            return False, (f"回転検出なし (target={target:.0f}mA, velocity={velocity:.1f}rpm)")

        # 指令電流符号と rpm 符号が一致 → 駆動方向が正しい
        if (target > 0 and velocity > 0) or (target < 0 and velocity < 0):
            return True, None

        return False, (f"回転方向不一致 (target={target:.0f}mA, velocity={velocity:.1f}rpm)")

    def reset_after_check(self) -> can.Message:
        # 駆動状態を残さないよう必ず 0 mA を再送する
        return self.encode_target(ControlMode.CURRENT, 0)

    @staticmethod
    def encode_current_frame(currents: list[int]) -> can.Message:
        """4モータ分の電流指令を1つの CAN フレームにまとめる。"""
        clamped = [_clamp(c, CURRENT_MIN, CURRENT_MAX) for c in currents]
        return can.Message(
            arbitration_id=_TX_ARBITRATION_ID,
            data=struct.pack(">hhhh", *clamped),
            is_extended_id=False,
        )
