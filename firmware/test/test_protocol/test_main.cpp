// MotorCan（プロトコル層 + 安全機構）の native ユニットテスト。
// 実機を用意せずにプロトコルの取り違えを検出するのが目的なので、
// ここで検証するのはすべて docs/motor_driver_can_protocol.md に明記された挙動に限る。

#include <unity.h>

#include <math.h>
#include <string.h>

#include "DcChannel.h"
#include "MotorCanProtocol.h"
#include "MotorSafety.h"

using namespace motorcan;


void setUp() {}
void tearDown() {}

// --------------------------------------------------------------------------
// §2 CAN ID レイアウト
// --------------------------------------------------------------------------

static void test_build_can_id() {
    TEST_ASSERT_EQUAL_UINT16(0x002, buildCanId(CommandType::EStop, 0x02));
    TEST_ASSERT_EQUAL_UINT16(0x102, buildCanId(CommandType::SetTarget, 0x02));
    TEST_ASSERT_EQUAL_UINT16(0x202, buildCanId(CommandType::SetParam, 0x02));
    TEST_ASSERT_EQUAL_UINT16(0x302, buildCanId(CommandType::Feedback, 0x02));
    TEST_ASSERT_EQUAL_UINT16(0x402, buildCanId(CommandType::Info, 0x02));
    TEST_ASSERT_EQUAL_UINT16(0x0FF, buildCanId(CommandType::EStop, kDeviceIdBroadcast));
}

// **CAN の調停は ID が小さいほど優先。止めるフレームが目標値やフィードバックに
// 追い越されてはならない。** かつては E_STOP が 0b111 で、ブロードキャスト停止の
// 0x7FF は Standard ID 全 2048 個のうち最も優先度が低かった。
static void test_e_stop_outranks_every_other_frame() {
    const uint8_t dev = 0x7F;  // 同じデバイスで比べる
    const uint16_t estop = buildCanId(CommandType::EStop, dev);
    TEST_ASSERT_TRUE(estop < buildCanId(CommandType::SetTarget, dev));
    TEST_ASSERT_TRUE(estop < buildCanId(CommandType::SetParam, dev));
    TEST_ASSERT_TRUE(estop < buildCanId(CommandType::Feedback, dev));
    TEST_ASSERT_TRUE(estop < buildCanId(CommandType::Info, dev));

    // ブロードキャスト停止も、他のどのフレームより先に通ること
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

// 予約値 0b101 / 0b110 / 0b111 を「有効なコマンド」として扱うと、
// PC 側 parse_can_id が例外を投げて受信ループごと落ちる（仕様書 §2.1）。
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

// --------------------------------------------------------------------------
// §4 固定小数点
// --------------------------------------------------------------------------

// CAN 上を流れるのは int16 だけで、float は 1 バイトも流れない（仕様書 §4）。
// **NaN の防御はプロトコル全体で toRaw の 1 箇所だけ**になったので、
// ここが素通しになると NaN が内部へ入る経路が復活する。
static void test_to_raw_saturates_nan_and_out_of_range() {
    TEST_ASSERT_EQUAL_INT16(0, toRaw(NAN, kAngleScale));
    TEST_ASSERT_EQUAL_INT16(32767, toRaw(1e9f, kAngleScale));
    TEST_ASSERT_EQUAL_INT16(-32768, toRaw(-1e9f, kAngleScale));
}

static void test_fixed_point_roundtrip_keeps_the_unit() {
    // 0.1deg 単位。90.0deg → 900
    TEST_ASSERT_EQUAL_INT16(900, toRaw(90.0f, kAngleScale));
    TEST_ASSERT_EQUAL_FLOAT(90.0f, fromRaw(900, kAngleScale));

    // duty は 1/10000 単位。0.3 → 3000
    TEST_ASSERT_EQUAL_INT16(3000, toRaw(0.3f, kDutyScale));
    TEST_ASSERT_EQUAL_FLOAT(0.3f, fromRaw(3000, kDutyScale));
    TEST_ASSERT_EQUAL_INT16(-10000, toRaw(-1.0f, kDutyScale));

    // 四捨五入すること。切り捨てると 0.1deg 刻みの指令が 1 つ下へずれ続ける
    TEST_ASSERT_EQUAL_INT16(56, toRaw(5.55f, kAngleScale));
    TEST_ASSERT_EQUAL_INT16(-56, toRaw(-5.55f, kAngleScale));
}


// --------------------------------------------------------------------------
// §3.1 SET_TARGET
// --------------------------------------------------------------------------

static void test_decode_set_target() {
    // Byte0=制御タイプ / Byte1-2=目標値(int16)。途中に予約バイトを挟まない
    const uint8_t data[3] = {static_cast<uint8_t>(ControlType::Duty), 0xB8, 0x0B};  // 3000
    const SetTargetCommand cmd = decodeSetTarget(data, 3);
    TEST_ASSERT_TRUE(cmd.valid);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(ControlType::Duty),
                            static_cast<uint8_t>(cmd.type));
    TEST_ASSERT_EQUAL_INT16(3000, cmd.raw);
    TEST_ASSERT_EQUAL_FLOAT(0.3f, fromRaw(cmd.raw, kDutyScale));
}

