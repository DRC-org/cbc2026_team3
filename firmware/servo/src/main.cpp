#include <Arduino.h>
#include <Servo.h>
#include <stdlib.h>

#include "MotorCanProtocol.h"
#include "MotorCanRouter.h"
#include "MotorLoopTimer.h"
#include "MotorTxHealth.h"
#include "SerialLineBuffer.h"
#include "SerialOverride.h"
#include "ServoChannel.h"
#include "ServoMotion.h"
#include "can_backend.h"
#include "config.h"

#if HAS_RGB_LED
#include <Adafruit_NeoPixel.h>
#endif

using namespace motorcan;

// ===========================================================================
// 配線の静的検証
// ===========================================================================

#if defined(ARDUINO_ARCH_RENESAS)
static constexpr uint8_t kFixedPins[] = {
    kPinUartRx, kPinUartTx, kPinRgb, kPinDip[0], kPinDip[1], kPinDip[2], kPinDip[3],
};
#else
static constexpr uint8_t kFixedPins[] = {
    kPinUartRx, kPinUartTx, kPinMcpInt,  kPinMcpCs,  kPinSpiMosi, kPinSpiMiso,
    kPinSpiSck, kPinRgb,    kPinDip[0],  kPinDip[1], kPinDip[2],  kPinDip[3],
};
#endif
static constexpr uint8_t kFixedPinCount = sizeof(kFixedPins) / sizeof(kFixedPins[0]);

static constexpr bool slotPinsAreSane() {
    for (uint8_t b = 0; b < kServoBoardCount; ++b) {
        for (uint8_t i = 0; i < kServoSlotCount; ++i) {
            for (uint8_t f = 0; f < kFixedPinCount; ++f) {
                if (kServoBoards[b].slots[i].pin == kFixedPins[f]) {
                    return false;
                }
            }
            for (uint8_t j = static_cast<uint8_t>(i + 1); j < kServoSlotCount; ++j) {
                if (kServoBoards[b].slots[i].pin == kServoBoards[b].slots[j].pin) {
                    return false;
                }
            }
        }
    }
    return true;
}
static_assert(slotPinsAreSane(),
              "config.h のスロットのピンが UART/SPI/CAN/RGB/DIP と衝突しているか、重複している"
              "（pin が 0 なら kServoBoards の要素の書き忘れでゼロ埋めされている）");

static constexpr bool fixedPinsAreUnique() {
    for (uint8_t i = 0; i < kFixedPinCount; ++i) {
        for (uint8_t j = static_cast<uint8_t>(i + 1); j < kFixedPinCount; ++j) {
            if (kFixedPins[i] == kFixedPins[j]) {
                return false;
            }
        }
    }
    return true;
}
static_assert(fixedPinsAreUnique(), "config.h の固定ピン（UART/SPI/CAN/RGB/DIP）が重複している");

#if defined(ARDUINO_ARCH_RENESAS)
static constexpr bool pinsAvoidCan() {
    for (uint8_t i = 0; i < kFixedPinCount; ++i) {
        if (kFixedPins[i] == PIN_CAN0_TX || kFixedPins[i] == PIN_CAN0_RX) {
            return false;
        }
    }
    for (uint8_t b = 0; b < kServoBoardCount; ++b) {
        for (uint8_t i = 0; i < kServoSlotCount; ++i) {
            if (kServoBoards[b].slots[i].pin == PIN_CAN0_TX ||
                kServoBoards[b].slots[i].pin == PIN_CAN0_RX) {
                return false;
            }
        }
    }
    return true;
}
static_assert(pinsAvoidCan(), "config.h のピンが CAN(D4/D5) と衝突している");
#endif

static_assert(kServoSlotCount <= motorcan::kMaxSlotNumber + 1,
              "スロット数がデバイス ID のスロット番号（3bit）に収まらない");

static_assert(kServoSlotCount <= motorcan::kMaxChannels,
              "スロット数が FrameRoute::channelMask のビット数を超えている");

