#include <unity.h>

#include <math.h>
#include <string.h>

#include "MotorCanProtocol.h"
#include "ServoChannel.h"
#include "ServoMotion.h"

using namespace motorcan;

void setUp() {}
void tearDown() {}

static const ServoPulseSpec kSpec270{500, 2500, 270.0f};

static ServoLimits wideLimits() { return ServoLimits{0.0f, 180.0f, 90.0f}; }

static void test_angle_to_pulse_at_range_ends() {
    TEST_ASSERT_EQUAL_UINT16(500, angleToPulseUs(0.0f, kSpec270));
    TEST_ASSERT_EQUAL_UINT16(2500, angleToPulseUs(270.0f, kSpec270));
}

static void test_angle_to_pulse_is_linear() {
    TEST_ASSERT_EQUAL_UINT16(1500, angleToPulseUs(135.0f, kSpec270));
    TEST_ASSERT_EQUAL_UINT16(1000, angleToPulseUs(67.5f, kSpec270));
    TEST_ASSERT_EQUAL_UINT16(2000, angleToPulseUs(202.5f, kSpec270));
}

static void test_angle_to_pulse_reaches_max_at_full_range() {
    TEST_ASSERT_TRUE(angleToPulseUs(180.0f, kSpec270) < angleToPulseUs(270.0f, kSpec270));
    TEST_ASSERT_EQUAL_UINT16(kSpec270.maxUs, angleToPulseUs(270.0f, kSpec270));
}

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

static void test_fixed_point_keeps_nan_out_of_the_motion_layer() {
    const float sanitized = fromRaw(toRaw(NAN, kAngleScale), kAngleScale);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, sanitized);

    ServoMotion motion(30.0f, ServoLimits{0.0f, 90.0f, 90.0f});
    motion.setTarget(sanitized, 0);
    motion.update(10000);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, motion.currentAngleDeg());
}

static void test_slew_rate_limits_motion() {
    ServoMotion motion(0.0f, wideLimits());
    motion.setTarget(180.0f, 0);

    motion.update(100);
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 9.0f, motion.currentAngleDeg());
    TEST_ASSERT_FALSE(motion.isReached());

    motion.update(200);
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 18.0f, motion.currentAngleDeg());
}

static void test_reach_time_matches_distance_over_slew_rate() {
    ServoMotion motion(0.0f, wideLimits());
    motion.setTarget(90.0f, 0);

    for (uint32_t t = 10; t <= 990; t += 10) {
        motion.update(t);
        TEST_ASSERT_FALSE(motion.isReached());
    }
    motion.update(1000);
    TEST_ASSERT_TRUE(motion.isReached());
    TEST_ASSERT_EQUAL_FLOAT(90.0f, motion.currentAngleDeg());
}

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

static void test_motion_follows_direction() {
    ServoMotion motion(90.0f, wideLimits());
    motion.setTarget(0.0f, 0);
    motion.update(100);
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 81.0f, motion.currentAngleDeg());
}

static void test_reached_tolerance_reports_early() {
    ServoMotion motion(0.0f, wideLimits());
    motion.setReachedToleranceDeg(5.0f);
    motion.setTarget(90.0f, 0);

    motion.update(900);
    TEST_ASSERT_FALSE(motion.isReached());
    motion.update(950);
    TEST_ASSERT_TRUE(motion.isReached());
}

static void test_survives_millis_wraparound() {
    ServoMotion motion(0.0f, wideLimits());
    const uint32_t start = 0xFFFFFF00u;

    motion.setTarget(90.0f, start);
    motion.update(start + 500u);
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 45.0f, motion.currentAngleDeg());
    TEST_ASSERT_FALSE(motion.isReached());

    motion.update(start + 1000u);
    TEST_ASSERT_TRUE(motion.isReached());
    TEST_ASSERT_EQUAL_FLOAT(90.0f, motion.currentAngleDeg());
}

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

