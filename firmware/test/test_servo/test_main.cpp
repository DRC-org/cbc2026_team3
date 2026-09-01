// ServoMotion（角度補間・可動範囲クランプ・到達推定）と ServoChannel
// （安全機構と補間の結線）の native ユニットテスト。
// 実機を用意せずにサーボ固有の取り違えを検出するのが目的なので、
// ここで検証するのはすべて docs/motor_driver_can_protocol.md §7 に明記された挙動に限る。

#include <unity.h>

#include <math.h>
#include <string.h>

#include "MotorCanProtocol.h"
#include "ServoChannel.h"
#include "ServoMotion.h"

using namespace motorcan;


void setUp() {}
void tearDown() {}

// 270 度サーボの典型値。実機のデータシート値は config.h 側で持つ。
static const ServoPulseSpec kSpec270{500, 2500, 270.0f};

// 可動範囲 0–180deg / 90deg/s。所要時間が暗算できる値にしてある。
static ServoLimits wideLimits() { return ServoLimits{0.0f, 180.0f, 90.0f}; }

// --------------------------------------------------------------------------
// §7.2 角度 → パルス幅
// --------------------------------------------------------------------------

static void test_angle_to_pulse_at_range_ends() {
    TEST_ASSERT_EQUAL_UINT16(500, angleToPulseUs(0.0f, kSpec270));
    TEST_ASSERT_EQUAL_UINT16(2500, angleToPulseUs(270.0f, kSpec270));
}

static void test_angle_to_pulse_is_linear() {
    TEST_ASSERT_EQUAL_UINT16(1500, angleToPulseUs(135.0f, kSpec270));
    TEST_ASSERT_EQUAL_UINT16(1000, angleToPulseUs(67.5f, kSpec270));
    TEST_ASSERT_EQUAL_UINT16(2000, angleToPulseUs(202.5f, kSpec270));
}

// サンプルコードのように角度へ 180/270 を掛けて write() に渡すと、
// 可動範囲の上端がパルス幅の上端に届かなくなる。スケール変換をしていないことの確認。
static void test_angle_to_pulse_reaches_max_at_full_range() {
    TEST_ASSERT_TRUE(angleToPulseUs(180.0f, kSpec270) < angleToPulseUs(270.0f, kSpec270));
    TEST_ASSERT_EQUAL_UINT16(kSpec270.maxUs, angleToPulseUs(270.0f, kSpec270));
}

// 可動範囲外の角度でメカストッパに当たり続けると焼損する（仕様書 §7.2）。
// クランプの一次防壁は ServoMotion 側だが、変換側も外へ出さない。
static void test_angle_to_pulse_clamps_out_of_range() {
    TEST_ASSERT_EQUAL_UINT16(500, angleToPulseUs(-10.0f, kSpec270));
    TEST_ASSERT_EQUAL_UINT16(2500, angleToPulseUs(400.0f, kSpec270));
}

static void test_angle_to_pulse_rejects_degenerate_spec() {
    const ServoPulseSpec zeroRange{500, 2500, 0.0f};
    TEST_ASSERT_EQUAL_UINT16(500, angleToPulseUs(10.0f, zeroRange));

    const ServoPulseSpec nanAngle{500, 2500, 270.0f};
    TEST_ASSERT_EQUAL_UINT16(500, angleToPulseUs(NAN, nanAngle));
}

// --------------------------------------------------------------------------
// §7.2 可動範囲クランプ
// --------------------------------------------------------------------------

static void test_set_target_clamps_to_limits() {
    const ServoLimits limits{10.0f, 20.0f, 1000.0f};
    ServoMotion motion(15.0f, limits);

    motion.setTarget(90.0f, 0);
    TEST_ASSERT_EQUAL_FLOAT(20.0f, motion.targetAngleDeg());

    motion.setTarget(-90.0f, 0);
    TEST_ASSERT_EQUAL_FLOAT(10.0f, motion.targetAngleDeg());

    motion.setTarget(12.5f, 0);
    TEST_ASSERT_EQUAL_FLOAT(12.5f, motion.targetAngleDeg());
}

