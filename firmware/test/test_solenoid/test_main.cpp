// 電磁弁用モタドラ（firmware/solenoid/）の native ユニットテスト。
// 実機を用意せずにプロトコルの取り違えを検出するのが目的なので、
// ここで検証するのはすべて docs/motor_driver_can_protocol.md §9 に明記された挙動に限る。
//
// **実機ビルドは CubeMX + CMake だが、この層は STM32 HAL を一切参照しない。**
// SolenoidChannel と MotorCanProtocol が Arduino.h も stm32f3xx_hal.h も include しないのは
// そのためで、ビルド系が違っても安全機構の規則は 3 枚で 1 つに保たれる。

#include <unity.h>

#include <string.h>

#include "MotorCanProtocol.h"
#include "SolenoidChannel.h"

using namespace motorcan;

namespace {

constexpr uint32_t kTimeoutMs = 500;

// 起動直後は §5.4 により「SET_TARGET 未受信」で出力禁止。
// 出力が許可された状態を作るには feed() が要る。
SolenoidChannel makeFedChannel(uint32_t nowMs) {
    SolenoidChannel channel(kTimeoutMs);
    channel.feed(nowMs);
    return channel;
}

}  // namespace

void setUp() {}
void tearDown() {}

// --------------------------------------------------------------------------
// §2.2 デバイス ID
// --------------------------------------------------------------------------

// 電磁弁基板は種別 3。ID を見ればどの基板のどのチャンネルかが直接読める。
static void test_solenoid_device_id_is_a_fixed_bit_split() {
    TEST_ASSERT_EQUAL_UINT8(0xC0, makeDeviceId(BoardKind::Solenoid, 0, 0));
    TEST_ASSERT_EQUAL_UINT8(0xC5, makeDeviceId(BoardKind::Solenoid, 0, 5));
    TEST_ASSERT_EQUAL_UINT8(0xC8, makeDeviceId(BoardKind::Solenoid, 1, 0));
    TEST_ASSERT_EQUAL_UINT8(0xCD, makeDeviceId(BoardKind::Solenoid, 1, 5));
}

// **0xFF はブロードキャストの予約なので、そこへ着地する 1 個だけは未設定へ倒す。**
// ブロードキャストと同じデバイス ID を名乗る基板が居ると、そのスロット宛の SET_TARGET と
// 全基板向けの E_STOP がデバイス ID の上で区別できなくなる。
static void test_broadcast_slot_falls_back_to_unconfigured() {
    TEST_ASSERT_EQUAL_UINT8(kDeviceIdUnconfigured, makeDeviceId(BoardKind::Solenoid, 7, 7));
    // その手前と隣は通常どおり使える（潰れるのは 512 個中 1 個だけ）
    TEST_ASSERT_EQUAL_UINT8(0xFE, makeDeviceId(BoardKind::Solenoid, 7, 6));
    TEST_ASSERT_EQUAL_UINT8(0xF7, makeDeviceId(BoardKind::Solenoid, 6, 7));
}

// 基板種別が 3 つに増えても、基板番号とスロットがどう組み合わさっても衝突しない。
// 予約されている 0x00 / 0xFF にも決して着地しない。
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
        }
    }
}

// --------------------------------------------------------------------------
// §3.1 / §9.2 on_off の復号
// --------------------------------------------------------------------------

// 制御タイプ 3 を復号層が知らないと、SET_TARGET が丸ごと捨てられて弁が一度も動かない
// （PC 側からは「指令したのに反応しない基板」にしか見えない）。
static void test_decode_set_target_accepts_on_off() {
    const uint8_t frame[3] = {static_cast<uint8_t>(ControlType::OnOff), 0x01, 0x00};
    const SetTargetCommand cmd = decodeSetTarget(frame, sizeof(frame));

    TEST_ASSERT_TRUE(cmd.valid);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(ControlType::OnOff), static_cast<uint8_t>(cmd.type));
    TEST_ASSERT_EQUAL_INT16(1, cmd.raw);
}

// **on_off の目標値に固定小数点のスケールは掛からない**（仕様書 §4 の表）。
// kDutyScale を掛けると 1 が 10000 になり、0 との区別しか使わないこの基板では
// 症状が出ないまま PC 側と単位が食い違う。
static void test_on_off_target_is_zero_or_not_zero() {
    const uint8_t off[3] = {static_cast<uint8_t>(ControlType::OnOff), 0x00, 0x00};
    const uint8_t on[3] = {static_cast<uint8_t>(ControlType::OnOff), 0x01, 0x00};

    TEST_ASSERT_EQUAL_INT16(0, decodeSetTarget(off, sizeof(off)).raw);
    TEST_ASSERT_TRUE(decodeSetTarget(on, sizeof(on)).raw != 0);
}

// 予約されている制御タイプは受理しない。ここが素通しになると、
// 未定義の値が「ON」として解釈されうる。
static void test_decode_set_target_rejects_reserved_control_type() {
    const uint8_t frame[3] = {4, 0x01, 0x00};
    TEST_ASSERT_FALSE(decodeSetTarget(frame, sizeof(frame)).valid);
}

