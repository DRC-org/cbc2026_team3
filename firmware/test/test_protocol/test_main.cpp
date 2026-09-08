#include <unity.h>

#include <math.h>
#include <string.h>

#include "DcChannel.h"
#include "MotorCanProtocol.h"
#include "MotorSafety.h"

using namespace motorcan;

void setUp() {}
void tearDown() {}

static void test_build_can_id() {
    TEST_ASSERT_EQUAL_UINT16(0x002, buildCanId(CommandType::EStop, 0x02));
    TEST_ASSERT_EQUAL_UINT16(0x102, buildCanId(CommandType::SetTarget, 0x02));
    TEST_ASSERT_EQUAL_UINT16(0x202, buildCanId(CommandType::SetParam, 0x02));
    TEST_ASSERT_EQUAL_UINT16(0x302, buildCanId(CommandType::Feedback, 0x02));
    TEST_ASSERT_EQUAL_UINT16(0x402, buildCanId(CommandType::Info, 0x02));
    TEST_ASSERT_EQUAL_UINT16(0x0FF, buildCanId(CommandType::EStop, kDeviceIdBroadcast));
}

static void test_e_stop_outranks_every_other_frame() {
    const uint8_t dev = 0x7F;
    const uint16_t estop = buildCanId(CommandType::EStop, dev);
    TEST_ASSERT_TRUE(estop < buildCanId(CommandType::SetTarget, dev));
    TEST_ASSERT_TRUE(estop < buildCanId(CommandType::SetParam, dev));
    TEST_ASSERT_TRUE(estop < buildCanId(CommandType::Feedback, dev));
    TEST_ASSERT_TRUE(estop < buildCanId(CommandType::Info, dev));

    const uint16_t broadcast = buildCanId(CommandType::EStop, kDeviceIdBroadcast);
    TEST_ASSERT_EQUAL_UINT16(kBroadcastEStopCanId, broadcast);
    TEST_ASSERT_TRUE(broadcast < buildCanId(CommandType::SetTarget, 0x00));
}

static void test_parse_can_id_roundtrip() {
    const CommandType kinds[] = {CommandType::EStop, CommandType::SetTarget,
                                 CommandType::SetParam, CommandType::Feedback,
                                 CommandType::Info};
    for (CommandType kind : kinds) {
        for (uint16_t dev = 0; dev <= 0xFF; ++dev) {
            const CanIdInfo info = parseCanId(buildCanId(kind, static_cast<uint8_t>(dev)));
            TEST_ASSERT_TRUE(info.valid);
            TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(kind),
                                    static_cast<uint8_t>(info.command));
            TEST_ASSERT_EQUAL_UINT8(dev, info.deviceId);
        }
    }
}

static void test_parse_can_id_reserved_is_invalid() {
    for (uint16_t cmd = 5; cmd <= 7; ++cmd) {
        const CanIdInfo info = parseCanId(static_cast<uint16_t>((cmd << 8) | 0x02));
        TEST_ASSERT_FALSE(info.valid);
    }
}

static void test_parse_can_id_rejects_out_of_range() {
    TEST_ASSERT_FALSE(parseCanId(0x800).valid);
    TEST_ASSERT_FALSE(parseCanId(0xFFFF).valid);
}

static void test_to_raw_saturates_nan_and_out_of_range() {
    TEST_ASSERT_EQUAL_INT16(0, toRaw(NAN, kAngleScale));
    TEST_ASSERT_EQUAL_INT16(32767, toRaw(1e9f, kAngleScale));
    TEST_ASSERT_EQUAL_INT16(-32768, toRaw(-1e9f, kAngleScale));
}

static void test_fixed_point_roundtrip_keeps_the_unit() {
    TEST_ASSERT_EQUAL_INT16(900, toRaw(90.0f, kAngleScale));
    TEST_ASSERT_EQUAL_FLOAT(90.0f, fromRaw(900, kAngleScale));

    TEST_ASSERT_EQUAL_INT16(3000, toRaw(0.3f, kDutyScale));
    TEST_ASSERT_EQUAL_FLOAT(0.3f, fromRaw(3000, kDutyScale));
    TEST_ASSERT_EQUAL_INT16(-10000, toRaw(-1.0f, kDutyScale));

    TEST_ASSERT_EQUAL_INT16(56, toRaw(5.55f, kAngleScale));
    TEST_ASSERT_EQUAL_INT16(-56, toRaw(-5.55f, kAngleScale));
}

static void test_decode_set_target() {
    const uint8_t data[3] = {static_cast<uint8_t>(ControlType::Duty), 0xB8, 0x0B};
    const SetTargetCommand cmd = decodeSetTarget(data, 3);
    TEST_ASSERT_TRUE(cmd.valid);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(ControlType::Duty),
                            static_cast<uint8_t>(cmd.type));
    TEST_ASSERT_EQUAL_INT16(3000, cmd.raw);
    TEST_ASSERT_EQUAL_FLOAT(0.3f, fromRaw(cmd.raw, kDutyScale));
}

static void test_decode_set_target_keeps_sign() {
    const uint8_t data[3] = {static_cast<uint8_t>(ControlType::Duty), 0x48, 0xF4};
    const SetTargetCommand cmd = decodeSetTarget(data, 3);
    TEST_ASSERT_TRUE(cmd.valid);
    TEST_ASSERT_EQUAL_INT16(-3000, cmd.raw);
}

static void test_decode_set_target_rejects_unknown_type() {
    uint8_t data[3] = {0};
    data[0] = 4;
    TEST_ASSERT_FALSE(decodeSetTarget(data, 3).valid);
    data[0] = 0xFF;
    TEST_ASSERT_FALSE(decodeSetTarget(data, 3).valid);
}

