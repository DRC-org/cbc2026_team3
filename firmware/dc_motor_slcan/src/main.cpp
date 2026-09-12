#include <Arduino.h>

#include "DcChannel.h"
#include "MotorCanProtocol.h"
#include "MotorCanRouter.h"
#include "MotorLoopTimer.h"
#include "MotorTxHealth.h"
#include "config.h"
#include "pwm.h"
#include "slcan_backend.h"

#if HAS_RGB_LED
#include <Adafruit_NeoPixel.h>
#endif

// SLCAN が USB CDC の Serial を占有するので、duty 直叩きのデバッグ経路とは両立しない。
#ifdef ENABLE_SERIAL_DEBUG
#error "SLCAN 版に ENABLE_SERIAL_DEBUG は持たせられない（同じ Serial の奪い合いになる）"
#endif

using namespace motorcan;

// ===========================================================================
// 配線の静的検証
// ===========================================================================

static constexpr uint8_t kAllPins[] = {
    kPinPwm[0], kPinPwm[1], kPinPwm[2], kPinDir[0], kPinDir[1], kPinDir[2],
    kPinRef,    kPinLed,    kPinRgb,    kPinDip[0], kPinDip[1],
};
static constexpr uint8_t kAllPinCount = sizeof(kAllPins) / sizeof(kAllPins[0]);

// CAN ペリフェラルは使わないが基板は dc_motor と同一。D4/D5 は空けたままにする。
static constexpr bool pinsAvoidCan() {
    for (uint8_t i = 0; i < kAllPinCount; ++i) {
        if (kAllPins[i] == PIN_CAN0_TX || kAllPins[i] == PIN_CAN0_RX) {
            return false;
        }
    }
    return true;
}
static_assert(pinsAvoidCan(), "config.h のピンが CAN(D4/D5) と衝突している");

static constexpr bool pinsAreUnique() {
    for (uint8_t i = 0; i < kAllPinCount; ++i) {
        for (uint8_t j = static_cast<uint8_t>(i + 1); j < kAllPinCount; ++j) {
            if (kAllPins[i] == kAllPins[j]) {
                return false;
            }
        }
    }
    return true;
}
static_assert(pinsAreUnique(), "config.h のピンが重複している");

static_assert(kDcChannelCount == 3,
              "チャンネル数を変えたら g_pwm / g_channel の初期化子も更新すること");
static_assert(kDcChannelCount <= motorcan::kMaxSlotNumber + 1,
              "チャンネル数がデバイス ID のスロット番号（3bit）に収まらない");

static_assert(kDcChannelCount <= motorcan::kMaxChannels,
              "チャンネル数が FrameRoute::channelMask のビット数を超えている");

static_assert(kDipBitCount == sizeof(kPinDip) / sizeof(kPinDip[0]),
              "kDipBitCount と kPinDip の要素数が一致していない");

// ===========================================================================
// ペリフェラルと状態（すべてチャンネル単位）
// ===========================================================================

static PwmOut g_pwm[kDcChannelCount] = {
    PwmOut(kDcChannels[0].pwmPin),
    PwmOut(kDcChannels[1].pwmPin),
    PwmOut(kDcChannels[2].pwmPin),
};

static DcChannel g_channel[kDcChannelCount] = {
    DcChannel(kDefaultCommandTimeoutMs),
    DcChannel(kDefaultCommandTimeoutMs),
    DcChannel(kDefaultCommandTimeoutMs),
};

static uint8_t g_deviceId[kDcChannelCount] = {0, 0, 0};

static bool g_pwmStarted[kDcChannelCount] = {false, false, false};

static float g_maxDuty[kDcChannelCount] = {
    kDcChannels[0].maxDuty,
    kDcChannels[1].maxDuty,
    kDcChannels[2].maxDuty,
};

static uint16_t g_feedbackIntervalMs = kDefaultFeedbackIntervalMs;
static PeriodicTimer g_feedbackTimer[kDcChannelCount];

static PeriodicTimer g_infoTimer;

static uint8_t g_infoPendingCh = kDcChannelCount;
static PeriodicTimer g_blinkTimer;
static bool g_ledOn = false;

static bool g_canFailed = false;

static TxFailCounter g_txFail;

#if HAS_RGB_LED
static Adafruit_NeoPixel g_strip(1, kPinRgb, NEO_GRB + NEO_KHZ800);
#endif

