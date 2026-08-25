// DC モータ用自作モタドラのファームウェア本体（Arduino UNO R4 / RA4M1）。
//
// プロトコルの単一情報源は docs/motor_driver_can_protocol.md。
// 機体依存の定数はすべて include/config.h にある。
//
// 責務の分割:
//   MotorCan（Arduino 非依存）… フレームの符号化・復号、緊急停止ラッチ、ウォッチドッグ、PID
//   このファイル              … ペリフェラル初期化、制御ループ、CAN 送受信の配線

#include <Arduino.h>
#include <Arduino_CAN.h>

#include "MotorCanProtocol.h"
#include "MotorPid.h"
#include "MotorSafety.h"
#include "config.h"
#include "pwm.h"

using namespace motorcan;

// CAN 線を他用途に奪われた基板は PC から停止できなくなる。config.h のピン定数を
// 書き換えたときに実機で気付くのではなく、ビルドで止まるようにしておく。
#if HAS_STATUS_LED
static_assert(kPinLed != PIN_CAN0_TX && kPinLed != PIN_CAN0_RX,
              "ステータス LED が CAN ピンと衝突している (config.h の HAS_STATUS_LED 判定を確認)");
#endif
static_assert(kPinPwmN != PIN_CAN0_TX && kPinPwmN != PIN_CAN0_RX, "PWM_N が CAN ピンと衝突");
static_assert(kPinPwmL != PIN_CAN0_TX && kPinPwmL != PIN_CAN0_RX, "PWM_L が CAN ピンと衝突");
static_assert(kPinDis != PIN_CAN0_TX && kPinDis != PIN_CAN0_RX, "DIS が CAN ピンと衝突");

// ===========================================================================
// ペリフェラル
// ===========================================================================

static PwmOut g_pwmN(kPinPwmN);
static PwmOut g_pwmL(kPinPwmL);

// ===========================================================================
// 状態
// ===========================================================================

static uint8_t g_deviceId = kDeviceIdUnconfigured;

// 仕様書 §5.4: 起動時は出力停止・duty モード・目標 0・ラッチ解除済み。
static ControlType g_mode = ControlType::Duty;
static float g_target = 0.0f;
static float g_appliedDuty = 0.0f;

static float g_maxDuty = kDefaultMaxDuty;
static float g_overcurrentThresholdMa = kDefaultOvercurrentThresholdMa;
static float g_reachedToleranceDeg = kDefaultReachedToleranceDeg;
static float g_reachedToleranceRpm = kDefaultReachedToleranceRpm;
static uint32_t g_feedbackIntervalMs = kDefaultFeedbackIntervalMs;

static MotorSafety g_safety(kDefaultCommandTimeoutMs);
static MotorPid g_pid(kDefaultKp, kDefaultKi, kDefaultKd, kIntegralLimit);

// 出力軸換算の観測値。
static float g_positionDeg = 0.0f;
static float g_velocityRpm = 0.0f;
static float g_currentMa = 0.0f;
static bool g_reached = false;

static uint32_t g_lastControlUs = 0;
static uint32_t g_lastFeedbackMs = 0;
static uint32_t g_lastBlinkMs = 0;
static bool g_ledOn = false;

#if ENABLE_SERIAL_DEBUG
// シリアルから duty を入力している間だけ true。
// CAN の SET_TARGET を受けたら解除して、PC の指令とシリアルが競合しないようにする。
static bool g_serialOverride = false;
static String g_serialLine;
#endif

// ===========================================================================
// エンコーダ
// ===========================================================================

#if HAS_ENCODER

static volatile int32_t g_encoderCount = 0;
static volatile uint8_t g_encoderState = 0;

// 4 逓倍の状態遷移表。添字は (前回 AB << 2) | 今回 AB。
// 不正遷移（両相同時変化 = パルス取りこぼし）は 0 にして、誤った方向に数えない。
static const int8_t kQuadratureTable[16] = {0, -1, 1, 0, 1, 0, 0, -1,
                                            -1, 0, 0, 1, 0, 1, -1, 0};

static void encoderIsr() {
    const uint8_t state =
        static_cast<uint8_t>((digitalRead(kPinEncA) << 1) | digitalRead(kPinEncB));
    g_encoderCount += kQuadratureTable[(g_encoderState << 2) | state];
    g_encoderState = state;
}

