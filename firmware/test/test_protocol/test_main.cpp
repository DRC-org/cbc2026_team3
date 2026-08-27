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

// PC が送ってくる SET_TARGET / SET_PARAM のバイト列をテストから組み立てるヘルパ。
// **本番のファームは float を送らない**（FEEDBACK は int16 だけ）ので、
// 書く側は MotorCan には置かずここに持つ。
static void packFloatLe(uint8_t *dst, float value) {
    uint32_t bits = 0;
    memcpy(&bits, &value, sizeof(bits));
    dst[0] = static_cast<uint8_t>(bits & 0xFF);
    dst[1] = static_cast<uint8_t>((bits >> 8) & 0xFF);
    dst[2] = static_cast<uint8_t>((bits >> 16) & 0xFF);
    dst[3] = static_cast<uint8_t>((bits >> 24) & 0xFF);
}

void setUp() {}
void tearDown() {}

// --------------------------------------------------------------------------
// §2 CAN ID レイアウト
// --------------------------------------------------------------------------

static void test_build_can_id() {
    TEST_ASSERT_EQUAL_UINT16(0x002, buildCanId(CommandType::SetTarget, 0x02));
    TEST_ASSERT_EQUAL_UINT16(0x102, buildCanId(CommandType::Feedback, 0x02));
    TEST_ASSERT_EQUAL_UINT16(0x202, buildCanId(CommandType::SetMode, 0x02));
    TEST_ASSERT_EQUAL_UINT16(0x302, buildCanId(CommandType::SetParam, 0x02));
    TEST_ASSERT_EQUAL_UINT16(0x702, buildCanId(CommandType::EStop, 0x02));
    TEST_ASSERT_EQUAL_UINT16(0x7FF, buildCanId(CommandType::EStop, kDeviceIdBroadcast));
}

static void test_parse_can_id_roundtrip() {
    const CommandType kinds[] = {CommandType::SetTarget, CommandType::Feedback,
                                 CommandType::SetMode, CommandType::SetParam,
                                 CommandType::EStop};
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

// 予約値 0b100 / 0b101 / 0b110 を「有効なコマンド」として扱うと、
// PC 側 parse_can_id が例外を投げて受信ループごと落ちる（仕様書 §2.1）。
static void test_parse_can_id_reserved_is_invalid() {
    for (uint16_t cmd = 4; cmd <= 6; ++cmd) {
        const CanIdInfo info = parseCanId(static_cast<uint16_t>((cmd << 8) | 0x02));
        TEST_ASSERT_FALSE(info.valid);
    }
}

static void test_parse_can_id_rejects_out_of_range() {
    TEST_ASSERT_FALSE(parseCanId(0x800).valid);
    TEST_ASSERT_FALSE(parseCanId(0xFFFF).valid);
}

// --------------------------------------------------------------------------
// §3 float32 リトルエンディアン
// --------------------------------------------------------------------------

// PC（Python の struct）が送ってくるバイト列を、こちらが同じ値として読めること。
// **自前の pack で書いて unpack で読み直す往復にしてはならない** —— 両方が同じように
// 壊れていても通ってしまう。契約はバイト列そのものなので、リテラルで固定する。
static void test_unpack_float_from_known_bytes() {
    // 0.3f = 0x3E99999A（IEEE754）。LE なので下位バイトから並ぶ
    const uint8_t p03[4] = {0x9A, 0x99, 0x99, 0x3E};
    TEST_ASSERT_EQUAL_FLOAT(0.3f, unpackFloatLe(p03));

    const uint8_t zero[4] = {0x00, 0x00, 0x00, 0x00};
    TEST_ASSERT_EQUAL_FLOAT(0.0f, unpackFloatLe(zero));

    // -1.0f = 0xBF800000
    const uint8_t minus_one[4] = {0x00, 0x00, 0x80, 0xBF};
    TEST_ASSERT_EQUAL_FLOAT(-1.0f, unpackFloatLe(minus_one));

    // 90.0f = 0x42B40000（サーボの既定スルーレート）
    const uint8_t ninety[4] = {0x00, 0x00, 0xB4, 0x42};
    TEST_ASSERT_EQUAL_FLOAT(90.0f, unpackFloatLe(ninety));

    // 3276.7f = 0x454CCB33（位置の飽和境界）
    const uint8_t saturation[4] = {0x33, 0xCB, 0x4C, 0x45};
    TEST_ASSERT_EQUAL_FLOAT(3276.7f, unpackFloatLe(saturation));
}

// --------------------------------------------------------------------------
// §3.1 SET_TARGET
// --------------------------------------------------------------------------

static void test_decode_set_target() {
    uint8_t data[8] = {0};
    data[0] = static_cast<uint8_t>(ControlType::Duty);
    packFloatLe(&data[2], 0.3f);
    const SetTargetCommand cmd = decodeSetTarget(data, 8);
    TEST_ASSERT_TRUE(cmd.valid);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(ControlType::Duty),
                            static_cast<uint8_t>(cmd.type));
    TEST_ASSERT_EQUAL_FLOAT(0.3f, cmd.value);
}

