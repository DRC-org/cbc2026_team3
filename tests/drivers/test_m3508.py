from __future__ import annotations

import struct

import can
import pytest

from lib.drivers.base import ControlMode, MotorState
from lib.drivers.m3508 import GEAR_RATIO, M3508Driver
from tests.fake_clock import FakeClock
from tests.feedback_frames import feed_m3508


class TestEncodeCurrentCommand:
    def setup_method(self) -> None:
        self.driver = M3508Driver("test_motor", can_id=1)

    def test_encode_current_command(self) -> None:
        msg = self.driver.encode_target(ControlMode.CURRENT, 5000)
        assert msg.arbitration_id == 0x200
        assert msg.is_extended_id is False
        values = struct.unpack(">hhhh", msg.data)
        assert values[0] == 5000
        assert values[1] == 0
        assert values[2] == 0
        assert values[3] == 0

    def test_encode_current_command_negative(self) -> None:
        msg = self.driver.encode_target(ControlMode.CURRENT, -10000)
        values = struct.unpack(">hhhh", msg.data)
        assert values[0] == -10000

    def test_encode_current_command_clamp(self) -> None:
        msg_over = self.driver.encode_target(ControlMode.CURRENT, 20000)
        values_over = struct.unpack(">hhhh", msg_over.data)
        assert values_over[0] == 16384

        msg_under = self.driver.encode_target(ControlMode.CURRENT, -20000)
        values_under = struct.unpack(">hhhh", msg_under.data)
        assert values_under[0] == -16384


class TestEncodeCurrentCommandMotor3:
    """モータ ID 3 の場合、バイト 4-5 にスロットされることを確認。"""

    def test_encode_motor3_slot(self) -> None:
        driver = M3508Driver("motor3", can_id=3)
        msg = driver.encode_target(ControlMode.CURRENT, 1000)
        values = struct.unpack(">hhhh", msg.data)
        assert values[0] == 0
        assert values[1] == 0
        assert values[2] == 1000
        assert values[3] == 0


class TestEncodeCurrentCommandInvalidMode:
    def test_velocity_mode_raises(self) -> None:
        driver = M3508Driver("test", can_id=1)
        with pytest.raises(ValueError, match="CURRENT"):
            driver.encode_target(ControlMode.VELOCITY, 100)


class TestDecodeFeedback:
    def setup_method(self) -> None:
        self.driver = M3508Driver("test_motor", can_id=1)

    def test_decode_feedback(self) -> None:
        angle_raw = 4096
        rpm_raw = 1000
        current_raw = 500
        temp_raw = 40
        data = struct.pack(">hhhBB", angle_raw, rpm_raw, current_raw, temp_raw, 0)
        msg = can.Message(arbitration_id=0x201, data=data, is_extended_id=False)

        state = self.driver.decode_feedback(msg)
        assert isinstance(state, MotorState)
        assert state.position == pytest.approx(4096 / 8191 * 360, abs=0.1)
        assert state.velocity == pytest.approx(1000.0)
        assert state.current == pytest.approx(500.0)
        assert state.temperature == pytest.approx(40.0)

    def test_decode_feedback_negative_rpm(self) -> None:
        data = struct.pack(">hhhBB", 0, -3000, -200, 25, 0)
        msg = can.Message(arbitration_id=0x201, data=data, is_extended_id=False)

        state = self.driver.decode_feedback(msg)
        assert state.velocity == pytest.approx(-3000.0)
        assert state.current == pytest.approx(-200.0)


class TestMatchesFeedback:
    def test_matches_feedback(self) -> None:
        driver = M3508Driver("test", can_id=2)
        msg = can.Message(arbitration_id=0x202, data=bytes(8), is_extended_id=False)
        assert driver.matches_feedback(msg) is True

    def test_matches_feedback_wrong_id(self) -> None:
        driver = M3508Driver("test", can_id=2)
        msg = can.Message(arbitration_id=0x201, data=bytes(8), is_extended_id=False)
        assert driver.matches_feedback(msg) is False

        msg_unrelated = can.Message(arbitration_id=0x100, data=bytes(8), is_extended_id=False)
        assert driver.matches_feedback(msg_unrelated) is False