// 負の目標値がそのまま符号付きで届くこと。符号を落とすと duty が逆転する
static void test_decode_set_target_keeps_sign() {
    const uint8_t data[3] = {static_cast<uint8_t>(ControlType::Duty), 0x48, 0xF4};  // -3000
    const SetTargetCommand cmd = decodeSetTarget(data, 3);
    TEST_ASSERT_TRUE(cmd.valid);
    TEST_ASSERT_EQUAL_INT16(-3000, cmd.raw);
}

static void test_decode_set_target_rejects_unknown_type() {
    uint8_t data[3] = {0};
    data[0] = 4;  // position/velocity/duty/on_off 以外（仕様書 §4 の表に無い値）
    TEST_ASSERT_FALSE(decodeSetTarget(data, 3).valid);
    data[0] = 0xFF;
    TEST_ASSERT_FALSE(decodeSetTarget(data, 3).valid);
}

// 制御タイプ 3 は電磁弁用の on_off（仕様書 §9.2）。復号層が知らないと SET_TARGET が
// 丸ごと捨てられ、PC からは「指令しても反応しない基板」にしか見えない。
// **受理するのは復号層まで**で、on_off を実際に駆動へ通すかは各基板の main / app が決める
// （DC 基板とサーボ基板は黙って捨てる。仕様書 §3.1）。
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


// --------------------------------------------------------------------------
// §3.4 SET_PARAM
// --------------------------------------------------------------------------

static void test_decode_set_param() {
    const uint8_t data[3] = {static_cast<uint8_t>(ParamId::MaxDuty), 0x88, 0x13};  // 5000
    const SetParamCommand cmd = decodeSetParam(data, 3);
    TEST_ASSERT_TRUE(cmd.valid);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(ParamId::MaxDuty),
                            static_cast<uint8_t>(cmd.id));
    TEST_ASSERT_EQUAL_FLOAT(0.5f, fromRaw(cmd.raw, kDutyScale));
}

// パラメータ ID は穴を空けずに詰めてある（仕様書 §3.4）。
// 途中に「予約」を挟むと、対応表を読むたびに使われていない ID を数えることになる。
static void test_param_ids_are_packed() {
    TEST_ASSERT_EQUAL_UINT8(0x00, static_cast<uint8_t>(ParamId::MaxDuty));
    TEST_ASSERT_EQUAL_UINT8(0x01, static_cast<uint8_t>(ParamId::CommandTimeoutMs));
    TEST_ASSERT_EQUAL_UINT8(0x02, static_cast<uint8_t>(ParamId::FeedbackIntervalMs));
    TEST_ASSERT_EQUAL_UINT8(0x03, static_cast<uint8_t>(ParamId::ReachedTolerance));
    TEST_ASSERT_EQUAL_UINT8(0x04, static_cast<uint8_t>(ParamId::SlewRate));
    TEST_ASSERT_EQUAL_UINT8(0x05, static_cast<uint8_t>(ParamId::AngleMin));
    TEST_ASSERT_EQUAL_UINT8(0x06, static_cast<uint8_t>(ParamId::AngleMax));
    // 末尾の次は未知として弾かれること
    uint8_t data[3] = {0x07, 0, 0};
    TEST_ASSERT_FALSE(decodeSetParam(data, 3).valid);
}

// 未知のパラメータ ID は無視する（新ファームと旧基板の混在で止まらないため。仕様書 §3.4）
static void test_decode_set_param_unknown_id_is_ignored() {
    uint8_t data[3] = {0x42, 0, 0};
    TEST_ASSERT_FALSE(decodeSetParam(data, 3).valid);
}