static int32_t readEncoderCount() {
    // 32bit の読み出しは ISR に割り込まれると上位/下位が混ざりうる
    noInterrupts();
    const int32_t count = g_encoderCount;
    interrupts();
    return count;
}

#endif  // HAS_ENCODER

// ===========================================================================
// 出力
// ===========================================================================

static void setGateDriverEnabled(bool enabled) {
    // DIS はアクティブ HIGH（出力禁止）と仮定。config.h の kDisActiveHigh を参照。
    const bool assertDisable = !enabled;
    digitalWrite(kPinDis, (assertDisable == kDisActiveHigh) ? HIGH : LOW);
}

// duty を PWM へ反映する。符号-絶対値方式のハーフブリッジ 2 系統。
// 仕様書 §4: duty は -1.0～+1.0。pulse_perc() は百分率なので 100 倍する。
static void writePwm(float duty) {
    const float percent = (duty < 0.0f ? -duty : duty) * 100.0f;
    if (duty > 0.0f) {
        g_pwmL.pulse_perc(0.0f);
        g_pwmN.pulse_perc(percent);
    } else if (duty < 0.0f) {
        g_pwmN.pulse_perc(0.0f);
        g_pwmL.pulse_perc(percent);
    } else {
        // 両方 0 でコースト。ブレーキではないのは、脱力させた方が
        // 機構の噛み込みからの復帰が容易なため。
        g_pwmN.pulse_perc(0.0f);
        g_pwmL.pulse_perc(0.0f);
    }
}

static bool isDriveAllowed(uint32_t nowMs) {
    // 仕様書 §2.2: DIP 未設定の基板は駆動しない。
    // 設定ミスで意図しないアクチュエータが動くより、動かない方が安全。
    if (g_deviceId == kDeviceIdUnconfigured) {
        return false;
    }
    return g_safety.isOutputAllowed(nowMs);
}

static void applyOutput(float duty, uint32_t nowMs) {
    if (!isDriveAllowed(nowMs)) {
        // 出力を止めるだけでなく積分項も捨てる。
        // 残したままだと緊急停止解除やウォッチドッグ復帰の瞬間に急発進する。
        duty = 0.0f;
        g_pid.reset();
        setGateDriverEnabled(false);
    } else {
        setGateDriverEnabled(true);
    }
    g_appliedDuty = clampDuty(duty, g_maxDuty);
    writePwm(g_appliedDuty);
}

// ===========================================================================
// センサ更新
// ===========================================================================

static void updateSensors(float dtSec) {
#if HAS_ENCODER
    static int32_t lastCount = 0;
    const int32_t count = readEncoderCount();
    const int32_t delta = count - lastCount;
    lastCount = count;

    g_positionDeg = kEncoderDirectionSign * (static_cast<float>(count) * 360.0f) /
                    kEncoderCountsPerOutputRev;
    if (dtSec > 0.0f) {
        const float revPerSec =
            (static_cast<float>(delta) / kEncoderCountsPerOutputRev) / dtSec;
        // 1 制御周期あたりのカウントが少なく分解能が粗いので一次 IIR で均す。
        // 生値のままだと velocity 制御の微分項が刻みノイズを増幅する。
        const float rawRpm = kEncoderDirectionSign * revPerSec * 60.0f;
        g_velocityRpm += 0.2f * (rawRpm - g_velocityRpm);
    }
#else
    (void)dtSec;
    g_positionDeg = 0.0f;
    g_velocityRpm = 0.0f;
#endif

    const int32_t raw = analogRead(kPinSens);
    const float sensed =
        (static_cast<float>(raw) - static_cast<float>(kCurrentSenseZeroCount)) *
        kCurrentSenseMaPerCount;
    g_currentMa += 0.2f * (sensed - g_currentMa);
}

// ===========================================================================
// 制御
// ===========================================================================

static void runControl(float dtSec, uint32_t nowMs) {
    if (!isDriveAllowed(nowMs)) {
        applyOutput(0.0f, nowMs);
        g_reached = false;
        return;
    }

    float duty = 0.0f;
    switch (g_mode) {
        case ControlType::Position: {
            const float error = g_target - g_positionDeg;
            duty = g_pid.update(error, dtSec);
            g_reached = (error < 0.0f ? -error : error) <= g_reachedToleranceDeg;
            break;
        }
        case ControlType::Velocity: {
            const float error = g_target - g_velocityRpm;
            duty = g_pid.update(error, dtSec);
            g_reached = (error < 0.0f ? -error : error) <= g_reachedToleranceRpm;
            break;
        }
        case ControlType::Duty:
        default:
            duty = g_target;
            // duty は開ループなので「指令した時点で到達」とみなす。
            // PC 側 GenericDriver も duty の到達判定にこのビットを使っていない。
            g_reached = true;
            break;
    }

    applyOutput(duty, nowMs);
}