static void test_decode_set_target_accepts_on_off() {
    const uint8_t data[3] = {static_cast<uint8_t>(ControlType::OnOff), 0x01, 0x00};
    const SetTargetCommand cmd = decodeSetTarget(data, 3);
    TEST_ASSERT_TRUE(cmd.valid);
    TEST_ASSERT_EQUAL_UINT8(3, static_cast<uint8_t>(cmd.type));
    TEST_ASSERT_EQUAL_INT16(1, cmd.raw);
}

static void test_decode_set_target_rejects_short_frame() {
    uint8_t data[3] = {0};
    TEST_ASSERT_FALSE(decodeSetTarget(data, 2).valid);
}

static void test_decode_set_param() {
    const uint8_t data[3] = {static_cast<uint8_t>(ParamId::MaxDuty), 0x88, 0x13};
    const SetParamCommand cmd = decodeSetParam(data, 3);
    TEST_ASSERT_TRUE(cmd.valid);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(ParamId::MaxDuty),
                            static_cast<uint8_t>(cmd.id));
    TEST_ASSERT_EQUAL_FLOAT(0.5f, fromRaw(cmd.raw, kDutyScale));
}

static void test_param_ids_are_packed() {
    TEST_ASSERT_EQUAL_UINT8(0x00, static_cast<uint8_t>(ParamId::MaxDuty));
    TEST_ASSERT_EQUAL_UINT8(0x01, static_cast<uint8_t>(ParamId::CommandTimeoutMs));
    TEST_ASSERT_EQUAL_UINT8(0x02, static_cast<uint8_t>(ParamId::FeedbackIntervalMs));
    TEST_ASSERT_EQUAL_UINT8(0x03, static_cast<uint8_t>(ParamId::ReachedTolerance));
    TEST_ASSERT_EQUAL_UINT8(0x04, static_cast<uint8_t>(ParamId::SlewRate));
    TEST_ASSERT_EQUAL_UINT8(0x05, static_cast<uint8_t>(ParamId::AngleMin));
    TEST_ASSERT_EQUAL_UINT8(0x06, static_cast<uint8_t>(ParamId::AngleMax));
    uint8_t data[3] = {0x07, 0, 0};
    TEST_ASSERT_FALSE(decodeSetParam(data, 3).valid);
}

static void test_decode_set_param_unknown_id_is_ignored() {
    uint8_t data[3] = {0x42, 0, 0};
    TEST_ASSERT_FALSE(decodeSetParam(data, 3).valid);
}

static void test_decode_e_stop_stop() {
    uint8_t data[8] = {0};
    TEST_ASSERT_EQUAL_INT(static_cast<int>(EStopAction::Stop),
                          static_cast<int>(decodeEStop(data, 8)));
}

static void test_decode_e_stop_clear_requires_magic() {
    uint8_t data[8] = {0x01, 0x5A, 0xA5, 0, 0, 0, 0, 0};
    TEST_ASSERT_EQUAL_INT(static_cast<int>(EStopAction::Clear),
                          static_cast<int>(decodeEStop(data, 8)));
}

static void test_decode_e_stop_wrong_magic_is_not_clear() {
    uint8_t wrong1[8] = {0x01, 0x00, 0xA5, 0, 0, 0, 0, 0};
    uint8_t wrong2[8] = {0x01, 0x5A, 0x00, 0, 0, 0, 0, 0};
    uint8_t wrong3[8] = {0x01, 0xA5, 0x5A, 0, 0, 0, 0, 0};
    uint8_t wrong4[8] = {0x01, 0x00, 0x00, 0, 0, 0, 0, 0};
    TEST_ASSERT_EQUAL_INT(static_cast<int>(EStopAction::None),
                          static_cast<int>(decodeEStop(wrong1, 8)));
    TEST_ASSERT_EQUAL_INT(static_cast<int>(EStopAction::None),
                          static_cast<int>(decodeEStop(wrong2, 8)));
    TEST_ASSERT_EQUAL_INT(static_cast<int>(EStopAction::None),
                          static_cast<int>(decodeEStop(wrong3, 8)));
    TEST_ASSERT_EQUAL_INT(static_cast<int>(EStopAction::None),
                          static_cast<int>(decodeEStop(wrong4, 8)));
}

static void test_decode_e_stop_unknown_byte0_is_none() {
    uint8_t data[8] = {0x02, 0x5A, 0xA5, 0, 0, 0, 0, 0};
    TEST_ASSERT_EQUAL_INT(static_cast<int>(EStopAction::None),
                          static_cast<int>(decodeEStop(data, 8)));
}

static void test_encode_feedback_flags_only() {
    uint8_t out[8];
    memset(out, 0xFF, sizeof(out));
    const uint8_t len = encodeFeedback(out, status_flag::kReached | status_flag::kEStop);
    TEST_ASSERT_EQUAL_UINT8(1, len);
    TEST_ASSERT_EQUAL_UINT8(status_flag::kReached | status_flag::kEStop, out[0]);
}

static void test_encode_feedback_with_position() {
    uint8_t out[8];
    memset(out, 0xFF, sizeof(out));
    const uint8_t len = encodeFeedback(out, status_flag::kReached, 900 /* 90.0deg */);
    TEST_ASSERT_EQUAL_UINT8(3, len);
    TEST_ASSERT_EQUAL_UINT8(status_flag::kReached, out[0]);
    TEST_ASSERT_EQUAL_INT16(900, static_cast<int16_t>(out[1] | (out[2] << 8)));
}

static void test_encode_info() {
    uint8_t out[8];
    memset(out, 0xFF, sizeof(out));
    const uint8_t len = encodeInfo(out, 7, BoardKind::Servo, SlotKind::Sensor);
    TEST_ASSERT_EQUAL_UINT8(3, len);
    TEST_ASSERT_EQUAL_UINT8(7, out[0]);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(BoardKind::Servo), out[1]);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlotKind::Sensor), out[2]);
    TEST_ASSERT_EQUAL_UINT8(0xFF, out[3]);
    TEST_ASSERT_EQUAL_UINT8(0xFF, out[4]);
}

