// 基板共通部（フレームの振り分け・デバイス ID の解決・周期タイマ・シリアル行バッファ）の
// native ユニットテスト。
//
// ここが守るのは main.cpp の「配線」だった部分で、以前はどちらのファームにも
// テストが 1 件も無かった。宛先判定を間違えると、他のアクチュエータ宛のフレームで
// 自分が動く／ブロードキャスト緊急停止が届かない、のどちらも起こりうる。

#include <unity.h>

#include <string.h>

#include "MotorCanProtocol.h"
#include "MotorCanRouter.h"
#include "MotorLoopTimer.h"
#include "SerialLineBuffer.h"

using namespace motorcan;

void setUp() {}
void tearDown() {}

// サーボ基板の既定構成（config/main_hand.yaml の gripper / wall_f / wall_r）。
static const uint8_t kServoIds[3] = {0x01, 0x03, 0x04};
// DC 基板は 1 チャンネル。DIP の値がそのままデバイス ID。
static const uint8_t kDcId[1] = {0x02};

static FrameRoute routeStandard(uint16_t canId, const uint8_t *ids, uint8_t count) {
    return routeFrame(canId, true, ids, count);
}

// --------------------------------------------------------------------------
// §3.5 ブロードキャスト E_STOP
// --------------------------------------------------------------------------

// 0x7FF は全チャンネルに届かなければならない。1 チャンネルでも取りこぼすと、
// PC が緊急停止を送ってもそのサーボだけ動き続ける。
static void test_broadcast_e_stop_reaches_every_channel() {
    const FrameRoute route = routeStandard(kBroadcastEStopCanId, kServoIds, 3);
    TEST_ASSERT_TRUE(route.accepted);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(CommandType::EStop),
                            static_cast<uint8_t>(route.command));
    TEST_ASSERT_EQUAL_UINT8(0b111, route.channelMask);
}

// デバイス ID 未設定のチャンネルにも緊急停止だけは届く（仕様書 §2.2 / §5.4）。
// 未設定チャンネルは駆動しないが、「未設定だから止められない」経路を作らない。
static void test_broadcast_e_stop_reaches_unconfigured_channels() {
    const uint8_t ids[3] = {kDeviceIdUnconfigured, kDeviceIdUnconfigured, kDeviceIdUnconfigured};
    const FrameRoute route = routeStandard(kBroadcastEStopCanId, ids, 3);
    TEST_ASSERT_TRUE(route.accepted);
    TEST_ASSERT_EQUAL_UINT8(0b111, route.channelMask);
}

// 0x7FE は「デバイス 0xFE への E_STOP」であってブロードキャストではない。
// 1 ビットの取り違えで全基板が止まる／止まらないが入れ替わる。
static void test_e_stop_to_other_device_is_dropped() {
    TEST_ASSERT_FALSE(routeStandard(0x7FE, kServoIds, 3).accepted);
    TEST_ASSERT_FALSE(routeStandard(0x7FE, kDcId, 1).accepted);
}

// 0xFF は E_STOP のブロードキャスト専用に予約されている（仕様書 §2.2）。
// SET_TARGET を 0xFF で送っても誰も動かしてはならない。
static void test_broadcast_device_id_is_only_for_e_stop() {
    TEST_ASSERT_FALSE(routeStandard(0x0FF, kServoIds, 3).accepted);
    TEST_ASSERT_FALSE(routeStandard(0x2FF, kServoIds, 3).accepted);
    TEST_ASSERT_FALSE(routeStandard(0x3FF, kServoIds, 3).accepted);
}

// --------------------------------------------------------------------------
// §2.2 / §7.1 宛先判定
// --------------------------------------------------------------------------

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

// 同じ can_generic バスをメインハンドとサブハンドが共有するので、
// 他機のフレームは必ず捨てなければならない（仕様書 §2.2）。
static void test_frames_for_other_devices_are_dropped() {
    for (uint16_t dev = 0x01; dev <= 0x10; ++dev) {
        const bool mine = (dev == 0x02);
        const FrameRoute route =
            routeStandard(buildCanId(CommandType::SetTarget, static_cast<uint8_t>(dev)), kDcId, 1);
        TEST_ASSERT_EQUAL(mine, route.accepted);
    }
}