// 仕様書 §3.3: モード切替では目標値を 0 にリセットし積分項をクリアする。
// 位置目標 90.0[deg] が duty 90.0（= 9000%）として解釈される事故を防ぐ。
static void switchMode(ControlType mode) {
    if (mode == g_mode) {
        return;
    }
    g_mode = mode;
    g_target = 0.0f;
    g_pid.reset();
}

// ===========================================================================
// CAN
// ===========================================================================

static uint8_t buildStatusFlags(uint32_t nowMs) {
    uint8_t flags = g_safety.statusFlags(nowMs);
    if (g_reached) {
        flags |= status_flag::kReached;
    }
    const float absCurrent = g_currentMa < 0.0f ? -g_currentMa : g_currentMa;
    if (absCurrent >= g_overcurrentThresholdMa) {
        flags |= status_flag::kOvercurrent;
    }
    // 温度センサが無いので過熱ビットは立てない（仕様書 §7 の既知の制限）。
    if (g_deviceId == kDeviceIdUnconfigured) {
        flags |= status_flag::kDeviceIdUnconfigured;
    }
    return flags;
}

static void sendFeedback(uint32_t nowMs) {
    uint8_t data[kFrameLength];

    // 仕様書 §3.2: 位置は 0.1deg 単位。±3276.7deg で飽和する（encodeFeedback が飽和させる）。
    const int32_t position = static_cast<int32_t>(lroundf(g_positionDeg * 10.0f));
    const int32_t rpm = static_cast<int32_t>(lroundf(g_velocityRpm));
    const int32_t currentMa = static_cast<int32_t>(lroundf(g_currentMa));

    encodeFeedback(data, position, rpm, currentMa, 0, buildStatusFlags(nowMs));

    // 緊急停止中・ウォッチドッグ作動中も送り続ける。
    // 止めると PC 側が STALE になり、なぜ動かないのかを操縦者が判別できなくなる。
    const CanMsg msg(CanStandardId(buildCanId(CommandType::Feedback, g_deviceId)),
                     kFrameLength, data);
    CAN.write(msg);
}