static void test_encode_info_with_servo_range() {
    uint8_t out[8];
    memset(out, 0xFF, sizeof(out));
    const uint8_t len = encodeInfo(out, 2, BoardKind::Servo, SlotKind::Actuator, 270.0f);
    TEST_ASSERT_EQUAL_UINT8(5, len);
    TEST_ASSERT_EQUAL_UINT8(2, out[0]);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(BoardKind::Servo), out[1]);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlotKind::Actuator), out[2]);
    TEST_ASSERT_EQUAL_INT16(2700, static_cast<int16_t>(out[3] | (out[4] << 8)));

    encodeInfo(out, 2, BoardKind::Servo, SlotKind::Actuator, 180.0f);
    TEST_ASSERT_EQUAL_INT16(1800, static_cast<int16_t>(out[3] | (out[4] << 8)));
}

static void test_encode_feedback_saturates_position() {
    uint8_t out[8];

    encodeFeedback(out, 0, 40000 /* +4000.0deg */);
    TEST_ASSERT_EQUAL_INT16(32767, static_cast<int16_t>(out[1] | (out[2] << 8)));

    encodeFeedback(out, 0, -40000 /* -4000.0deg */);
    TEST_ASSERT_EQUAL_INT16(-32768, static_cast<int16_t>(out[1] | (out[2] << 8)));
}

static void test_clamp_duty() {
    TEST_ASSERT_EQUAL_FLOAT(0.30f, clampDuty(1.0f, 0.30f));
    TEST_ASSERT_EQUAL_FLOAT(-0.30f, clampDuty(-1.0f, 0.30f));
    TEST_ASSERT_EQUAL_FLOAT(0.20f, clampDuty(0.20f, 0.30f));
    TEST_ASSERT_EQUAL_FLOAT(-0.20f, clampDuty(-0.20f, 0.30f));
    TEST_ASSERT_EQUAL_FLOAT(0.0f, clampDuty(0.0f, 0.30f));
    TEST_ASSERT_EQUAL_FLOAT(1.0f, clampDuty(5.0f, 3.0f));
    TEST_ASSERT_EQUAL_FLOAT(0.0f, clampDuty(0.5f, -1.0f));
}

static void test_watchdog_expires_and_recovers() {
    MotorSafety safety(500);
    safety.feed(1000);
    TEST_ASSERT_FALSE(safety.isExpired(1000));
    TEST_ASSERT_FALSE(safety.isExpired(1499));
    TEST_ASSERT_TRUE(safety.isExpired(1500));
    TEST_ASSERT_TRUE(safety.isExpired(9999));

    safety.feed(10000);
    TEST_ASSERT_FALSE(safety.isExpired(10000));
}

static void test_watchdog_expired_before_first_feed() {
    MotorSafety safety(500);
    TEST_ASSERT_TRUE(safety.isExpired(0));
}

static void test_watchdog_handles_millis_wraparound() {
    MotorSafety safety(500);
    safety.feed(0xFFFFFF00u);
    TEST_ASSERT_FALSE(safety.isExpired(0x00000050u));
    TEST_ASSERT_TRUE(safety.isExpired(0x000000FFu));
}

static void test_watchdog_timeout_is_configurable() {
    MotorSafety safety(500);
    safety.setTimeoutMs(1500);
    safety.feed(0);
    TEST_ASSERT_FALSE(safety.isExpired(1400));
    TEST_ASSERT_TRUE(safety.isExpired(1500));
}

static void test_e_stop_latch() {
    MotorSafety safety(500);
    TEST_ASSERT_FALSE(safety.isLatched());
    safety.stop();
    TEST_ASSERT_TRUE(safety.isLatched());
    safety.clear();
    TEST_ASSERT_FALSE(safety.isLatched());
}

static void test_feed_works_while_latched() {
    MotorSafety safety(500);
    safety.stop();
    safety.feed(1000);
    TEST_ASSERT_TRUE(safety.isLatched());
    TEST_ASSERT_FALSE(safety.isExpired(1400));
}

static void test_e_stop_frame_clears_only_with_magic() {
    MotorSafety safety(500);

    uint8_t stop[8] = {0};
    TEST_ASSERT_EQUAL_INT(static_cast<int>(EStopAction::Stop),
                          static_cast<int>(safety.handleEStopFrame(stop, 8)));
    TEST_ASSERT_TRUE(safety.isLatched());

    uint8_t bad[8] = {0x01, 0x00, 0x00, 0, 0, 0, 0, 0};
    safety.handleEStopFrame(bad, 8);
    TEST_ASSERT_TRUE(safety.isLatched());

    uint8_t good[8] = {0x01, 0x5A, 0xA5, 0, 0, 0, 0, 0};
    TEST_ASSERT_EQUAL_INT(static_cast<int>(EStopAction::Clear),
                          static_cast<int>(safety.handleEStopFrame(good, 8)));
    TEST_ASSERT_FALSE(safety.isLatched());
}

static void test_safety_output_permission() {
    MotorSafety safety(500);
    safety.feed(0);
    TEST_ASSERT_TRUE(safety.isOutputAllowed(100));

    safety.stop();
    TEST_ASSERT_FALSE(safety.isOutputAllowed(100));
    safety.clear();
    TEST_ASSERT_TRUE(safety.isOutputAllowed(100));

    TEST_ASSERT_FALSE(safety.isOutputAllowed(600));
}

static void test_status_flags_are_reported() {
    MotorSafety safety(500);
    safety.feed(0);
    TEST_ASSERT_EQUAL_UINT8(0, safety.statusFlags(100));

    safety.stop();
    TEST_ASSERT_EQUAL_UINT8(status_flag::kEStop, safety.statusFlags(100));

    TEST_ASSERT_EQUAL_UINT8(status_flag::kEStop | status_flag::kWatchdog,
                            safety.statusFlags(600));
}

