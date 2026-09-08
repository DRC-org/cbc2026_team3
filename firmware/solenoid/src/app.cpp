#include "app.h"

#include <stdlib.h>

#include "MotorCanProtocol.h"
#include "MotorCanRouter.h"
#include "MotorLoopTimer.h"
#include "MotorPinTable.h"
#include "MotorTxHealth.h"
#include "SerialLineBuffer.h"
#include "SerialOverride.h"
#include "SolenoidChannel.h"
#include "config.h"
#include "main.h"

extern "C" {
extern CAN_HandleTypeDef hcan;
extern UART_HandleTypeDef huart1;
}

using namespace motorcan;

namespace {

// ===========================================================================
// config.h の妥当性検査（ビルド時）
// ===========================================================================

struct PinRef {
    Port port;
    uint16_t pin;
};

constexpr PinRef kAllPins[] = {
    {kSolenoidChannels[0].port, kSolenoidChannels[0].pin},
    {kSolenoidChannels[1].port, kSolenoidChannels[1].pin},
    {kSolenoidChannels[2].port, kSolenoidChannels[2].pin},
    {kSolenoidChannels[3].port, kSolenoidChannels[3].pin},
    {kSolenoidChannels[4].port, kSolenoidChannels[4].pin},
    {kSolenoidChannels[5].port, kSolenoidChannels[5].pin},
    {kPortLed, kPinLed},
    {kDipPorts[0], kDipPins[0]},
    {kDipPorts[1], kDipPins[1]},
    {kDipPorts[2], kDipPins[2]},
    {kDipPorts[3], kDipPins[3]},
    {kPortCanRx, kPinCanRx},
    {kPortCanTx, kPinCanTx},
    {kPortUartTx, kPinUartTx},
    {kPortUartRx, kPinUartRx},
};

constexpr uint8_t kAllPinCount = sizeof(kAllPins) / sizeof(kAllPins[0]);

constexpr bool pinsAreUnique() {
    for (uint8_t i = 0; i < kAllPinCount; ++i) {
        for (uint8_t j = static_cast<uint8_t>(i + 1); j < kAllPinCount; ++j) {
            if (kAllPins[i].port == kAllPins[j].port && kAllPins[i].pin == kAllPins[j].pin) {
                return false;
            }
        }
    }
    return true;
}

static_assert(pinsAreUnique(), "config.h のピンが重複している（CAN / UART / LED / DIP 含む）");

static_assert(kSolenoidChannelCount == 6,
              "チャンネル数を変えたら kAllPins / g_channel の初期化子も更新すること");
static_assert(kSolenoidChannelCount <= motorcan::kMaxSlotNumber + 1,
              "チャンネル数がデバイス ID のスロット幅（3bit）を超えている");
static_assert(kSolenoidChannelCount <= motorcan::kMaxChannels,
              "チャンネル数が FrameRoute の channelMask（8bit）を超えている");
static_assert(kDipBitCount == sizeof(kDipPins) / sizeof(kDipPins[0]),
              "kDipBitCount と kDipPins の数が食い違っている");
static_assert(kDipBitCount == sizeof(kDipPorts) / sizeof(kDipPorts[0]),
              "kDipBitCount と kDipPorts の数が食い違っている");

static_assert(kSolenoidChannels[0].pin == PUMP1_SW_Pin, "ch0 が PUMP1_SW と食い違っている");
static_assert(kSolenoidChannels[1].pin == PUMP2_SW_Pin, "ch1 が PUMP2_SW と食い違っている");
static_assert(kSolenoidChannels[2].pin == PUMP3_SW_Pin, "ch2 が PUMP3_SW と食い違っている");
static_assert(kSolenoidChannels[3].pin == PUMP4_SW_Pin, "ch3 が PUMP4_SW と食い違っている");
static_assert(kSolenoidChannels[4].pin == PUMP5_SW_Pin, "ch4 が PUMP5_SW と食い違っている");
static_assert(kSolenoidChannels[5].pin == PUMP6_SW_Pin, "ch5 が PUMP6_SW と食い違っている");
static_assert(kPinLed == LED_BI_Pin, "LED が main.h と食い違っている");
static_assert(kDipPins[0] == DIP1_Pin, "DIP1 が main.h と食い違っている");
static_assert(kDipPins[1] == DIP2_Pin, "DIP2 が main.h と食い違っている");
static_assert(kDipPins[2] == DIP3_Pin, "DIP3 が main.h と食い違っている");
static_assert(kDipPins[3] == DIP4_Pin, "DIP4 が main.h と食い違っている");

// ===========================================================================
// 状態
// ===========================================================================

SolenoidChannel g_channel[kSolenoidChannelCount] = {
    SolenoidChannel(kDefaultCommandTimeoutMs), SolenoidChannel(kDefaultCommandTimeoutMs),
    SolenoidChannel(kDefaultCommandTimeoutMs), SolenoidChannel(kDefaultCommandTimeoutMs),
    SolenoidChannel(kDefaultCommandTimeoutMs), SolenoidChannel(kDefaultCommandTimeoutMs),
};

uint8_t g_deviceId[kSolenoidChannelCount] = {0};
PeriodicTimer g_feedbackTimer[kSolenoidChannelCount];
PeriodicTimer g_infoTimer;

uint8_t g_infoPendingCh = kSolenoidChannelCount;

PeriodicTimer g_blinkTimer;

uint16_t g_feedbackIntervalMs = kDefaultFeedbackIntervalMs;

bool g_canFailed = false;

bool g_configMismatch = false;

bool g_ledOn = false;

TxFailCounter g_txFail;

#if ENABLE_SERIAL_DEBUG
char g_serialStorage[kSerialLineCapacity];
SerialLineBuffer g_serialLine(g_serialStorage, sizeof(g_serialStorage));
SerialOverride g_serialOverride;
#endif

// ===========================================================================
// GPIO
// ===========================================================================

GPIO_TypeDef *portOf(Port port) { return port == Port::A ? GPIOA : GPIOB; }

uint8_t portIndexOf(GPIO_TypeDef *port) {
    if (port == GPIOA) {
        return static_cast<uint8_t>(Port::A);
    }
    if (port == GPIOB) {
        return static_cast<uint8_t>(Port::B);
    }
    return kPortIndexUnknown;
}

// 上の static_assert 群はピン番号（`*_Pin`）しか突き合わせていない。ポートは HAL の
// GPIOA / GPIOB が constexpr 文脈に持ち込めないため、ここで実行時に照合する。
bool portsMatchCubeMx() {
    constexpr PortPin actual[] = {
        {static_cast<uint8_t>(kSolenoidChannels[0].port), kSolenoidChannels[0].pin},
        {static_cast<uint8_t>(kSolenoidChannels[1].port), kSolenoidChannels[1].pin},
        {static_cast<uint8_t>(kSolenoidChannels[2].port), kSolenoidChannels[2].pin},
        {static_cast<uint8_t>(kSolenoidChannels[3].port), kSolenoidChannels[3].pin},
        {static_cast<uint8_t>(kSolenoidChannels[4].port), kSolenoidChannels[4].pin},
        {static_cast<uint8_t>(kSolenoidChannels[5].port), kSolenoidChannels[5].pin},
        {static_cast<uint8_t>(kPortLed), kPinLed},
        {static_cast<uint8_t>(kDipPorts[0]), kDipPins[0]},
        {static_cast<uint8_t>(kDipPorts[1]), kDipPins[1]},
        {static_cast<uint8_t>(kDipPorts[2]), kDipPins[2]},
        {static_cast<uint8_t>(kDipPorts[3]), kDipPins[3]},
    };
    const PortPin expected[] = {
        {portIndexOf(PUMP1_SW_GPIO_Port), PUMP1_SW_Pin},
        {portIndexOf(PUMP2_SW_GPIO_Port), PUMP2_SW_Pin},
        {portIndexOf(PUMP3_SW_GPIO_Port), PUMP3_SW_Pin},
        {portIndexOf(PUMP4_SW_GPIO_Port), PUMP4_SW_Pin},
        {portIndexOf(PUMP5_SW_GPIO_Port), PUMP5_SW_Pin},
        {portIndexOf(PUMP6_SW_GPIO_Port), PUMP6_SW_Pin},
        {portIndexOf(LED_BI_GPIO_Port), LED_BI_Pin},
        {portIndexOf(DIP1_GPIO_Port), DIP1_Pin},
        {portIndexOf(DIP2_GPIO_Port), DIP2_Pin},
        {portIndexOf(DIP3_GPIO_Port), DIP3_Pin},
        {portIndexOf(DIP4_GPIO_Port), DIP4_Pin},
    };
    constexpr uint8_t kCheckedPinCount = sizeof(actual) / sizeof(actual[0]);
    static_assert(sizeof(expected) / sizeof(expected[0]) == kCheckedPinCount,
                  "config.h 側と main.h 側の行数が食い違っている（1 対 1 に並べること）");
    static_assert(kCheckedPinCount == kSolenoidChannelCount + 1 + kDipBitCount,
                  "照合していないピンがある（チャンネル + LED + DIP の全部を並べること）");

    return pinTablesMatch(actual, expected, kCheckedPinCount);
}

void writeLed(bool on) {
    HAL_GPIO_WritePin(LED_BI_GPIO_Port, LED_BI_Pin, on ? GPIO_PIN_SET : GPIO_PIN_RESET);
}

bool isChannelConfigured(uint8_t ch) { return g_deviceId[ch] != kDeviceIdUnconfigured; }

void applyChannelOutput(uint8_t ch, uint32_t nowMs) {
    if (!isChannelConfigured(ch)) {
        return;
    }
    g_channel[ch].tick(nowMs);

    const bool on = g_channel[ch].outputOn(nowMs);
    HAL_GPIO_WritePin(portOf(kSolenoidChannels[ch].port), kSolenoidChannels[ch].pin,
                      on ? GPIO_PIN_SET : GPIO_PIN_RESET);
}

void applyAllOutputs(uint32_t nowMs) {
    for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
        applyChannelOutput(ch, nowMs);
    }
}