// --------------------------------------------------------------------------
// §3.5 E_STOP
// --------------------------------------------------------------------------

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

// マジックバイトが 1 つでも違えば解除してはならない（安全装置が 1 バイトで開かないように）
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

// --------------------------------------------------------------------------
// §3.2 FEEDBACK
// --------------------------------------------------------------------------

// **全基板が必ず持つ状態フラグを先頭に置く。** 逆順（フラグを末尾）にすると、
// 位置を持たない基板も 8 バイト送ることになる。
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

// 仕様書 §3.6: 焼き忘れた基板をセッティングタイムに見つけるための自己申告。
static void test_encode_info() {
    uint8_t out[8];
    memset(out, 0xFF, sizeof(out));
    const uint8_t len = encodeInfo(out, 7, BoardKind::Servo, SlotKind::Sensor);
    TEST_ASSERT_EQUAL_UINT8(3, len);
    TEST_ASSERT_EQUAL_UINT8(7, out[0]);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(BoardKind::Servo), out[1]);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlotKind::Sensor), out[2]);
    // 仕様書 §3.4: 角度を持たないスロットに可動レンジを運ばせない。書き込むと
    // PC 側には「レンジ 0deg」という測ったように見える値が届く
    TEST_ASSERT_EQUAL_UINT8(0xFF, out[3]);
    TEST_ASSERT_EQUAL_UINT8(0xFF, out[4]);
}

// 仕様書 §3.4 / §7.7: サーボスロットだけが可動レンジを足す（DLC=5）。
// **この 2 バイトだけが、180 度サーボと 270 度サーボの取り違えを CAN 越しに
// 見える形にしている。** ファームと実物が食い違っても、FEEDBACK が返すのは
// クランプ後の指令角なので PC には正常に動いたようにしか見えない。
static void test_encode_info_with_servo_range() {
    uint8_t out[8];
    memset(out, 0xFF, sizeof(out));
    const uint8_t len = encodeInfo(out, 2, BoardKind::Servo, SlotKind::Actuator, 270.0f);
    TEST_ASSERT_EQUAL_UINT8(5, len);
    TEST_ASSERT_EQUAL_UINT8(2, out[0]);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(BoardKind::Servo), out[1]);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(SlotKind::Actuator), out[2]);
    // 0.1deg 単位（仕様書 §4）。float は 1 バイトも流れない
    TEST_ASSERT_EQUAL_INT16(2700, static_cast<int16_t>(out[3] | (out[4] << 8)));

    // 180 度品では別の値になる。ここが同じ値になる実装だと照合が素通りする
    encodeInfo(out, 2, BoardKind::Servo, SlotKind::Actuator, 180.0f);
    TEST_ASSERT_EQUAL_INT16(1800, static_cast<int16_t>(out[3] | (out[4] << 8)));
}

// int16 をそのままキャストすると +4000deg が負値に化け、PC 側が逆方向へ位置制御しかねない。
// 折り返しではなく飽和させる（仕様書 §3.2 の ±3276.7deg）。
static void test_encode_feedback_saturates_position() {
    uint8_t out[8];

    encodeFeedback(out, 0, 40000 /* +4000.0deg */);
    TEST_ASSERT_EQUAL_INT16(32767, static_cast<int16_t>(out[1] | (out[2] << 8)));

    encodeFeedback(out, 0, -40000 /* -4000.0deg */);
    TEST_ASSERT_EQUAL_INT16(-32768, static_cast<int16_t>(out[1] | (out[2] << 8)));
}


// --------------------------------------------------------------------------
// §5.3 duty クランプ
// --------------------------------------------------------------------------

static void test_clamp_duty() {
    TEST_ASSERT_EQUAL_FLOAT(0.30f, clampDuty(1.0f, 0.30f));
    TEST_ASSERT_EQUAL_FLOAT(-0.30f, clampDuty(-1.0f, 0.30f));
    TEST_ASSERT_EQUAL_FLOAT(0.20f, clampDuty(0.20f, 0.30f));
    TEST_ASSERT_EQUAL_FLOAT(-0.20f, clampDuty(-0.20f, 0.30f));
    TEST_ASSERT_EQUAL_FLOAT(0.0f, clampDuty(0.0f, 0.30f));
    // max_duty 自体が範囲外でも 0.0–1.0 に丸める
    TEST_ASSERT_EQUAL_FLOAT(1.0f, clampDuty(5.0f, 3.0f));
    TEST_ASSERT_EQUAL_FLOAT(0.0f, clampDuty(0.5f, -1.0f));
}


