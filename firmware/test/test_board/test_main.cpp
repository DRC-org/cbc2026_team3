#include <unity.h>

#include <string.h>

#include "MotorCanProtocol.h"
#include "MotorCanRouter.h"
#include "MotorLoopTimer.h"
#include "MotorTxHealth.h"
#include "SerialLineBuffer.h"
#include "SerialOverride.h"

using namespace motorcan;

void setUp() {}
void tearDown() {}

static const uint8_t kServoIds[3] = {0x01, 0x03, 0x04};
static const uint8_t kDcId[1] = {0x02};

static FrameRoute routeStandard(uint16_t canId, const uint8_t *ids, uint8_t count) {
    return routeFrame(canId, true, ids, count);
}

static void test_broadcast_e_stop_reaches_every_channel() {
    const FrameRoute route = routeStandard(kBroadcastEStopCanId, kServoIds, 3);
    TEST_ASSERT_TRUE(route.accepted);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(CommandType::EStop),
                            static_cast<uint8_t>(route.command));
    TEST_ASSERT_EQUAL_UINT8(0b111, route.channelMask);
}

static void test_broadcast_e_stop_reaches_unconfigured_channels() {
    const uint8_t ids[3] = {kDeviceIdUnconfigured, kDeviceIdUnconfigured, kDeviceIdUnconfigured};
    const FrameRoute route = routeStandard(kBroadcastEStopCanId, ids, 3);
    TEST_ASSERT_TRUE(route.accepted);
    TEST_ASSERT_EQUAL_UINT8(0b111, route.channelMask);
}

static void test_e_stop_to_other_device_is_dropped() {
    TEST_ASSERT_FALSE(routeStandard(0x7FE, kServoIds, 3).accepted);
    TEST_ASSERT_FALSE(routeStandard(0x7FE, kDcId, 1).accepted);
}

static void test_broadcast_device_id_is_only_for_e_stop() {
    TEST_ASSERT_TRUE(routeStandard(kBroadcastEStopCanId, kServoIds, 3).accepted);
    TEST_ASSERT_FALSE(routeStandard(0x1FF, kServoIds, 3).accepted);
    TEST_ASSERT_FALSE(routeStandard(0x2FF, kServoIds, 3).accepted);
    TEST_ASSERT_FALSE(routeStandard(0x3FF, kServoIds, 3).accepted);
    TEST_ASSERT_FALSE(routeStandard(0x4FF, kServoIds, 3).accepted);
}

static void test_channel_count_beyond_mask_width_is_rejected() {
    const uint8_t ids[10] = {0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A};
    TEST_ASSERT_FALSE(routeStandard(kBroadcastEStopCanId, ids, 10).accepted);
    TEST_ASSERT_FALSE(routeStandard(buildCanId(CommandType::SetTarget, 0x01), ids, 10).accepted);

    TEST_ASSERT_TRUE(routeStandard(kBroadcastEStopCanId, ids, kMaxChannels).accepted);
}

static void test_own_frame_is_routed_to_matching_channel() {
    const FrameRoute target = routeStandard(buildCanId(CommandType::SetTarget, 0x03), kServoIds, 3);
    TEST_ASSERT_TRUE(target.accepted);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(CommandType::SetTarget),
                            static_cast<uint8_t>(target.command));
    TEST_ASSERT_EQUAL_UINT8(0b010, target.channelMask);

    const FrameRoute param = routeStandard(buildCanId(CommandType::SetParam, 0x04), kServoIds, 3);
    TEST_ASSERT_TRUE(param.accepted);
    TEST_ASSERT_EQUAL_UINT8(0b100, param.channelMask);
}

static void test_frames_for_other_devices_are_dropped() {
    for (uint16_t dev = 0x01; dev <= 0x10; ++dev) {
        const bool mine = (dev == 0x02);
        const FrameRoute route =
            routeStandard(buildCanId(CommandType::SetTarget, static_cast<uint8_t>(dev)), kDcId, 1);
        TEST_ASSERT_EQUAL(mine, route.accepted);
    }
}