static void handleFrame(const CanMsg &msg) {
    // 本プロトコルは Standard Frame のみ（仕様書 §1）。
    if (!msg.isStandardId()) {
        return;
    }

    const CanIdInfo info = parseCanId(static_cast<uint16_t>(msg.getStandardId()));
    if (!info.valid) {
        // 予約コマンド種別（仕様書 §2.1）や他プロトコルの相乗り。無視する。
        return;
    }

    // ブロードキャスト E_STOP（0x7FF）と自分宛の両方を受理し、それ以外は無視する。
    const bool broadcastEStop =
        (info.command == CommandType::EStop && info.deviceId == kDeviceIdBroadcast);
    if (!broadcastEStop && info.deviceId != g_deviceId) {
        return;
    }
    // ID 未設定（0x00）の基板に「自分宛」は存在しない。ブロードキャストの緊急停止だけ受ける。
    if (!broadcastEStop && g_deviceId == kDeviceIdUnconfigured) {
        return;
    }

    const uint32_t nowMs = millis();

    switch (info.command) {
        case CommandType::SetTarget: {
            // 仕様書 §6: 緊急停止ラッチ中でもウォッチドッグは養う。
            // 養わないと解除した直後に満了済みで動かない。
            g_safety.feed(nowMs);
            const SetTargetCommand cmd = decodeSetTarget(msg.data, msg.data_length);
            if (!cmd.valid) {
                return;
            }
#if ENABLE_SERIAL_DEBUG
            g_serialOverride = false;
#endif
            // 仕様書 §3.1: Byte0 の制御タイプで即座に制御則を切り替える。
            // switchMode は目標値を 0 に落とすので、目標値の代入はそのあと。
            switchMode(cmd.type);
            g_target = cmd.value;
            break;
        }
        case CommandType::SetMode: {
            const SetModeCommand cmd = decodeSetMode(msg.data, msg.data_length);
            if (cmd.valid) {
                switchMode(cmd.type);
            }
            break;
        }
        case CommandType::SetParam: {
            const SetParamCommand cmd = decodeSetParam(msg.data, msg.data_length);
            if (!cmd.valid) {
                // 未知のパラメータ ID は無視する（仕様書 §3.4）。
                return;
            }
            switch (cmd.id) {
                case ParamId::Kp:
                    g_pid.setKp(cmd.value);
                    break;
                case ParamId::Ki:
                    g_pid.setKi(cmd.value);
                    break;
                case ParamId::Kd:
                    g_pid.setKd(cmd.value);
                    break;
                case ParamId::MaxDuty:
                    g_maxDuty = clampDuty(cmd.value, 1.0f);
                    break;
                case ParamId::CommandTimeoutMs:
                    g_safety.setTimeoutMs(static_cast<uint32_t>(cmd.value));
                    break;
                case ParamId::FeedbackIntervalMs:
                    // 0 にすると送信が詰まってバスを埋めるので下限を置く
                    g_feedbackIntervalMs =
                        cmd.value < 1.0f ? 1u : static_cast<uint32_t>(cmd.value);
                    break;
                case ParamId::OvercurrentThresholdMa:
                    g_overcurrentThresholdMa = cmd.value;
                    break;
                case ParamId::ReachedTolerance:
                    // 仕様書 §3.4 の既定値が deg / rpm の 2 種あるとおり、
                    // 単位はモード依存。いま有効なモード側を書き換える。
                    if (g_mode == ControlType::Velocity) {
                        g_reachedToleranceRpm = cmd.value;
                    } else {
                        g_reachedToleranceDeg = cmd.value;
                    }
                    break;
            }
            break;
        }
        case CommandType::EStop: {
            const EStopAction action = g_safety.handleEStopFrame(msg.data, msg.data_length);
            if (action == EStopAction::Stop) {
                applyOutput(0.0f, nowMs);
            } else if (action == EStopAction::Clear) {
                // 仕様書 §3.5: 解除直後の目標値は 0 から始める。
                // 停止前の目標を復元すると解除した瞬間にモータが動き出す。
                g_target = 0.0f;
                g_pid.reset();
#if ENABLE_SERIAL_DEBUG
                g_serialOverride = false;
#endif
            }
            break;
        }
        case CommandType::Feedback:
            // 他基板の FEEDBACK。自分宛の ID とは衝突しないが念のため無視する。
            break;
    }
}

static void pollCan() {
    while (CAN.available()) {
        const CanMsg msg = CAN.read();
        handleFrame(msg);
    }
}

// ===========================================================================
// デバイス ID（DIP スイッチ）
// ===========================================================================

static uint8_t readDeviceId() {
    uint8_t id = 0;
    for (uint8_t bit = 0; bit < 4; ++bit) {
        // INPUT_PULLUP の負論理: LOW = ON = 1
        if (digitalRead(kPinDip[bit]) == LOW) {
            id |= static_cast<uint8_t>(1u << bit);
        }
    }
    return id;
}

// ===========================================================================
// LED
// ===========================================================================

static void updateLed(uint32_t nowMs) {
    // ID 未設定は赤（RGB 非搭載時はオンボード LED）の速い点滅で知らせる（仕様書 §2.2）。
    const bool unconfigured = (g_deviceId == kDeviceIdUnconfigured);
    const uint32_t interval =
        unconfigured ? kUnconfiguredBlinkIntervalMs : kHeartbeatIntervalMs;

    if (nowMs - g_lastBlinkMs < interval) {
        return;
    }
    g_lastBlinkMs = nowMs;
    g_ledOn = !g_ledOn;
#if HAS_STATUS_LED
    digitalWrite(kPinLed, g_ledOn ? HIGH : LOW);
#endif

#if HAS_RGB_LED
    // TODO(実機で確認): RGB LED ライブラリを platformio.ini の lib_deps に追加し、
    // ここで unconfigured=赤 / 緊急停止=橙 / 通常=緑 を出す。
    // 依存が取れない環境でもビルドが通るよう既定では無効にしてある。
#endif
}

// ===========================================================================
// デバッグ用シリアル
// ===========================================================================

#if ENABLE_SERIAL_DEBUG