static void test_status_flags_omit_watchdog_before_first_feed() {
    MotorSafety safety(500);

    TEST_ASSERT_TRUE(safety.isExpired(0));
    TEST_ASSERT_FALSE(safety.isOutputAllowed(0));

    TEST_ASSERT_EQUAL_UINT8(0, safety.statusFlags(0) & status_flag::kWatchdog);
    TEST_ASSERT_EQUAL_UINT8(status_flag::kNeverCommanded, safety.statusFlags(100000));

    safety.stop();
    TEST_ASSERT_EQUAL_UINT8(status_flag::kEStop | status_flag::kNeverCommanded,
                            safety.statusFlags(100000));
}

static void test_status_flags_report_watchdog_after_first_feed() {
    MotorSafety safety(500);
    safety.feed(1000);
    TEST_ASSERT_EQUAL_UINT8(0, safety.statusFlags(1499));
    TEST_ASSERT_EQUAL_UINT8(status_flag::kWatchdog, safety.statusFlags(1500));

    safety.feed(2000);
    TEST_ASSERT_EQUAL_UINT8(0, safety.statusFlags(2100));
}

static void test_command_lost_separates_startup_from_dropout() {
    MotorSafety safety(500);
    TEST_ASSERT_FALSE(safety.isOutputAllowed(100000));
    TEST_ASSERT_FALSE(safety.isCommandLost(100000));

    safety.feed(1000);
    TEST_ASSERT_FALSE(safety.isCommandLost(1400));
    TEST_ASSERT_TRUE(safety.isCommandLost(1500));
}

static void test_watchdog_is_enabled_by_default() {
    MotorSafety safety(500);
    safety.feed(0);
    TEST_ASSERT_TRUE(safety.isOutputAllowed(499));
    TEST_ASSERT_FALSE(safety.isOutputAllowed(500));
}

static void test_disabled_watchdog_allows_output_and_hides_bit2() {
    MotorSafety safety(500);
    safety.setWatchdogEnabled(false);

    safety.feed(1000);
    TEST_ASSERT_TRUE(safety.isOutputAllowed(9999));
    TEST_ASSERT_EQUAL_UINT8(0, safety.statusFlags(9999));

    TEST_ASSERT_TRUE(safety.isExpired(9999));
    TEST_ASSERT_TRUE(safety.isCommandLost(9999));
}

static void test_disabled_watchdog_still_requires_first_command() {
    MotorSafety safety(500);
    safety.setWatchdogEnabled(false);

    TEST_ASSERT_FALSE(safety.isOutputAllowed(0));
    TEST_ASSERT_FALSE(safety.isOutputAllowed(100000));

    safety.feed(1000);
    TEST_ASSERT_TRUE(safety.isOutputAllowed(1000));
    TEST_ASSERT_TRUE(safety.isOutputAllowed(999999));
}

static void test_disabled_watchdog_still_honors_e_stop_latch() {
    MotorSafety safety(500);
    safety.setWatchdogEnabled(false);
    safety.feed(0);

    safety.stop();
    TEST_ASSERT_FALSE(safety.isOutputAllowed(100));
    TEST_ASSERT_EQUAL_UINT8(status_flag::kEStop, safety.statusFlags(100));

    safety.clear();
    TEST_ASSERT_TRUE(safety.isOutputAllowed(100));
}

static void test_watchdog_can_be_re_enabled() {
    MotorSafety safety(500);
    safety.setWatchdogEnabled(false);
    safety.feed(0);
    TEST_ASSERT_TRUE(safety.isOutputAllowed(600));

    safety.setWatchdogEnabled(true);
    TEST_ASSERT_FALSE(safety.isOutputAllowed(600));
    TEST_ASSERT_EQUAL_UINT8(status_flag::kWatchdog, safety.statusFlags(600));
}

static void test_protocol_defaults_match_spec() {
    TEST_ASSERT_EQUAL_UINT32(500, kDefaultCommandTimeoutMs);
    TEST_ASSERT_EQUAL_UINT32(10, kDefaultFeedbackIntervalMs);
}

static void test_command_timeout_param_has_upper_bound() {
    TEST_ASSERT_EQUAL_UINT16(kMaxCommandTimeoutMs, clampCommandTimeoutMs(32767));
    TEST_ASSERT_EQUAL_UINT16(kMaxCommandTimeoutMs, clampCommandTimeoutMs(3000));
}

static void test_command_timeout_param_has_lower_bound() {
    TEST_ASSERT_EQUAL_UINT16(kMinCommandTimeoutMs, clampCommandTimeoutMs(-1));
    TEST_ASSERT_EQUAL_UINT16(kMinCommandTimeoutMs, clampCommandTimeoutMs(0));
    TEST_ASSERT_EQUAL_UINT16(kMinCommandTimeoutMs, clampCommandTimeoutMs(10));
}

static void test_command_timeout_param_keeps_values_in_range() {
    TEST_ASSERT_EQUAL_UINT16(250, clampCommandTimeoutMs(250));
    TEST_ASSERT_EQUAL_UINT16(kDefaultCommandTimeoutMs, clampCommandTimeoutMs(500));
}

static void test_feedback_interval_param_is_bounded() {
    TEST_ASSERT_EQUAL_UINT16(kMinFeedbackIntervalMs, clampFeedbackIntervalMs(0));
    TEST_ASSERT_EQUAL_UINT16(kMinFeedbackIntervalMs, clampFeedbackIntervalMs(-5));
    TEST_ASSERT_EQUAL_UINT16(kMaxFeedbackIntervalMs, clampFeedbackIntervalMs(32767));
    TEST_ASSERT_EQUAL_UINT16(20, clampFeedbackIntervalMs(20));
}