static void test_unconfigured_device_receives_only_broadcast_e_stop() {
    const uint8_t ids[1] = {kDeviceIdUnconfigured};
    TEST_ASSERT_FALSE(routeStandard(buildCanId(CommandType::SetTarget, 0x00), ids, 1).accepted);
    TEST_ASSERT_FALSE(routeStandard(buildCanId(CommandType::SetParam, 0x00), ids, 1).accepted);
    TEST_ASSERT_FALSE(routeStandard(buildCanId(CommandType::SetParam, 0x00), ids, 1).accepted);
    TEST_ASSERT_FALSE(routeStandard(buildCanId(CommandType::EStop, 0x00), ids, 1).accepted);
    TEST_ASSERT_TRUE(routeStandard(kBroadcastEStopCanId, ids, 1).accepted);
}

static void test_unconfigured_channel_is_skipped_in_mixed_table() {
    const uint8_t ids[3] = {0x01, kDeviceIdUnconfigured, 0x04};
    const FrameRoute route = routeStandard(buildCanId(CommandType::SetTarget, 0x00), ids, 3);
    TEST_ASSERT_FALSE(route.accepted);
}

static void test_extended_frame_is_dropped() {
    TEST_ASSERT_FALSE(routeFrame(kBroadcastEStopCanId, false, kServoIds, 3).accepted);
    TEST_ASSERT_FALSE(routeFrame(buildCanId(CommandType::SetTarget, 0x03), false, kServoIds, 3)
                          .accepted);
}

static void test_reserved_and_out_of_range_ids_are_dropped() {
    for (uint16_t cmd = 5; cmd <= 7; ++cmd) {
        TEST_ASSERT_FALSE(routeStandard(static_cast<uint16_t>((cmd << 8) | 0x03), kServoIds, 3)
                              .accepted);
    }
    TEST_ASSERT_FALSE(routeStandard(0x800, kServoIds, 3).accepted);
}

static void test_feedback_of_other_boards_is_dropped() {
    TEST_ASSERT_FALSE(routeStandard(buildCanId(CommandType::Feedback, 0x05), kServoIds, 3)
                          .accepted);
}

static uint8_t g_stubPins[4] = {0, 0, 0, 0};
static int stubReadPin(uint8_t pin) { return g_stubPins[pin]; }

static void test_device_id_is_a_fixed_bit_split() {
    TEST_ASSERT_EQUAL_UINT8(0x40, makeDeviceId(BoardKind::Servo, 0, 0));
    TEST_ASSERT_EQUAL_UINT8(0x44, makeDeviceId(BoardKind::Servo, 0, 4));
    TEST_ASSERT_EQUAL_UINT8(0x48, makeDeviceId(BoardKind::Servo, 1, 0));
    TEST_ASSERT_EQUAL_UINT8(0x4A, makeDeviceId(BoardKind::Servo, 1, 2));
    TEST_ASSERT_EQUAL_UINT8(0x80, makeDeviceId(BoardKind::Dc, 0, 0));
    TEST_ASSERT_EQUAL_UINT8(0x82, makeDeviceId(BoardKind::Dc, 0, 2));
    TEST_ASSERT_EQUAL_UINT8(0x88, makeDeviceId(BoardKind::Dc, 1, 0));
}

static void test_device_id_out_of_range_is_unconfigured() {
    TEST_ASSERT_EQUAL_UINT8(kDeviceIdUnconfigured, makeDeviceId(BoardKind::Servo, 8, 0));
    TEST_ASSERT_EQUAL_UINT8(kDeviceIdUnconfigured, makeDeviceId(BoardKind::Servo, 15, 0));
    TEST_ASSERT_EQUAL_UINT8(kDeviceIdUnconfigured, makeDeviceId(BoardKind::Dc, 0, 8));
}

static void test_dip_reads_two_bits() {
    const uint8_t pins[2] = {0, 1};
    const int kLow = 0;
    const int kHigh = 1;

    g_stubPins[0] = kHigh;
    g_stubPins[1] = kHigh;
    TEST_ASSERT_EQUAL_UINT8(0x00, readDipSwitch(pins, 2, stubReadPin, kLow));

    g_stubPins[0] = kLow;
    TEST_ASSERT_EQUAL_UINT8(0x01, readDipSwitch(pins, 2, stubReadPin, kLow));

    g_stubPins[0] = kHigh;
    g_stubPins[1] = kLow;
    TEST_ASSERT_EQUAL_UINT8(0x02, readDipSwitch(pins, 2, stubReadPin, kLow));

    g_stubPins[0] = kLow;
    TEST_ASSERT_EQUAL_UINT8(0x03, readDipSwitch(pins, 2, stubReadPin, kLow));
}