// ===========================================================================
// CAN
// ===========================================================================

uint8_t buildStatusFlags(uint8_t ch, uint32_t nowMs) {
    return composeFeedbackFlags(kBoardKind, SlotKind::Actuator,
                                g_channel[ch].safetyStatusFlags(nowMs), isChannelConfigured(ch),
                                /*reached=*/false, /*sensorActive=*/false);
}

bool sendFrame(uint16_t canId, uint8_t *data, uint8_t length) {
    if (HAL_CAN_GetTxMailboxesFreeLevel(&hcan) == 0) {
        g_txFail.onFailure();
        return false;
    }
    CAN_TxHeaderTypeDef header{};
    header.StdId = canId;
    header.ExtId = 0;
    header.IDE = CAN_ID_STD;
    header.RTR = CAN_RTR_DATA;
    header.DLC = length;
    header.TransmitGlobalTime = DISABLE;

    uint32_t mailbox = 0;
    if (HAL_CAN_AddTxMessage(&hcan, &header, data, &mailbox) != HAL_OK) {
        g_txFail.onFailure();
        return false;
    }
    g_txFail.onSuccess();
    return true;
}

void sendFeedback(uint8_t ch, uint32_t nowMs) {
    uint8_t data[kFeedbackFlagsOnlyLength];
    const uint8_t len = encodeFeedback(data, buildStatusFlags(ch, nowMs));

    sendFrame(buildCanId(CommandType::Feedback, g_deviceId[ch]), data, len);
}