// ===========================================================================
// 入力
// ===========================================================================

static bool isPhysicalStopPressed() {
    const int level = digitalRead(kPinRef);
    return kRefActiveLow ? (level == LOW) : (level == HIGH);
}

// ===========================================================================
// 出力
// ===========================================================================

static bool isChannelConfigured(uint8_t ch) {
    return g_deviceId[ch] != kDeviceIdUnconfigured;
}

static void applyChannelOutput(uint8_t ch, uint32_t nowMs) {
    if (!isChannelConfigured(ch) || !g_pwmStarted[ch]) {
        return;
    }

    g_channel[ch].tick(nowMs);

    const DutyOutput out = splitDuty(g_channel[ch].outputDuty(nowMs), g_maxDuty[ch]);
    digitalWrite(kDcChannels[ch].dirPin, (out.reverse != kDirForwardIsLow) ? LOW : HIGH);
    g_pwm[ch].pulse_perc(out.magnitude * 100.0f);
}

// ===========================================================================
// CAN（搬送は SLCAN / USB CDC）
// ===========================================================================

static uint8_t buildStatusFlags(uint8_t ch, uint32_t nowMs) {
    return composeFeedbackFlags(kBoardKind, SlotKind::Actuator,
                                g_channel[ch].safetyStatusFlags(nowMs), isChannelConfigured(ch),
                                /*reached=*/false, /*sensorActive=*/false);
}

static bool sendFrame(uint16_t canId, uint8_t len, const uint8_t *data) {
    if (dc_slcan::send(canId, len, data)) {
        g_txFail.onSuccess();
        return true;
    }
    g_txFail.onFailure();
    return false;
}

static void sendFeedback(uint8_t ch, uint32_t nowMs) {
    uint8_t data[kFeedbackFlagsOnlyLength];
    const uint8_t len = encodeFeedback(data, buildStatusFlags(ch, nowMs));

    sendFrame(buildCanId(CommandType::Feedback, g_deviceId[ch]), len, data);
}

static bool sendInfo(uint8_t ch) {
    uint8_t data[kInfoBaseLength];
    const uint8_t len = encodeInfo(data, kFirmwareVersion, kBoardKind, SlotKind::Actuator);
    return sendFrame(buildCanId(CommandType::Info, g_deviceId[ch]), len, data);
}

static void applyParam(uint8_t ch, const SetParamCommand &cmd) {
    if (applyCommonParam(cmd, g_channel[ch], g_feedbackIntervalMs)) {
        return;
    }
    switch (cmd.id) {
        case ParamId::MaxDuty:
            g_maxDuty[ch] = clampDuty(fromRaw(cmd.raw, kDutyScale), 1.0f);
            break;
        case ParamId::CommandTimeoutMs:
        case ParamId::FeedbackIntervalMs:
            break;
        case ParamId::ReachedTolerance:
        case ParamId::SlewRate:
        case ParamId::AngleMin:
        case ParamId::AngleMax:
            break;
    }
}

static void handleChannelFrame(uint8_t ch, CommandType command, const uint8_t *data, uint8_t len,
                               uint32_t nowMs) {
    switch (command) {
        case CommandType::SetTarget: {
            g_channel[ch].feed(nowMs);

            const SetTargetCommand cmd = decodeSetTarget(data, len);
            if (!cmd.valid) {
                return;
            }
            g_channel[ch].applySetTarget(cmd, nowMs);
            break;
        }
        case CommandType::SetParam: {
            const SetParamCommand cmd = decodeSetParam(data, len);
            if (cmd.valid) {
                applyParam(ch, cmd);
            }
            break;
        }
        case CommandType::EStop: {
            const EStopAction action = g_channel[ch].handleEStopFrame(data, len);
            if (action != EStopAction::None) {
                applyChannelOutput(ch, nowMs);
            }
            break;
        }
        case CommandType::Feedback:
        case CommandType::Info:
            break;
    }
}

static void handleFrame(uint16_t canId, bool standard, const uint8_t *data, uint8_t len) {
    const FrameRoute route = routeFrame(canId, standard, g_deviceId, kDcChannelCount);
    if (!route.accepted) {
        return;
    }

    const uint32_t nowMs = millis();
    for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
        if ((route.channelMask & static_cast<uint8_t>(1u << ch)) != 0) {
            handleChannelFrame(ch, route.command, data, len, nowMs);
        }
    }
}

static void pollCan() {
    dc_slcan::poll(handleFrame);
}