static void test_dip_is_active_low_and_lsb_first() {
    const uint8_t pins[4] = {0, 1, 2, 3};
    const int kLow = 0;
    const int kHigh = 1;

    for (uint8_t i = 0; i < 4; ++i) {
        g_stubPins[i] = kHigh;
    }
    TEST_ASSERT_EQUAL_UINT8(0x00, readDipSwitch(pins, 4, stubReadPin, kLow));

    g_stubPins[0] = kLow;
    TEST_ASSERT_EQUAL_UINT8(0x01, readDipSwitch(pins, 4, stubReadPin, kLow));

    g_stubPins[0] = kHigh;
    g_stubPins[3] = kLow;
    TEST_ASSERT_EQUAL_UINT8(0x08, readDipSwitch(pins, 4, stubReadPin, kLow));

    for (uint8_t i = 0; i < 4; ++i) {
        g_stubPins[i] = kLow;
    }
    TEST_ASSERT_EQUAL_UINT8(0x0F, readDipSwitch(pins, 4, stubReadPin, kLow));
}

static void test_periodic_timer_fires_on_interval() {
    PeriodicTimer timer;
    timer.reset(1000);
    TEST_ASSERT_FALSE(timer.due(1009, 10));
    TEST_ASSERT_TRUE(timer.due(1010, 10));
    TEST_ASSERT_FALSE(timer.due(1010, 10));
    TEST_ASSERT_TRUE(timer.due(1020, 10));
}

static void test_periodic_timer_survives_millis_wraparound() {
    PeriodicTimer timer;
    timer.reset(0xFFFFFFF8u);
    TEST_ASSERT_FALSE(timer.due(0xFFFFFFFEu, 10));
    TEST_ASSERT_TRUE(timer.due(0x00000002u, 10));
}

static void test_periodic_timer_staggers_phase() {
    PeriodicTimer second;
    second.stagger(1000, 10, 1, 2);
    TEST_ASSERT_FALSE(second.due(1000, 10));
    TEST_ASSERT_TRUE(second.due(1005, 10));

    PeriodicTimer first;
    first.stagger(1000, 10, 0, 2);
    TEST_ASSERT_TRUE(first.due(1000, 10));

    TEST_ASSERT_TRUE(first.due(1010, 10));
    TEST_ASSERT_TRUE(second.due(1015, 10));
}

static void test_periodic_timer_stagger_handles_zero_count() {
    PeriodicTimer timer;
    timer.stagger(1000, 10, 0, 0);
    TEST_ASSERT_FALSE(timer.due(1009, 10));
    TEST_ASSERT_TRUE(timer.due(1010, 10));
}

static uint8_t feed(SerialLineBuffer &buffer, const char *text, char *out) {
    uint8_t lines = 0;
    out[0] = '\0';
    for (const char *p = text; *p != '\0'; ++p) {
        if (buffer.push(*p)) {
            ++lines;
            strcpy(out, buffer.line());
        }
    }
    return lines;
}

static void test_line_buffer_completes_on_lf_and_cr() {
    char storage[16];
    char last[32];
    SerialLineBuffer buffer(storage, sizeof(storage));

    TEST_ASSERT_EQUAL_UINT8(1, feed(buffer, "0.3\n", last));
    TEST_ASSERT_EQUAL_STRING("0.3", last);

    TEST_ASSERT_EQUAL_UINT8(1, feed(buffer, "1 45.0\r", last));
    TEST_ASSERT_EQUAL_STRING("1 45.0", last);

    TEST_ASSERT_EQUAL_UINT8(2, feed(buffer, "s\n0.2\n", last));
    TEST_ASSERT_EQUAL_STRING("0.2", last);
}

static void test_line_buffer_ignores_empty_lines() {
    char storage[16];
    char last[32];
    SerialLineBuffer buffer(storage, sizeof(storage));

    TEST_ASSERT_EQUAL_UINT8(0, feed(buffer, "\n\r\n\n", last));
    TEST_ASSERT_EQUAL_UINT8(1, feed(buffer, "s\r\n", last));
    TEST_ASSERT_EQUAL_STRING("s", last);
}