bool sendInfo(uint8_t ch) {
    uint8_t data[kInfoBaseLength];
    const uint8_t len = encodeInfo(data, kFirmwareVersion, kBoardKind, SlotKind::Actuator);
    return sendFrame(buildCanId(CommandType::Info, g_deviceId[ch]), data, len);
}

void applyParam(uint8_t ch, const SetParamCommand &cmd) {
    if (applyCommonParam(cmd, g_channel[ch], g_feedbackIntervalMs)) {
        return;
    }
    switch (cmd.id) {
        case ParamId::CommandTimeoutMs:
        case ParamId::FeedbackIntervalMs:
            break;
        case ParamId::MaxDuty:
        case ParamId::ReachedTolerance:
        case ParamId::SlewRate:
        case ParamId::AngleMin:
        case ParamId::AngleMax:
            break;
    }
}

void handleChannelFrame(uint8_t ch, CommandType command, const uint8_t *data, uint8_t length,
                        uint32_t nowMs) {
    switch (command) {
        case CommandType::SetTarget: {
            g_channel[ch].feed(nowMs);

            const SetTargetCommand cmd = decodeSetTarget(data, length);
            if (!cmd.valid) {
                return;
            }
#if ENABLE_SERIAL_DEBUG
            g_serialOverride.clear();
#endif
            g_channel[ch].applySetTarget(cmd, nowMs);
            break;
        }
        case CommandType::SetParam: {
            const SetParamCommand cmd = decodeSetParam(data, length);
            if (cmd.valid) {
                applyParam(ch, cmd);
            }
            break;
        }
        case CommandType::EStop: {
            const EStopAction action = g_channel[ch].handleEStopFrame(data, length);
            if (action != EStopAction::None) {
                applyChannelOutput(ch, nowMs);
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

void handleFrame(uint16_t canId, bool isStandardId, const uint8_t *data, uint8_t length) {
    const FrameRoute route = routeFrame(canId, isStandardId, g_deviceId, kSolenoidChannelCount);
    if (!route.accepted) {
        return;
    }

    const uint32_t nowMs = HAL_GetTick();
    for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
        if ((route.channelMask & static_cast<uint8_t>(1u << ch)) != 0) {
            handleChannelFrame(ch, route.command, data, length, nowMs);
        }
    }
}

void pollCan() {
    CAN_RxHeaderTypeDef header{};
    uint8_t data[8] = {0};

    while (HAL_CAN_GetRxFifoFillLevel(&hcan, CAN_RX_FIFO0) > 0) {
        if (HAL_CAN_GetRxMessage(&hcan, CAN_RX_FIFO0, &header, data) != HAL_OK) {
            return;
        }
        handleFrame(static_cast<uint16_t>(header.StdId), header.IDE == CAN_ID_STD, data,
                    static_cast<uint8_t>(header.DLC));
    }
}

constexpr uint16_t kStdIdShiftInFilterReg = 5;

constexpr uint16_t toFilterReg(uint16_t stdId) {
    return static_cast<uint16_t>(stdId << kStdIdShiftInFilterReg);
}

bool configureCanFilters() {
    CAN_FilterTypeDef filter{};
    filter.FilterMode = CAN_FILTERMODE_IDMASK;
    filter.FilterScale = CAN_FILTERSCALE_32BIT;
    filter.FilterFIFOAssignment = CAN_FILTER_FIFO0;
    filter.FilterActivation = CAN_FILTER_ENABLE;
    filter.SlaveStartFilterBank = 14;

    filter.FilterBank = 0;
    filter.FilterIdHigh = toFilterReg(kEStopAndSetTargetFilter.id);
    filter.FilterIdLow = 0;
    filter.FilterMaskIdHigh = toFilterReg(kEStopAndSetTargetFilter.mask);
    filter.FilterMaskIdLow = 0;
    if (HAL_CAN_ConfigFilter(&hcan, &filter) != HAL_OK) {
        return false;
    }

    filter.FilterBank = 1;
    filter.FilterIdHigh = toFilterReg(kSetParamFilter.id);
    filter.FilterMaskIdHigh = toFilterReg(kSetParamFilter.mask);
    return HAL_CAN_ConfigFilter(&hcan, &filter) == HAL_OK;
}

// ===========================================================================
// デバイス ID（DIP スイッチ = チャンネル表全体へのブロックオフセット）
// ===========================================================================

const uint8_t kDipIndices[kDipBitCount] = {0, 1, 2, 3};
static_assert(kDipBitCount == 4, "kDipBitCount を変えたら kDipIndices も更新すること");

void resolveDeviceIds() {
    const uint8_t boardNumber = readDipSwitch(
        kDipIndices, kDipBitCount,
        [](uint8_t index) {
            return static_cast<int>(HAL_GPIO_ReadPin(portOf(kDipPorts[index]), kDipPins[index]));
        },
        kDipActiveLevel);

    motorcan::resolveDeviceIds(g_deviceId, kSolenoidChannelCount, kBoardKind, boardNumber,
                               nullptr);
}

// ===========================================================================
// LED
// ===========================================================================

void updateLed(uint32_t nowMs) {
#if HAS_STATUS_LED
    BoardIndication indication(g_canFailed || g_configMismatch ||
                               g_txFail.isAlarming(kCanTxFailStreakAlarm));
    for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
        indication.observe(isChannelConfigured(ch),
                           (g_channel[ch].safetyStatusFlags(nowMs) & status_flag::kEStop) != 0);
    }

    const uint32_t interval = blinkIntervalFor(indication, kUnconfiguredBlinkIntervalMs,
                                               kEStopBlinkIntervalMs, kHeartbeatIntervalMs);

    if (!g_blinkTimer.due(nowMs, interval)) {
        return;
    }
    g_ledOn = !g_ledOn;
    writeLed(g_ledOn);
#else
    (void)nowMs;
#endif
}

// ===========================================================================
// デバッグ用シリアル
// ===========================================================================

#if ENABLE_SERIAL_DEBUG

void pollSerial(uint32_t nowMs) {
    if (__HAL_UART_GET_FLAG(&huart1, UART_FLAG_ORE)) {
        __HAL_UART_CLEAR_OREFLAG(&huart1);
    }

    while (__HAL_UART_GET_FLAG(&huart1, UART_FLAG_RXNE)) {
        const char c = static_cast<char>(huart1.Instance->RDR & 0xFF);
        if (!g_serialLine.push(c)) {
            continue;
        }
        const SerialCommand cmd = parseSerialCommand(g_serialLine.line(), kSolenoidChannelCount);
        if (cmd.kind == SerialCommand::Kind::StopAll) {
            g_serialOverride.clear();
            for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
                g_channel[ch].hold();
            }
        } else if (cmd.kind == SerialCommand::Kind::Channel) {
            g_channel[cmd.channel].setOn(strtol(cmd.value, nullptr, 10) != 0, nowMs);
            g_serialOverride.note(cmd.channel, nowMs);
        }
    }

    for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
        if (g_serialOverride.shouldFeed(ch, nowMs)) {
            g_channel[ch].feed(nowMs);
        }
    }
}