static_assert(kDipBitCount == sizeof(kPinDip) / sizeof(kPinDip[0]),
              "kDipBitCount と kPinDip の要素数が一致していない");

static_assert(sizeof(kServoBoards) / sizeof(kServoBoards[0]) == kServoBoardCount,
              "kServoBoards の行数と kServoBoardCount が一致していない");

static constexpr bool boardNumbersAreUnique() {
    for (uint8_t i = 0; i < kServoBoardCount; ++i) {
        for (uint8_t j = static_cast<uint8_t>(i + 1); j < kServoBoardCount; ++j) {
            if (kServoBoards[i].boardNumber == kServoBoards[j].boardNumber) {
                return false;
            }
        }
    }
    return true;
}
static_assert(boardNumbersAreUnique(), "kServoBoards の boardNumber が重複している");

static constexpr bool boardNumbersFitInDeviceId() {
    for (uint8_t i = 0; i < kServoBoardCount; ++i) {
        if (kServoBoards[i].boardNumber > motorcan::kMaxBoardNumber) {
            return false;
        }
    }
    return true;
}
static_assert(boardNumbersFitInDeviceId(),
              "kServoBoards の boardNumber がデバイス ID の基板番号（3bit）に収まらない");

// ===========================================================================
// ペリフェラルと状態（すべてスロット単位）
// ===========================================================================

static bool g_canFailed = false;

static TxFailCounter g_txFail;

static Servo g_servo[kServoSlotCount];

static ServoChannel g_channel[kServoSlotCount];

static uint8_t g_deviceId[kServoSlotCount] = {};

static bool g_attached[kServoSlotCount] = {};

static bool g_sensorActive[kServoSlotCount] = {};

static uint16_t g_feedbackIntervalMs = kDefaultFeedbackIntervalMs;
static PeriodicTimer g_feedbackTimer[kServoSlotCount];

static PeriodicTimer g_motionTimer;
static PeriodicTimer g_infoTimer;

static uint8_t g_infoPendingSlot = kServoSlotCount;

static PeriodicTimer g_blinkTimer;
static bool g_ledOn = false;

#if HAS_RGB_LED
static Adafruit_NeoPixel g_strip(1, kPinRgb, NEO_GRB + NEO_KHZ800);
#endif

#if ENABLE_SERIAL_DEBUG
static SerialOverride g_serialOverride;
static char g_serialStorage[kSerialLineCapacity];
static SerialLineBuffer g_serialLine(g_serialStorage, sizeof(g_serialStorage));
#endif

// ===========================================================================
// スロット設定（実行時に選ばれた 1 行）
// ===========================================================================

static const ServoSlotConfig *g_slots = nullptr;

static SlotRole slotRole(uint8_t slot) {
    return g_slots == nullptr ? SlotRole::Unused : g_slots[slot].role;
}

static bool isServoSlot(uint8_t slot) { return slotRole(slot) == SlotRole::Servo; }
static bool isSensorSlot(uint8_t slot) { return slotRole(slot) == SlotRole::TouchSensor; }

static bool isDeviceSlot(uint8_t slot) { return slotRole(slot) != SlotRole::Unused; }

static bool isSlotConfigured(uint8_t slot) {
    return isDeviceSlot(slot) && g_deviceId[slot] != kDeviceIdUnconfigured;
}

// ===========================================================================
// センサ
// ===========================================================================

static void readSensors() {
    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        if (!isSensorSlot(slot)) {
            continue;
        }
        const int level = digitalRead(g_slots[slot].pin);
        g_sensorActive[slot] =
            g_slots[slot].sensorActiveLow ? (level == LOW) : (level == HIGH);
    }
}

// ===========================================================================
// 出力
// ===========================================================================