static void test_line_buffer_caps_length() {
    char storage[8];
    char last[32];
    SerialLineBuffer buffer(storage, sizeof(storage));

    TEST_ASSERT_EQUAL_UINT8(1, feed(buffer, "0123456789\n", last));
    TEST_ASSERT_EQUAL_STRING("0123456", last);
}

static void test_resolve_device_ids_fills_the_table() {
    uint8_t ids[3] = {0xFF, 0xFF, 0xFF};
    resolveDeviceIds(ids, 3, BoardKind::Dc, 1, nullptr);
    TEST_ASSERT_EQUAL_UINT8(0x88, ids[0]);
    TEST_ASSERT_EQUAL_UINT8(0x89, ids[1]);
    TEST_ASSERT_EQUAL_UINT8(0x8A, ids[2]);
}

static bool onlySlotOneIsUnused(uint8_t slot) { return slot != 1; }

static void test_resolve_device_ids_skips_non_device_slots() {
    uint8_t ids[3] = {0xFF, 0xFF, 0xFF};
    resolveDeviceIds(ids, 3, BoardKind::Servo, 0, onlySlotOneIsUnused);
    TEST_ASSERT_EQUAL_UINT8(0x40, ids[0]);
    TEST_ASSERT_EQUAL_UINT8(kDeviceIdUnconfigured, ids[1]);
    TEST_ASSERT_EQUAL_UINT8(0x42, ids[2]);
}

static void test_resolve_device_ids_out_of_range_board_number() {
    uint8_t ids[2] = {0xFF, 0xFF};
    resolveDeviceIds(ids, 2, BoardKind::Servo, 8, nullptr);
    TEST_ASSERT_EQUAL_UINT8(kDeviceIdUnconfigured, ids[0]);
    TEST_ASSERT_EQUAL_UINT8(kDeviceIdUnconfigured, ids[1]);
}

static void test_serial_command_stop_all() {
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SerialCommand::Kind::StopAll),
                            static_cast<uint8_t>(parseSerialCommand("s", 3).kind));
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SerialCommand::Kind::StopAll),
                            static_cast<uint8_t>(parseSerialCommand("S", 3).kind));
}

static void test_serial_command_splits_channel_and_value() {
    const SerialCommand cmd = parseSerialCommand("2 -0.35", 3);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SerialCommand::Kind::Channel),
                            static_cast<uint8_t>(cmd.kind));
    TEST_ASSERT_EQUAL_UINT8(2, cmd.channel);
    TEST_ASSERT_EQUAL_STRING("-0.35", cmd.value);
}

static void test_serial_command_rejects_ambiguous_lines() {
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SerialCommand::Kind::None),
                            static_cast<uint8_t>(parseSerialCommand("1,0.3", 3).kind));
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SerialCommand::Kind::None),
                            static_cast<uint8_t>(parseSerialCommand(" 0.3", 3).kind));
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SerialCommand::Kind::None),
                            static_cast<uint8_t>(parseSerialCommand("1", 3).kind));
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SerialCommand::Kind::None),
                            static_cast<uint8_t>(parseSerialCommand("3 0.3", 3).kind));
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SerialCommand::Kind::None),
                            static_cast<uint8_t>(parseSerialCommand("-1 0.3", 3).kind));
}

static void test_blink_interval_prefers_urgent() {
    BoardIndication canDown(true);
    canDown.observe(true, /*latched=*/true);
    TEST_ASSERT_EQUAL_UINT32(200, blinkIntervalFor(canDown, 200, 500, 1000));

    BoardIndication unconfigured(false);
    unconfigured.observe(/*configured=*/false, false);
    TEST_ASSERT_EQUAL_UINT32(200, blinkIntervalFor(unconfigured, 200, 500, 1000));
}

static void test_blink_interval_flags_board_with_no_devices() {
    BoardIndication nothingConfigured(false);
    TEST_ASSERT_TRUE(nothingConfigured.urgent());
    TEST_ASSERT_EQUAL_UINT32(200, blinkIntervalFor(nothingConfigured, 200, 500, 1000));

    BoardIndication oneConfigured(false);
    oneConfigured.observe(/*configured=*/true, false);
    TEST_ASSERT_FALSE(oneConfigured.urgent());
    TEST_ASSERT_EQUAL_UINT32(1000, blinkIntervalFor(oneConfigured, 200, 500, 1000));
}