class TestEncodeCurrentFrame:
    def test_encode_current_frame(self) -> None:
        msg = M3508Driver.encode_current_frame([1000, -2000, 3000, -4000])
        assert msg.arbitration_id == 0x200
        assert msg.is_extended_id is False
        values = struct.unpack(">hhhh", msg.data)
        assert values == (1000, -2000, 3000, -4000)

    def test_encode_current_frame_clamp(self) -> None:
        msg = M3508Driver.encode_current_frame([20000, -20000, 0, 0])
        values = struct.unpack(">hhhh", msg.data)
        assert values[0] == 16384
        assert values[1] == -16384


class TestHealth:
    """ヘルスチェック判定 (Phase 6 段階②)。"""

    def setup_method(self) -> None:
        self.driver = M3508Driver("test_motor", can_id=1)

    def _feed(self, *, current: int = 0, temp: int = 25) -> None:
        feed_m3508(self.driver, angle_raw=0, current=current, temp=temp)

    def test_thermal_warning_below_threshold(self) -> None:
        self._feed(temp=60)
        assert self.driver.has_thermal_warning(temp_warning_c=65) is False

    def test_thermal_warning_at_threshold(self) -> None:
        self._feed(temp=65)
        assert self.driver.has_thermal_warning(temp_warning_c=65) is True

    def test_thermal_fault_at_critical(self) -> None:
        self._feed(temp=80)
        assert self.driver.has_thermal_fault(temp_critical_c=80) is True
        # critical 未満なら fault ではない
        self._feed(temp=79)
        assert self.driver.has_thermal_fault(temp_critical_c=80) is False

    def test_overcurrent_warning_above_threshold(self) -> None:
        # しきい値 18000 mA を超える電流で警告
        self._feed(current=18500)
        assert self.driver.has_overcurrent_warning() is True

    def test_overcurrent_warning_negative_above_threshold(self) -> None:
        # 逆方向の電流暴走も検出 (絶対値判定)
        self._feed(current=-19000)
        assert self.driver.has_overcurrent_warning() is True

    def test_overcurrent_warning_within_limit(self) -> None:
        self._feed(current=15000)
        assert self.driver.has_overcurrent_warning() is False

    def test_is_fault_default_false(self) -> None:
        # M3508 には明示的な fault フラグがないので常に False
        self._feed(temp=200, current=20000)
        assert self.driver.is_fault() is False


class TestMultiTurn:
    """多回転累積角 (リフト軸の位置制御用)。"""

    def setup_method(self) -> None:
        self.driver = M3508Driver("lift", can_id=1)

    def _feed_angle(self, angle_raw: int) -> None:
        feed_m3508(self.driver, angle_raw=angle_raw)

    @staticmethod
    def _deg(counts: float) -> float:
        return counts / 8192 * 360.0

    def test_initial_position_is_zero_before_any_feedback(self) -> None:
        assert self.driver.multi_turn_position == pytest.approx(0.0)

    def test_first_feedback_becomes_origin(self) -> None:
        # 起動位置を原点にすることで、目標 0 が「電源投入時の姿勢維持」を意味する
        self._feed_angle(3000)
        assert self.driver.multi_turn_position == pytest.approx(0.0)

    def test_accumulates_forward_within_one_turn(self) -> None:
        self._feed_angle(1000)
        self._feed_angle(3048)
        assert self.driver.multi_turn_position == pytest.approx(self._deg(2048), abs=1e-6)

    def test_accumulates_backward_within_one_turn(self) -> None:
        self._feed_angle(3048)
        self._feed_angle(1000)
        assert self.driver.multi_turn_position == pytest.approx(self._deg(-2048), abs=1e-6)

    def test_wraparound_forward(self) -> None:
        # 8000 → 200 は 0 を跨いだ正転 (+392 counts)
        self._feed_angle(8000)
        self._feed_angle(200)
        assert self.driver.multi_turn_position == pytest.approx(self._deg(392), abs=1e-6)

    def test_wraparound_backward(self) -> None:
        # 200 → 8000 は 0 を跨いだ逆転 (-392 counts)
        self._feed_angle(200)
        self._feed_angle(8000)
        assert self.driver.multi_turn_position == pytest.approx(self._deg(-392), abs=1e-6)

    def test_multiple_revolutions_forward(self) -> None:
        self._feed_angle(0)
        for raw in (2000, 4000, 6000, 8000, 1808, 3808):
            self._feed_angle(raw)
        # 2000 counts x 6 = 12000 counts (1 回転と少し)
        assert self.driver.multi_turn_position == pytest.approx(self._deg(12000), abs=1e-6)

    def test_multiple_revolutions_backward(self) -> None:
        self._feed_angle(0)
        for raw in (6192, 4192, 2192, 192, 6384):
            self._feed_angle(raw)
        assert self.driver.multi_turn_position == pytest.approx(self._deg(-10000), abs=1e-6)

    def test_reset_origin_makes_current_position_zero(self) -> None:
        self._feed_angle(1000)
        self._feed_angle(5096)
        assert self.driver.multi_turn_position != pytest.approx(0.0)

        self.driver.reset_multi_turn_origin()
        assert self.driver.multi_turn_position == pytest.approx(0.0)

        # 原点リセット後も累積は継続する
        self._feed_angle(6096)
        assert self.driver.multi_turn_position == pytest.approx(self._deg(1000), abs=1e-6)

    def test_single_turn_position_still_reported_in_degrees(self) -> None:
        # decode_feedback の position は 0〜360 のまま (既存 API の互換性)
        self._feed_angle(4096)
        assert self.driver.state.position == pytest.approx(4096 / 8191 * 360, abs=0.1)