static void test_initial_angle_is_clamped() {
    const ServoLimits limits{10.0f, 20.0f, 90.0f};
    ServoMotion motion(999.0f, limits);
    TEST_ASSERT_EQUAL_FLOAT(20.0f, motion.currentAngleDeg());
    TEST_ASSERT_EQUAL_FLOAT(20.0f, motion.targetAngleDeg());
}

// NaN は ServoMotion では防いでいない。**CAN は int16 しか運ばず**（仕様書 §4）、
// float が入る唯一の経路であるシリアルデバッグは motorcan::toRaw を通すので、
// NaN が内部へ到達する道が構造的に無い。防御を 1 箇所へ寄せた形。
static void test_fixed_point_keeps_nan_out_of_the_motion_layer() {
    // シリアル経路と同じ変換を通せば、NaN は 0 に飽和して届く
    const float sanitized = fromRaw(toRaw(NAN, kAngleScale), kAngleScale);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, sanitized);

    ServoMotion motion(30.0f, ServoLimits{0.0f, 90.0f, 90.0f});
    motion.setTarget(sanitized, 0);
    motion.update(10000);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, motion.currentAngleDeg());
}

// --------------------------------------------------------------------------
// §7.3 スルーレート制限と到達推定
// --------------------------------------------------------------------------

static void test_slew_rate_limits_motion() {
    ServoMotion motion(0.0f, wideLimits());
    motion.setTarget(180.0f, 0);

    motion.update(100);
    // 90deg/s × 0.1s = 9deg を超えて動いてはならない
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 9.0f, motion.currentAngleDeg());
    TEST_ASSERT_FALSE(motion.isReached());

    // 次の 0.1s でも同じだけしか進まない（定速であること）
    motion.update(200);
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 18.0f, motion.currentAngleDeg());
}

static void test_reach_time_matches_distance_over_slew_rate() {
    ServoMotion motion(0.0f, wideLimits());
    motion.setTarget(90.0f, 0);  // 90deg / 90deg/s = 1000ms

    for (uint32_t t = 10; t <= 990; t += 10) {
        motion.update(t);
        TEST_ASSERT_FALSE(motion.isReached());
    }
    motion.update(1000);
    TEST_ASSERT_TRUE(motion.isReached());
    TEST_ASSERT_EQUAL_FLOAT(90.0f, motion.currentAngleDeg());
}

// 目標に達したら、以後どれだけ時間が経っても角度が動かないこと。
// 「静止中」を進行の有無で見る（速度の観測値そのものはプロトコルに無い。仕様書 §3.2）。
static void test_angle_is_still_while_idle() {
    ServoMotion motion(0.0f, wideLimits());
    motion.update(100);
    TEST_ASSERT_TRUE(motion.isReached());
    TEST_ASSERT_EQUAL_FLOAT(0.0f, motion.currentAngleDeg());

    motion.setTarget(45.0f, 100);
    motion.update(600);
    TEST_ASSERT_TRUE(motion.isReached());
    TEST_ASSERT_EQUAL_FLOAT(45.0f, motion.currentAngleDeg());
    motion.update(60000);
    TEST_ASSERT_EQUAL_FLOAT(45.0f, motion.currentAngleDeg());
}

// 目標が現在角より小さいときは減る方向へ補間する。符号を落とすと、
// 戻す指令のたびにサーボが逆側のメカストッパへ走る。
static void test_motion_follows_direction() {
    ServoMotion motion(90.0f, wideLimits());
    motion.setTarget(0.0f, 0);
    motion.update(100);
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 81.0f, motion.currentAngleDeg());
}

// SET_PARAM 0x03（reached_tolerance）。既定 0 は「補間完了＝到達」を意味する。
static void test_reached_tolerance_reports_early() {
    ServoMotion motion(0.0f, wideLimits());
    motion.setReachedToleranceDeg(5.0f);
    motion.setTarget(90.0f, 0);

    motion.update(900);  // 残り 9deg
    TEST_ASSERT_FALSE(motion.isReached());
    motion.update(950);  // 残り 4.5deg
    TEST_ASSERT_TRUE(motion.isReached());
}