// duty を直接入力する。緊急停止ラッチ中は applyOutput が一括で出力を禁止するため、
// ここで指令しても駆動されない（仕様書 §5.2 の要求）。
static void pollSerial(uint32_t nowMs) {
    while (Serial.available() > 0) {
        const char c = static_cast<char>(Serial.read());
        if (c != '\n' && c != '\r') {
            if (g_serialLine.length() < 16) {
                g_serialLine += c;
            }
            continue;
        }
        if (g_serialLine.length() == 0) {
            continue;
        }

        if (g_serialLine[0] == 's' || g_serialLine[0] == 'S') {
            g_serialOverride = false;
            g_target = 0.0f;
        } else {
            switchMode(ControlType::Duty);
            g_target = g_serialLine.toFloat();
            g_serialOverride = true;
        }
        g_serialLine = "";
    }

    // シリアル操作中はウォッチドッグを養い続ける。
    // 1 回だけ養う実装だと command_timeout_ms 後に必ず止まってデバッグにならない。
    // 'S' 入力・CAN の SET_TARGET・電源断のいずれでもこのモードは解除される。
    if (g_serialOverride) {
        g_safety.feed(nowMs);
    }
}

#endif  // ENABLE_SERIAL_DEBUG

// ===========================================================================
// setup / loop
// ===========================================================================

void setup() {
    // 何よりも先にゲートドライバを禁止する。PWM 初期化前にピンが不定のまま
    // ハーフブリッジが導通すると貫通電流で MOSFET が飛ぶ。
    pinMode(kPinDis, OUTPUT);
    setGateDriverEnabled(false);

#if HAS_STATUS_LED
    pinMode(kPinLed, OUTPUT);
    digitalWrite(kPinLed, LOW);
#endif

    for (uint8_t bit = 0; bit < 4; ++bit) {
        pinMode(kPinDip[bit], INPUT_PULLUP);
    }
    pinMode(kPinSwA, INPUT_PULLUP);
    pinMode(kPinSwB, INPUT_PULLUP);
    pinMode(kPinInt, INPUT_PULLUP);

#if ENABLE_SERIAL_DEBUG
    // USB CDC。D0/D1 は DIP に使っているので Serial1 は開かない（config.h 参照）。
    Serial.begin(kSerialBaud);
#endif

    // PwmOut::begin() の引数無し版は 490Hz・duty 50% で始まる。
    // それではモータが一瞬回るので、周期とパルス幅を明示して duty 0 から立ち上げる。
    g_pwmN.begin(kPwmPeriodUs, static_cast<uint32_t>(0));
    g_pwmL.begin(kPwmPeriodUs, static_cast<uint32_t>(0));
    writePwm(0.0f);

#if HAS_ENCODER
    pinMode(kPinEncA, INPUT_PULLUP);
    pinMode(kPinEncB, INPUT_PULLUP);
    pinMode(kPinEncX, INPUT_PULLUP);
    g_encoderState =
        static_cast<uint8_t>((digitalRead(kPinEncA) << 1) | digitalRead(kPinEncB));
    attachInterrupt(digitalPinToInterrupt(kPinEncA), encoderIsr, CHANGE);
    attachInterrupt(digitalPinToInterrupt(kPinEncB), encoderIsr, CHANGE);
#endif

    g_deviceId = readDeviceId();

    // 仕様書 §1: 1 Mbps。
    // CAN が上がらない基板を駆動させると PC から止められないので、
    // begin 失敗時は緊急停止ラッチに落として出力を封じる。
    if (!CAN.begin(CanBitRate::BR_1000k)) {
        g_safety.stop();
    }

    g_lastControlUs = micros();
    g_lastFeedbackMs = millis();
    g_lastBlinkMs = g_lastFeedbackMs;
}

void loop() {
    const uint32_t nowMs = millis();

    pollCan();
#if ENABLE_SERIAL_DEBUG
    pollSerial(nowMs);
#endif

    const uint32_t nowUs = micros();
    if (nowUs - g_lastControlUs >= kControlIntervalUs) {
        const float dtSec = static_cast<float>(nowUs - g_lastControlUs) * 1e-6f;
        g_lastControlUs = nowUs;
        updateSensors(dtSec);
        runControl(dtSec, nowMs);
    }

    if (nowMs - g_lastFeedbackMs >= g_feedbackIntervalMs) {
        g_lastFeedbackMs = nowMs;
        sendFeedback(nowMs);
    }

    updateLed(nowMs);
}
