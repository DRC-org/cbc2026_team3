#pragma once

#include <stdint.h>

namespace motorcan {

enum class CommandType : uint8_t {
    EStop = 0,
    SetTarget = 1,
    SetParam = 2,
    Feedback = 3,
    Info = 4,
};

enum class ControlType : uint8_t {
    Position = 0,
    Velocity = 1,
    Duty = 2,
    OnOff = 3,
};

enum class ParamId : uint8_t {
    MaxDuty = 0x00,
    CommandTimeoutMs = 0x01,
    FeedbackIntervalMs = 0x02,
    ReachedTolerance = 0x03,
    SlewRate = 0x04,
    AngleMin = 0x05,
    AngleMax = 0x06,
};

namespace status_flag {
constexpr uint8_t kReached = 1 << 0;
constexpr uint8_t kEStop = 1 << 1;
constexpr uint8_t kWatchdog = 1 << 2;
constexpr uint8_t kDeviceIdUnconfigured = 1 << 3;
constexpr uint8_t kSensor = 1 << 4;
constexpr uint8_t kNeverCommanded = 1 << 5;
}

enum class BoardKind : uint8_t {
    Servo = 1,
    Dc = 2,
    Solenoid = 3,
};

constexpr uint8_t kBoardKindShift = 6;
constexpr uint8_t kBoardNumberShift = 3;
constexpr uint8_t kMaxBoardNumber = 7;
constexpr uint8_t kMaxSlotNumber = 7;

enum class SlotKind : uint8_t {
    Actuator = 0,
    Sensor = 1,
};

constexpr uint8_t kDeviceIdUnconfigured = 0x00;
constexpr uint8_t kDeviceIdBroadcast = 0xFF;
constexpr uint16_t kBroadcastEStopCanId = 0x0FF;

uint8_t makeDeviceId(BoardKind board, uint8_t boardNumber, uint8_t slot);

constexpr int32_t kAngleScale = 10;
constexpr int32_t kDutyScale = 10000;
constexpr int32_t kRateScale = 10;

int16_t toRaw(float value, int32_t scale);
float fromRaw(int16_t raw, int32_t scale);

int16_t saturateToInt16(int32_t value);

constexpr uint8_t kCommandTypeShift = 8;
constexpr uint16_t kCommandTypeMask = static_cast<uint16_t>(0x7u << kCommandTypeShift);

constexpr uint16_t commandIdBase(CommandType command) {
    return static_cast<uint16_t>(static_cast<uint16_t>(command) << kCommandTypeShift);
}

uint16_t buildCanId(CommandType command, uint8_t deviceId);

// マスクをレジスタへどう載せるかは MCU 固有（bxCAN の 32bit スケールでは STID が
// FilterIdHigh の bit15..5）なので、そこは呼び出し側に残す。
struct CanIdFilter {
    uint16_t id;
    uint16_t mask;
};

constexpr bool canIdPassesFilter(const CanIdFilter &filter, uint16_t canId) {
    return (canId & filter.mask) == filter.id;
}

constexpr CanIdFilter kEStopAndSetTargetFilter{
    commandIdBase(CommandType::EStop),
    static_cast<uint16_t>(kCommandTypeMask & ~(commandIdBase(CommandType::EStop) ^
                                               commandIdBase(CommandType::SetTarget)))};

constexpr CanIdFilter kSetParamFilter{commandIdBase(CommandType::SetParam), kCommandTypeMask};

constexpr bool passesPcToBoardFilters(uint16_t canId) {
    return canIdPassesFilter(kEStopAndSetTargetFilter, canId) ||
           canIdPassesFilter(kSetParamFilter, canId);
}

static_assert(passesPcToBoardFilters(commandIdBase(CommandType::EStop)),
              "E_STOP が受信フィルタを通らない（電磁弁基板だけ緊急停止が効かなくなる）");
static_assert(passesPcToBoardFilters(commandIdBase(CommandType::SetTarget)),
              "SET_TARGET が受信フィルタを通らない");
static_assert(passesPcToBoardFilters(commandIdBase(CommandType::SetParam)),
              "SET_PARAM が受信フィルタを通らない");
static_assert(!passesPcToBoardFilters(commandIdBase(CommandType::Feedback)),
              "FEEDBACK が受信フィルタを通ってしまう");
static_assert(!passesPcToBoardFilters(commandIdBase(CommandType::Info)),
              "INFO が受信フィルタを通ってしまう");
static_assert(!passesPcToBoardFilters(static_cast<uint16_t>(0x5u << kCommandTypeShift)) &&
                  !passesPcToBoardFilters(static_cast<uint16_t>(0x6u << kCommandTypeShift)) &&
                  !passesPcToBoardFilters(static_cast<uint16_t>(0x7u << kCommandTypeShift)),
              "予約コマンド種別が受信フィルタを通ってしまう");

struct CanIdInfo {
    CommandType command;
    uint8_t deviceId;
    bool valid;
};

CanIdInfo parseCanId(uint16_t canId);

void packInt16Le(uint8_t *dst, int16_t value);
int16_t unpackInt16Le(const uint8_t *src);

struct SetTargetCommand {
    ControlType type;
    int16_t raw;
    bool valid;
};
SetTargetCommand decodeSetTarget(const uint8_t *data, uint8_t length);

struct SetParamCommand {
    ParamId id;
    int16_t raw;
    bool valid;
};
SetParamCommand decodeSetParam(const uint8_t *data, uint8_t length);

enum class EStopAction : uint8_t {
    None = 0,
    Stop = 1,
    Clear = 2,
};
EStopAction decodeEStop(const uint8_t *data, uint8_t length);

constexpr uint8_t kFeedbackFlagsOnlyLength = 1;
constexpr uint8_t kFeedbackWithPositionLength = 3;
uint8_t encodeFeedback(uint8_t *out, uint8_t flags);
uint8_t encodeFeedback(uint8_t *out, uint8_t flags, int32_t position_0p1deg);

uint8_t composeFeedbackFlags(BoardKind board, SlotKind slot, uint8_t safetyFlags,
                             bool configured, bool reached, bool sensorActive);

constexpr uint8_t kInfoBaseLength = 3;
constexpr uint8_t kInfoWithServoRangeLength = 5;
uint8_t encodeInfo(uint8_t *out, uint8_t firmwareVersion, BoardKind board, SlotKind slot);
uint8_t encodeInfo(uint8_t *out, uint8_t firmwareVersion, BoardKind board, SlotKind slot,
                   float angleRangeDeg);

constexpr uint16_t kDefaultCommandTimeoutMs = 500;
constexpr uint16_t kDefaultFeedbackIntervalMs = 10;

constexpr uint16_t kMinCommandTimeoutMs = 50;
constexpr uint16_t kMaxCommandTimeoutMs = 2000;

constexpr uint16_t kMinFeedbackIntervalMs = 1;
constexpr uint16_t kMaxFeedbackIntervalMs = 1000;

uint16_t clampCommandTimeoutMs(int16_t raw);
uint16_t clampFeedbackIntervalMs(int16_t raw);

template <typename Channel>
bool applyCommonParam(const SetParamCommand &cmd, Channel &channel,
                      uint16_t &feedbackIntervalMs) {
    switch (cmd.id) {
        case ParamId::CommandTimeoutMs:
            channel.setCommandTimeoutMs(clampCommandTimeoutMs(cmd.raw));
            return true;
        case ParamId::FeedbackIntervalMs:
            feedbackIntervalMs = clampFeedbackIntervalMs(cmd.raw);
            return true;
        default:
            return false;
    }
}

float clampDuty(float duty, float maxDuty);

struct DutyOutput {
    float magnitude;
    bool reverse;
};
DutyOutput splitDuty(float duty, float maxDuty);

}