// millis() は約 49.7 日で 0 に戻る。符号なし減算で経過時間を出していれば
// 折り返しをまたいでも補間は継続する。
static void test_survives_millis_wraparound() {
    ServoMotion motion(0.0f, wideLimits());
    const uint32_t start = 0xFFFFFF00u;

    motion.setTarget(90.0f, start);
    motion.update(start + 500u);  // 折り返し後の小さい値になる
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 45.0f, motion.currentAngleDeg());
    TEST_ASSERT_FALSE(motion.isReached());

    motion.update(start + 1000u);
    TEST_ASSERT_TRUE(motion.isReached());
    TEST_ASSERT_EQUAL_FLOAT(90.0f, motion.currentAngleDeg());
}

// --------------------------------------------------------------------------
// §7.5 現在角の保持
// --------------------------------------------------------------------------

static void test_hold_here_freezes_target_at_current_angle() {
    ServoMotion motion(0.0f, wideLimits());
    motion.setTarget(180.0f, 0);
    motion.update(500);

    const float held = motion.currentAngleDeg();
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 45.0f, held);

    motion.holdHere(500);
    TEST_ASSERT_EQUAL_FLOAT(held, motion.targetAngleDeg());
    TEST_ASSERT_TRUE(motion.isReached());

    motion.update(5000);
    TEST_ASSERT_EQUAL_FLOAT(held, motion.currentAngleDeg());
}

// --------------------------------------------------------------------------
// §7.6 SET_PARAM 0x04-0x06（可動範囲・スルーレートの実行時変更）
// --------------------------------------------------------------------------

static void test_set_limits_clamps_existing_target() {
    ServoMotion motion(0.0f, wideLimits());
    motion.setTarget(180.0f, 0);
    motion.update(100);

    motion.setLimits(ServoLimits{0.0f, 30.0f, 90.0f});
    TEST_ASSERT_EQUAL_FLOAT(30.0f, motion.targetAngleDeg());

    for (uint32_t t = 200; t <= 5000; t += 100) {
        motion.update(t);
    }
    TEST_ASSERT_EQUAL_FLOAT(30.0f, motion.currentAngleDeg());
}

// slew_rate 0 は「即座に飛ぶ」とも「永久に到達しない」とも読めてどちらも危険なので、
// 非正の値は採用せず従来値を維持する。
static void test_set_limits_rejects_non_positive_slew_rate() {
    ServoMotion motion(0.0f, wideLimits());
    motion.setLimits(ServoLimits{0.0f, 180.0f, 0.0f});
    TEST_ASSERT_EQUAL_FLOAT(90.0f, motion.limits().slewRateDegPerSec);

    motion.setLimits(ServoLimits{0.0f, 180.0f, -5.0f});
    TEST_ASSERT_EQUAL_FLOAT(90.0f, motion.limits().slewRateDegPerSec);
}

// angle_min > angle_max のまま使うとクランプが定義できない。入れ替えて成立させる。
static void test_set_limits_normalizes_inverted_range() {
    ServoMotion motion(30.0f, wideLimits());
    motion.setLimits(ServoLimits{50.0f, 10.0f, 90.0f});
    TEST_ASSERT_EQUAL_FLOAT(10.0f, motion.limits().angleMinDeg);
    TEST_ASSERT_EQUAL_FLOAT(50.0f, motion.limits().angleMaxDeg);
}

// **現在角が新しい範囲の外へ出るケース。** かつて setLimits は
// `currentAngleDeg_ = clampAngle(currentAngleDeg_)` で現在角そのものを書き換えており、
// 可動範囲を狭めた瞬間に**スルーレート制限の外側で**指令パルスが飛んだ。
// 上の 2 本は「範囲を広げる」「現在角が既に新範囲内」しか見ておらず、この形を
// 1 件も押さえていなかった。
static void test_set_limits_does_not_teleport_current_angle() {
    ServoMotion motion(0.0f, ServoLimits{0.0f, 30.0f, 90.0f});
    motion.update(100);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, motion.currentAngleDeg());

    motion.setLimits(ServoLimits{20.0f, 30.0f, 90.0f});
    TEST_ASSERT_EQUAL_FLOAT(0.0f, motion.currentAngleDeg());
    // 制約が掛かるのは目標だけ
    TEST_ASSERT_EQUAL_FLOAT(20.0f, motion.targetAngleDeg());

    // 範囲内へはスルーレート制限に従って戻る（90deg/s なので 100ms で 9deg）
    motion.update(200);
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 9.0f, motion.currentAngleDeg());
    motion.update(500);
    TEST_ASSERT_EQUAL_FLOAT(20.0f, motion.currentAngleDeg());
}