class TestMultiTurnTargetReached:
    """到達判定が多回転累積角基準で行われること (位置制御ループと同じ次元)。"""

    def setup_method(self) -> None:
        self.driver = M3508Driver("lift", can_id=1)

    def _feed(self, angle_raw: int, *, velocity: int = 0, current: int = 0) -> None:
        feed_m3508(self.driver, angle_raw=angle_raw, rpm=velocity, current=current)

    def _spin_two_turns(self) -> None:
        """累積角をちょうど +720deg (16384 counts) にし、単回転角は 0 に戻す。"""
        for raw in (0, 2048, 4096, 6144, 0, 2048, 4096, 6144, 0):
            self._feed(raw)
        assert self.driver.multi_turn_position == pytest.approx(720.0)
        assert self.driver.state.position == pytest.approx(0.0)

    def test_reached_at_multi_turn_target(self) -> None:
        self._spin_two_turns()
        assert self.driver.is_target_reached(720.0, ControlMode.POSITION) is True

    def test_wrapped_angle_matching_target_is_not_reached(self) -> None:
        # 累積 720deg の時点で単回転角は 0deg。ラップ角で判定すると誤って到達扱いになる
        self._spin_two_turns()
        assert self.driver.is_target_reached(0.0, ControlMode.POSITION) is False

    def test_not_reached_before_finishing_the_turns(self) -> None:
        for raw in (0, 2048, 4096, 6144, 0):
            self._feed(raw)
        assert self.driver.multi_turn_position == pytest.approx(360.0)
        assert self.driver.is_target_reached(720.0, ControlMode.POSITION) is False

    def test_negative_multi_turn_target(self) -> None:
        for raw in (0, 6144, 4096, 2048, 0, 6144, 4096, 2048, 0):
            self._feed(raw)
        assert self.driver.multi_turn_position == pytest.approx(-720.0)
        assert self.driver.is_target_reached(-720.0, ControlMode.POSITION) is True
        assert self.driver.is_target_reached(720.0, ControlMode.POSITION) is False

    def test_explicit_tolerance_boundary(self) -> None:
        self._spin_two_turns()
        assert self.driver.is_target_reached(730.0, ControlMode.POSITION, tolerance=10.0) is True
        assert self.driver.is_target_reached(730.1, ControlMode.POSITION, tolerance=10.0) is False

    def test_default_tolerance_is_one_degree_at_output_shaft(self) -> None:
        # フィードバックはモータ軸基準なので、既定許容差も減速比分だけ広げる
        assert self.driver.default_tolerance(ControlMode.POSITION) == pytest.approx(GEAR_RATIO)

    def test_default_tolerance_applies_to_multi_turn_error(self) -> None:
        self._spin_two_turns()
        inside = 720.0 + GEAR_RATIO * 0.9
        outside = 720.0 + GEAR_RATIO * 1.1
        assert self.driver.is_target_reached(inside, ControlMode.POSITION) is True
        assert self.driver.is_target_reached(outside, ControlMode.POSITION) is False

    def test_reached_after_origin_reset(self) -> None:
        self._spin_two_turns()
        self.driver.reset_multi_turn_origin()
        assert self.driver.is_target_reached(0.0, ControlMode.POSITION) is True
        assert self.driver.is_target_reached(720.0, ControlMode.POSITION) is False

    def test_current_mode_is_always_reached(self) -> None:
        # 開ループ指令なので目標電流とフィードバックの一致は問わない
        self._spin_two_turns()
        self._feed(0, current=0)
        assert self.driver.is_target_reached(5000.0, ControlMode.CURRENT) is True

    def test_velocity_mode_still_uses_feedback_rpm(self) -> None:
        self._feed(0, velocity=1000)
        assert self.driver.is_target_reached(1002.0, ControlMode.VELOCITY) is True
        assert self.driver.is_target_reached(1020.0, ControlMode.VELOCITY) is False