static void test_blink_interval_separates_stop_from_heartbeat() {
    BoardIndication stopped(false);
    stopped.observe(true, /*latched=*/true);
    TEST_ASSERT_EQUAL_UINT32(500, blinkIntervalFor(stopped, 200, 500, 1000));
    TEST_ASSERT_EQUAL_UINT32(1000, blinkIntervalFor(stopped, 200, 1000, 1000));

    BoardIndication healthy(false);
    healthy.observe(true, false);
    TEST_ASSERT_EQUAL_UINT32(1000, blinkIntervalFor(healthy, 200, 500, 1000));
}

static void test_serial_override_feeds_only_the_touched_channel() {
    SerialOverride override;
    override.note(2, 1000);

    TEST_ASSERT_TRUE(override.shouldFeed(2, 1000));
    TEST_ASSERT_FALSE(override.shouldFeed(0, 1000));
    TEST_ASSERT_FALSE(override.shouldFeed(1, 1000));
    TEST_ASSERT_FALSE(override.shouldFeed(5, 1000));
}

static void test_serial_override_starts_inactive() {
    SerialOverride override;
    TEST_ASSERT_FALSE(override.active(0));
    for (uint8_t ch = 0; ch < kMaxChannels; ++ch) {
        TEST_ASSERT_FALSE(override.shouldFeed(ch, 0));
    }
}

static void test_serial_override_expires_without_new_input() {
    SerialOverride override;
    override.note(1, 1000);

    TEST_ASSERT_TRUE(override.shouldFeed(1, 1000 + kSerialOverrideHoldMs));
    TEST_ASSERT_FALSE(override.shouldFeed(1, 1000 + kSerialOverrideHoldMs + 1));
    TEST_ASSERT_FALSE(override.active(1000 + kSerialOverrideHoldMs + 1));
}

static void test_serial_override_does_not_revive_expired_channels() {
    SerialOverride override;
    override.note(1, 1000);
    override.note(2, 1000 + kSerialOverrideHoldMs + 1);

    TEST_ASSERT_TRUE(override.shouldFeed(2, 1000 + kSerialOverrideHoldMs + 1));
    TEST_ASSERT_FALSE(override.shouldFeed(1, 1000 + kSerialOverrideHoldMs + 1));
}

static void test_serial_override_keeps_channels_touched_within_the_window() {
    SerialOverride override;
    override.note(0, 1000);
    override.note(3, 1500);

    TEST_ASSERT_TRUE(override.shouldFeed(0, 1500));
    TEST_ASSERT_TRUE(override.shouldFeed(3, 1500));
    TEST_ASSERT_FALSE(override.shouldFeed(1, 1500));
}

static void test_serial_override_clears_immediately() {
    SerialOverride override;
    override.note(4, 1000);
    override.clear();

    TEST_ASSERT_FALSE(override.active(1000));
    TEST_ASSERT_FALSE(override.shouldFeed(4, 1000));
}

static void test_serial_override_hold_is_within_the_command_timeout_ceiling() {
    TEST_ASSERT_TRUE(kSerialOverrideHoldMs <= kMaxCommandTimeoutMs);
}

static void test_serial_override_survives_millis_wraparound() {
    SerialOverride override;
    const uint32_t start = 0xFFFFFF00u;
    override.note(0, start);

    TEST_ASSERT_TRUE(override.shouldFeed(0, start + kSerialOverrideHoldMs));
    TEST_ASSERT_FALSE(override.shouldFeed(0, start + kSerialOverrideHoldMs + 1));
}

static void test_tx_fail_counter_starts_silent() {
    TxFailCounter counter;
    TEST_ASSERT_EQUAL_UINT16(0, counter.streak());
    TEST_ASSERT_FALSE(counter.isAlarming(1));
}

static void test_tx_fail_counter_accumulates_failures() {
    TxFailCounter counter;
    for (uint16_t i = 0; i < 3; ++i) {
        counter.onFailure();
    }
    TEST_ASSERT_EQUAL_UINT16(3, counter.streak());
}

static void test_tx_fail_counter_resets_on_success() {
    TxFailCounter counter;
    counter.onFailure();
    counter.onFailure();
    counter.onSuccess();

    TEST_ASSERT_EQUAL_UINT16(0, counter.streak());
    TEST_ASSERT_FALSE(counter.isAlarming(1));
}