static void test_split_duty_separates_magnitude_and_direction() {
    const DutyOutput forward = splitDuty(0.4f, 1.0f);
    TEST_ASSERT_EQUAL_FLOAT(0.4f, forward.magnitude);
    TEST_ASSERT_FALSE(forward.reverse);

    const DutyOutput backward = splitDuty(-0.4f, 1.0f);
    TEST_ASSERT_EQUAL_FLOAT(0.4f, backward.magnitude);
    TEST_ASSERT_TRUE(backward.reverse);
}

static void test_split_duty_zero_does_not_flip_direction() {
    const DutyOutput stopped = splitDuty(0.0f, 1.0f);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, stopped.magnitude);
    TEST_ASSERT_FALSE(stopped.reverse);
}

static void test_split_duty_applies_max_duty() {
    const DutyOutput clamped = splitDuty(-1.0f, 0.3f);
    TEST_ASSERT_EQUAL_FLOAT(0.3f, clamped.magnitude);
    TEST_ASSERT_TRUE(clamped.reverse);

}

static void test_physical_stop_latches() {
    MotorSafety safety(500);
    safety.feed(0);
    TEST_ASSERT_TRUE(safety.isOutputAllowed(0));

    safety.applyPhysicalStop(true);
    TEST_ASSERT_TRUE(safety.isLatched());
    TEST_ASSERT_FALSE(safety.isOutputAllowed(0));
}

static void test_physical_stop_does_not_auto_release() {
    MotorSafety safety(500);
    safety.feed(0);
    safety.applyPhysicalStop(true);

    safety.applyPhysicalStop(false);
    TEST_ASSERT_TRUE(safety.isLatched());
    TEST_ASSERT_FALSE(safety.isOutputAllowed(0));
}

static void test_physical_stop_survives_clear_frame_while_held() {
    MotorSafety safety(500);
    safety.feed(0);
    safety.applyPhysicalStop(true);

    uint8_t clear[8] = {0x01, 0x5A, 0xA5, 0, 0, 0, 0, 0};
    TEST_ASSERT_EQUAL_INT(static_cast<int>(EStopAction::Clear),
                          static_cast<int>(safety.handleEStopFrame(clear, 8)));
    TEST_ASSERT_FALSE(safety.isLatched());

    safety.applyPhysicalStop(true);
    TEST_ASSERT_TRUE(safety.isLatched());
    TEST_ASSERT_FALSE(safety.isOutputAllowed(0));
}

static void test_physical_stop_clears_after_release() {
    MotorSafety safety(500);
    safety.feed(0);
    safety.applyPhysicalStop(true);
    safety.applyPhysicalStop(false);

    uint8_t clear[8] = {0x01, 0x5A, 0xA5, 0, 0, 0, 0, 0};
    safety.handleEStopFrame(clear, 8);
    safety.applyPhysicalStop(false);
    TEST_ASSERT_TRUE(safety.isOutputAllowed(0));
}

static void test_dc_channel_starts_stopped() {
    DcChannel ch(500);
    TEST_ASSERT_FALSE(ch.isOutputAllowed(0));
    TEST_ASSERT_EQUAL_FLOAT(0.0f, ch.outputDuty(0));
}

static void test_dc_channel_accepts_duty_after_first_command() {
    DcChannel ch(500);
    ch.feed(0);
    TEST_ASSERT_TRUE(ch.setDuty(0.4f, 0));
    TEST_ASSERT_EQUAL_FLOAT(0.4f, ch.outputDuty(0));
}

static void test_dc_channel_rejects_duty_while_latched() {
    DcChannel ch(500);
    ch.feed(0);
    ch.setDuty(0.4f, 0);
    ch.stop();

    TEST_ASSERT_FALSE(ch.setDuty(0.9f, 10));
    TEST_ASSERT_EQUAL_FLOAT(0.0f, ch.outputDuty(10));

    uint8_t clear[8] = {0x01, 0x5A, 0xA5, 0, 0, 0, 0, 0};
    ch.handleEStopFrame(clear, 8);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, ch.outputDuty(10));
}

static void test_dc_channel_output_stops_on_watchdog_and_recovers() {
    DcChannel ch(500);
    ch.feed(0);
    ch.setDuty(0.4f, 0);

    TEST_ASSERT_EQUAL_FLOAT(0.0f, ch.outputDuty(600));
    TEST_ASSERT_TRUE((ch.safetyStatusFlags(600) & status_flag::kWatchdog) != 0);

    ch.feed(600);
    TEST_ASSERT_TRUE(ch.setDuty(0.4f, 600));
    TEST_ASSERT_EQUAL_FLOAT(0.4f, ch.outputDuty(600));
}

static void test_dc_channel_forgets_target_while_output_is_blocked() {
    DcChannel ch(500);
    ch.feed(0);
    ch.setDuty(0.30f, 0);
    TEST_ASSERT_EQUAL_FLOAT(0.30f, ch.outputDuty(0));

    TEST_ASSERT_EQUAL_FLOAT(0.0f, ch.outputDuty(600));
    ch.tick(600);

    ch.feed(700);
    TEST_ASSERT_FALSE(ch.applySetTarget(SetTargetCommand{ControlType::Position, 900, true}, 700));

    TEST_ASSERT_EQUAL_FLOAT(0.0f, ch.outputDuty(700));
}