// ===========================================================================
// デバイス ID（DIP スイッチ = チャンネル表全体へのブロックオフセット）
// ===========================================================================

static void resolveDeviceIds() {
    const uint8_t boardNumber = readDipSwitch(
        kPinDip, kDipBitCount, [](uint8_t pin) { return static_cast<int>(digitalRead(pin)); },
        LOW);
    motorcan::resolveDeviceIds(g_deviceId, kDcChannelCount, kBoardKind, boardNumber, nullptr);
}

// ===========================================================================
// LED
// ===========================================================================

static void updateLed(uint32_t nowMs) {
    BoardIndication indication(g_canFailed || g_txFail.isAlarming(kCanTxFailStreakAlarm));
    for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
        indication.observe(isChannelConfigured(ch),
                           (g_channel[ch].safetyStatusFlags(nowMs) & status_flag::kEStop) != 0);
    }

    const uint32_t interval = blinkIntervalFor(indication, kUnconfiguredBlinkIntervalMs,
                                               kHeartbeatIntervalMs, kHeartbeatIntervalMs);
    if (!g_blinkTimer.due(nowMs, interval)) {
        return;
    }
    g_ledOn = !g_ledOn;
    digitalWrite(kPinLed, g_ledOn ? HIGH : LOW);

#if HAS_RGB_LED
    uint8_t r = 0;
    uint8_t g = 0;
    uint8_t b = 0;
    if (indication.urgent()) {
        r = g_ledOn ? 255 : 0;
    } else if (indication.stopped()) {
        r = 255;
        g = 96;
    } else {
        b = g_ledOn ? 255 : 32;
    }
    g_strip.setPixelColor(0, g_strip.Color(r, g, b));
    g_strip.show();
#endif
}

// ===========================================================================
// setup / loop
// ===========================================================================

void setup() {
    for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
        pinMode(kDcChannels[ch].pwmPin, OUTPUT);
        digitalWrite(kDcChannels[ch].pwmPin, LOW);
        pinMode(kDcChannels[ch].dirPin, OUTPUT);
        digitalWrite(kDcChannels[ch].dirPin, kDirForwardIsLow ? LOW : HIGH);
    }

    pinMode(kPinRef, INPUT_PULLUP);
    pinMode(kPinLed, OUTPUT);
    digitalWrite(kPinLed, LOW);

    for (uint8_t bit = 0; bit < kDipBitCount; ++bit) {
        pinMode(kPinDip[bit], INPUT_PULLUP);
    }

#if HAS_RGB_LED
    g_strip.begin();
    g_strip.setBrightness(kRgbBrightness);
#endif

    resolveDeviceIds();

    const uint32_t startMs = millis();

    for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
        g_feedbackTimer[ch].stagger(startMs, g_feedbackIntervalMs, ch, kDcChannelCount);

        if (!isChannelConfigured(ch)) {
            continue;
        }
        g_pwmStarted[ch] = g_pwm[ch].begin(kPwmFrequencyHz, 0.0f);
    }

    if (!dc_slcan::begin()) {
        g_canFailed = true;
        for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
            g_channel[ch].stop();
        }
    }

    for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
        g_channel[ch].setWatchdogEnabled(WATCHDOG_ENABLED != 0);
    }

    const bool pressed = isPhysicalStopPressed();
    for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
        g_channel[ch].applyPhysicalStop(pressed);
    }

    g_infoTimer.reset(startMs);
    g_blinkTimer.reset(startMs);
}

void loop() {
    const uint32_t nowMs = millis();

    pollCan();

    const bool pressed = isPhysicalStopPressed();
    for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
        g_channel[ch].applyPhysicalStop(pressed);
    }

    for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
        applyChannelOutput(ch, nowMs);
    }

    for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
        if (isChannelConfigured(ch) && g_feedbackTimer[ch].due(nowMs, g_feedbackIntervalMs)) {
            sendFeedback(ch, nowMs);
        }
    }

    if (g_infoTimer.due(nowMs, kInfoIntervalMs)) {
        g_infoPendingCh = 0;
    }
    if (g_infoPendingCh < kDcChannelCount) {
        if (!isChannelConfigured(g_infoPendingCh)) {
            ++g_infoPendingCh;
        } else if (sendInfo(g_infoPendingCh)) {
            ++g_infoPendingCh;
        }
    }

    updateLed(nowMs);
}