static void test_decode_set_target_rejects_unknown_type() {
    uint8_t data[8] = {0};
    data[0] = 3;  // position/velocity/duty 以外
    TEST_ASSERT_FALSE(decodeSetTarget(data, 8).valid);
}

static void test_decode_set_target_rejects_short_frame() {
    uint8_t data[8] = {0};
    TEST_ASSERT_FALSE(decodeSetTarget(data, 5).valid);
}

// 化けた float32 の NaN をそのまま目標値にすると、DC 側は PID の積分項が NaN に
// 汚染されて以後**正常な目標に対しても**出力が NaN のままになる（clampDuty が 0 に
// 落とすので「無言で死んだモータ」になり、診断ビットも到達フラグも立たない）。
// サーボ側 ServoMotion::setTarget が NaN を捨てているのと同じ判断を復号層に置く。
static void test_decode_set_target_rejects_nan() {
    uint8_t data[8] = {0};
    data[0] = static_cast<uint8_t>(ControlType::Position);
    packFloatLe(&data[2], NAN);
    TEST_ASSERT_FALSE(decodeSetTarget(data, 8).valid);
}

// --------------------------------------------------------------------------
// §3.4 SET_PARAM
// --------------------------------------------------------------------------

static void test_decode_set_param() {
    uint8_t data[8] = {0};
    data[0] = static_cast<uint8_t>(ParamId::MaxDuty);
    packFloatLe(&data[2], 0.5f);
    const SetParamCommand cmd = decodeSetParam(data, 8);
    TEST_ASSERT_TRUE(cmd.valid);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(ParamId::MaxDuty),
                            static_cast<uint8_t>(cmd.id));
    TEST_ASSERT_EQUAL_FLOAT(0.5f, cmd.value);
}

// 未知のパラメータ ID は無視する（新ファームと旧基板の混在で止まらないため。仕様書 §3.4）
static void test_decode_set_param_unknown_id_is_ignored() {
    uint8_t data[8] = {0};
    data[0] = 0x42;
    packFloatLe(&data[2], 1.0f);
    TEST_ASSERT_FALSE(decodeSetParam(data, 8).valid);
}