// setLimits は直近に観測した時刻へ補間をアンカーし直す。
// し直さないと、スルーレート変更が過去の経過時間にさかのぼって効いて角度が飛ぶ。
static void test_set_limits_does_not_jump() {
    ServoMotion motion(0.0f, wideLimits());
    motion.setTarget(180.0f, 0);
    motion.update(100);
    const float before = motion.currentAngleDeg();

    motion.setLimits(ServoLimits{0.0f, 180.0f, 900.0f});
    motion.update(100);
    TEST_ASSERT_EQUAL_FLOAT(before, motion.currentAngleDeg());
}

// --------------------------------------------------------------------------
// §3.3 SET_PARAM（サーボが使う ID）
// --------------------------------------------------------------------------

// かつてはサーボ専用の ServoParamId / decodeServoSetParam があり、同じフレームに
// 2 つの enum と 2 つの復号器が並んでいた。ID を 1 つの表へ詰めたので、
// 各基板は「自分が使わない ID を無視する」だけになった。
static void test_servo_params_share_one_table() {
    uint8_t frame[3] = {0, 0, 0};
    const ParamId used[] = {ParamId::CommandTimeoutMs, ParamId::FeedbackIntervalMs,
                            ParamId::ReachedTolerance, ParamId::SlewRate, ParamId::AngleMin,
                            ParamId::AngleMax};
    for (ParamId id : used) {
        frame[0] = static_cast<uint8_t>(id);
        packInt16Le(&frame[1], 900);
        const SetParamCommand cmd = decodeSetParam(frame, 3);
        TEST_ASSERT_TRUE(cmd.valid);
        TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(id), static_cast<uint8_t>(cmd.id));
        TEST_ASSERT_EQUAL_INT16(900, cmd.raw);
    }
}

// 未知の ID は無視する（新しい PC 側ファームと古い基板が混在しても止まらないように）。
static void test_unknown_param_id_is_ignored() {
    uint8_t frame[3] = {0x42, 0, 0};
    TEST_ASSERT_FALSE(decodeSetParam(frame, 3).valid);
}

// --------------------------------------------------------------------------
// §7.5 ServoChannel（緊急停止・ウォッチドッグと補間の結線）
// --------------------------------------------------------------------------

// PC は §5.1 の契約どおり最後の指令を 20Hz で再送し続ける。緊急停止ラッチ中に
// その再送を受け付けると、再送のたびに補間が再アンカーされ、次のモーションティックで
// slew_rate × 5ms ずつ進んでから凍結する。既定なら 1 秒のラッチで 7.2 度。
// グリッパの全ストロークは 5 度・壁は 6 度なので、緊急停止中に可動範囲を丸ごと動ける。
static void test_latched_channel_does_not_creep_under_resent_targets() {
    ServoChannel channel(0.0f, wideLimits(), 500);
    channel.feed(0);
    channel.tick(0);

    uint8_t stop[8] = {0};
    channel.handleEStopFrame(stop, 8, 0);
    const float held = channel.currentAngleDeg();

    // 実機の loop() と同じ順序で 1 秒回す（pollCan が 50ms 周期、updateMotion が 5ms 周期）
    for (uint32_t t = 1; t <= 1000; ++t) {
        if (t % 50 == 0) {
            channel.feed(t);
            channel.setTarget(180.0f, t);
        }
        if (t % 5 == 0) {
            channel.tick(t);
        }
    }

    TEST_ASSERT_EQUAL_FLOAT(held, channel.currentAngleDeg());
}