static void test_dc_channel_physical_stop_blocks_until_cleared() {
    DcChannel ch(500);
    ch.feed(0);
    ch.setDuty(0.4f, 0);

    ch.applyPhysicalStop(true);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, ch.outputDuty(0));
    TEST_ASSERT_TRUE((ch.safetyStatusFlags(0) & status_flag::kEStop) != 0);
    TEST_ASSERT_FALSE(ch.setDuty(0.4f, 0));

    ch.applyPhysicalStop(false);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, ch.outputDuty(0));

    uint8_t clear[8] = {0x01, 0x5A, 0xA5, 0, 0, 0, 0, 0};
    ch.handleEStopFrame(clear, 8);
    TEST_ASSERT_TRUE(ch.setDuty(0.4f, 0));
    TEST_ASSERT_EQUAL_FLOAT(0.4f, ch.outputDuty(0));
}

static void test_dc_channel_hold_stops_without_latching() {
    DcChannel ch(500);
    ch.feed(1000);
    TEST_ASSERT_TRUE(ch.setDuty(0.4f, 1000));
    TEST_ASSERT_EQUAL_FLOAT(0.4f, ch.outputDuty(1000));

    ch.hold();
    TEST_ASSERT_EQUAL_FLOAT(0.0f, ch.outputDuty(1000));
    TEST_ASSERT_TRUE(ch.isOutputAllowed(1000));

    TEST_ASSERT_TRUE(ch.setDuty(0.4f, 1000));
    TEST_ASSERT_EQUAL_FLOAT(0.4f, ch.outputDuty(1000));
}

static void test_dc_channel_accepts_only_duty_targets() {
    DcChannel ch(500);
    ch.feed(0);

    const SetTargetCommand position{ControlType::Position, 900, true};
    TEST_ASSERT_FALSE(ch.applySetTarget(position, 0));
    const SetTargetCommand velocity{ControlType::Velocity, 900, true};
    TEST_ASSERT_FALSE(ch.applySetTarget(velocity, 0));
    const SetTargetCommand onOff{ControlType::OnOff, 1, true};
    TEST_ASSERT_FALSE(ch.applySetTarget(onOff, 0));
    const SetTargetCommand invalid{ControlType::Duty, 4000, false};
    TEST_ASSERT_FALSE(ch.applySetTarget(invalid, 0));

    TEST_ASSERT_EQUAL_FLOAT(0.0f, ch.outputDuty(0));

    const SetTargetCommand duty{ControlType::Duty, 4000, true};
    TEST_ASSERT_TRUE(ch.applySetTarget(duty, 0));
    TEST_ASSERT_EQUAL_FLOAT(0.4f, ch.outputDuty(0));
}

static void test_dc_apply_set_target_still_honors_the_output_gate() {
    DcChannel ch(500);
    ch.feed(0);
    ch.stop();

    const SetTargetCommand duty{ControlType::Duty, 4000, true};
    ch.feed(10);
    TEST_ASSERT_FALSE(ch.applySetTarget(duty, 10));
    TEST_ASSERT_EQUAL_FLOAT(0.0f, ch.outputDuty(10));
}

static void test_status_flag_bits_do_not_overlap() {
    const uint8_t all[] = {status_flag::kReached, status_flag::kEStop, status_flag::kWatchdog,
                           status_flag::kDeviceIdUnconfigured, status_flag::kSensor};
    uint8_t seen = 0;
    for (uint8_t bit : all) {
        TEST_ASSERT_NOT_EQUAL_UINT8(0, bit);
        TEST_ASSERT_EQUAL_UINT8(0, seen & bit);
        seen = static_cast<uint8_t>(seen | bit);
    }
    TEST_ASSERT_EQUAL_UINT8(0x1F, seen);
}

static void test_sensor_flag_rides_in_its_own_feedback() {
    uint8_t out[8];
    const uint8_t len = encodeFeedback(out, status_flag::kSensor);
    TEST_ASSERT_EQUAL_UINT8(1, len);
    TEST_ASSERT_EQUAL_UINT8(status_flag::kSensor, out[0]);
}

static void test_dc_board_never_reports_reached() {
    const uint8_t flags = composeFeedbackFlags(BoardKind::Dc, SlotKind::Actuator, 0,
                                               /*configured=*/true, /*reached=*/true,
                                               /*sensorActive=*/false);
    TEST_ASSERT_EQUAL_UINT8(0, flags & status_flag::kReached);
}

static void test_solenoid_board_never_reports_reached() {
    const uint8_t flags = composeFeedbackFlags(BoardKind::Solenoid, SlotKind::Actuator, 0,
                                               /*configured=*/true, /*reached=*/true,
                                               /*sensorActive=*/false);
    TEST_ASSERT_EQUAL_UINT8(0, flags & status_flag::kReached);
}

static void test_servo_slot_reports_reached() {
    TEST_ASSERT_EQUAL_UINT8(status_flag::kReached,
                            composeFeedbackFlags(BoardKind::Servo, SlotKind::Actuator, 0, true,
                                                 /*reached=*/true, false) &
                                status_flag::kReached);
    TEST_ASSERT_EQUAL_UINT8(0, composeFeedbackFlags(BoardKind::Servo, SlotKind::Actuator, 0, true,
                                                    /*reached=*/false, false) &
                                   status_flag::kReached);
}

static void test_sensor_slot_drops_safety_flags() {
    const uint8_t safety = static_cast<uint8_t>(status_flag::kEStop | status_flag::kWatchdog |
                                                status_flag::kNeverCommanded);
    const uint8_t flags = composeFeedbackFlags(BoardKind::Servo, SlotKind::Sensor, safety,
                                               /*configured=*/true, /*reached=*/true,
                                               /*sensorActive=*/true);
    TEST_ASSERT_EQUAL_UINT8(status_flag::kSensor, flags);
}

static void test_sensor_slot_reports_only_its_own_contact() {
    TEST_ASSERT_EQUAL_UINT8(0, composeFeedbackFlags(BoardKind::Servo, SlotKind::Sensor, 0, true,
                                                    false, /*sensorActive=*/false));
    TEST_ASSERT_EQUAL_UINT8(0, composeFeedbackFlags(BoardKind::Servo, SlotKind::Actuator, 0, true,
                                                    false, /*sensorActive=*/true) &
                                   status_flag::kSensor);
}