// NaN のゲインを受け付けると PID の出力が永久に NaN になる。SET_TARGET と同じく捨てる。
static void test_decode_set_param_rejects_nan() {
    uint8_t data[8] = {0};
    data[0] = static_cast<uint8_t>(ParamId::Ki);
    packFloatLe(&data[2], NAN);
    TEST_ASSERT_FALSE(decodeSetParam(data, 8).valid);
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

static void test_encode_feedback_layout() {
    uint8_t out[8];
    encodeFeedback(out, 900 /* 90.0deg */, -1500, 2500, 0,
                   status_flag::kReached | status_flag::kEStop);
    TEST_ASSERT_EQUAL_INT16(900, static_cast<int16_t>(out[0] | (out[1] << 8)));
    TEST_ASSERT_EQUAL_INT16(-1500, static_cast<int16_t>(out[2] | (out[3] << 8)));
    TEST_ASSERT_EQUAL_INT16(2500, static_cast<int16_t>(out[4] | (out[5] << 8)));
    TEST_ASSERT_EQUAL_UINT8(0, out[6]);  // DC モタドラは温度センサ非搭載（§8）
    TEST_ASSERT_EQUAL_UINT8(0x09, out[7]);
}

// int16 をそのままキャストすると +4000deg が負値に化け、PC 側が逆方向へ位置制御しかねない。
// 折り返しではなく飽和させる（仕様書 §3.2 の ±3276.7deg）。
static void test_encode_feedback_saturates_position() {
    uint8_t out[8];

    encodeFeedback(out, 40000 /* +4000.0deg */, 0, 0, 0, 0);
    TEST_ASSERT_EQUAL_INT16(32767, static_cast<int16_t>(out[0] | (out[1] << 8)));

    encodeFeedback(out, -40000 /* -4000.0deg */, 0, 0, 0, 0);
    TEST_ASSERT_EQUAL_INT16(-32768, static_cast<int16_t>(out[0] | (out[1] << 8)));
}

static void test_encode_feedback_saturates_velocity_and_current() {
    uint8_t out[8];
    encodeFeedback(out, 0, 100000, -100000, 0, 0);
    TEST_ASSERT_EQUAL_INT16(32767, static_cast<int16_t>(out[2] | (out[3] << 8)));
    TEST_ASSERT_EQUAL_INT16(-32768, static_cast<int16_t>(out[4] | (out[5] << 8)));
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

static void test_clamp_duty_rejects_nan() {
    // NaN が PWM まで届くと duty 計算が不定になるため 0 に落とす
    const float nan_value = 0.0f / (float)(0.0f == 0.0f ? 0.0 : 1.0);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, clampDuty(nan_value, 0.30f));
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

    TEST_ASSERT_EQUAL_UINT8(0, safety.statusFlags(0));
    TEST_ASSERT_EQUAL_UINT8(0, safety.statusFlags(100000));

    // 起動直後でも緊急停止ラッチ（bit3）はそのまま報告する
    safety.stop();
    TEST_ASSERT_EQUAL_UINT8(status_flag::kEStop, safety.statusFlags(100000));
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
    TEST_ASSERT_EQUAL_UINT32(kMaxCommandTimeoutMs, sanitizeCommandTimeoutMs(1e9f, 500));
    // (uint32_t)(-1.0f) を狙った値。処理系によって 4294967295 か 0 に化ける
    TEST_ASSERT_EQUAL_UINT32(kMaxCommandTimeoutMs, sanitizeCommandTimeoutMs(4.2949673e9f, 500));
}

// 負値・0 は「起動直後から永久に出力禁止」に倒れる。止まる方向でも無言で壊れるので弾く。
// 下限は PC 側の再送周期（既定 500ms に対して 50ms）で、それより短い猶予は
// 契約どおり再送している健全な機体を止めてしまう。
static void test_command_timeout_param_has_lower_bound() {
    TEST_ASSERT_EQUAL_UINT32(kMinCommandTimeoutMs, sanitizeCommandTimeoutMs(-1.0f, 500));
    TEST_ASSERT_EQUAL_UINT32(kMinCommandTimeoutMs, sanitizeCommandTimeoutMs(0.0f, 500));
    TEST_ASSERT_EQUAL_UINT32(kMinCommandTimeoutMs, sanitizeCommandTimeoutMs(10.0f, 500));
}

static void test_command_timeout_param_keeps_values_in_range() {
    TEST_ASSERT_EQUAL_UINT32(250u, sanitizeCommandTimeoutMs(250.0f, 500));
    TEST_ASSERT_EQUAL_UINT32(kDefaultCommandTimeoutMs,
                             sanitizeCommandTimeoutMs(500.0f, kDefaultCommandTimeoutMs));
}

// 化けた float32 を uint32_t へキャストするのは未定義動作。指令ごと捨てて現在値を保つ。
static void test_timing_params_reject_nan() {
    TEST_ASSERT_EQUAL_UINT32(500u, sanitizeCommandTimeoutMs(NAN, 500));
    TEST_ASSERT_EQUAL_UINT32(10u, sanitizeFeedbackIntervalMs(NAN, 10));
}

// feedback_interval_ms（0x05）。0 は送信が詰まってバスを埋め、極端に大きい値は
// PC 側から「基板が死んだ」ようにしか見えない。
static void test_feedback_interval_param_is_bounded() {
    TEST_ASSERT_EQUAL_UINT32(kMinFeedbackIntervalMs, sanitizeFeedbackIntervalMs(0.0f, 10));
    TEST_ASSERT_EQUAL_UINT32(kMinFeedbackIntervalMs, sanitizeFeedbackIntervalMs(-5.0f, 10));
    TEST_ASSERT_EQUAL_UINT32(kMaxFeedbackIntervalMs, sanitizeFeedbackIntervalMs(1e9f, 10));
    TEST_ASSERT_EQUAL_UINT32(20u, sanitizeFeedbackIntervalMs(20.0f, 10));
}

// --------------------------------------------------------------------------
// §4 / §5.3 duty の分解（PWM の大きさ + 方向ピンの向き）
// --------------------------------------------------------------------------

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

    const DutyOutput nan_value = splitDuty(NAN, 0.3f);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, nan_value.magnitude);
    TEST_ASSERT_FALSE(nan_value.reverse);
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

    uint8_t clear[8] = {0x01, kEStopClearMagic1, kEStopClearMagic2, 0, 0, 0, 0, 0};
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

    uint8_t clear[8] = {0x01, kEStopClearMagic1, kEStopClearMagic2, 0, 0, 0, 0, 0};
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

    uint8_t clear[8] = {0x01, kEStopClearMagic1, kEStopClearMagic2, 0, 0, 0, 0, 0};
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

    uint8_t clear[8] = {0x01, kEStopClearMagic1, kEStopClearMagic2, 0, 0, 0, 0, 0};
    ch.handleEStopFrame(clear, 8);
    TEST_ASSERT_TRUE(ch.setDuty(0.4f, 0));
    TEST_ASSERT_EQUAL_FLOAT(0.4f, ch.outputDuty(0));
}