// 仕様書 §2.2: デバイス ID 0x00 の基板／チャンネルに「自分宛」は存在しない。
// 受けるのはブロードキャスト E_STOP だけ。
static void test_unconfigured_device_receives_only_broadcast_e_stop() {
    const uint8_t ids[1] = {kDeviceIdUnconfigured};
    TEST_ASSERT_FALSE(routeStandard(buildCanId(CommandType::SetTarget, 0x00), ids, 1).accepted);
    TEST_ASSERT_FALSE(routeStandard(buildCanId(CommandType::SetMode, 0x00), ids, 1).accepted);
    TEST_ASSERT_FALSE(routeStandard(buildCanId(CommandType::SetParam, 0x00), ids, 1).accepted);
    TEST_ASSERT_FALSE(routeStandard(buildCanId(CommandType::EStop, 0x00), ids, 1).accepted);
    TEST_ASSERT_TRUE(routeStandard(kBroadcastEStopCanId, ids, 1).accepted);
}

// 混在した基板構成でも、未設定チャンネルは自分宛フレームを持たない。
static void test_unconfigured_channel_is_skipped_in_mixed_table() {
    const uint8_t ids[3] = {0x01, kDeviceIdUnconfigured, 0x04};
    const FrameRoute route = routeStandard(buildCanId(CommandType::SetTarget, 0x00), ids, 3);
    TEST_ASSERT_FALSE(route.accepted);
}

// --------------------------------------------------------------------------
// §1 / §2.1 フィルタ
// --------------------------------------------------------------------------

// 本プロトコルは Standard Frame のみ。同じ PC 上の EDULITE は Extended Frame を使う。
static void test_extended_frame_is_dropped() {
    TEST_ASSERT_FALSE(routeFrame(kBroadcastEStopCanId, false, kServoIds, 3).accepted);
    TEST_ASSERT_FALSE(routeFrame(buildCanId(CommandType::SetTarget, 0x03), false, kServoIds, 3)
                          .accepted);
}

// 予約コマンド種別（0b100 / 0b101 / 0b110）と 11bit を超える ID は無視する。
static void test_reserved_and_out_of_range_ids_are_dropped() {
    for (uint16_t cmd = 4; cmd <= 6; ++cmd) {
        TEST_ASSERT_FALSE(routeStandard(static_cast<uint16_t>((cmd << 8) | 0x03), kServoIds, 3)
                              .accepted);
    }
    TEST_ASSERT_FALSE(routeStandard(0x800, kServoIds, 3).accepted);
}

// 他基板の FEEDBACK は自分の ID 空間と重ならないが、届いても駆動へ触れないこと。
static void test_feedback_of_other_boards_is_dropped() {
    TEST_ASSERT_FALSE(routeStandard(buildCanId(CommandType::Feedback, 0x05), kServoIds, 3)
                          .accepted);
}

// --------------------------------------------------------------------------
// §7.1 DIP オフセットによるデバイス ID の解決
// --------------------------------------------------------------------------

static void test_offset_shifts_the_whole_channel_table() {
    TEST_ASSERT_EQUAL_UINT8(0x05, applyDeviceIdOffset(0x01, 4));
    TEST_ASSERT_EQUAL_UINT8(0x07, applyDeviceIdOffset(0x03, 4));
    TEST_ASSERT_EQUAL_UINT8(0x08, applyDeviceIdOffset(0x04, 4));
}

static void test_zero_offset_keeps_the_table_as_is() {
    TEST_ASSERT_EQUAL_UINT8(0x01, applyDeviceIdOffset(0x01, 0));
    TEST_ASSERT_EQUAL_UINT8(0x04, applyDeviceIdOffset(0x04, 0));
}

// 8bit の足し算は回り込む。0x00（未設定）や 0xFF（ブロードキャスト予約）になった
// チャンネルをそのまま使うと、E_STOP のブロードキャストに応答する側へ回ったり、
// 未設定判定を素通りして駆動してしまう。
static void test_offset_wraparound_to_reserved_ids_is_unconfigured() {
    TEST_ASSERT_EQUAL_UINT8(kDeviceIdUnconfigured, applyDeviceIdOffset(0x01, 0xFF));
    TEST_ASSERT_EQUAL_UINT8(kDeviceIdUnconfigured, applyDeviceIdOffset(0x01, 0xFE));
    TEST_ASSERT_EQUAL_UINT8(kDeviceIdUnconfigured, applyDeviceIdOffset(0x04, 0xFC));
}

// 基準 ID そのものが未設定なら、オフセットで有効な ID に化けさせない。
static void test_unconfigured_base_stays_unconfigured() {
    TEST_ASSERT_EQUAL_UINT8(kDeviceIdUnconfigured, applyDeviceIdOffset(kDeviceIdUnconfigured, 4));
}

// --------------------------------------------------------------------------
// DIP スイッチの読み出し
// --------------------------------------------------------------------------

static uint8_t g_stubPins[4] = {0, 0, 0, 0};
static int stubReadPin(uint8_t pin) { return g_stubPins[pin]; }