class TestFeedbackPosition:
    """偏差監視 (左右直結軸) が使う共通 API が多回転累積角を返すこと。"""

    def setup_method(self) -> None:
        self.driver = M3508Driver("lift", can_id=1)

    def _feed_angle(self, angle_raw: int) -> None:
        feed_m3508(self.driver, angle_raw=angle_raw)

    def test_returns_multi_turn_position(self) -> None:
        self._feed_angle(0)
        self._feed_angle(2048)
        assert self.driver.feedback_position() == pytest.approx(self.driver.multi_turn_position)
        assert self.driver.feedback_position() == pytest.approx(90.0)

    def test_continuous_across_wraparound(self) -> None:
        # 単回転角は 0 に戻るが、偏差監視では連続した値でなければならない
        for raw in (0, 2048, 4096, 6144, 0):
            self._feed_angle(raw)
        assert self.driver.state.position == pytest.approx(0.0)
        assert self.driver.feedback_position() == pytest.approx(360.0)

    def test_negative_direction_is_continuous(self) -> None:
        for raw in (0, 6144, 4096, 2048, 0):
            self._feed_angle(raw)
        assert self.driver.feedback_position() == pytest.approx(-360.0)


class TestWrapInferenceAcrossFeedbackGap:
    """フィードバックが途切れた窓を跨いだときの折り返し推定。

    単回転角のアンラップは「半周を超える差分は 0 を跨いだ折り返し」という推定に
    立っており、**これはフィードバックが 1kHz で途切れず届いている間しか成り立たない**。
    途切れた窓でモータ軸が半周以上回ると方向を取り違え、累積角に 360deg が乗る。
    `config/main_hand_positions.yaml` の scale 55.0131deg/mm では 6.54mm 相当で、
    同じ y_axis の `sync_tolerance` 2.0mm の 3 倍を超える —— 実在しないずれで
    全体緊急停止が掛かる。

    窓は CAN 受信の中断で実際に開く (`scripts/can_watchdog.sh` の down/up は約 1 秒)。
    """

    def setup_method(self) -> None:
        self.clock = FakeClock()
        self.driver = M3508Driver("y_axis_r", can_id=1, time_source=self.clock)

    def _feed(self, angle_raw: int, *, rpm: int = 0, after_s: float = 0.001) -> None:
        self.clock.advance(after_s)
        feed_m3508(self.driver, angle_raw=angle_raw, rpm=rpm)

    @staticmethod
    def _deg(counts: float) -> float:
        return counts / 8192 * 360.0

    def test_平常の1ms間隔では従来どおり折り返しを推定する(self) -> None:
        # 途切れていないので推定は正しい。ここが壊れると多回転機構が動かなくなる
        self._feed(8000)
        self._feed(100)

        assert self.driver.multi_turn_position == pytest.approx(self._deg(292), abs=1e-6)
        assert self.driver.origin_trusted is True
        assert self.driver.health_detail() is None

    def test_長い窓を跨いだ差分は折り返しを推定せず累積しない(self) -> None:
        """1 回転ぶんの偽の飛びを作らない。

        実際に +4300 カウント (0.52 回転) 回ったとき、単回転角は
        (8000 + 4300) % 8192 = 4108 になる。差分は -3892 で半周に届かないため、
        推定を続けると「-3892 カウント動いた」と読む —— 真値との差は
        ちょうど 1 回転 (-8192 カウント = -360deg) になる。
        """
        self._feed(8000)
        self._feed(4108, after_s=1.0)

        # 偽の -3892 カウントを積んでいないこと (推定を諦めて再アンカーする)
        assert self.driver.multi_turn_position == pytest.approx(0.0)
        assert self.driver.origin_trusted is False
        assert self.driver.reanchor_count == 1

    def test_再アンカーはヘルスの詳細として報告される(self) -> None:
        # 黙って再アンカーすると、位置がずれたまま平常どおりに見える機体ができる
        self._feed(8000)
        self._feed(4108, after_s=1.0)

        detail = self.driver.health_detail()
        assert detail is not None
        assert "原点" in detail

    def test_高速回転なら短い窓でも折り返しを推定しない(self) -> None:
        # 3000rpm では 20ms で 1 回転する。窓が短くても半周を越えうる
        self._feed(8000, rpm=3000)
        self._feed(4108, rpm=3000, after_s=0.02)

        assert self.driver.origin_trusted is False

    def test_低速なら同じ窓でも折り返しを推定する(self) -> None:
        # 100rpm では 20ms で 0.033 回転。半周には遠く、推定は安全
        self._feed(8000, rpm=100)
        self._feed(100, rpm=100, after_s=0.02)

        assert self.driver.multi_turn_position == pytest.approx(self._deg(292), abs=1e-6)
        assert self.driver.origin_trusted is True

    def test_rpmが両端で0でも長すぎる窓は信じない(self) -> None:
        """窓の中で回って戻った場合、両端の rpm は上限にならない。

        rpm による見積もりだけだと「両端が 0 だから動いていない」と読むので、
        窓の長さそのものに歯止めが要る。
        """
        self._feed(8000, rpm=0)
        self._feed(4108, rpm=0, after_s=0.2)

        assert self.driver.origin_trusted is False

    def test_原点確定で信頼が戻る(self) -> None:
        # 「今どこにいるか」が改めて確定するので、それ以前のずれは意味を持たなくなる
        self._feed(8000)
        self._feed(4108, after_s=1.0)
        assert self.driver.origin_trusted is False

        self.driver.reset_multi_turn_origin()

        assert self.driver.origin_trusted is True
        assert self.driver.health_detail() is None

    def test_受信復帰だけでは信頼は戻らない(self) -> None:
        """ずれは受信が戻っても消えない。戻す経路は原点確定だけ。"""
        self._feed(8000)
        self._feed(4108, after_s=1.0)

        for _ in range(50):
            self._feed(4108)

        assert self.driver.origin_trusted is False