static void test_set_limits_rejects_non_positive_slew_rate() {
    ServoMotion motion(0.0f, wideLimits());
    motion.setLimits(ServoLimits{0.0f, 180.0f, 0.0f});
    TEST_ASSERT_EQUAL_FLOAT(90.0f, motion.limits().slewRateDegPerSec);

    motion.setLimits(ServoLimits{0.0f, 180.0f, -5.0f});
    TEST_ASSERT_EQUAL_FLOAT(90.0f, motion.limits().slewRateDegPerSec);
}

static void test_set_limits_normalizes_inverted_range() {
    ServoMotion motion(30.0f, wideLimits());
    motion.setLimits(ServoLimits{50.0f, 10.0f, 90.0f});
    TEST_ASSERT_EQUAL_FLOAT(10.0f, motion.limits().angleMinDeg);
    TEST_ASSERT_EQUAL_FLOAT(50.0f, motion.limits().angleMaxDeg);
}

static void test_set_limits_does_not_teleport_current_angle() {
    ServoMotion motion(0.0f, ServoLimits{0.0f, 30.0f, 90.0f});
    motion.update(100);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, motion.currentAngleDeg());

    motion.setLimits(ServoLimits{20.0f, 30.0f, 90.0f});
    TEST_ASSERT_EQUAL_FLOAT(0.0f, motion.currentAngleDeg());
    TEST_ASSERT_EQUAL_FLOAT(20.0f, motion.targetAngleDeg());

    motion.update(200);
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 9.0f, motion.currentAngleDeg());
    motion.update(500);
    TEST_ASSERT_EQUAL_FLOAT(20.0f, motion.currentAngleDeg());
}

static void test_set_limits_does_not_jump() {
    ServoMotion motion(0.0f, wideLimits());
    motion.setTarget(180.0f, 0);
    motion.update(100);
    const float before = motion.currentAngleDeg();

    motion.setLimits(ServoLimits{0.0f, 180.0f, 900.0f});
    motion.update(100);
    TEST_ASSERT_EQUAL_FLOAT(before, motion.currentAngleDeg());
}

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

static void test_unknown_param_id_is_ignored() {
    uint8_t frame[3] = {0x42, 0, 0};
    TEST_ASSERT_FALSE(decodeSetParam(frame, 3).valid);
}

static void test_latched_channel_does_not_creep_under_resent_targets() {
    ServoChannel channel(0.0f, wideLimits(), 500);
    channel.feed(0);
    channel.tick(0);

    uint8_t stop[8] = {0};
    channel.handleEStopFrame(stop, 8, 0);
    const float held = channel.currentAngleDeg();

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

static void test_target_commanded_while_latched_does_not_survive_release() {
    ServoChannel channel(0.0f, wideLimits(), 500);
    channel.feed(0);
    channel.tick(0);

    uint8_t stop[8] = {0};
    channel.handleEStopFrame(stop, 8, 0);

    channel.feed(1);
    channel.setTarget(30.0f, 1);
    uint8_t clear[8] = {0x01, 0x5A, 0xA5, 0, 0, 0, 0, 0};
    channel.handleEStopFrame(clear, 8, 1);

    for (uint32_t t = 5; t <= 1000; t += 5) {
        channel.tick(t);
    }
    TEST_ASSERT_EQUAL_FLOAT(0.0f, channel.currentAngleDeg());
}

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

static void test_first_target_after_power_on_is_accepted() {
    ServoChannel channel(0.0f, wideLimits(), 500);

    channel.feed(0);
    TEST_ASSERT_TRUE(channel.setTarget(90.0f, 0));

    channel.tick(100);
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 9.0f, channel.currentAngleDeg());
}

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

