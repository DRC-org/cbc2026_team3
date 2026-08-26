// MotorCan（プロトコル層 + 安全機構）の native ユニットテスト。
// 実機を用意せずにプロトコルの取り違えを検出するのが目的なので、
// ここで検証するのはすべて docs/motor_driver_can_protocol.md に明記された挙動に限る。

#include <unity.h>

#include <string.h>

#include "MotorCanProtocol.h"
#include "MotorControlTarget.h"
#include "MotorPid.h"
#include "MotorSafety.h"

using namespace motorcan;

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

static void test_float_le_known_bytes() {
    // 0.3f = 0x3E99999A（IEEE754）。LE なので下位バイトから並ぶ。
    uint8_t buf[4] = {0, 0, 0, 0};
    packFloatLe(buf, 0.3f);
    TEST_ASSERT_EQUAL_UINT8(0x9A, buf[0]);
    TEST_ASSERT_EQUAL_UINT8(0x99, buf[1]);
    TEST_ASSERT_EQUAL_UINT8(0x99, buf[2]);
    TEST_ASSERT_EQUAL_UINT8(0x3E, buf[3]);
    TEST_ASSERT_EQUAL_FLOAT(0.3f, unpackFloatLe(buf));
}

static void test_float_le_roundtrip() {
    const float values[] = {0.0f, -0.0f, 1.0f, -1.0f, 0.30f, -0.30f, 90.0f, 3276.7f, -12345.5f};
    for (float v : values) {
        uint8_t buf[4];
        packFloatLe(buf, v);
        TEST_ASSERT_EQUAL_FLOAT(v, unpackFloatLe(buf));
    }
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

// --------------------------------------------------------------------------
// §3.3 SET_MODE / §3.4 SET_PARAM
// --------------------------------------------------------------------------

static void test_decode_set_mode() {
    uint8_t data[8] = {0};
    data[0] = static_cast<uint8_t>(ControlType::Velocity);
    const SetModeCommand cmd = decodeSetMode(data, 8);
    TEST_ASSERT_TRUE(cmd.valid);
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(ControlType::Velocity),
                            static_cast<uint8_t>(cmd.type));

    data[0] = 9;
    TEST_ASSERT_FALSE(decodeSetMode(data, 8).valid);
}

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
    TEST_ASSERT_FALSE(safety.hasEverBeenFed());
    TEST_ASSERT_FALSE(safety.isCommandLost(100000));

    safety.feed(1000);
    TEST_ASSERT_TRUE(safety.hasEverBeenFed());
    TEST_ASSERT_FALSE(safety.isCommandLost(1400));
    TEST_ASSERT_TRUE(safety.isCommandLost(1500));
}

// --------------------------------------------------------------------------
// §5.1 ウォッチドッグの有効/無効
// --------------------------------------------------------------------------

// 試合では必ず有効。config.h の WATCHDOG_ENABLED を写し忘れた基板が
// 「気付かないうちに無効」になっていないよう、既定は有効側に倒す。
static void test_watchdog_is_enabled_by_default() {
    MotorSafety safety(500);
    TEST_ASSERT_TRUE(safety.isWatchdogEnabled());
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
// §3.3 / §5.4 制御モードと目標値
// --------------------------------------------------------------------------

// 仕様書 §5.4: 電源投入直後は duty モード・目標 0。
static void test_control_target_starts_stopped_in_duty() {
    ControlTarget control;
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(ControlType::Duty),
                            static_cast<uint8_t>(control.mode()));
    TEST_ASSERT_EQUAL_FLOAT(0.0f, control.value());
}

// 仕様書 §3.3 の暴走防止の本体。位置目標 90.0[deg] がそのまま duty 90.0（= 9000%）として
// 解釈される事故を防ぐため、モードが変わったら目標値を必ず 0 に落とす。
static void test_switch_mode_resets_target_to_zero() {
    ControlTarget control;
    control.setValue(90.0f);

    TEST_ASSERT_TRUE(control.switchMode(ControlType::Position));
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(ControlType::Position),
                            static_cast<uint8_t>(control.mode()));
    TEST_ASSERT_EQUAL_FLOAT(0.0f, control.value());

    control.setValue(1500.0f);
    TEST_ASSERT_TRUE(control.switchMode(ControlType::Velocity));
    TEST_ASSERT_EQUAL_FLOAT(0.0f, control.value());
}