static void applyChannelOutput(uint8_t slot, uint32_t nowMs) {
    if (!isServoSlot(slot) || !isSlotConfigured(slot) || !g_attached[slot]) {
        return;
    }

    if (kEStopDetach && !g_channel[slot].isOutputAllowed(nowMs)) {
        if (g_servo[slot].attached()) {
            g_servo[slot].detach();
        }
        return;
    }
    if (kEStopDetach && !g_servo[slot].attached()) {
        g_servo[slot].attach(g_slots[slot].pin, g_slots[slot].pulse.minUs,
                             g_slots[slot].pulse.maxUs);
    }

    const uint16_t pulseUs =
        angleToPulseUs(g_channel[slot].currentAngleDeg(), g_slots[slot].pulse);
    g_servo[slot].writeMicroseconds(static_cast<int>(pulseUs));
}

// ===========================================================================
// 補間
// ===========================================================================

static void updateMotion(uint32_t nowMs) {
    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        if (!isServoSlot(slot)) {
            continue;
        }
        g_channel[slot].tick(nowMs);
        applyChannelOutput(slot, nowMs);
    }
}

// ===========================================================================
// CAN
// ===========================================================================

static uint8_t buildStatusFlags(uint8_t slot, uint32_t nowMs) {
    const SlotKind kind = isSensorSlot(slot) ? SlotKind::Sensor : SlotKind::Actuator;
    return composeFeedbackFlags(kBoardKind, kind, g_channel[slot].safetyStatusFlags(nowMs),
                                isSlotConfigured(slot), g_channel[slot].isReached(),
                                g_sensorActive[slot]);
}

static bool sendFrame(uint16_t canId, uint8_t length, const uint8_t *data) {
    if (servo_can::send(canId, length, data)) {
        g_txFail.onSuccess();
        return true;
    }
    g_txFail.onFailure();
    return false;
}

static void sendFeedback(uint8_t slot, uint32_t nowMs) {
    uint8_t data[kFeedbackWithPositionLength];
    const uint8_t flags = buildStatusFlags(slot, nowMs);

    const uint8_t len =
        isSensorSlot(slot)
            ? encodeFeedback(data, flags)
            : encodeFeedback(data, flags,
                             static_cast<int32_t>(
                                 lroundf(g_channel[slot].currentAngleDeg() * kAngleScale)));

    sendFrame(buildCanId(CommandType::Feedback, g_deviceId[slot]), len, data);
}

static bool sendInfo(uint8_t slot) {
    uint8_t data[kInfoWithServoRangeLength];
    if (isServoSlot(slot)) {
        const uint8_t len = encodeInfo(data, kFirmwareVersion, kBoardKind, SlotKind::Actuator,
                                       g_slots[slot].pulse.angleRangeDeg);
        return sendFrame(buildCanId(CommandType::Info, g_deviceId[slot]), len, data);
    }

    const SlotKind kind = isSensorSlot(slot) ? SlotKind::Sensor : SlotKind::Actuator;
    const uint8_t len = encodeInfo(data, kFirmwareVersion, kBoardKind, kind);
    return sendFrame(buildCanId(CommandType::Info, g_deviceId[slot]), len, data);
}

static void applyParam(uint8_t slot, const SetParamCommand &cmd, uint32_t nowMs) {
    if (applyCommonParam(cmd, g_channel[slot], g_feedbackIntervalMs)) {
        return;
    }
    switch (cmd.id) {
        case ParamId::CommandTimeoutMs:
        case ParamId::FeedbackIntervalMs:
            break;
        case ParamId::ReachedTolerance:
            g_channel[slot].setReachedToleranceDeg(fromRaw(cmd.raw, kAngleScale), nowMs);
            break;
        case ParamId::SlewRate: {
            ServoLimits limits = g_channel[slot].limits();
            limits.slewRateDegPerSec = fromRaw(cmd.raw, kRateScale);
            g_channel[slot].setLimits(limits, nowMs);
            break;
        }
        case ParamId::AngleMin: {
            ServoLimits limits = g_channel[slot].limits();
            limits.angleMinDeg = fromRaw(cmd.raw, kAngleScale);
            g_channel[slot].setLimits(limits, nowMs);
            break;
        }
        case ParamId::AngleMax: {
            ServoLimits limits = g_channel[slot].limits();
            limits.angleMaxDeg = fromRaw(cmd.raw, kAngleScale);
            g_channel[slot].setLimits(limits, nowMs);
            break;
        }
        case ParamId::MaxDuty:
            break;
    }
}