static void test_unconfigured_is_reported_on_every_slot_kind() {
    const BoardKind boards[] = {BoardKind::Servo, BoardKind::Dc, BoardKind::Solenoid};
    for (BoardKind board : boards) {
        TEST_ASSERT_EQUAL_UINT8(status_flag::kDeviceIdUnconfigured,
                                composeFeedbackFlags(board, SlotKind::Actuator, 0,
                                                     /*configured=*/false, false, false) &
                                    status_flag::kDeviceIdUnconfigured);
    }
    TEST_ASSERT_EQUAL_UINT8(status_flag::kDeviceIdUnconfigured,
                            composeFeedbackFlags(BoardKind::Servo, SlotKind::Sensor, 0,
                                                 /*configured=*/false, false, false));
}

static void test_actuator_slot_relays_safety_flags() {
    MotorSafety safety(500);
    const uint8_t fresh = composeFeedbackFlags(BoardKind::Dc, SlotKind::Actuator,
                                               safety.statusFlags(0), true, false, false);
    TEST_ASSERT_EQUAL_UINT8(status_flag::kNeverCommanded, fresh);

    safety.feed(1000);
    safety.stop();
    const uint8_t stopped = composeFeedbackFlags(BoardKind::Dc, SlotKind::Actuator,
                                                 safety.statusFlags(1000), true, false, false);
    TEST_ASSERT_EQUAL_UINT8(status_flag::kEStop, stopped);
}

static void test_common_param_routes_timeout_and_interval() {
    DcChannel channel(500);
    uint16_t interval = kDefaultFeedbackIntervalMs;

    uint8_t frame[3] = {static_cast<uint8_t>(ParamId::CommandTimeoutMs), 0, 0};
    packInt16Le(&frame[1], 800);
    TEST_ASSERT_TRUE(applyCommonParam(decodeSetParam(frame, 3), channel, interval));
    TEST_ASSERT_EQUAL_UINT32(800, channel.commandTimeoutMs());
    TEST_ASSERT_EQUAL_UINT16(kDefaultFeedbackIntervalMs, interval);

    frame[0] = static_cast<uint8_t>(ParamId::FeedbackIntervalMs);
    packInt16Le(&frame[1], 20);
    TEST_ASSERT_TRUE(applyCommonParam(decodeSetParam(frame, 3), channel, interval));
    TEST_ASSERT_EQUAL_UINT16(20, interval);
    TEST_ASSERT_EQUAL_UINT32(800, channel.commandTimeoutMs());
}

static void test_common_param_clamps_out_of_range() {
    DcChannel channel(500);
    uint16_t interval = kDefaultFeedbackIntervalMs;

    uint8_t frame[3] = {static_cast<uint8_t>(ParamId::CommandTimeoutMs), 0, 0};
    packInt16Le(&frame[1], 30000);
    applyCommonParam(decodeSetParam(frame, 3), channel, interval);
    TEST_ASSERT_EQUAL_UINT32(kMaxCommandTimeoutMs, channel.commandTimeoutMs());

    frame[0] = static_cast<uint8_t>(ParamId::FeedbackIntervalMs);
    packInt16Le(&frame[1], 0);
    applyCommonParam(decodeSetParam(frame, 3), channel, interval);
    TEST_ASSERT_EQUAL_UINT16(kMinFeedbackIntervalMs, interval);
}

static void test_common_param_leaves_board_specific_ids() {
    DcChannel channel(500);
    uint16_t interval = kDefaultFeedbackIntervalMs;
    const ParamId others[] = {ParamId::MaxDuty, ParamId::ReachedTolerance, ParamId::SlewRate,
                              ParamId::AngleMin, ParamId::AngleMax};
    for (ParamId id : others) {
        uint8_t frame[3] = {static_cast<uint8_t>(id), 0, 0};
        packInt16Le(&frame[1], 123);
        TEST_ASSERT_FALSE(applyCommonParam(decodeSetParam(frame, 3), channel, interval));
    }
    TEST_ASSERT_EQUAL_UINT32(500, channel.commandTimeoutMs());
    TEST_ASSERT_EQUAL_UINT16(kDefaultFeedbackIntervalMs, interval);
}

static void test_pc_to_board_commands_pass_the_filter() {
    const CommandType passing[] = {CommandType::EStop, CommandType::SetTarget,
                                   CommandType::SetParam};
    for (CommandType command : passing) {
        for (uint16_t dev = 0; dev <= 0xFF; ++dev) {
            TEST_ASSERT_TRUE(
                passesPcToBoardFilters(buildCanId(command, static_cast<uint8_t>(dev))));
        }
    }
}

static void test_board_to_pc_frames_are_filtered_out() {
    for (uint16_t dev = 0; dev <= 0xFF; ++dev) {
        TEST_ASSERT_FALSE(
            passesPcToBoardFilters(buildCanId(CommandType::Feedback, static_cast<uint8_t>(dev))));
        TEST_ASSERT_FALSE(
            passesPcToBoardFilters(buildCanId(CommandType::Info, static_cast<uint8_t>(dev))));
    }
    for (uint16_t command = 5; command <= 7; ++command) {
        TEST_ASSERT_FALSE(passesPcToBoardFilters(
            static_cast<uint16_t>((command << kCommandTypeShift) | 0x2A)));
    }
}

