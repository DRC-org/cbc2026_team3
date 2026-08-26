// DC モータ用自作モタドラのファームウェア本体（Arduino UNO R4 / RA4M1）。
//
// プロトコルの単一情報源は docs/motor_driver_can_protocol.md。
// 機体依存の定数はすべて include/config.h にある。
//
// 責務の分割:
//   MotorCan（Arduino 非依存）… フレームの符号化・復号、宛先判定、緊急停止ラッチ、
//                                ウォッチドッグ、PID、周期タイマ、シリアル行組み立て
//   このファイル              … ペリフェラル初期化、制御ループ、CAN 送受信の配線
//
// サーボ用（firmware/servo）と同じ判断をする箇所は MotorCan 側に置くこと。
// 両 main.cpp が同じ分岐を各自で持つと、片方だけ直したことに誰も気付けない。

#include <Arduino.h>
#include <Arduino_CAN.h>
#include <stdlib.h>

#include "MotorCanProtocol.h"
#include "MotorCanRouter.h"
#include "MotorControlTarget.h"
#include "MotorLoopTimer.h"
#include "MotorPid.h"
#include "MotorSafety.h"
#include "SerialLineBuffer.h"
#include "config.h"
#include "pwm.h"

using namespace motorcan;

// CAN 線を他用途に奪われた基板は PC から停止できなくなる。config.h のピン定数を
// 書き換えたときに実機で気付くのではなく、ビルドで止まるようにしておく。
static_assert(kPinLed != PIN_CAN0_TX && kPinLed != PIN_CAN0_RX, "ステータス LED が CAN ピンと衝突");
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
// モードと目標値を別々の変数で持たないのは、モードだけ切り替えて前の目標値が残る
// 経路を書けなくするため（仕様書 §3.3。ControlTarget が規則を持つ）。
static ControlTarget g_control;
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

// 制御ループだけ us 基準で、経過時間を PID の dt にも使うので PeriodicTimer に載せない。
static uint32_t g_lastControlUs = 0;
static PeriodicTimer g_feedbackTimer;
static PeriodicTimer g_blinkTimer;
static bool g_ledOn = false;

#if ENABLE_SERIAL_DEBUG
// シリアルから duty を入力している間だけ true。
// CAN の SET_TARGET を受けたら解除して、PC の指令とシリアルが競合しないようにする。
static bool g_serialOverride = false;
static char g_serialStorage[16];
static SerialLineBuffer g_serialLine(g_serialStorage, sizeof(g_serialStorage));
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
    // 緊急停止ラッチとウォッチドッグ（WATCHDOG_ENABLED による無効化を含む）の判定は
    // MotorSafety が持つ。ここで isExpired() を直に見ると無効化フラグを迂回する。
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

#if HAS_CURRENT_SENSE
    const int32_t raw = analogRead(kPinSens);
    const float sensed =
        (static_cast<float>(raw) - static_cast<float>(kCurrentSenseZeroCount)) *
        kCurrentSenseMaPerCount;
    g_currentMa += 0.2f * (sensed - g_currentMa);
#else
    // センス未実装の基板では SENS ピンが浮き、ADC の振れがそのまま
    // (raw - zero) * mAPerCount [mA] として過電流しきい値を跨いで誤発火する。
    // 仕様書 §3.2 のとおり、センサを持たない項目は 0 を送る。
    g_currentMa = 0.0f;
#endif
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
    switch (g_control.mode()) {
        case ControlType::Position: {
            const float error = g_control.value() - g_positionDeg;
            duty = g_pid.update(error, dtSec);
            g_reached = (error < 0.0f ? -error : error) <= g_reachedToleranceDeg;
            break;
        }
        case ControlType::Velocity: {
            const float error = g_control.value() - g_velocityRpm;
            duty = g_pid.update(error, dtSec);
            g_reached = (error < 0.0f ? -error : error) <= g_reachedToleranceRpm;
            break;
        }
        case ControlType::Duty:
        default:
            duty = g_control.value();
            // duty は開ループなので「指令した時点で到達」とみなす。
            // PC 側 GenericDriver も duty の到達判定にこのビットを使っていない。
            g_reached = true;
            break;
    }

    applyOutput(duty, nowMs);
}

