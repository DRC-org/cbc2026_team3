#include <unity.h>

#include <string.h>

#include "MotorCanProtocol.h"
#include "MotorPinTable.h"
#include "SolenoidChannel.h"

using namespace motorcan;

namespace {

constexpr uint32_t kTimeoutMs = 500;

SolenoidChannel makeFedChannel(uint32_t nowMs) {
    SolenoidChannel channel(kTimeoutMs);
    channel.feed(nowMs);
    return channel;
}

}

void setUp() {}
void tearDown() {}

static void test_solenoid_device_id_is_a_fixed_bit_split() {
    TEST_ASSERT_EQUAL_UINT8(0xC0, makeDeviceId(BoardKind::Solenoid, 0, 0));
    TEST_ASSERT_EQUAL_UINT8(0xC5, makeDeviceId(BoardKind::Solenoid, 0, 5));
    TEST_ASSERT_EQUAL_UINT8(0xC8, makeDeviceId(BoardKind::Solenoid, 1, 0));
    TEST_ASSERT_EQUAL_UINT8(0xCD, makeDeviceId(BoardKind::Solenoid, 1, 5));
}

static void test_broadcast_slot_falls_back_to_unconfigured() {
    TEST_ASSERT_EQUAL_UINT8(kDeviceIdUnconfigured, makeDeviceId(BoardKind::Solenoid, 7, 7));
    TEST_ASSERT_EQUAL_UINT8(0xFE, makeDeviceId(BoardKind::Solenoid, 7, 6));
    TEST_ASSERT_EQUAL_UINT8(0xF7, makeDeviceId(BoardKind::Solenoid, 6, 7));
}

static void test_device_ids_never_collide_across_three_boards() {
    for (uint8_t board = 0; board <= kMaxBoardNumber; ++board) {
        for (uint8_t slot = 0; slot <= kMaxSlotNumber; ++slot) {
            const uint8_t servo = makeDeviceId(BoardKind::Servo, board, slot);
            const uint8_t dc = makeDeviceId(BoardKind::Dc, board, slot);
            const uint8_t solenoid = makeDeviceId(BoardKind::Solenoid, board, slot);

            TEST_ASSERT_NOT_EQUAL_UINT8(servo, dc);
            TEST_ASSERT_NOT_EQUAL_UINT8(servo, solenoid);
            TEST_ASSERT_NOT_EQUAL_UINT8(dc, solenoid);

            TEST_ASSERT_NOT_EQUAL_UINT8(kDeviceIdBroadcast, servo);
            TEST_ASSERT_NOT_EQUAL_UINT8(kDeviceIdBroadcast, dc);
            TEST_ASSERT_NOT_EQUAL_UINT8(kDeviceIdBroadcast, solenoid);

            TEST_ASSERT_NOT_EQUAL_UINT8(kDeviceIdUnconfigured, servo);
            TEST_ASSERT_NOT_EQUAL_UINT8(kDeviceIdUnconfigured, dc);
            if (board != kMaxBoardNumber || slot != kMaxSlotNumber) {
                TEST_ASSERT_NOT_EQUAL_UINT8(kDeviceIdUnconfigured, solenoid);
            }
        }
    }
}

static void test_on_off_target_is_zero_or_not_zero() {
    const uint8_t off[3] = {static_cast<uint8_t>(ControlType::OnOff), 0x00, 0x00};
    const uint8_t on[3] = {static_cast<uint8_t>(ControlType::OnOff), 0x01, 0x00};

    TEST_ASSERT_EQUAL_INT16(0, decodeSetTarget(off, sizeof(off)).raw);
    TEST_ASSERT_TRUE(decodeSetTarget(on, sizeof(on)).raw != 0);
}

static void test_decode_set_target_rejects_reserved_control_type() {
    const uint8_t frame[3] = {4, 0x01, 0x00};
    TEST_ASSERT_FALSE(decodeSetTarget(frame, sizeof(frame)).valid);
}

static void test_encode_info_reports_solenoid_board() {
    uint8_t out[8] = {0};
    const uint8_t length = encodeInfo(out, 7, BoardKind::Solenoid, SlotKind::Actuator);

    TEST_ASSERT_EQUAL_UINT8(kInfoBaseLength, length);
    TEST_ASSERT_EQUAL_UINT8(7, out[0]);
    TEST_ASSERT_EQUAL_UINT8(3, out[1]);
    TEST_ASSERT_EQUAL_UINT8(0, out[2]);
}