#endif

}

// ===========================================================================
// setup / loop
// ===========================================================================

extern "C" void setup() {
    g_configMismatch = !portsMatchCubeMx();

    if (!g_configMismatch) {
        for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
            HAL_GPIO_WritePin(portOf(kSolenoidChannels[ch].port), kSolenoidChannels[ch].pin,
                              GPIO_PIN_RESET);
        }
    }
    writeLed(false);

    if (!g_configMismatch) {
        resolveDeviceIds();
    }

    const uint32_t startMs = HAL_GetTick();

    for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
        g_feedbackTimer[ch].stagger(startMs, g_feedbackIntervalMs, ch, kSolenoidChannelCount);
    }

    if (!configureCanFilters() || HAL_CAN_Start(&hcan) != HAL_OK) {
        g_canFailed = true;
        for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
            g_channel[ch].stop();
        }
    }

    for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
        g_channel[ch].setWatchdogEnabled(WATCHDOG_ENABLED != 0);
    }

    g_infoTimer.reset(startMs);
    g_blinkTimer.reset(startMs);

    applyAllOutputs(startMs);
}

extern "C" void loop() {
    const uint32_t nowMs = HAL_GetTick();

    pollCan();
#if ENABLE_SERIAL_DEBUG
    pollSerial(nowMs);
#endif

    applyAllOutputs(nowMs);

    for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
        if (isChannelConfigured(ch) && g_feedbackTimer[ch].due(nowMs, g_feedbackIntervalMs)) {
            sendFeedback(ch, nowMs);
        }
    }

    if (g_infoTimer.due(nowMs, kInfoIntervalMs)) {
        g_infoPendingCh = 0;
    }
    if (g_infoPendingCh < kSolenoidChannelCount) {
        if (!isChannelConfigured(g_infoPendingCh)) {
            ++g_infoPendingCh;
        } else if (sendInfo(g_infoPendingCh)) {
            ++g_infoPendingCh;
        }
    }

    updateLed(nowMs);
}