// --------------------------------------------------------------------------
// §3.4 INFO
// --------------------------------------------------------------------------

// 焼き忘れた基板をセッティングタイムに見つけるための自己申告。
// 基板種別が 3（電磁弁）で出ないと、PC 側は DC 基板と区別できない。
static void test_encode_info_reports_solenoid_board() {
    uint8_t out[8] = {0};
    const uint8_t length = encodeInfo(out, 7, BoardKind::Solenoid, SlotKind::Actuator);

    TEST_ASSERT_EQUAL_UINT8(kInfoLength, length);
    TEST_ASSERT_EQUAL_UINT8(7, out[0]);
    TEST_ASSERT_EQUAL_UINT8(3, out[1]);
    TEST_ASSERT_EQUAL_UINT8(0, out[2]);
}

// --------------------------------------------------------------------------
// §5.4 起動時の状態
// --------------------------------------------------------------------------

// 電源投入直後は目標 OFF・出力禁止。**SET_TARGET を 1 通も受け取るまで通電しない。**
static void test_solenoid_channel_starts_de_energized() {
    SolenoidChannel channel(kTimeoutMs);

    TEST_ASSERT_FALSE(channel.isOutputAllowed(0));
    TEST_ASSERT_FALSE(channel.outputOn(0));
    // 未受信の間は指令そのものが通らない
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

// --------------------------------------------------------------------------
// §5.1 コマンドウォッチドッグ
// --------------------------------------------------------------------------

// PC の停止・ケーブル断で弁が開きっぱなしになるのを防ぐ最後の砦。
// 通信が戻れば復帰する（ラッチしない）。
static void test_output_stops_on_watchdog_and_recovers() {
    SolenoidChannel channel = makeFedChannel(1000);
    TEST_ASSERT_TRUE(channel.setOn(true, 1000));
    TEST_ASSERT_TRUE(channel.outputOn(1000));

    // 満了直前はまだ通電している
    TEST_ASSERT_TRUE(channel.outputOn(1000 + kTimeoutMs - 1));
    // 満了で消磁
    TEST_ASSERT_FALSE(channel.outputOn(1000 + kTimeoutMs + 1));

    // 再送が届けば通常動作へ戻る
    channel.feed(2000);
    TEST_ASSERT_TRUE(channel.setOn(true, 2000));
    TEST_ASSERT_TRUE(channel.outputOn(2000));
}

// **出力へ至る経路は outputOn() の 1 本だけ。** 目標が ON のまま残っていても、
// ゲートが閉じていれば false を返す。ここが素通しになると、app.cpp が
// 安全機構を迂回して GPIO を叩ける形になる。
static void test_output_gate_overrides_stale_target() {
    SolenoidChannel channel = makeFedChannel(1000);
    TEST_ASSERT_TRUE(channel.setOn(true, 1000));

    // 目標は ON のままだが、満了後は通電しない
    TEST_ASSERT_FALSE(channel.outputOn(1000 + kTimeoutMs + 1));
}

// --------------------------------------------------------------------------
// §3.5 / §9.4 緊急停止
// --------------------------------------------------------------------------

// ラッチ中は新しい指令を受け付けない。受け付けると、PC が §5.1 の契約どおり
// 20Hz で再送している間ずっと目標が更新され続け、解除した瞬間にその状態で通電する。
static void test_rejects_command_while_latched() {
    SolenoidChannel channel = makeFedChannel(1000);
    const uint8_t stop[3] = {0x00, 0x00, 0x00};

    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(EStopAction::Stop),
                            static_cast<uint8_t>(channel.handleEStopFrame(stop, sizeof(stop))));

    TEST_ASSERT_FALSE(channel.setOn(true, 1000));
    TEST_ASSERT_FALSE(channel.outputOn(1000));
}

// **解除した瞬間に動き出さない**（仕様書 §3.5）。解除フレームは目標そのものを
// OFF へ落とすので、次の SET_TARGET が来るまで通電しない。
static void test_clear_does_not_re_energize() {
    SolenoidChannel channel = makeFedChannel(1000);
    TEST_ASSERT_TRUE(channel.setOn(true, 1000));

    const uint8_t stop[3] = {0x00, 0x00, 0x00};
    channel.handleEStopFrame(stop, sizeof(stop));

    const uint8_t clear[3] = {0x01, 0x5A, 0xA5};
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(EStopAction::Clear),
                            static_cast<uint8_t>(channel.handleEStopFrame(clear, sizeof(clear))));

    // 解除はされたが、目標は OFF に落ちている
    TEST_ASSERT_TRUE(channel.isOutputAllowed(1000));
    TEST_ASSERT_FALSE(channel.outputOn(1000));
}