// シリアルデバッグの "nan" のように復号層を通らない経路もあるので、保持側でも弾く。
static void test_dc_channel_ignores_nan_duty() {
    DcChannel ch(500);
    ch.feed(0);
    ch.setDuty(0.4f, 0);
    TEST_ASSERT_FALSE(ch.setDuty(NAN, 0));
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
    const uint8_t all[] = {status_flag::kReached,  status_flag::kOvercurrent,
                           status_flag::kOverheat, status_flag::kEStop,
                           status_flag::kWatchdog, status_flag::kDeviceIdUnconfigured,
                           status_flag::kSensor};
    uint8_t seen = 0;
    for (uint8_t bit : all) {
        TEST_ASSERT_NOT_EQUAL_UINT8(0, bit);
        TEST_ASSERT_EQUAL_UINT8(0, seen & bit);
        seen = static_cast<uint8_t>(seen | bit);
    }
    TEST_ASSERT_EQUAL_UINT8(1 << 6, status_flag::kSensor);
    // bit7 は予約のまま空けてある
    TEST_ASSERT_EQUAL_UINT8(0, seen & (1 << 7));
}

// センサは自分のデバイス ID で FEEDBACK を送るので、1 枚に何個載っていてもビットは
// 1 つで足りる。位置・速度・電流・温度は持たないので 0 のまま。
static void test_sensor_flag_rides_in_its_own_feedback() {
    uint8_t out[8];
    encodeFeedback(out, 0, 0, 0, 0, status_flag::kSensor);
    for (uint8_t i = 0; i < 7; ++i) {
        TEST_ASSERT_EQUAL_UINT8(0, out[i]);
    }
    TEST_ASSERT_EQUAL_UINT8(status_flag::kSensor, out[7]);
}

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_build_can_id);
    RUN_TEST(test_parse_can_id_roundtrip);
    RUN_TEST(test_parse_can_id_reserved_is_invalid);
    RUN_TEST(test_parse_can_id_rejects_out_of_range);
    RUN_TEST(test_unpack_float_from_known_bytes);
    RUN_TEST(test_decode_set_target);
    RUN_TEST(test_decode_set_target_rejects_unknown_type);
    RUN_TEST(test_decode_set_target_rejects_short_frame);
    RUN_TEST(test_decode_set_target_rejects_nan);
    RUN_TEST(test_decode_set_param);
    RUN_TEST(test_decode_set_param_unknown_id_is_ignored);
    RUN_TEST(test_decode_set_param_rejects_nan);
    RUN_TEST(test_decode_e_stop_stop);
    RUN_TEST(test_decode_e_stop_clear_requires_magic);
    RUN_TEST(test_decode_e_stop_wrong_magic_is_not_clear);
    RUN_TEST(test_decode_e_stop_unknown_byte0_is_none);
    RUN_TEST(test_status_flag_bits_do_not_overlap);
    RUN_TEST(test_sensor_flag_rides_in_its_own_feedback);
    RUN_TEST(test_encode_feedback_layout);
    RUN_TEST(test_encode_feedback_saturates_position);
    RUN_TEST(test_encode_feedback_saturates_velocity_and_current);
    RUN_TEST(test_clamp_duty);
    RUN_TEST(test_clamp_duty_rejects_nan);
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
    RUN_TEST(test_timing_params_reject_nan);
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
    RUN_TEST(test_dc_channel_ignores_nan_duty);
    return UNITY_END();
}