class TestWrapInferenceWhenFramesAreDropped:
    """**カーネルに捨てられた窓を「途切れていない」と読んではならない。**

    受信が 1kHz に追いつかないとソケットバッファが溢れ、カーネルはフレームを捨てる
    (実機の `can_m3508` は受信 369 万通に対し 77 万通 = 17% が `rx_dropped`)。
    このとき残った分は**滞留を詰めて処理される**ので、処理時刻 (単調クロック) で
    測った間隔は 1ms 程度にしか見えない —— 実際には数十 ms 途切れているのに。

    巡航 200mm/s では減速比込みでモータ軸 1834rpm なので、**16ms 欠けるだけで
    半周を越える**。そこへ「半周を超える差分は折り返し」という推定を当てると、
    累積角に 360deg (`y_axis` の scale 55.0131deg/mm で 6.54mm) が入る。しかも
    再アンカーの記録は残らないので、原点がずれたまま平常どおりに見える。

    症状は「動作中に軸が荒れて (左右が押し合って) 同期ずれで緊急停止」で、
    実機で発生済み。窓の長さは**フレーム自身のタイムスタンプ**で測るしかない。
    """

    # 巡航 200mm/s 相当。1834rpm = 8192counts * 1834 / 60 / 1000 ≒ 250counts/ms
    CRUISE_RPM = 1834
    COUNTS_PER_MS = 250

    def setup_method(self) -> None:
        self.clock = FakeClock()
        self.driver = M3508Driver("y_axis_r", can_id=1, time_source=self.clock)
        self.stamp = 5000.0
        self.angle = 0

    def _feed(self, *, elapsed_ms: float, processed_after_ms: float) -> None:
        """``elapsed_ms`` ぶん機構が進んだフィードバックを 1 通流す。

        ``processed_after_ms`` は「このプロセスが前の 1 通を処理してからの時間」で、
        滞留を詰めて処理している間は実経過より短くなる。
        """
        self.angle += int(self.COUNTS_PER_MS * elapsed_ms)
        self.stamp += elapsed_ms / 1000.0
        self.clock.advance(processed_after_ms / 1000.0)
        feed_m3508(
            self.driver,
            angle_raw=self.angle % 8192,
            rpm=self.CRUISE_RPM,
            timestamp=self.stamp,
        )

    def test_捨てられた窓は処理間隔が詰まっていても折り返しを推定しない(self) -> None:
        """壊れていると、この 1 通で累積角が 1 回転ぶん (6.54mm) 巻き戻る。

        30ms 欠けた間の実移動は 7500counts (0.92 回転)。単回転角の差分は
        7500 % 8192 = 7500 で半周を超えるため、推定を続けると -692counts と読む
        —— 真値との差はちょうど -8192counts = -360deg になる。
        """
        for _ in range(5):
            self._feed(elapsed_ms=1, processed_after_ms=1)
        before = self.driver.multi_turn_position

        # 30 通ぶんがカーネルで捨てられる。滞留を詰めているので処理間隔は 1ms
        self._feed(elapsed_ms=30, processed_after_ms=1)

        advanced = self.driver.multi_turn_position - before
        assert advanced == pytest.approx(0.0), (
            f"捨てられた窓に折り返し推定を当てている (累積角が {advanced:.1f}deg 動いた)"
        )
        assert self.driver.reanchor_count == 1
        assert self.driver.origin_trusted is False

    def test_取りこぼしの報告はヘルスに出る(self) -> None:
        """黙って再アンカーすると、ずれた原点のまま平常に見える機体ができる。"""
        self._feed(elapsed_ms=1, processed_after_ms=1)
        self._feed(elapsed_ms=30, processed_after_ms=1)

        detail = self.driver.health_detail()
        assert detail is not None
        assert "原点" in detail

    def test_滞留しているだけで取りこぼしが無ければ推定を続ける(self) -> None:
        """**処理が遅れただけで再アンカーしてはならない。**

        イベントループが 30ms 止まっても、フレームがバッファに残っていれば
        1 通も失われていない。そこで再アンカーすると、原点の信頼を失う理由が
        「処理が遅れた」だけになり、`health_detail` が実害の無い警告で埋まる。
        """
        self._feed(elapsed_ms=1, processed_after_ms=1)
        self._feed(elapsed_ms=1, processed_after_ms=30)

        assert self.driver.reanchor_count == 0
        assert self.driver.origin_trusted is True

    def test_時刻を持たないフレームでは処理時刻で測る(self) -> None:
        """virtual バス (``--dry-run``) のフレームは時刻を持たない。

        そこで「間隔 0」に倒すと、どれだけ途切れても推定を続けることになる。
        時刻が無いなら処理時刻で測るのが唯一の手掛かりで、安全側でもある。
        """
        driver = M3508Driver("y_axis_r", can_id=1, time_source=self.clock)
        feed_m3508(driver, angle_raw=8000, rpm=self.CRUISE_RPM)
        self.clock.advance(1.0)
        feed_m3508(driver, angle_raw=4108, rpm=self.CRUISE_RPM)

        assert driver.origin_trusted is False

    def test_時刻が巻き戻ったら処理時刻へ落ちる(self) -> None:
        """カーネルの時刻は壁時計なので NTP 補正で飛びうる。

        逆走した差は「途切れた時間」として意味を成さない。0 と読むと、そこだけ
        推定の歯止めが外れる。
        """
        feed_m3508(self.driver, angle_raw=0, rpm=self.CRUISE_RPM, timestamp=5000.0)
        self.clock.advance(1.0)
        feed_m3508(self.driver, angle_raw=4108, rpm=self.CRUISE_RPM, timestamp=4999.0)

        assert self.driver.origin_trusted is False