// --------------------------------------------------------------------------
// §5.1 / §5.2 MotorSafety
// --------------------------------------------------------------------------

static void test_watchdog_expires_and_recovers() {
    MotorSafety safety(500);
    safety.feed(1000);
    TEST_ASSERT_FALSE(safety.isExpired(1000));
    TEST_ASSERT_FALSE(safety.isExpired(1499));
    TEST_ASSERT_TRUE(safety.isExpired(1500));
    TEST_ASSERT_TRUE(safety.isExpired(9999));

    // 新しい SET_TARGET で復帰する（ラッチしない。仕様書 §5.1）
    safety.feed(10000);
    TEST_ASSERT_FALSE(safety.isExpired(10000));
}

// 給餌前は「コマンドを 1 通も受けていない」状態なので、出力停止側に倒す
static void test_watchdog_expired_before_first_feed() {
    MotorSafety safety(500);
    TEST_ASSERT_TRUE(safety.isExpired(0));
}

static void test_watchdog_handles_millis_wraparound() {
    // millis() は約 49.7 日で 0 に戻る。符号なし減算で比較しないと
    // 折り返した瞬間に永久満了して現場で原因不明の停止になる。
    MotorSafety safety(500);
    safety.feed(0xFFFFFF00u);
    TEST_ASSERT_FALSE(safety.isExpired(0x00000050u));  // 経過 0x150 = 336ms
    TEST_ASSERT_TRUE(safety.isExpired(0x000000FFu));   // 経過 0x1FF = 511ms
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
    TEST_ASSERT_FALSE(safety.isLatched());  // 起動時は解除済み（仕様書 §5.4）
    safety.stop();
    TEST_ASSERT_TRUE(safety.isLatched());
    TEST_ASSERT_TRUE(safety.tryClear());
    TEST_ASSERT_FALSE(safety.isLatched());
}

// ラッチ中でも SET_TARGET でウォッチドッグを養う。
// これをしないと解除した瞬間にウォッチドッグが満了していて動かない（仕様書 §6）。
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
    safety.tryClear();
    TEST_ASSERT_TRUE(safety.isOutputAllowed(100));

    TEST_ASSERT_FALSE(safety.isOutputAllowed(600));  // ウォッチドッグ満了
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

// 起動直後は出力を止めるが、FEEDBACK bit4（ウォッチドッグ作動中）は立てない。
// bit4 は「CAN 通信が途絶した」ことの報告であり、指令をまだ 1 通も送っていない
// 状態はそれに当たらない。立ててしまうと PC 側 check_safety_error() が
// セッティングタイムの動作確認を指令送信前に FAILED で打ち切り、
// 健全な基板に対して配線を疑わせる誤誘導になる。
static void test_status_flags_omit_watchdog_before_first_feed() {
    MotorSafety safety(500);

    // 出力禁止側の判定は従来どおり満了扱いのまま（仕様書 §5.4）
    TEST_ASSERT_TRUE(safety.isExpired(0));
    TEST_ASSERT_FALSE(safety.isOutputAllowed(0));

    // ウォッチドッグは立てないが、「起動後まだ指令を受けていない」は立てる。
    // これが無いと基板の再起動が PC から見えない（仕様書 §3.2）
    TEST_ASSERT_EQUAL_UINT8(0, safety.statusFlags(0) & status_flag::kWatchdog);
    TEST_ASSERT_EQUAL_UINT8(status_flag::kNeverCommanded, safety.statusFlags(100000));

    // 起動直後でも緊急停止ラッチはそのまま報告する
    safety.stop();
    TEST_ASSERT_EQUAL_UINT8(status_flag::kEStop | status_flag::kNeverCommanded,
                            safety.statusFlags(100000));
}

// 一度でも指令を受けた後の満了は本物の通信途絶なので bit4 を立てる
static void test_status_flags_report_watchdog_after_first_feed() {
    MotorSafety safety(500);
    safety.feed(1000);
    TEST_ASSERT_EQUAL_UINT8(0, safety.statusFlags(1499));
    TEST_ASSERT_EQUAL_UINT8(status_flag::kWatchdog, safety.statusFlags(1500));

    // 通信が復旧したら下りる（ラッチしない。仕様書 §5.1）
    safety.feed(2000);
    TEST_ASSERT_EQUAL_UINT8(0, safety.statusFlags(2100));
}