static void test_filter_ranges_are_derived_from_the_enum() {
    TEST_ASSERT_EQUAL_UINT16(commandIdBase(CommandType::EStop), kEStopAndSetTargetFilter.id);
    TEST_ASSERT_EQUAL_UINT16(commandIdBase(CommandType::SetParam), kSetParamFilter.id);
    TEST_ASSERT_EQUAL_UINT16(kCommandTypeMask, kSetParamFilter.mask);
    const uint16_t differingBits = static_cast<uint16_t>(
        commandIdBase(CommandType::EStop) ^ commandIdBase(CommandType::SetTarget));
    TEST_ASSERT_EQUAL_UINT16(static_cast<uint16_t>(kCommandTypeMask & ~differingBits),
                             kEStopAndSetTargetFilter.mask);
}

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_build_can_id);
    RUN_TEST(test_e_stop_outranks_every_other_frame);
    RUN_TEST(test_parse_can_id_roundtrip);
    RUN_TEST(test_parse_can_id_reserved_is_invalid);
    RUN_TEST(test_parse_can_id_rejects_out_of_range);
    RUN_TEST(test_to_raw_saturates_nan_and_out_of_range);
    RUN_TEST(test_fixed_point_roundtrip_keeps_the_unit);
    RUN_TEST(test_decode_set_target);
    RUN_TEST(test_decode_set_target_keeps_sign);
    RUN_TEST(test_decode_set_target_rejects_unknown_type);
    RUN_TEST(test_decode_set_target_accepts_on_off);
    RUN_TEST(test_decode_set_target_rejects_short_frame);
    RUN_TEST(test_decode_set_param);
    RUN_TEST(test_param_ids_are_packed);
    RUN_TEST(test_decode_set_param_unknown_id_is_ignored);
    RUN_TEST(test_decode_e_stop_stop);
    RUN_TEST(test_decode_e_stop_clear_requires_magic);
    RUN_TEST(test_decode_e_stop_wrong_magic_is_not_clear);
    RUN_TEST(test_decode_e_stop_unknown_byte0_is_none);
    RUN_TEST(test_status_flag_bits_do_not_overlap);
    RUN_TEST(test_sensor_flag_rides_in_its_own_feedback);
    RUN_TEST(test_encode_feedback_flags_only);
    RUN_TEST(test_encode_feedback_with_position);
    RUN_TEST(test_encode_info);
    RUN_TEST(test_encode_info_with_servo_range);
    RUN_TEST(test_encode_feedback_saturates_position);
    RUN_TEST(test_clamp_duty);
    RUN_TEST(test_watchdog_expires_and_recovers);
    RUN_TEST(test_watchdog_expired_before_first_feed);
    RUN_TEST(test_watchdog_handles_millis_wraparound);
    RUN_TEST(test_watchdog_timeout_is_configurable);
    RUN_TEST(test_e_stop_latch);
    RUN_TEST(test_feed_works_while_latched);
    RUN_TEST(test_e_stop_frame_clears_only_with_magic);
    RUN_TEST(test_safety_output_permission);
    RUN_TEST(test_status_flags_are_reported);
    RUN_TEST(test_status_flags_omit_watchdog_before_first_feed);
    RUN_TEST(test_status_flags_report_watchdog_after_first_feed);
    RUN_TEST(test_command_lost_separates_startup_from_dropout);
    RUN_TEST(test_watchdog_is_enabled_by_default);
    RUN_TEST(test_disabled_watchdog_allows_output_and_hides_bit2);
    RUN_TEST(test_disabled_watchdog_still_requires_first_command);
    RUN_TEST(test_disabled_watchdog_still_honors_e_stop_latch);
    RUN_TEST(test_watchdog_can_be_re_enabled);
    RUN_TEST(test_protocol_defaults_match_spec);
    RUN_TEST(test_command_timeout_param_has_upper_bound);
    RUN_TEST(test_command_timeout_param_has_lower_bound);
    RUN_TEST(test_command_timeout_param_keeps_values_in_range);
    RUN_TEST(test_feedback_interval_param_is_bounded);
    RUN_TEST(test_split_duty_separates_magnitude_and_direction);
    RUN_TEST(test_split_duty_zero_does_not_flip_direction);
    RUN_TEST(test_split_duty_applies_max_duty);
    RUN_TEST(test_physical_stop_latches);
    RUN_TEST(test_physical_stop_does_not_auto_release);
    RUN_TEST(test_physical_stop_survives_clear_frame_while_held);
    RUN_TEST(test_physical_stop_clears_after_release);
    RUN_TEST(test_dc_channel_starts_stopped);
    RUN_TEST(test_dc_channel_accepts_duty_after_first_command);
    RUN_TEST(test_dc_channel_rejects_duty_while_latched);
    RUN_TEST(test_dc_channel_output_stops_on_watchdog_and_recovers);
    RUN_TEST(test_dc_channel_forgets_target_while_output_is_blocked);
    RUN_TEST(test_dc_channel_physical_stop_blocks_until_cleared);
    RUN_TEST(test_dc_channel_hold_stops_without_latching);
    RUN_TEST(test_dc_channel_accepts_only_duty_targets);
    RUN_TEST(test_dc_apply_set_target_still_honors_the_output_gate);
    RUN_TEST(test_dc_board_never_reports_reached);
    RUN_TEST(test_solenoid_board_never_reports_reached);
    RUN_TEST(test_servo_slot_reports_reached);
    RUN_TEST(test_sensor_slot_drops_safety_flags);
    RUN_TEST(test_sensor_slot_reports_only_its_own_contact);
    RUN_TEST(test_unconfigured_is_reported_on_every_slot_kind);
    RUN_TEST(test_actuator_slot_relays_safety_flags);
    RUN_TEST(test_common_param_routes_timeout_and_interval);
    RUN_TEST(test_common_param_clamps_out_of_range);
    RUN_TEST(test_common_param_leaves_board_specific_ids);
    RUN_TEST(test_pc_to_board_commands_pass_the_filter);
    RUN_TEST(test_board_to_pc_frames_are_filtered_out);
    RUN_TEST(test_filter_ranges_are_derived_from_the_enum);
    return UNITY_END();
}
