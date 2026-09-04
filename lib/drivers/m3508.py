from __future__ import annotations

import logging
import struct
import time
from collections.abc import Callable

import can

from lib.drivers.base import ControlMode, MotorDriver, MotorState

# 動作確認時に「回転検出なし」とみなす rpm のしきい値。
# C620 のフィードバックノイズ・微小逆起電力を除去するため小さめに固定。

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

# --- 折り返し推定を信頼してよい条件 -------------------------------------------
# 単回転角のアンラップは「半周を超える差分は 0 を跨いだ折り返し」という推定に立つ。
# **これはフィードバックが 1kHz で途切れなく届いている間しか成り立たない。**
# 途切れた窓でモータ軸が半周以上回ると方向を取り違え、累積角が 1 回転ぶん飛ぶ。
#
# 飛ぶ量は 360deg。`config/main_hand_positions.yaml` の scale 55.0131deg/mm では
# **6.54mm** に相当し、同じ y_axis の `sync_tolerance` 2.0mm の 3 倍を超える。
# 左右の片方だけに乗れば、その瞬間に偏差超過で全体緊急停止になる ——
# 実在しないずれで試合が止まるので、推定できない窓では推定しないほうが安全。
#
# **窓の長さはフレーム自身のタイムスタンプで測る (`_elapsed_since_previous`)。**
# 処理時刻で測ると、取りこぼしが起きている最中 —— まさにこの判定が要る場面 ——
# だけ窓が詰まって見え、ガードが素通りする。実機ではこれで累積角に 360deg が
# 注入されていた (`lib/can_manager.py` の `_ReadableFd` に経緯がある)。
#
# 判定は 2 つ。どちらか一方でも引っ掛かれば推定をやめる:
#   ① 窓の間に回りえた回転数が半周に届くか (フィードバックの rpm から見積もる)
#   ② 窓そのものが長すぎるか (rpm が両端でたまたま 0 に見える場合の歯止め)
#
# ①の見積もりには窓の前後で観測した rpm の大きいほうを使い、さらに余裕を掛ける。
# 昇降軸は窓の間フィードバック途絶で電流 0 に落ちるため重力で加速する —— 窓の
# 入口の rpm だけでは上限にならない。出口の rpm も見ることで、加速していれば
# 「信頼できない」側へ倒れる。
_GAP_SPEED_MARGIN = 2.0

# ②の上限 [秒]。1kHz のフィードバックに対し 100 通ぶんの欠落で、平常時の揺らぎでは
# 到達しない。`health` の `feedback_timeout_ms` (既定 500ms) を流用しないのは、
# あちらが「途絶とみなす境界」であってこちらの「折り返しを推定できる境界」とは
# 別概念のため。同じ数を共有すると、片方の都合で動かしたときにもう片方が黙って狂う。
_MAX_TRUSTED_GAP_S = 0.1