// 仕様書 §7.5 の「新しい角度指令の受け付けを止め」は文字どおり受け口で止める。
// 補間側の凍結だけに頼ると、指令から次のティックまでの間、目標角だけが書き換わった
// 状態が残る。その間 FEEDBACK bit0（到達）は偽の未到達を報告し、PC 側 move_to は
// 緊急停止中の機体が動くのを待ち続ける。
static void test_latched_channel_rejects_targets_at_the_entrance() {
    ServoChannel channel(0.0f, wideLimits(), 500);
    channel.feed(0);
    channel.tick(0);
    TEST_ASSERT_TRUE(channel.isReached());

    uint8_t stop[8] = {0};
    channel.handleEStopFrame(stop, 8, 0);

    channel.feed(10);
    TEST_ASSERT_FALSE(channel.setTarget(180.0f, 10));
    TEST_ASSERT_EQUAL_FLOAT(0.0f, channel.currentAngleDeg());
    TEST_ASSERT_TRUE(channel.isReached());
}

// SET_TARGET と E_STOP 解除が同じ pollCan() のバッチに入ると、解除の時点では
// まだ 1 度もモーションティックが走っていない。ラッチ中の指令を受け付けていると、
// 解除直後の補間がその角度へ駆動する（＝ラッチ中に予約した動きが解除で実行される）。
static void test_target_commanded_while_latched_does_not_survive_release() {
    ServoChannel channel(0.0f, wideLimits(), 500);
    channel.feed(0);
    channel.tick(0);

    uint8_t stop[8] = {0};
    channel.handleEStopFrame(stop, 8, 0);

    // 同じバッチ内: SET_TARGET → E_STOP 解除
    channel.feed(1);
    channel.setTarget(30.0f, 1);
    uint8_t clear[8] = {0x01, 0x5A, 0xA5, 0, 0, 0, 0, 0};
    channel.handleEStopFrame(clear, 8, 1);

    for (uint32_t t = 5; t <= 1000; t += 5) {
        channel.tick(t);
    }
    TEST_ASSERT_EQUAL_FLOAT(0.0f, channel.currentAngleDeg());
}

// ウォッチドッグ満了には E_STOP フレームのような「その場で凍結する」きっかけが無い。
// 補間を進めてから凍結すると、満了の瞬間に 1 ティック分だけ動く。
static void test_watchdog_expiry_freezes_before_interpolating() {
    ServoChannel channel(0.0f, wideLimits(), 500);
    channel.feed(0);
    channel.setTarget(180.0f, 0);

    for (uint32_t t = 5; t <= 495; t += 5) {
        channel.tick(t);
    }
    const float atExpiry = channel.currentAngleDeg();
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 44.55f, atExpiry);

    channel.tick(500);
    TEST_ASSERT_EQUAL_FLOAT(atExpiry, channel.currentAngleDeg());
    channel.tick(505);
    TEST_ASSERT_EQUAL_FLOAT(atExpiry, channel.currentAngleDeg());
}

// 受理判定は「養ってから」でなければならない。順序を逆にすると、電源投入後の
// 最初の 1 通が §5.4 の「未受信＝出力禁止」で捨てられ、以後も同じ理由で捨て続ける
// （＝永久に動かない基板になる）。
static void test_first_target_after_power_on_is_accepted() {
    ServoChannel channel(0.0f, wideLimits(), 500);

    channel.feed(0);
    TEST_ASSERT_TRUE(channel.setTarget(90.0f, 0));

    channel.tick(100);
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 9.0f, channel.currentAngleDeg());
}

// 途絶から復帰したら動かせること（ウォッチドッグはラッチしない。仕様書 §5.1）。
static void test_channel_recovers_after_watchdog_and_release() {
    ServoChannel channel(0.0f, wideLimits(), 500);
    channel.feed(0);
    channel.setTarget(180.0f, 0);
    for (uint32_t t = 5; t <= 600; t += 5) {
        channel.tick(t);
    }
    const float stopped = channel.currentAngleDeg();

    channel.feed(600);
    TEST_ASSERT_TRUE(channel.setTarget(stopped + 9.0f, 600));
    channel.tick(700);
    TEST_ASSERT_FLOAT_WITHIN(0.01f, stopped + 9.0f, channel.currentAngleDeg());
}

// 緊急停止の解除そのものでは動かない（§3.5 の「解除した瞬間に動き出さない」）。
static void test_e_stop_release_alone_does_not_move() {
    ServoChannel channel(0.0f, wideLimits(), 500);
    channel.feed(0);
    channel.setTarget(180.0f, 0);
    channel.tick(100);
    const float held = channel.currentAngleDeg();

    uint8_t stop[8] = {0};
    channel.handleEStopFrame(stop, 8, 100);
    uint8_t clear[8] = {0x01, 0x5A, 0xA5, 0, 0, 0, 0, 0};
    channel.handleEStopFrame(clear, 8, 200);

    for (uint32_t t = 205; t <= 2000; t += 5) {
        channel.tick(t);
    }
    TEST_ASSERT_EQUAL_FLOAT(held, channel.currentAngleDeg());
}

