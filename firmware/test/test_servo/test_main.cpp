// ServoMotion（角度補間・可動範囲クランプ・到達推定）の native ユニットテスト。
// 実機を用意せずにサーボ固有の取り違えを検出するのが目的なので、
// ここで検証するのはすべて docs/motor_driver_can_protocol.md §7 に明記された挙動に限る。

#include <unity.h>

#include <math.h>

#include "MotorCanProtocol.h"
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

// 化けた float32 が目標角として通ると、クランプもパルス変換もすり抜けて
// サーボが不定の位置へ飛ぶ。NaN は指令ごと捨てる。
static void test_set_target_ignores_nan() {
    ServoMotion motion(5.0f, wideLimits());
    motion.setTarget(30.0f, 0);
    motion.setTarget(NAN, 0);
    TEST_ASSERT_EQUAL_FLOAT(30.0f, motion.targetAngleDeg());
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
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 90.0f, motion.currentSlewDegPerSec());
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

static void test_slew_is_zero_while_idle() {
    ServoMotion motion(0.0f, wideLimits());
    motion.update(100);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, motion.currentSlewDegPerSec());
    TEST_ASSERT_TRUE(motion.isReached());

    motion.setTarget(45.0f, 100);
    motion.update(600);
    TEST_ASSERT_TRUE(motion.isReached());
    TEST_ASSERT_EQUAL_FLOAT(0.0f, motion.currentSlewDegPerSec());
}

static void test_slew_sign_follows_direction() {
    ServoMotion motion(90.0f, wideLimits());
    motion.setTarget(0.0f, 0);
    motion.update(100);
    TEST_ASSERT_FLOAT_WITHIN(0.01f, -90.0f, motion.currentSlewDegPerSec());
}

// SET_PARAM 0x07（reached_tolerance）。既定 0 は「補間完了＝到達」を意味する。
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
    TEST_ASSERT_EQUAL_FLOAT(0.0f, motion.currentSlewDegPerSec());
}

// --------------------------------------------------------------------------
// §7.6 SET_PARAM 0x10-0x12（可動範囲・スルーレートの実行時変更）
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
// §7.6 SET_PARAM のサーボ向け ID
// --------------------------------------------------------------------------

static void buildParamFrame(uint8_t *out, uint8_t id, float value) {
    for (uint8_t i = 0; i < kFrameLength; ++i) {
        out[i] = 0;
    }
    out[0] = id;
    packFloatLe(&out[2], value);
}

static void test_decode_servo_param_accepts_servo_ids() {
    uint8_t frame[kFrameLength];

    buildParamFrame(frame, 0x10, 120.0f);
    ServoParamCommand cmd = decodeServoSetParam(frame, kFrameLength);
    TEST_ASSERT_TRUE(cmd.valid);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(ServoParamId::SlewRate),
                            static_cast<uint8_t>(cmd.id));
    TEST_ASSERT_EQUAL_FLOAT(120.0f, cmd.value);

    buildParamFrame(frame, 0x11, -10.0f);
    cmd = decodeServoSetParam(frame, kFrameLength);
    TEST_ASSERT_TRUE(cmd.valid);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(ServoParamId::AngleMin),
                            static_cast<uint8_t>(cmd.id));

    buildParamFrame(frame, 0x12, 200.0f);
    cmd = decodeServoSetParam(frame, kFrameLength);
    TEST_ASSERT_TRUE(cmd.valid);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(ServoParamId::AngleMax),
                            static_cast<uint8_t>(cmd.id));
}

static void test_decode_servo_param_accepts_shared_ids() {
    uint8_t frame[kFrameLength];
    const uint8_t shared[] = {0x04, 0x05, 0x07};
    for (uint8_t id : shared) {
        buildParamFrame(frame, id, 250.0f);
        const ServoParamCommand cmd = decodeServoSetParam(frame, kFrameLength);
        TEST_ASSERT_TRUE(cmd.valid);
        TEST_ASSERT_EQUAL_UINT8(id, static_cast<uint8_t>(cmd.id));
    }
}

// 仕様書 §7.6: kp / ki / kd / max_duty / overcurrent は制御則を持たないので無視する。
static void test_decode_servo_param_ignores_dc_only_ids() {
    uint8_t frame[kFrameLength];
    const uint8_t ignored[] = {0x00, 0x01, 0x02, 0x03, 0x06, 0x08, 0x13, 0xFF};
    for (uint8_t id : ignored) {
        buildParamFrame(frame, id, 1.0f);
        TEST_ASSERT_FALSE(decodeServoSetParam(frame, kFrameLength).valid);
    }
}

static void test_decode_servo_param_rejects_short_frame() {
    uint8_t frame[kFrameLength];
    buildParamFrame(frame, 0x10, 120.0f);
    TEST_ASSERT_FALSE(decodeServoSetParam(frame, 5).valid);
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
    RUN_TEST(test_set_target_ignores_nan);
    RUN_TEST(test_slew_rate_limits_motion);
    RUN_TEST(test_reach_time_matches_distance_over_slew_rate);
    RUN_TEST(test_slew_is_zero_while_idle);
    RUN_TEST(test_slew_sign_follows_direction);
    RUN_TEST(test_reached_tolerance_reports_early);
    RUN_TEST(test_survives_millis_wraparound);
    RUN_TEST(test_hold_here_freezes_target_at_current_angle);
    RUN_TEST(test_set_limits_clamps_existing_target);
    RUN_TEST(test_set_limits_rejects_non_positive_slew_rate);
    RUN_TEST(test_set_limits_normalizes_inverted_range);
    RUN_TEST(test_set_limits_does_not_jump);
    RUN_TEST(test_decode_servo_param_accepts_servo_ids);
    RUN_TEST(test_decode_servo_param_accepts_shared_ids);
    RUN_TEST(test_decode_servo_param_ignores_dc_only_ids);
    RUN_TEST(test_decode_servo_param_rejects_short_frame);
    return UNITY_END();
}