// マジックバイトが揃わない解除要求ではラッチを維持する（仕様書 §3.5）。
// 1 バイトの値だけで安全装置が外れてはならない。
static void test_clear_requires_magic_bytes() {
    SolenoidChannel channel = makeFedChannel(1000);
    const uint8_t stop[3] = {0x00, 0x00, 0x00};
    channel.handleEStopFrame(stop, sizeof(stop));

    const uint8_t bogus[3] = {0x01, 0x00, 0x00};
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(EStopAction::None),
                            static_cast<uint8_t>(channel.handleEStopFrame(bogus, sizeof(bogus))));
    TEST_ASSERT_FALSE(channel.isOutputAllowed(1000));
}

// 緊急停止ラッチ中でもウォッチドッグは養う（仕様書 §6）。
// 養わないと、解除した直後に満了済みで一切動かない基板になる。
static void test_feed_works_while_latched() {
    SolenoidChannel channel = makeFedChannel(1000);
    const uint8_t stop[3] = {0x00, 0x00, 0x00};
    channel.handleEStopFrame(stop, sizeof(stop));

    channel.feed(1400);

    const uint8_t clear[3] = {0x01, 0x5A, 0xA5};
    channel.handleEStopFrame(clear, sizeof(clear));

    // 解除直後にウォッチドッグが満了していない
    TEST_ASSERT_TRUE(channel.isOutputAllowed(1400));
}

// CAN が上がらなかったときなど、PC から止められない状態で通電させないための停止。
static void test_stop_latches_and_de_energizes() {
    SolenoidChannel channel = makeFedChannel(1000);
    TEST_ASSERT_TRUE(channel.setOn(true, 1000));

    channel.stop();

    TEST_ASSERT_FALSE(channel.isOutputAllowed(1000));
    TEST_ASSERT_FALSE(channel.outputOn(1000));
}

// シリアルデバッグからその場で消磁する経路。ラッチはしないので、
// 次の SET_TARGET で通常どおり動く。
static void test_hold_de_energizes_without_latching() {
    SolenoidChannel channel = makeFedChannel(1000);
    TEST_ASSERT_TRUE(channel.setOn(true, 1000));

    channel.hold();
    TEST_ASSERT_FALSE(channel.outputOn(1000));
    TEST_ASSERT_TRUE(channel.isOutputAllowed(1000));

    TEST_ASSERT_TRUE(channel.setOn(true, 1000));
    TEST_ASSERT_TRUE(channel.outputOn(1000));
}

// --------------------------------------------------------------------------
// §3.2 状態フラグ
// --------------------------------------------------------------------------

// 緊急停止とウォッチドッグのビットは MotorSafety が持つものをそのまま中継する。
// **起動直後の未受信ではウォッチドッグのビットを立てない**（仕様書 §5.1 の表）。
// 立てると PC 側 check_safety_error() が最初の指令を送る前に動作確認を打ち切る。
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

// ウォッチドッグの無効化（config.h の WATCHDOG_ENABLED 0）でも、
// **§5.4 の「1 通も受け取るまで通電しない」ゲートは外れない。**
// まとめて 1 つの条件にすると、CAN 通信ゼロのまま電源投入と同時に弁が開きうる。
static void test_disabled_watchdog_still_requires_first_command() {
    SolenoidChannel channel(kTimeoutMs);
    channel.setWatchdogEnabled(false);

    TEST_ASSERT_FALSE(channel.isOutputAllowed(0));
    TEST_ASSERT_FALSE(channel.setOn(true, 0));

    channel.feed(1000);
    TEST_ASSERT_TRUE(channel.setOn(true, 1000));
    // 無効化してあるので満了しても通電し続ける
    TEST_ASSERT_TRUE(channel.outputOn(1000 + kTimeoutMs * 10));
}

int main() {
    UNITY_BEGIN();
    RUN_TEST(test_solenoid_device_id_is_a_fixed_bit_split);
    RUN_TEST(test_broadcast_slot_falls_back_to_unconfigured);
    RUN_TEST(test_device_ids_never_collide_across_three_boards);
    RUN_TEST(test_decode_set_target_accepts_on_off);
    RUN_TEST(test_on_off_target_is_zero_or_not_zero);
    RUN_TEST(test_decode_set_target_rejects_reserved_control_type);
    RUN_TEST(test_encode_info_reports_solenoid_board);
    RUN_TEST(test_solenoid_channel_starts_de_energized);
    RUN_TEST(test_solenoid_channel_energizes_after_first_command);
    RUN_TEST(test_output_stops_on_watchdog_and_recovers);
    RUN_TEST(test_output_gate_overrides_stale_target);
    RUN_TEST(test_rejects_command_while_latched);
    RUN_TEST(test_clear_does_not_re_energize);
    RUN_TEST(test_clear_requires_magic_bytes);
    RUN_TEST(test_feed_works_while_latched);
    RUN_TEST(test_stop_latches_and_de_energizes);
    RUN_TEST(test_hold_de_energizes_without_latching);
    RUN_TEST(test_status_flags_follow_safety);
    RUN_TEST(test_disabled_watchdog_still_requires_first_command);
    return UNITY_END();
}