logger = logging.getLogger(__name__)

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

        # 多回転累積 (リフト軸のように 1 回転を超える機構の位置制御に必要)。
        # C620 のフィードバックは単回転角しか持たないため PC 側でアンラップする
        self._prev_angle_raw: int | None = None
        self._accumulated_counts: int = 0
        self._origin_counts: int = 0

        # 折り返し推定の可否を測るための、前回フィードバックの時刻と回転数。
        # 単調クロックを使うのは壁時計だと NTP 補正で窓が伸縮するため
        # (`lib/control/periodic.py` の LogThrottle と同じ理由)。
        # **ただし単調クロックは「このプロセスが処理した時刻」しか答えられない。**
        # 実際に測りたいのは「バス上でフレームが途切れた時間」なので、
        # フレーム自身のタイムスタンプがあればそちらを優先する (_elapsed_since)
        self._time_source = time_source
        self._prev_at: float | None = None
        self._prev_stamp: float | None = None
        self._prev_rpm: int = 0

        # 推定を諦めて再アンカーした記録。**原点は以後ずれている可能性がある。**
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

    # ------------------------------------------------------------------ #
    #  多回転累積角
    # ------------------------------------------------------------------ #

    def update_state(self, msg: can.Message) -> MotorState:
        # decode_feedback の position は 0〜360 のまま保つ規約なので、
        # 累積はここ (副作用を持てる場所) で別管理する
        angle_raw, rpm = struct.unpack(">Hh", msg.data[:4])
        now = self._time_source()

        if self._prev_angle_raw is not None:
            gap_s = self._elapsed_since_previous(msg, now)
            if self._can_trust_wrap(gap_s, rpm):
                diff = angle_raw - self._prev_angle_raw
                # 半周を超える差分は 0 を跨いだ折り返しとみなす。
                # M3508 のフィードバック周期 (1kHz) に対し半周分回るには 3600rpm 超が
                # 必要で、減速機出力側の実回転数では起こり得ない ——
                # **ただしフィードバックが途切れていない限りにおいて。**
                # 途切れた窓の扱いは _can_trust_wrap が上で切り分けている
                if diff > _COUNTS_HALF_REV:
                    diff -= _COUNTS_PER_REV
                elif diff < -_COUNTS_HALF_REV:
                    diff += _COUNTS_PER_REV
                self._accumulated_counts += diff
            else:
                self._reanchor(gap_s, rpm)

        # 初回は差分を取れない。起動姿勢を原点にすることで、目標 0 が
        # 「電源投入時の位置を保持」を意味するようになり、起動直後の暴走を防ぐ
        self._prev_angle_raw = angle_raw
        self._prev_rpm = rpm
        self._prev_at = now
        self._prev_stamp = self._frame_stamp(msg)

        return super().update_state(msg)

    @staticmethod
    def _frame_stamp(msg: can.Message) -> float | None:
        """フレーム自身の受信時刻 [秒]。持っていなければ None。

        SocketCAN はカーネルが受信した時刻を載せる (python-can が
        ``Message.timestamp`` として渡す)。**0.0 は「時刻が無い」の意味**で、
        テストが組み立てた素の ``can.Message`` がこれに当たる。
        """
        stamp = getattr(msg, "timestamp", None)
        if stamp is None:
            return None
        stamp = float(stamp)
        return stamp if stamp > 0.0 else None

    def _elapsed_since_previous(self, msg: can.Message, now: float) -> float:
        """前回フィードバックからの経過 [秒]。**フレーム自身の時刻を優先する。**

        折り返し推定が知りたいのは「バス上でフィードバックが途切れた時間」であって
        「このプロセスが処理した間隔」ではない。両者は普段は一致するが、**取りこぼしが
        起きている間だけ食い違い、しかもそこが唯一この判定が要る場面**である ——
        カーネルのバッファ溢れでフレームが捨てられると、残った分は滞留を詰めて
        処理されるので処理間隔は 1ms 程度にしか見えない。単調クロックで測ると、
        実際には数十 ms 途切れた窓を「途切れていない」と読み、半周を超えた回転に
        折り返し推定を当てて**累積角に 360deg (y_axis で 6.54mm) を注入する**。
        これは再アンカーの記録にも残らないので、原点がずれたまま平常に見える。

        カーネルの時刻は壁時計 (CLOCK_REALTIME) なので NTP 補正で飛びうるが、
        飛んだ結果は「窓が長く見える = 推定をやめて再アンカーする」側 (安全側) に
        倒れる。逆走 (負の間隔) だけは意味を成さないので単調クロックへ落とす。
        """
        stamp = self._frame_stamp(msg)
        if stamp is not None and self._prev_stamp is not None:
            gap = stamp - self._prev_stamp
            if gap >= 0.0:
                return gap
        if self._prev_at is None:
            return 0.0
        return now - self._prev_at

    def _can_trust_wrap(self, gap_s: float, rpm_now: int) -> bool:
        """この間隔を跨いで折り返しを推定してよいか。

        推定が成り立つのは「窓の間に半周以上回っていない」ときだけ。回りえた量は
        窓の前後で観測した rpm の大きいほうから見積もる (窓の中で加速していれば
        出口の rpm に現れる)。
        """
        if gap_s > _MAX_TRUSTED_GAP_S:
            return False

        max_rpm = max(abs(self._prev_rpm), abs(rpm_now))
        plausible_rev = gap_s * max_rpm / 60.0 * _GAP_SPEED_MARGIN
        return plausible_rev < 0.5

    def _reanchor(self, gap_s: float, rpm_now: int) -> None:
        """折り返しを推定せず、今の角度を新しい起点にする。

        **差分を積まないので、窓の間に実際に動いたぶんは累積角から失われる。**
        それでも 1 回転を捏造するよりましで、誤差は窓の中の実移動量に収まる
        (推定を続けると、実移動量に加えて必ず 360deg が乗る)。

        失った量は原理的に測れないので、代わりに「原点はもう信用できない」ことを
        記録して操縦者へ渡す (`health_detail`)。黙って再アンカーすると、
        位置がずれたまま平常どおりに見える機体ができる。
        """
        self._reanchor_count += 1
        self._last_reanchor_gap_s = gap_s
        self._origin_trusted = False
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
        """原点からの累積回転角 [deg]。複数回転しても折り返さない。"""
        return (self._accumulated_counts - self._origin_counts) / _COUNTS_PER_REV * 360.0

    @property
    def origin_trusted(self) -> bool:
        """累積角の原点が起動時 (または前回の原点確定時) のまま信用できるか。

        False になるのはフィードバックの途切れで再アンカーしたときだけ。
        戻す唯一の経路は原点の確定 (`reset_multi_turn_origin`) で、
        時間経過や受信の復帰では戻らない —— ずれは復帰しても消えないため。
        """
        return self._origin_trusted

    @property
    def reanchor_count(self) -> int:
        """折り返し推定を諦めた回数。0 でないかぎり原点はずれている。"""
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
        """現在位置を累積角の原点にする (ホーミング完了後に呼ぶ)。

        再アンカーで失われた原点の信頼はここでだけ回復する。原点を確定した時点で
        「今どこにいるか」が改めて確定するので、それ以前のずれは意味を持たなくなる。
        """
        self._origin_counts = self._accumulated_counts
        self._origin_trusted = True

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

    @staticmethod
    def encode_current_frame(currents: list[int]) -> can.Message:
        """4モータ分の電流指令を1つの CAN フレームにまとめる。"""
        clamped = [_clamp(c, CURRENT_MIN, CURRENT_MAX) for c in currents]
        return can.Message(
            arbitration_id=_TX_ARBITRATION_ID,
            data=struct.pack(">hhhh", *clamped),
            is_extended_id=False,
        )