// 「起動直後で未受信」と「受信後に途絶」を呼び出し側が区別できること。
// サーボ側 main.cpp は bit3/bit4 を手書きで組み立てているため、
// 同じ判定を共有できないと基板ごとに挙動がずれる。
static void test_command_lost_separates_startup_from_dropout() {
    MotorSafety safety(500);
    // 起動直後は出力を許可しないが、途絶したわけではないので報告もしない
    TEST_ASSERT_FALSE(safety.isOutputAllowed(100000));
    TEST_ASSERT_FALSE(safety.isCommandLost(100000));

    safety.feed(1000);
    TEST_ASSERT_FALSE(safety.isCommandLost(1400));
    TEST_ASSERT_TRUE(safety.isCommandLost(1500));
}

// --------------------------------------------------------------------------
// §5.1 ウォッチドッグの有効/無効
// --------------------------------------------------------------------------

// 試合では必ず有効。config.h の WATCHDOG_ENABLED を写し忘れた基板が
// 「気付かないうちに無効」になっていないよう、既定は有効側に倒す。
// フラグを直接覗かず振る舞いで見るのは、写し忘れが効くのは出力の可否だけだから。
static void test_watchdog_is_enabled_by_default() {
    MotorSafety safety(500);
    safety.feed(0);
    TEST_ASSERT_TRUE(safety.isOutputAllowed(499));
    TEST_ASSERT_FALSE(safety.isOutputAllowed(500));
}

// 無効化した基板は途絶しても駆動を続け、bit4 も報告しない（仕様書 §5.1 / §8）。
// 以前は両 main.cpp が #if で同じ分岐を持っており、servo にだけ実装されて
// dc_motor では「設定しても効かないフラグ」になっていた。判定は MotorSafety に 1 つだけ置く。
static void test_disabled_watchdog_allows_output_and_hides_bit4() {
    MotorSafety safety(500);
    safety.setWatchdogEnabled(false);

    safety.feed(1000);
    TEST_ASSERT_TRUE(safety.isOutputAllowed(9999));
    TEST_ASSERT_EQUAL_UINT8(0, safety.statusFlags(9999));

    // 生の満了判定そのものは無効化の影響を受けない（報告と駆動可否だけが変わる）
    TEST_ASSERT_TRUE(safety.isExpired(9999));
    TEST_ASSERT_TRUE(safety.isCommandLost(9999));
}

// 無効化して外れるのは「途絶したら止める」ことだけで、仕様書 §5.4 の
// 「SET_TARGET を 1 通も受け取るまで出力を許可しない」ゲートは外れない。
// 外れると setup() が CAN 通信ゼロのままゲートドライバを開く基板になる
// （ウォッチドッグを実行時フラグにした時点で DC 基板でも到達可能になった経路）。
static void test_disabled_watchdog_still_requires_first_command() {
    MotorSafety safety(500);
    safety.setWatchdogEnabled(false);

    TEST_ASSERT_FALSE(safety.isOutputAllowed(0));
    TEST_ASSERT_FALSE(safety.isOutputAllowed(100000));

    // ベンチ確認（手打ちの cansend）の逃げ道は残す。最初の 1 通で開き、
    // 以後は途絶しても閉じない。
    safety.feed(1000);
    TEST_ASSERT_TRUE(safety.isOutputAllowed(1000));
    TEST_ASSERT_TRUE(safety.isOutputAllowed(999999));
}

// 無効化は「最後の砦を 1 枚外す」だけであって、緊急停止まで無効にしてはならない。
static void test_disabled_watchdog_still_honors_e_stop_latch() {
    MotorSafety safety(500);
    safety.setWatchdogEnabled(false);
    safety.feed(0);

    safety.stop();
    TEST_ASSERT_FALSE(safety.isOutputAllowed(100));
    TEST_ASSERT_EQUAL_UINT8(status_flag::kEStop, safety.statusFlags(100));

    safety.tryClear();
    TEST_ASSERT_TRUE(safety.isOutputAllowed(100));
}