static void test_solenoid_channel_starts_de_energized() {
    SolenoidChannel channel(kTimeoutMs);

    TEST_ASSERT_FALSE(channel.isOutputAllowed(0));
    TEST_ASSERT_FALSE(channel.outputOn(0));
    TEST_ASSERT_FALSE(channel.setOn(true, 0));
    TEST_ASSERT_FALSE(channel.outputOn(0));
}

static void test_solenoid_channel_energizes_after_first_command() {
    SolenoidChannel channel = makeFedChannel(1000);

    TEST_ASSERT_TRUE(channel.setOn(true, 1000));
    TEST_ASSERT_TRUE(channel.outputOn(1000));

    TEST_ASSERT_TRUE(channel.setOn(false, 1000));
    TEST_ASSERT_FALSE(channel.outputOn(1000));
}

static void test_output_stops_on_watchdog_and_recovers() {
    SolenoidChannel channel = makeFedChannel(1000);
    TEST_ASSERT_TRUE(channel.setOn(true, 1000));
    TEST_ASSERT_TRUE(channel.outputOn(1000));

    TEST_ASSERT_TRUE(channel.outputOn(1000 + kTimeoutMs - 1));
    TEST_ASSERT_FALSE(channel.outputOn(1000 + kTimeoutMs + 1));

    channel.feed(2000);
    TEST_ASSERT_TRUE(channel.setOn(true, 2000));
    TEST_ASSERT_TRUE(channel.outputOn(2000));
}

static void test_target_is_forgotten_while_output_is_blocked() {
    SolenoidChannel channel = makeFedChannel(1000);
    TEST_ASSERT_TRUE(channel.setOn(true, 1000));

    const uint32_t expired = 1000 + kTimeoutMs + 1;
    TEST_ASSERT_FALSE(channel.outputOn(expired));
    channel.tick(expired);

    channel.feed(expired + 10);
    TEST_ASSERT_FALSE(
        channel.applySetTarget(SetTargetCommand{ControlType::Duty, 1000, true}, expired + 10));

    TEST_ASSERT_FALSE(channel.outputOn(expired + 10));
}

static void test_output_gate_overrides_stale_target() {
    SolenoidChannel channel = makeFedChannel(1000);
    TEST_ASSERT_TRUE(channel.setOn(true, 1000));

    TEST_ASSERT_FALSE(channel.outputOn(1000 + kTimeoutMs + 1));
}

static void test_rejects_command_while_latched() {
    SolenoidChannel channel = makeFedChannel(1000);
    const uint8_t stop[3] = {0x00, 0x00, 0x00};

    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(EStopAction::Stop),
                            static_cast<uint8_t>(channel.handleEStopFrame(stop, sizeof(stop))));

    TEST_ASSERT_FALSE(channel.setOn(true, 1000));
    TEST_ASSERT_FALSE(channel.outputOn(1000));
}

static void test_clear_does_not_re_energize() {
    SolenoidChannel channel = makeFedChannel(1000);
    TEST_ASSERT_TRUE(channel.setOn(true, 1000));

    const uint8_t stop[3] = {0x00, 0x00, 0x00};
    channel.handleEStopFrame(stop, sizeof(stop));

    const uint8_t clear[3] = {0x01, 0x5A, 0xA5};
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(EStopAction::Clear),
                            static_cast<uint8_t>(channel.handleEStopFrame(clear, sizeof(clear))));

    TEST_ASSERT_TRUE(channel.isOutputAllowed(1000));
    TEST_ASSERT_FALSE(channel.outputOn(1000));
}

static void test_clear_requires_magic_bytes() {
    SolenoidChannel channel = makeFedChannel(1000);
    const uint8_t stop[3] = {0x00, 0x00, 0x00};
    channel.handleEStopFrame(stop, sizeof(stop));

    const uint8_t bogus[3] = {0x01, 0x00, 0x00};
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(EStopAction::None),
                            static_cast<uint8_t>(channel.handleEStopFrame(bogus, sizeof(bogus))));
    TEST_ASSERT_FALSE(channel.isOutputAllowed(1000));
}