static void handleSlotFrame(uint8_t slot, CommandType command, const uint8_t *data, uint8_t len,
                            uint32_t nowMs) {
    if (!isServoSlot(slot)) {
        return;
    }

    switch (command) {
        case CommandType::SetTarget: {
            g_channel[slot].feed(nowMs);

            const SetTargetCommand cmd = decodeSetTarget(data, len);
            if (!cmd.valid) {
                return;
            }
#if ENABLE_SERIAL_DEBUG
            g_serialOverride.clear();
#endif
            g_channel[slot].applySetTarget(cmd, nowMs);
            break;
        }
        case CommandType::SetParam: {
            const SetParamCommand cmd = decodeSetParam(data, len);
            if (cmd.valid) {
                applyParam(slot, cmd, nowMs);
            }
            break;
        }
        case CommandType::EStop: {
            const EStopAction action = g_channel[slot].handleEStopFrame(data, len, nowMs);
            if (action != EStopAction::None) {
                applyChannelOutput(slot, nowMs);
#if ENABLE_SERIAL_DEBUG
                g_serialOverride.clear();
#endif
            }
            break;
        }
        case CommandType::Feedback:
        case CommandType::Info:
            break;
    }
}

static void handleFrame(uint16_t canId, bool standard, const uint8_t *data, uint8_t len) {
    const FrameRoute route = routeFrame(canId, standard, g_deviceId, kServoSlotCount);
    if (!route.accepted) {
        return;
    }

    const uint32_t nowMs = millis();
    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        if ((route.channelMask & static_cast<uint8_t>(1u << slot)) != 0) {
            handleSlotFrame(slot, route.command, data, len, nowMs);
        }
    }
}

// ===========================================================================
// スロット設定とデバイス ID（どちらも DIP の基板番号で決まる）
// ===========================================================================

static void resolveSlotsAndDeviceIds() {
    const uint8_t boardNumber = readDipSwitch(
        kPinDip, kDipBitCount, [](uint8_t pin) { return static_cast<int>(digitalRead(pin)); },
        LOW);
    for (uint8_t b = 0; b < kServoBoardCount; ++b) {
        if (kServoBoards[b].boardNumber == boardNumber) {
            g_slots = kServoBoards[b].slots;
            break;
        }
    }
    motorcan::resolveDeviceIds(g_deviceId, kServoSlotCount, kBoardKind, boardNumber,
                               isDeviceSlot);
}

// ===========================================================================
// LED
// ===========================================================================

static void updateLed(uint32_t nowMs) {
    BoardIndication indication(g_canFailed || g_txFail.isAlarming(kCanTxFailStreakAlarm));
    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        if (!isDeviceSlot(slot)) {
            continue;
        }
        indication.observe(
            isSlotConfigured(slot),
            isServoSlot(slot) &&
                (g_channel[slot].safetyStatusFlags(nowMs) & status_flag::kEStop) != 0);
    }

    const uint32_t interval = blinkIntervalFor(indication, kUnconfiguredBlinkIntervalMs,
                                               kHeartbeatIntervalMs, kHeartbeatIntervalMs);
    if (!g_blinkTimer.due(nowMs, interval)) {
        return;
    }
    g_ledOn = !g_ledOn;

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

    static uint8_t lastR = 0xFF;
    static uint8_t lastG = 0xFF;
    static uint8_t lastB = 0xFF;
    if (r != lastR || g != lastG || b != lastB) {
        lastR = r;
        lastG = g;
        lastB = b;
        g_strip.setPixelColor(0, g_strip.Color(r, g, b));
        g_strip.show();
    }
#endif
}

// ===========================================================================
// デバッグ用シリアル
// ===========================================================================

#if ENABLE_SERIAL_DEBUG