// ウォッチドッグの無効化（config.h の WATCHDOG_ENABLED 0）でも、仕様書 §5.4 の
// 「SET_TARGET を 1 通も受け取るまで出力を許可しない」ゲートは外れない。
// **層ごとに単独で確かめる。** MotorSafety 層と SolenoidChannel 層には既にあるが、
// ServoChannel だけ無く、この結線を素通しにしても 1 件も落ちなかった。
// 外れると、CAN 通信を 1 通も受けないまま setup() の初期角へ駆動できる基板になる。
static void test_disabled_watchdog_still_requires_first_command() {
    ServoChannel channel(0.0f, wideLimits(), 500);
    channel.setWatchdogEnabled(false);

    TEST_ASSERT_FALSE(channel.isOutputAllowed(0));
    TEST_ASSERT_FALSE(channel.setTarget(90.0f, 0));

    channel.feed(1000);
    TEST_ASSERT_TRUE(channel.setTarget(90.0f, 1000));
    // 無効化してあるので途絶しても受け付け続ける
    TEST_ASSERT_TRUE(channel.isOutputAllowed(1000 + 500 * 10));
}

// --------------------------------------------------------------------------
// §7.5 出力禁止中の SET_PARAM（ServoChannel が持つ出力ゲート）
// --------------------------------------------------------------------------

// **SET_PARAM は setTarget と同じ入口の拒否を持っていなかった。** angle_min /
// angle_max は ServoChannel を素通りして ServoMotion へ届くので、緊急停止ラッチ中でも
// 目標角が新しい範囲へクランプされ、FEEDBACK bit0（到達）が偽の未到達を報告する
// —— PC 側 move_to は緊急停止中の機体が動くのを待ち続ける
// （test_latched_channel_rejects_targets_at_the_entrance と同じ壊れ方が、
// SET_TARGET ではなく SET_PARAM の側に残っていた）。
static void test_latched_channel_defers_limit_change() {
    ServoChannel channel(0.0f, ServoLimits{0.0f, 30.0f, 90.0f}, 500);
    channel.feed(0);
    channel.tick(0);

    uint8_t stop[8] = {0};
    channel.handleEStopFrame(stop, 8, 0);
    TEST_ASSERT_TRUE(channel.isReached());

    ServoLimits narrowed = channel.limits();
    narrowed.angleMinDeg = 20.0f;
    channel.setLimits(narrowed, 10);

    TEST_ASSERT_TRUE(channel.isReached());
    TEST_ASSERT_EQUAL_FLOAT(0.0f, channel.currentAngleDeg());

    // 解除そのものでは動かない（§3.5）。取り込んだ範囲で現在角を引きずらないこと。
    uint8_t clear[8] = {0x01, 0x5A, 0xA5, 0, 0, 0, 0, 0};
    channel.handleEStopFrame(clear, 8, 20);
    for (uint32_t t = 25; t <= 400; t += 5) {
        channel.tick(t);
    }
    TEST_ASSERT_EQUAL_FLOAT(0.0f, channel.currentAngleDeg());

    // **範囲は覚えている。** 解除後の SET_TARGET は新しい下限でクランプされる。
    channel.feed(400);
    TEST_ASSERT_TRUE(channel.setTarget(0.0f, 400));
    for (uint32_t t = 405; t <= 1400; t += 5) {
        channel.tick(t);
    }
    TEST_ASSERT_EQUAL_FLOAT(20.0f, channel.currentAngleDeg());
}