static void test_disabled_watchdog_still_requires_first_command() {
    ServoChannel channel(0.0f, wideLimits(), 500);
    channel.setWatchdogEnabled(false);

    TEST_ASSERT_FALSE(channel.isOutputAllowed(0));
    TEST_ASSERT_FALSE(channel.setTarget(90.0f, 0));

    channel.feed(1000);
    TEST_ASSERT_TRUE(channel.setTarget(90.0f, 1000));
    TEST_ASSERT_TRUE(channel.isOutputAllowed(1000 + 500 * 10));
}

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

    uint8_t clear[8] = {0x01, 0x5A, 0xA5, 0, 0, 0, 0, 0};
    channel.handleEStopFrame(clear, 8, 20);
    for (uint32_t t = 25; t <= 400; t += 5) {
        channel.tick(t);
    }
    TEST_ASSERT_EQUAL_FLOAT(0.0f, channel.currentAngleDeg());

    channel.feed(400);
    TEST_ASSERT_TRUE(channel.setTarget(0.0f, 400));
    for (uint32_t t = 405; t <= 1400; t += 5) {
        channel.tick(t);
    }
    TEST_ASSERT_EQUAL_FLOAT(20.0f, channel.currentAngleDeg());
}

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
    for (uint32_t t = 25; t <= 920; t += 5) {
        channel.feed(t);
        channel.tick(t);
    }
    TEST_ASSERT_FALSE(channel.isReached());

    for (uint32_t t = 925; t <= 970; t += 5) {
        channel.feed(t);
        channel.tick(t);
    }
    TEST_ASSERT_TRUE(channel.isReached());
}

static void test_servo_channel_accepts_only_position_targets() {
    ServoChannel channel(0.0f, wideLimits(), 500);
    channel.feed(0);

    const SetTargetCommand duty{ControlType::Duty, 900, true};
    TEST_ASSERT_FALSE(channel.applySetTarget(duty, 0));
    const SetTargetCommand velocity{ControlType::Velocity, 900, true};
    TEST_ASSERT_FALSE(channel.applySetTarget(velocity, 0));
    const SetTargetCommand onOff{ControlType::OnOff, 1, true};
    TEST_ASSERT_FALSE(channel.applySetTarget(onOff, 0));
    const SetTargetCommand invalid{ControlType::Position, 900, false};
    TEST_ASSERT_FALSE(channel.applySetTarget(invalid, 0));

    channel.tick(0);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, channel.currentAngleDeg());

    const SetTargetCommand position{ControlType::Position, 300, true};
    TEST_ASSERT_TRUE(channel.applySetTarget(position, 0));
    channel.tick(400);
    TEST_ASSERT_EQUAL_FLOAT(30.0f, channel.currentAngleDeg());
}

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

static void test_channel_without_begin_never_allows_output() {
    ServoChannel channel;

    channel.feed(0);
    TEST_ASSERT_FALSE(channel.isOutputAllowed(0));
    TEST_ASSERT_FALSE(channel.setTarget(90.0f, 0));

    const SetTargetCommand position{ControlType::Position, 900, true};
    channel.feed(10);
    TEST_ASSERT_FALSE(channel.applySetTarget(position, 10));

    channel.tick(1000);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, channel.currentAngleDeg());
}

static void test_begin_installs_the_limits_used_for_clamping() {
    ServoChannel channel;
    channel.begin(0.0f, ServoLimits{0.0f, 30.0f, 90.0f}, 500);

    channel.feed(0);
    TEST_ASSERT_TRUE(channel.setTarget(180.0f, 0));
    channel.tick(400);
    TEST_ASSERT_EQUAL_FLOAT(30.0f, channel.currentAngleDeg());
}

static void test_begin_starts_at_the_given_initial_angle() {
    ServoChannel channel;
    channel.begin(12.0f, wideLimits(), 500);

    TEST_ASSERT_EQUAL_FLOAT(12.0f, channel.currentAngleDeg());
    TEST_ASSERT_TRUE(channel.isReached());
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
    RUN_TEST(test_channel_without_begin_never_allows_output);
    RUN_TEST(test_begin_installs_the_limits_used_for_clamping);
    RUN_TEST(test_begin_starts_at_the_given_initial_angle);
    return UNITY_END();
}