// 仕様書 §3.3: モード切替では目標値を 0 にリセットし積分項をクリアする。
// 目標値を落とす規則そのものは ControlTarget が持つ（native テストで守られている）。
// 残った積分項は解除直後の急発進になるので、切り替わったときだけ捨てる。
static void switchMode(ControlType mode) {
    if (g_control.switchMode(mode)) {
        g_pid.reset();
    }
}

// ===========================================================================
// CAN
// ===========================================================================

static uint8_t buildStatusFlags(uint32_t nowMs) {
    // bit3（緊急停止）/ bit4（ウォッチドッグ）の判定は MotorSafety に集約されている。
    // bit4 は指令を一度でも受けた後の満了でのみ立ち、無効化した基板では立たない。
    uint8_t flags = g_safety.statusFlags(nowMs);
    if (g_reached) {
        flags |= status_flag::kReached;
    }
#if HAS_CURRENT_SENSE
    const float absCurrent = g_currentMa < 0.0f ? -g_currentMa : g_currentMa;
    if (absCurrent >= g_overcurrentThresholdMa) {
        flags |= status_flag::kOvercurrent;
    }
#endif
    // 温度センサが無いので過熱ビットは立てない（仕様書 §8 の既知の制限）。
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
    // Standard Frame 判定・予約コマンド種別・宛先判定（自分宛 / ブロードキャスト E_STOP /
    // ID 未設定）はサーボ用と同じ規則なので MotorCanRouter に集約してある。
    // DC 基板はチャンネルが 1 つなので、デバイス ID 表も 1 要素。
    const FrameRoute route =
        routeFrame(static_cast<uint16_t>(msg.getStandardId()), msg.isStandardId(), &g_deviceId, 1);
    if (!route.accepted) {
        return;
    }

    const uint32_t nowMs = millis();

    switch (route.command) {
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
            g_control.setValue(cmd.value);
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
                    if (g_control.mode() == ControlType::Velocity) {
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
                g_control.clearValue();
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

// DC 用は DIP の値がそのままデバイス ID（サーボ用はチャンネル表へのオフセット）。
// 負論理とビット順の対応は MotorCanRouter が持つ。
static uint8_t readDeviceId() {
    return readDipSwitch(
        kPinDip, 4, [](uint8_t pin) { return static_cast<int>(digitalRead(pin)); }, LOW);
}

// ===========================================================================
// LED
// ===========================================================================

static void updateLed(uint32_t nowMs) {
    // ID 未設定は赤（RGB 非搭載時はオンボード LED）の速い点滅で知らせる（仕様書 §2.2）。
    const bool unconfigured = (g_deviceId == kDeviceIdUnconfigured);
    if (!g_blinkTimer.due(nowMs,
                          unconfigured ? kUnconfiguredBlinkIntervalMs : kHeartbeatIntervalMs)) {
        return;
    }
    g_ledOn = !g_ledOn;
    digitalWrite(kPinLed, g_ledOn ? HIGH : LOW);

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
        if (!g_serialLine.push(static_cast<char>(Serial.read()))) {
            continue;
        }
        const char *line = g_serialLine.line();

        if (line[0] == 's' || line[0] == 'S') {
            g_serialOverride = false;
            g_control.clearValue();
        } else {
            switchMode(ControlType::Duty);
            // 数値として読めない行は 0 になる。duty 0 = 停止なので安全側に落ちる。
            g_control.setValue(strtof(line, nullptr));
            g_serialOverride = true;
        }
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

    pinMode(kPinLed, OUTPUT);
    digitalWrite(kPinLed, LOW);

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

    // config.h のビルド時フラグを実行時フラグへ写す（仕様書 §5.1 / §8）。
    // 判定そのものは MotorSafety にしか無いので、写し忘れれば有効のまま動く。
    g_safety.setWatchdogEnabled(WATCHDOG_ENABLED != 0);

    // 仕様書 §1: 1 Mbps。
    // CAN が上がらない基板を駆動させると PC から止められないので、
    // begin 失敗時は緊急停止ラッチに落として出力を封じる。
    if (!CAN.begin(CanBitRate::BR_1000k)) {
        g_safety.stop();
    }

    const uint32_t startMs = millis();
    g_lastControlUs = micros();
    g_feedbackTimer.reset(startMs);
    g_blinkTimer.reset(startMs);
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

    if (g_feedbackTimer.due(nowMs, g_feedbackIntervalMs)) {
        sendFeedback(nowMs);
    }

    updateLed(nowMs);
}