// 有効へ戻したら即座に満了判定が効く（ベンチ確認から試合構成へ戻す経路）。
static void test_watchdog_can_be_re_enabled() {
    MotorSafety safety(500);
    safety.setWatchdogEnabled(false);
    safety.feed(0);
    TEST_ASSERT_TRUE(safety.isOutputAllowed(600));

    safety.setWatchdogEnabled(true);
    TEST_ASSERT_FALSE(safety.isOutputAllowed(600));
    TEST_ASSERT_EQUAL_UINT8(status_flag::kWatchdog, safety.statusFlags(600));
}

// --------------------------------------------------------------------------
// §3.4 パラメータ既定値
// --------------------------------------------------------------------------

// PC 側の再送周期（command_timeout_ms の数分の 1）と STALE 判定は、この 2 つの値が
// 仕様書どおりであることを前提にしている。基板ごとの config.h に書くと片方だけが
// 古くなるので、ここが単一定義を持つ。
static void test_protocol_defaults_match_spec() {
    TEST_ASSERT_EQUAL_UINT32(500, kDefaultCommandTimeoutMs);
    TEST_ASSERT_EQUAL_UINT32(10, kDefaultFeedbackIntervalMs);
}

// --------------------------------------------------------------------------
// §3.4 / §5.1 タイミングパラメータの受け付け範囲
// --------------------------------------------------------------------------

// command_timeout_ms（0x04）に上限が無いと、1 フレームでウォッチドッグを実質無効に
// できる。仕様書 §5.1 が「このフラグの ID は無く CAN からは変更できない」と書いて
// 最後の砦を守っているのに、猶予そのものを 49.7 日へ伸ばせば同じ結果になる。
static void test_command_timeout_param_has_upper_bound() {
    TEST_ASSERT_EQUAL_UINT16(kMaxCommandTimeoutMs, clampCommandTimeoutMs(32767));
    TEST_ASSERT_EQUAL_UINT16(kMaxCommandTimeoutMs, clampCommandTimeoutMs(3000));
}

// 負値・0 は「起動直後から永久に出力禁止」に倒れる。止まる方向でも無言で壊れるので弾く。
// 下限は PC 側の再送周期（既定 500ms に対して 50ms）で、それより短い猶予は
// 契約どおり再送している健全な機体を止めてしまう。
static void test_command_timeout_param_has_lower_bound() {
    TEST_ASSERT_EQUAL_UINT16(kMinCommandTimeoutMs, clampCommandTimeoutMs(-1));
    TEST_ASSERT_EQUAL_UINT16(kMinCommandTimeoutMs, clampCommandTimeoutMs(0));
    TEST_ASSERT_EQUAL_UINT16(kMinCommandTimeoutMs, clampCommandTimeoutMs(10));
}

static void test_command_timeout_param_keeps_values_in_range() {
    TEST_ASSERT_EQUAL_UINT16(250, clampCommandTimeoutMs(250));
    TEST_ASSERT_EQUAL_UINT16(kDefaultCommandTimeoutMs, clampCommandTimeoutMs(500));
}

// feedback_interval_ms。0 はバスを埋め、極端に大きい値は PC からは
// 「基板が死んだ」ようにしか見えない。
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

// duty 0 を「負でない」ではなく「負」と扱うと、停止指令のたびに方向ピンが
// 反転する。停止は毎ループ流れるので、機構に絶えず衝撃が入ることになる。
static void test_split_duty_zero_does_not_flip_direction() {
    const DutyOutput stopped = splitDuty(0.0f, 1.0f);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, stopped.magnitude);
    TEST_ASSERT_FALSE(stopped.reverse);
}

// 仕様書 §5.3 の上限は分解の前に掛かる。掛け忘れると max_duty を越えた PWM が出る。
static void test_split_duty_applies_max_duty() {
    const DutyOutput clamped = splitDuty(-1.0f, 0.3f);
    TEST_ASSERT_EQUAL_FLOAT(0.3f, clamped.magnitude);
    TEST_ASSERT_TRUE(clamped.reverse);

}

// --------------------------------------------------------------------------
// §5.2 物理緊急停止入力（DC 基板の REF）
// --------------------------------------------------------------------------

static void test_physical_stop_latches() {
    MotorSafety safety(500);
    safety.feed(0);
    TEST_ASSERT_TRUE(safety.isOutputAllowed(0));

    safety.applyPhysicalStop(true);
    TEST_ASSERT_TRUE(safety.isLatched());
    TEST_ASSERT_FALSE(safety.isOutputAllowed(0));
}