static void pollSerial(uint32_t nowMs) {
    while (Serial.available() > 0) {
        if (!g_serialLine.push(static_cast<char>(Serial.read()))) {
            continue;
        }
        const SerialCommand cmd = parseSerialCommand(g_serialLine.line(), kServoSlotCount);
        if (cmd.kind == SerialCommand::Kind::StopAll) {
            g_serialOverride.clear();
            for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
                if (isServoSlot(slot)) {
                    g_channel[slot].hold(nowMs);
                }
            }
        } else if (cmd.kind == SerialCommand::Kind::Channel && isServoSlot(cmd.channel)) {
            const float deg = fromRaw(
                // avr-libc に strtof は無い（AVR の double は 32bit float なので strtod で足りる）。
                toRaw(static_cast<float>(strtod(cmd.value, nullptr)), kAngleScale), kAngleScale);
            g_channel[cmd.channel].setTarget(deg, nowMs);
            g_serialOverride.note(cmd.channel, nowMs);
        }
    }

    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        if (isServoSlot(slot) && g_serialOverride.shouldFeed(slot, nowMs)) {
            g_channel[slot].feed(nowMs);
        }
    }
}

#endif

// ===========================================================================
// setup / loop
// ===========================================================================

void setup() {
#if ENABLE_SERIAL_DEBUG
    Serial.begin(kSerialBaud);
#endif

    for (uint8_t bit = 0; bit < kDipBitCount; ++bit) {
        pinMode(kPinDip[bit], INPUT_PULLUP);
    }

#if HAS_RGB_LED
    g_strip.begin();
    g_strip.setBrightness(kRgbBrightness);
#endif

    resolveSlotsAndDeviceIds();

    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        if (isServoSlot(slot)) {
            g_channel[slot].begin(g_slots[slot].initialAngleDeg, g_slots[slot].limits,
                                  kDefaultCommandTimeoutMs);
        }
    }

    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        if (isSensorSlot(slot)) {
            pinMode(g_slots[slot].pin, INPUT_PULLUP);
        }
    }

    const uint32_t startMs = millis();

    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        g_feedbackTimer[slot].stagger(startMs, g_feedbackIntervalMs, slot, kServoSlotCount);

        if (!isServoSlot(slot) || !isSlotConfigured(slot)) {
            continue;
        }
        g_servo[slot].attach(g_slots[slot].pin, g_slots[slot].pulse.minUs,
                             g_slots[slot].pulse.maxUs);
        g_attached[slot] = g_servo[slot].attached();
        if (g_attached[slot]) {
            g_servo[slot].writeMicroseconds(static_cast<int>(
                angleToPulseUs(g_channel[slot].currentAngleDeg(), g_slots[slot].pulse)));
        }
    }

    if (!servo_can::begin()) {
        g_canFailed = true;
        for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
            g_channel[slot].stop(startMs);
        }
    }

    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        g_channel[slot].setWatchdogEnabled(WATCHDOG_ENABLED != 0);
    }

    readSensors();
    g_motionTimer.reset(startMs);
    g_infoTimer.reset(startMs);
    g_blinkTimer.reset(startMs);
}

void loop() {
    const uint32_t nowMs = millis();

    servo_can::poll(handleFrame);
#if ENABLE_SERIAL_DEBUG
    pollSerial(nowMs);
#endif

    if (g_motionTimer.due(nowMs, kMotionIntervalMs)) {
        readSensors();
        updateMotion(nowMs);
    }

    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        if (isSlotConfigured(slot) && g_feedbackTimer[slot].due(nowMs, g_feedbackIntervalMs)) {
            sendFeedback(slot, nowMs);
        }
    }

    if (g_infoTimer.due(nowMs, kInfoIntervalMs)) {
        g_infoPendingSlot = 0;
    }
    if (g_infoPendingSlot < kServoSlotCount) {
        if (!isSlotConfigured(g_infoPendingSlot)) {
            ++g_infoPendingSlot;
        } else if (sendInfo(g_infoPendingSlot)) {
            ++g_infoPendingSlot;
        }
    }

    updateLed(nowMs);
}