// 添字がビット位置で、INPUT_PULLUP の負論理（LOW = ON = 1）。
// 順序を取り違えると 0b0001 と 0b1000 が入れ替わり、別のアクチュエータが動く。
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

// --------------------------------------------------------------------------
// 周期タイマ
// --------------------------------------------------------------------------

static void test_periodic_timer_fires_on_interval() {
    PeriodicTimer timer;
    timer.reset(1000);
    TEST_ASSERT_FALSE(timer.due(1009, 10));
    TEST_ASSERT_TRUE(timer.due(1010, 10));
    TEST_ASSERT_FALSE(timer.due(1010, 10));
    TEST_ASSERT_TRUE(timer.due(1020, 10));
}

// millis() は約 49.7 日で 0 に戻る。符号つきで比較すると折り返し直後に
// 永久に満了しなくなり、FEEDBACK も点滅も止まる。
static void test_periodic_timer_survives_millis_wraparound() {
    PeriodicTimer timer;
    timer.reset(0xFFFFFFF8u);
    TEST_ASSERT_FALSE(timer.due(0xFFFFFFFEu, 10));
    TEST_ASSERT_TRUE(timer.due(0x00000002u, 10));
}

// サーボは全チャンネルが同時に送らないよう、起点をずらして初期化する。
static void test_periodic_timer_accepts_phase_offset() {
    PeriodicTimer timer;
    timer.setLastMs(1000u - 10u + 5u);
    TEST_ASSERT_FALSE(timer.due(1000, 10));
    TEST_ASSERT_TRUE(timer.due(1005, 10));
}

// --------------------------------------------------------------------------
// シリアル行バッファ
// --------------------------------------------------------------------------

// 実際の使い方（push() が true を返したその場で行を読む）に合わせたヘルパ。
// 完成した行の本数を返し、最後の 1 行を out へ写す。
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

// CRLF の 2 文字目や区切りの連打で「空の行が完成した」と報告すると、
// 数値として 0 に化けて duty 0 や角度 0 を指令したことになる。
static void test_line_buffer_ignores_empty_lines() {
    char storage[16];
    char last[32];
    SerialLineBuffer buffer(storage, sizeof(storage));

    TEST_ASSERT_EQUAL_UINT8(0, feed(buffer, "\n\r\n\n", last));
    TEST_ASSERT_EQUAL_UINT8(1, feed(buffer, "s\r\n", last));
    TEST_ASSERT_EQUAL_STRING("s", last);
}

// 容量を超えた入力で書き潰さないこと（ノイズで長い行が来ても壊れない）。
static void test_line_buffer_caps_length() {
    char storage[8];
    char last[32];
    SerialLineBuffer buffer(storage, sizeof(storage));

    TEST_ASSERT_EQUAL_UINT8(1, feed(buffer, "0123456789\n", last));
    TEST_ASSERT_EQUAL_STRING("0123456", last);
}

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_broadcast_e_stop_reaches_every_channel);
    RUN_TEST(test_broadcast_e_stop_reaches_unconfigured_channels);
    RUN_TEST(test_e_stop_to_other_device_is_dropped);
    RUN_TEST(test_broadcast_device_id_is_only_for_e_stop);
    RUN_TEST(test_own_frame_is_routed_to_matching_channel);
    RUN_TEST(test_frames_for_other_devices_are_dropped);
    RUN_TEST(test_unconfigured_device_receives_only_broadcast_e_stop);
    RUN_TEST(test_unconfigured_channel_is_skipped_in_mixed_table);
    RUN_TEST(test_extended_frame_is_dropped);
    RUN_TEST(test_reserved_and_out_of_range_ids_are_dropped);
    RUN_TEST(test_feedback_of_other_boards_is_dropped);
    RUN_TEST(test_offset_shifts_the_whole_channel_table);
    RUN_TEST(test_zero_offset_keeps_the_table_as_is);
    RUN_TEST(test_offset_wraparound_to_reserved_ids_is_unconfigured);
    RUN_TEST(test_unconfigured_base_stays_unconfigured);
    RUN_TEST(test_dip_is_active_low_and_lsb_first);
    RUN_TEST(test_periodic_timer_fires_on_interval);
    RUN_TEST(test_periodic_timer_survives_millis_wraparound);
    RUN_TEST(test_periodic_timer_accepts_phase_offset);
    RUN_TEST(test_line_buffer_completes_on_lf_and_cr);
    RUN_TEST(test_line_buffer_ignores_empty_lines);
    RUN_TEST(test_line_buffer_caps_length);
    return UNITY_END();
}