// SET_TARGET は毎フレーム制御タイプを載せてくる（仕様書 §3.1）ので、同じモードでの
// 呼び出しで目標値を 0 に落としてはならない。落とすと 20Hz の再送のたびに
// 目標値が 0 と指令値の間で振動する。
static void test_switch_to_same_mode_keeps_target() {
    ControlTarget control;
    control.switchMode(ControlType::Velocity);
    control.setValue(1000.0f);

    TEST_ASSERT_FALSE(control.switchMode(ControlType::Velocity));
    TEST_ASSERT_EQUAL_FLOAT(1000.0f, control.value());
}

// 仕様書 §3.5: 緊急停止の解除直後は目標値 0 から始める。モードは変えない。
static void test_clear_value_keeps_mode() {
    ControlTarget control;
    control.switchMode(ControlType::Position);
    control.setValue(45.0f);

    control.clearValue();
    TEST_ASSERT_EQUAL_FLOAT(0.0f, control.value());
    TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(ControlType::Position),
                            static_cast<uint8_t>(control.mode()));
}

// --------------------------------------------------------------------------
// §3.3 モード切替時の PID リセット
// --------------------------------------------------------------------------

static void test_pid_proportional_only() {
    MotorPid pid(0.5f, 0.0f, 0.0f, 1.0f);
    TEST_ASSERT_EQUAL_FLOAT(1.0f, pid.update(2.0f, 0.001f));
}

// 初回の微分キックが出ると、目標を与えた瞬間にフルパワーが飛ぶ
static void test_pid_no_derivative_kick_on_first_update() {
    MotorPid pid(0.0f, 0.0f, 1.0f, 1.0f);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, pid.update(10.0f, 0.001f));
    TEST_ASSERT_EQUAL_FLOAT(0.0f, pid.update(10.0f, 0.001f));  // 誤差が変わらなければ 0
}

// 仕様書 §3.3: モード切替では積分項をクリアする。
// 残っていると新しいモードの目標 0 に対していきなり出力が乗る。
static void test_pid_reset_clears_integral() {
    MotorPid pid(0.0f, 1.0f, 0.0f, 1.0f);
    pid.update(1.0f, 0.1f);
    pid.update(1.0f, 0.1f);
    TEST_ASSERT_TRUE(pid.update(1.0f, 0.1f) > 0.0f);

    pid.reset();
    TEST_ASSERT_EQUAL_FLOAT(0.0f, pid.update(0.0f, 0.1f));
}

static void test_pid_integral_windup_is_limited() {
    MotorPid pid(0.0f, 1.0f, 0.0f, 1.0f);
    for (int i = 0; i < 1000; ++i) {
        pid.update(10.0f, 0.01f);
    }
    TEST_ASSERT_TRUE(pid.update(10.0f, 0.01f) <= 1.0f + 1e-4f);
}

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_build_can_id);
    RUN_TEST(test_parse_can_id_roundtrip);
    RUN_TEST(test_parse_can_id_reserved_is_invalid);
    RUN_TEST(test_parse_can_id_rejects_out_of_range);
    RUN_TEST(test_float_le_known_bytes);
    RUN_TEST(test_float_le_roundtrip);
    RUN_TEST(test_decode_set_target);
    RUN_TEST(test_decode_set_target_rejects_unknown_type);
    RUN_TEST(test_decode_set_target_rejects_short_frame);
    RUN_TEST(test_decode_set_mode);
    RUN_TEST(test_decode_set_param);
    RUN_TEST(test_decode_set_param_unknown_id_is_ignored);
    RUN_TEST(test_decode_e_stop_stop);
    RUN_TEST(test_decode_e_stop_clear_requires_magic);
    RUN_TEST(test_decode_e_stop_wrong_magic_is_not_clear);
    RUN_TEST(test_decode_e_stop_unknown_byte0_is_none);
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
    RUN_TEST(test_disabled_watchdog_still_honors_e_stop_latch);
    RUN_TEST(test_watchdog_can_be_re_enabled);
    RUN_TEST(test_protocol_defaults_match_spec);
    RUN_TEST(test_control_target_starts_stopped_in_duty);
    RUN_TEST(test_switch_mode_resets_target_to_zero);
    RUN_TEST(test_switch_to_same_mode_keeps_target);
    RUN_TEST(test_clear_value_keeps_mode);
    RUN_TEST(test_pid_proportional_only);
    RUN_TEST(test_pid_no_derivative_kick_on_first_update);
    RUN_TEST(test_pid_reset_clears_integral);
    RUN_TEST(test_pid_integral_windup_is_limited);
    return UNITY_END();
}