// **層を 1 枚だけにして見る。** 緊急停止ラッチを持たず、ウォッチドッグ満了だけで
// 出力が禁止されている状態でも同じ拒否が要る（ラッチ経路だけを塞いだ実装は
// 上のテストでは落ちない）。
static void test_watchdog_expired_channel_defers_limit_change() {
    ServoChannel channel(0.0f, ServoLimits{0.0f, 30.0f, 90.0f}, 500);
    channel.feed(0);
    channel.tick(0);

    channel.tick(600);
    TEST_ASSERT_FALSE(channel.isOutputAllowed(600));
    TEST_ASSERT_TRUE(channel.isReached());

    ServoLimits narrowed = channel.limits();
    narrowed.angleMinDeg = 20.0f;
    channel.setLimits(narrowed, 600);

    TEST_ASSERT_TRUE(channel.isReached());
    TEST_ASSERT_EQUAL_FLOAT(0.0f, channel.currentAngleDeg());
}

// 実機の applyParam は limits() を読んで 1 項目だけ書き換えて戻す。保留中に
// limits() が古い値を返すと、angle_min → angle_max と 2 通届いたときに 1 通目が消える。
static void test_limit_changes_while_latched_compose() {
    ServoChannel channel(0.0f, ServoLimits{0.0f, 30.0f, 90.0f}, 500);
    channel.feed(0);
    channel.tick(0);
    uint8_t stop[8] = {0};
    channel.handleEStopFrame(stop, 8, 0);

    ServoLimits limits = channel.limits();
    limits.angleMinDeg = 5.0f;
    channel.setLimits(limits, 10);

    limits = channel.limits();
    limits.angleMaxDeg = 12.0f;
    channel.setLimits(limits, 20);

    TEST_ASSERT_EQUAL_FLOAT(5.0f, channel.limits().angleMinDeg);
    TEST_ASSERT_EQUAL_FLOAT(12.0f, channel.limits().angleMaxDeg);

    uint8_t clear[8] = {0x01, 0x5A, 0xA5, 0, 0, 0, 0, 0};
    channel.handleEStopFrame(clear, 8, 30);
    channel.tick(35);
    TEST_ASSERT_EQUAL_FLOAT(5.0f, channel.limits().angleMinDeg);
    TEST_ASSERT_EQUAL_FLOAT(12.0f, channel.limits().angleMaxDeg);
}

// 保留した reached_tolerance は捨てない。捨てると、ラッチ中に設定した値が
// 「設定できたのに効かない」形で消える（PC 側からは区別が付かない）。
static void test_reached_tolerance_change_while_latched_survives_release() {
    ServoChannel channel(0.0f, wideLimits(), 500);
    channel.feed(0);
    channel.tick(0);
    uint8_t stop[8] = {0};
    channel.handleEStopFrame(stop, 8, 0);

    channel.setReachedToleranceDeg(5.0f, 10);

    uint8_t clear[8] = {0x01, 0x5A, 0xA5, 0, 0, 0, 0, 0};
    channel.handleEStopFrame(clear, 8, 20);

    channel.feed(20);
    TEST_ASSERT_TRUE(channel.setTarget(90.0f, 20));
    // PC は §5.1 の契約どおり再送し続ける（養わないと途中でウォッチドッグが満了する）
    for (uint32_t t = 25; t <= 920; t += 5) {
        channel.feed(t);
        channel.tick(t);
    }
    TEST_ASSERT_FALSE(channel.isReached());  // 残り 9deg

    for (uint32_t t = 925; t <= 970; t += 5) {
        channel.feed(t);
        channel.tick(t);
    }
    TEST_ASSERT_TRUE(channel.isReached());  // 残り 4.5deg → 許容差 5deg 以内
}

// --------------------------------------------------------------------------
// §7.2 受理する制御タイプ（ServoChannel が持つ）
// --------------------------------------------------------------------------