static void test_feed_works_while_latched() {
    SolenoidChannel channel = makeFedChannel(1000);
    const uint8_t stop[3] = {0x00, 0x00, 0x00};
    channel.handleEStopFrame(stop, sizeof(stop));

    channel.feed(1400);

    const uint8_t clear[3] = {0x01, 0x5A, 0xA5};
    channel.handleEStopFrame(clear, sizeof(clear));

    TEST_ASSERT_TRUE(channel.isOutputAllowed(1400));
}

static void test_stop_latches_and_de_energizes() {
    SolenoidChannel channel = makeFedChannel(1000);
    TEST_ASSERT_TRUE(channel.setOn(true, 1000));

    channel.stop();

    TEST_ASSERT_FALSE(channel.isOutputAllowed(1000));
    TEST_ASSERT_FALSE(channel.outputOn(1000));
}

static void test_hold_de_energizes_without_latching() {
    SolenoidChannel channel = makeFedChannel(1000);
    TEST_ASSERT_TRUE(channel.setOn(true, 1000));

    channel.hold();
    TEST_ASSERT_FALSE(channel.outputOn(1000));
    TEST_ASSERT_TRUE(channel.isOutputAllowed(1000));

    TEST_ASSERT_TRUE(channel.setOn(true, 1000));
    TEST_ASSERT_TRUE(channel.outputOn(1000));
}

static void test_status_flags_follow_safety() {
    SolenoidChannel fresh(kTimeoutMs);
    TEST_ASSERT_EQUAL_UINT8(0, fresh.safetyStatusFlags(0) & status_flag::kWatchdog);

    SolenoidChannel channel = makeFedChannel(1000);
    TEST_ASSERT_EQUAL_UINT8(0, channel.safetyStatusFlags(1000));

    TEST_ASSERT_EQUAL_UINT8(status_flag::kWatchdog,
                            channel.safetyStatusFlags(1000 + kTimeoutMs + 1) &
                                status_flag::kWatchdog);

    const uint8_t stop[3] = {0x00, 0x00, 0x00};
    channel.handleEStopFrame(stop, sizeof(stop));
    TEST_ASSERT_EQUAL_UINT8(status_flag::kEStop,
                            channel.safetyStatusFlags(1000) & status_flag::kEStop);
}

static void test_disabled_watchdog_still_requires_first_command() {
    SolenoidChannel channel(kTimeoutMs);
    channel.setWatchdogEnabled(false);

    TEST_ASSERT_FALSE(channel.isOutputAllowed(0));
    TEST_ASSERT_FALSE(channel.setOn(true, 0));

    channel.feed(1000);
    TEST_ASSERT_TRUE(channel.setOn(true, 1000));
    TEST_ASSERT_TRUE(channel.outputOn(1000 + kTimeoutMs * 10));
}

static void test_solenoid_channel_accepts_only_on_off_targets() {
    SolenoidChannel channel = makeFedChannel(1000);

    const SetTargetCommand duty{ControlType::Duty, 3000, true};
    TEST_ASSERT_FALSE(channel.applySetTarget(duty, 1000));
    const SetTargetCommand position{ControlType::Position, 900, true};
    TEST_ASSERT_FALSE(channel.applySetTarget(position, 1000));
    const SetTargetCommand velocity{ControlType::Velocity, 900, true};
    TEST_ASSERT_FALSE(channel.applySetTarget(velocity, 1000));
    const SetTargetCommand invalid{ControlType::OnOff, 1, false};
    TEST_ASSERT_FALSE(channel.applySetTarget(invalid, 1000));

    TEST_ASSERT_FALSE(channel.outputOn(1000));

    const SetTargetCommand on{ControlType::OnOff, 1, true};
    TEST_ASSERT_TRUE(channel.applySetTarget(on, 1000));
    TEST_ASSERT_TRUE(channel.outputOn(1000));

    const SetTargetCommand off{ControlType::OnOff, 0, true};
    TEST_ASSERT_TRUE(channel.applySetTarget(off, 1000));
    TEST_ASSERT_FALSE(channel.outputOn(1000));
}