// レベル追従にすると、PC が §5.1 の契約どおり再送し続けている以上、
// スイッチを離した瞬間に機体が動き出す。解除は操縦者の明示操作だけに限る。
static void test_physical_stop_does_not_auto_release() {
    MotorSafety safety(500);
    safety.feed(0);
    safety.applyPhysicalStop(true);

    safety.applyPhysicalStop(false);
    TEST_ASSERT_TRUE(safety.isLatched());
    TEST_ASSERT_FALSE(safety.isOutputAllowed(0));
}

// 押している間に解除フレームが届いても、次のループでここが再ラッチする。
// 「押している間は絶対に動かない」が呼び出し順序に依らず成立すること。
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

// 離したあとに解除フレームが来て初めて動けるようになる（＝復帰経路がある）。
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

// --------------------------------------------------------------------------
// §5.4 / §3.5 DcChannel（安全機構 + duty 目標の結線）
// --------------------------------------------------------------------------

// 仕様書 §5.4: 電源投入直後は目標 0・出力停止。SET_TARGET を 1 通も受けていない
// 間は駆動しない。
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

// ラッチ中の再送を受け付けると、解除した瞬間にその duty で回り出す。
// 入口で捨てることで、ラッチ中の指令が解除後に生き残る経路を無くす。
static void test_dc_channel_rejects_duty_while_latched() {
    DcChannel ch(500);
    ch.feed(0);
    ch.setDuty(0.4f, 0);
    ch.stop();

    TEST_ASSERT_FALSE(ch.setDuty(0.9f, 10));
    TEST_ASSERT_EQUAL_FLOAT(0.0f, ch.outputDuty(10));

    uint8_t clear[8] = {0x01, 0x5A, 0xA5, 0, 0, 0, 0, 0};
    ch.handleEStopFrame(clear, 8);
    // 仕様書 §3.5: 解除直後は目標 0 から始まる
    TEST_ASSERT_EQUAL_FLOAT(0.0f, ch.outputDuty(10));
}

// ウォッチドッグ満了は「フレームを伴わない出力禁止」なので、出力側で 0 に
// 落ちなければ止まらない。復帰は次の feed だけで足りること（ラッチしない）。
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

// REF を押している間は、PC が再送を続けても駆動しない。
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


// --------------------------------------------------------------------------
// §3.2 状態フラグのビット割り当て
// --------------------------------------------------------------------------

// センサ入力は予約ビットの bit6 に載せる。フレーム長も他のビットの位置も
// 変えないので、対応していない PC 側・基板が混在しても壊れない。
// 既存のビットと重なると、センサの ON がそのまま緊急停止やウォッチドッグの
// 報告として読まれる（＝押していないのに機体が止まる／止まったのに気付けない）。
static void test_status_flag_bits_do_not_overlap() {
    const uint8_t all[] = {status_flag::kReached, status_flag::kEStop, status_flag::kWatchdog,
                           status_flag::kDeviceIdUnconfigured, status_flag::kSensor};
    uint8_t seen = 0;
    for (uint8_t bit : all) {
        TEST_ASSERT_NOT_EQUAL_UINT8(0, bit);
        TEST_ASSERT_EQUAL_UINT8(0, seen & bit);
        seen = static_cast<uint8_t>(seen | bit);
    }
    // **頭から詰まっていること。** 途中に空きがあると、項目が増えたときに
    // 「空いているビットがあるのに末尾へ足す」ことになり、対応表が読みにくくなる
    TEST_ASSERT_EQUAL_UINT8(0x1F, seen);
}

// センサは自分のデバイス ID で FEEDBACK を送るので、1 枚に何個載っていてもビットは
// 1 つで足りる。位置・速度・電流・温度は持たないので 0 のまま。
static void test_sensor_flag_rides_in_its_own_feedback() {
    uint8_t out[8];
    const uint8_t len = encodeFeedback(out, status_flag::kSensor);
    TEST_ASSERT_EQUAL_UINT8(1, len);
    TEST_ASSERT_EQUAL_UINT8(status_flag::kSensor, out[0]);
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
    RUN_TEST(test_disabled_watchdog_allows_output_and_hides_bit4);
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
    RUN_TEST(test_dc_channel_physical_stop_blocks_until_cleared);
    return UNITY_END();
}