static void test_tx_fail_counter_alarms_at_the_threshold() {
    TxFailCounter counter;
    const uint16_t threshold = 3;

    counter.onFailure();
    counter.onFailure();
    TEST_ASSERT_FALSE(counter.isAlarming(threshold));

    counter.onFailure();
    TEST_ASSERT_TRUE(counter.isAlarming(threshold));

    counter.onFailure();
    TEST_ASSERT_TRUE(counter.isAlarming(threshold));
}

static void test_tx_fail_counter_saturates_instead_of_wrapping() {
    TxFailCounter counter;
    for (uint32_t i = 0; i < 0xFFFFu; ++i) {
        counter.onFailure();
    }
    TEST_ASSERT_EQUAL_UINT16(0xFFFF, counter.streak());

    counter.onFailure();
    counter.onFailure();
    TEST_ASSERT_EQUAL_UINT16(0xFFFF, counter.streak());
}

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_broadcast_e_stop_reaches_every_channel);
    RUN_TEST(test_broadcast_e_stop_reaches_unconfigured_channels);
    RUN_TEST(test_e_stop_to_other_device_is_dropped);
    RUN_TEST(test_broadcast_device_id_is_only_for_e_stop);
    RUN_TEST(test_channel_count_beyond_mask_width_is_rejected);
    RUN_TEST(test_own_frame_is_routed_to_matching_channel);
    RUN_TEST(test_frames_for_other_devices_are_dropped);
    RUN_TEST(test_unconfigured_device_receives_only_broadcast_e_stop);
    RUN_TEST(test_unconfigured_channel_is_skipped_in_mixed_table);
    RUN_TEST(test_extended_frame_is_dropped);
    RUN_TEST(test_reserved_and_out_of_range_ids_are_dropped);
    RUN_TEST(test_feedback_of_other_boards_is_dropped);
    RUN_TEST(test_device_id_is_a_fixed_bit_split);
    RUN_TEST(test_device_id_out_of_range_is_unconfigured);
    RUN_TEST(test_dip_reads_two_bits);
    RUN_TEST(test_dip_is_active_low_and_lsb_first);
    RUN_TEST(test_periodic_timer_fires_on_interval);
    RUN_TEST(test_periodic_timer_survives_millis_wraparound);
    RUN_TEST(test_periodic_timer_staggers_phase);
    RUN_TEST(test_periodic_timer_stagger_handles_zero_count);
    RUN_TEST(test_line_buffer_completes_on_lf_and_cr);
    RUN_TEST(test_line_buffer_ignores_empty_lines);
    RUN_TEST(test_line_buffer_caps_length);
    RUN_TEST(test_resolve_device_ids_fills_the_table);
    RUN_TEST(test_resolve_device_ids_skips_non_device_slots);
    RUN_TEST(test_resolve_device_ids_out_of_range_board_number);
    RUN_TEST(test_serial_command_stop_all);
    RUN_TEST(test_serial_command_splits_channel_and_value);
    RUN_TEST(test_serial_command_rejects_ambiguous_lines);
    RUN_TEST(test_blink_interval_prefers_urgent);
    RUN_TEST(test_blink_interval_flags_board_with_no_devices);
    RUN_TEST(test_blink_interval_separates_stop_from_heartbeat);
    RUN_TEST(test_serial_override_feeds_only_the_touched_channel);
    RUN_TEST(test_serial_override_starts_inactive);
    RUN_TEST(test_serial_override_expires_without_new_input);
    RUN_TEST(test_serial_override_does_not_revive_expired_channels);
    RUN_TEST(test_serial_override_keeps_channels_touched_within_the_window);
    RUN_TEST(test_serial_override_clears_immediately);
    RUN_TEST(test_serial_override_hold_is_within_the_command_timeout_ceiling);
    RUN_TEST(test_serial_override_survives_millis_wraparound);
    RUN_TEST(test_tx_fail_counter_starts_silent);
    RUN_TEST(test_tx_fail_counter_accumulates_failures);
    RUN_TEST(test_tx_fail_counter_resets_on_success);
    RUN_TEST(test_tx_fail_counter_alarms_at_the_threshold);
    RUN_TEST(test_tx_fail_counter_saturates_instead_of_wrapping);
    return UNITY_END();
}