// **この関門は 3 枚の main.cpp にしか無かった。** ペリフェラルの翻訳単位は
// native テストの対象外（common.ini の `test_ignore = *`）なので、
// `if (cmd.type != ControlType::Position) return;` を消しても全ケース緑だった。
// duty の 0.3 が 0.3deg として、on_off の 1 が 0.1deg として通ってしまう。
static void test_servo_channel_accepts_only_position_targets() {
    ServoChannel channel(0.0f, wideLimits(), 500);
    channel.feed(0);

    const SetTargetCommand duty{ControlType::Duty, 900, true};
    TEST_ASSERT_FALSE(channel.applySetTarget(duty, 0));
    const SetTargetCommand velocity{ControlType::Velocity, 900, true};
    TEST_ASSERT_FALSE(channel.applySetTarget(velocity, 0));
    const SetTargetCommand onOff{ControlType::OnOff, 1, true};
    TEST_ASSERT_FALSE(channel.applySetTarget(onOff, 0));
    // 復号に失敗したフレーム（予約された制御タイプ・DLC 不足）も同じく捨てる
    const SetTargetCommand invalid{ControlType::Position, 900, false};
    TEST_ASSERT_FALSE(channel.applySetTarget(invalid, 0));

    channel.tick(0);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, channel.currentAngleDeg());

    // position だけが通り、0.1deg 単位の固定小数点として解釈される（§4）
    const SetTargetCommand position{ControlType::Position, 300, true};
    TEST_ASSERT_TRUE(channel.applySetTarget(position, 0));
    channel.tick(400);  // 30deg / 90deg/s = 334ms（ウォッチドッグ満了より手前）
    TEST_ASSERT_EQUAL_FLOAT(30.0f, channel.currentAngleDeg());
}

// 制御タイプの判定より安全ゲートが優先する（ラッチ中は position でも通さない）。
static void test_apply_set_target_still_honors_the_output_gate() {
    ServoChannel channel(0.0f, wideLimits(), 500);
    channel.feed(0);
    uint8_t stop[8] = {0};
    channel.handleEStopFrame(stop, 8, 0);

    const SetTargetCommand position{ControlType::Position, 900, true};
    channel.feed(10);
    TEST_ASSERT_FALSE(channel.applySetTarget(position, 10));
    channel.tick(10000);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, channel.currentAngleDeg());
}

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_angle_to_pulse_at_range_ends);
    RUN_TEST(test_angle_to_pulse_is_linear);
    RUN_TEST(test_angle_to_pulse_reaches_max_at_full_range);
    RUN_TEST(test_angle_to_pulse_clamps_out_of_range);
    RUN_TEST(test_angle_to_pulse_rejects_degenerate_spec);
    RUN_TEST(test_set_target_clamps_to_limits);
    RUN_TEST(test_initial_angle_is_clamped);
    RUN_TEST(test_fixed_point_keeps_nan_out_of_the_motion_layer);
    RUN_TEST(test_slew_rate_limits_motion);
    RUN_TEST(test_reach_time_matches_distance_over_slew_rate);
    RUN_TEST(test_angle_is_still_while_idle);
    RUN_TEST(test_motion_follows_direction);
    RUN_TEST(test_reached_tolerance_reports_early);
    RUN_TEST(test_survives_millis_wraparound);
    RUN_TEST(test_hold_here_freezes_target_at_current_angle);
    RUN_TEST(test_set_limits_clamps_existing_target);
    RUN_TEST(test_set_limits_rejects_non_positive_slew_rate);
    RUN_TEST(test_set_limits_normalizes_inverted_range);
    RUN_TEST(test_set_limits_does_not_teleport_current_angle);
    RUN_TEST(test_set_limits_does_not_jump);
    RUN_TEST(test_servo_params_share_one_table);
    RUN_TEST(test_unknown_param_id_is_ignored);
    RUN_TEST(test_latched_channel_does_not_creep_under_resent_targets);
    RUN_TEST(test_latched_channel_rejects_targets_at_the_entrance);
    RUN_TEST(test_target_commanded_while_latched_does_not_survive_release);
    RUN_TEST(test_watchdog_expiry_freezes_before_interpolating);
    RUN_TEST(test_first_target_after_power_on_is_accepted);
    RUN_TEST(test_channel_recovers_after_watchdog_and_release);
    RUN_TEST(test_e_stop_release_alone_does_not_move);
    RUN_TEST(test_disabled_watchdog_still_requires_first_command);
    RUN_TEST(test_latched_channel_defers_limit_change);
    RUN_TEST(test_watchdog_expired_channel_defers_limit_change);
    RUN_TEST(test_limit_changes_while_latched_compose);
    RUN_TEST(test_reached_tolerance_change_while_latched_survives_release);
    RUN_TEST(test_servo_channel_accepts_only_position_targets);
    RUN_TEST(test_apply_set_target_still_honors_the_output_gate);
    return UNITY_END();
}