static void test_apply_set_target_still_honors_the_output_gate() {
    SolenoidChannel channel = makeFedChannel(1000);
    const uint8_t stop[3] = {0x00, 0x00, 0x00};
    channel.handleEStopFrame(stop, sizeof(stop));

    const SetTargetCommand on{ControlType::OnOff, 1, true};
    channel.feed(1000);
    TEST_ASSERT_FALSE(channel.applySetTarget(on, 1000));
    TEST_ASSERT_FALSE(channel.outputOn(1000));
}

static void test_pin_tables_match_when_identical() {
    const PortPin actual[] = {{1, 1u << 7}, {1, 1u << 3}, {0, 1u << 15}};
    const PortPin expected[] = {{1, 1u << 7}, {1, 1u << 3}, {0, 1u << 15}};
    TEST_ASSERT_TRUE(pinTablesMatch(actual, expected, 3));
}

static void test_pin_tables_detect_a_port_only_difference() {
    const PortPin actual[] = {{1, 1u << 7}, {0, 1u << 3}};
    const PortPin expected[] = {{1, 1u << 7}, {1, 1u << 3}};
    TEST_ASSERT_FALSE(pinTablesMatch(actual, expected, 2));
}

static void test_pin_tables_detect_a_pin_only_difference() {
    const PortPin actual[] = {{1, 1u << 7}, {1, 1u << 4}};
    const PortPin expected[] = {{1, 1u << 7}, {1, 1u << 3}};
    TEST_ASSERT_FALSE(pinTablesMatch(actual, expected, 2));
}

static void test_unknown_port_never_matches() {
    const PortPin actual[] = {{kPortIndexUnknown, 1u << 7}};
    const PortPin expected[] = {{kPortIndexUnknown, 1u << 7}};
    TEST_ASSERT_FALSE(pinTablesMatch(actual, expected, 1));
}

static void test_empty_table_is_not_a_match() {
    const PortPin actual[] = {{1, 1u << 7}};
    const PortPin expected[] = {{1, 1u << 7}};
    TEST_ASSERT_FALSE(pinTablesMatch(actual, expected, 0));
    TEST_ASSERT_FALSE(pinTablesMatch(nullptr, expected, 1));
    TEST_ASSERT_FALSE(pinTablesMatch(actual, nullptr, 1));
}

int main() {
    UNITY_BEGIN();
    RUN_TEST(test_solenoid_device_id_is_a_fixed_bit_split);
    RUN_TEST(test_broadcast_slot_falls_back_to_unconfigured);
    RUN_TEST(test_device_ids_never_collide_across_three_boards);
    RUN_TEST(test_on_off_target_is_zero_or_not_zero);
    RUN_TEST(test_decode_set_target_rejects_reserved_control_type);
    RUN_TEST(test_encode_info_reports_solenoid_board);
    RUN_TEST(test_solenoid_channel_starts_de_energized);
    RUN_TEST(test_solenoid_channel_energizes_after_first_command);
    RUN_TEST(test_output_stops_on_watchdog_and_recovers);
    RUN_TEST(test_target_is_forgotten_while_output_is_blocked);
    RUN_TEST(test_output_gate_overrides_stale_target);
    RUN_TEST(test_rejects_command_while_latched);
    RUN_TEST(test_clear_does_not_re_energize);
    RUN_TEST(test_clear_requires_magic_bytes);
    RUN_TEST(test_feed_works_while_latched);
    RUN_TEST(test_stop_latches_and_de_energizes);
    RUN_TEST(test_hold_de_energizes_without_latching);
    RUN_TEST(test_status_flags_follow_safety);
    RUN_TEST(test_disabled_watchdog_still_requires_first_command);
    RUN_TEST(test_solenoid_channel_accepts_only_on_off_targets);
    RUN_TEST(test_apply_set_target_still_honors_the_output_gate);
    RUN_TEST(test_pin_tables_match_when_identical);
    RUN_TEST(test_pin_tables_detect_a_port_only_difference);
    RUN_TEST(test_pin_tables_detect_a_pin_only_difference);
    RUN_TEST(test_unknown_port_never_matches);
    RUN_TEST(test_empty_table_is_not_a_match);
    return UNITY_END();
}
