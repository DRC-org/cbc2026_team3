#include "MotorCanProtocol.h"

namespace motorcan {

namespace {

constexpr uint8_t kCommandLength = 3;
constexpr uint8_t kValueOffset = 1;

bool isKnownControlType(uint8_t raw) {
    return raw == static_cast<uint8_t>(ControlType::Position) ||
           raw == static_cast<uint8_t>(ControlType::Velocity) ||
           raw == static_cast<uint8_t>(ControlType::Duty) ||
           raw == static_cast<uint8_t>(ControlType::OnOff);
}

bool isKnownParamId(uint8_t raw) {
    return raw <= static_cast<uint8_t>(ParamId::AngleMax);
}

uint16_t clampRawTo(int16_t raw, uint16_t lo, uint16_t hi) {
    if (raw < 0) {
        return lo;
    }
    const uint16_t value = static_cast<uint16_t>(raw);
    if (value < lo) {
        return lo;
    }
    if (value > hi) {
        return hi;
    }
    return value;
}

}

uint8_t makeDeviceId(BoardKind board, uint8_t boardNumber, uint8_t slot) {
    if (boardNumber > kMaxBoardNumber || slot > kMaxSlotNumber) {
        return kDeviceIdUnconfigured;
    }
    const uint8_t deviceId =
        static_cast<uint8_t>((static_cast<uint8_t>(board) << kBoardKindShift) |
                             (boardNumber << kBoardNumberShift) | slot);
    if (deviceId == kDeviceIdBroadcast) {
        return kDeviceIdUnconfigured;
    }
    return deviceId;
}

int16_t saturateToInt16(int32_t value) {
    if (value > 32767) {
        return 32767;
    }
    if (value < -32768) {
        return -32768;
    }
    return static_cast<int16_t>(value);
}

int16_t toRaw(float value, int32_t scale) {
    if (!(value == value)) {
        return 0;
    }
    const float scaled = value * static_cast<float>(scale);
    if (scaled >= 32767.0f) {
        return 32767;
    }
    if (scaled <= -32768.0f) {
        return -32768;
    }
    return static_cast<int16_t>(scaled >= 0.0f ? scaled + 0.5f : scaled - 0.5f);
}

float fromRaw(int16_t raw, int32_t scale) {
    return static_cast<float>(raw) / static_cast<float>(scale);
}

uint16_t buildCanId(CommandType command, uint8_t deviceId) {
    return static_cast<uint16_t>(commandIdBase(command) | deviceId);
}

CanIdInfo parseCanId(uint16_t canId) {
    CanIdInfo info{CommandType::EStop, 0, false};

    if (canId > 0x7FF) {
        return info;
    }

    const uint8_t raw = static_cast<uint8_t>((canId >> kCommandTypeShift) & 0x07);
    switch (raw) {
        case static_cast<uint8_t>(CommandType::EStop):
        case static_cast<uint8_t>(CommandType::SetTarget):
        case static_cast<uint8_t>(CommandType::SetParam):
        case static_cast<uint8_t>(CommandType::Feedback):
        case static_cast<uint8_t>(CommandType::Info):
            info.command = static_cast<CommandType>(raw);
            info.deviceId = static_cast<uint8_t>(canId & 0xFF);
            info.valid = true;
            break;
        default:
            break;
    }
    return info;
}

void packInt16Le(uint8_t *dst, int16_t value) {
    const uint16_t bits = static_cast<uint16_t>(value);
    dst[0] = static_cast<uint8_t>(bits & 0xFF);
    dst[1] = static_cast<uint8_t>((bits >> 8) & 0xFF);
}

int16_t unpackInt16Le(const uint8_t *src) {
    return static_cast<int16_t>(static_cast<uint16_t>(src[0]) |
                                (static_cast<uint16_t>(src[1]) << 8));
}

SetTargetCommand decodeSetTarget(const uint8_t *data, uint8_t length) {
    SetTargetCommand cmd{ControlType::Duty, 0, false};
    if (data == nullptr || length < kCommandLength) {
        return cmd;
    }
    if (!isKnownControlType(data[0])) {
        return cmd;
    }
    cmd.type = static_cast<ControlType>(data[0]);
    cmd.raw = unpackInt16Le(&data[kValueOffset]);
    cmd.valid = true;
    return cmd;
}

SetParamCommand decodeSetParam(const uint8_t *data, uint8_t length) {
    SetParamCommand cmd{ParamId::MaxDuty, 0, false};
    if (data == nullptr || length < kCommandLength) {
        return cmd;
    }
    if (!isKnownParamId(data[0])) {
        return cmd;
    }
    cmd.id = static_cast<ParamId>(data[0]);
    cmd.raw = unpackInt16Le(&data[kValueOffset]);
    cmd.valid = true;
    return cmd;
}

EStopAction decodeEStop(const uint8_t *data, uint8_t length) {
    if (data == nullptr || length < 3) {
        return EStopAction::None;
    }
    if (data[0] == 0x00) {
        return EStopAction::Stop;
    }
    if (data[0] == 0x01 && data[1] == 0x5A && data[2] == 0xA5) {
        return EStopAction::Clear;
    }
    return EStopAction::None;
}

uint8_t encodeFeedback(uint8_t *out, uint8_t flags) {
    out[0] = flags;
    return kFeedbackFlagsOnlyLength;
}

uint8_t encodeFeedback(uint8_t *out, uint8_t flags, int32_t position_0p1deg) {
    out[0] = flags;
    packInt16Le(&out[1], saturateToInt16(position_0p1deg));
    return kFeedbackWithPositionLength;
}

uint8_t composeFeedbackFlags(BoardKind board, SlotKind slot, uint8_t safetyFlags,
                             bool configured, bool reached, bool sensorActive) {
    uint8_t flags = 0;

    if (slot == SlotKind::Sensor) {
        if (!configured) {
            flags |= status_flag::kDeviceIdUnconfigured;
        }
        if (sensorActive) {
            flags |= status_flag::kSensor;
        }
        return flags;
    }

    flags = safetyFlags;
    if (!configured) {
        flags |= status_flag::kDeviceIdUnconfigured;
    }
    if (board == BoardKind::Servo && reached) {
        flags |= status_flag::kReached;
    }
    return flags;
}

uint8_t encodeInfo(uint8_t *out, uint8_t firmwareVersion, BoardKind board, SlotKind slot) {
    out[0] = firmwareVersion;
    out[1] = static_cast<uint8_t>(board);
    out[2] = static_cast<uint8_t>(slot);
    return kInfoBaseLength;
}

uint8_t encodeInfo(uint8_t *out, uint8_t firmwareVersion, BoardKind board, SlotKind slot,
                   float angleRangeDeg) {
    encodeInfo(out, firmwareVersion, board, slot);
    packInt16Le(&out[3], toRaw(angleRangeDeg, kAngleScale));
    return kInfoWithServoRangeLength;
}

uint16_t clampCommandTimeoutMs(int16_t raw) {
    return clampRawTo(raw, kMinCommandTimeoutMs, kMaxCommandTimeoutMs);
}

uint16_t clampFeedbackIntervalMs(int16_t raw) {
    return clampRawTo(raw, kMinFeedbackIntervalMs, kMaxFeedbackIntervalMs);
}

float clampDuty(float duty, float maxDuty) {
    if (maxDuty < 0.0f) {
        maxDuty = 0.0f;
    } else if (maxDuty > 1.0f) {
        maxDuty = 1.0f;
    }
    if (duty > maxDuty) {
        return maxDuty;
    }
    if (duty < -maxDuty) {
        return -maxDuty;
    }
    return duty;
}

DutyOutput splitDuty(float duty, float maxDuty) {
    const float clamped = clampDuty(duty, maxDuty);
    return DutyOutput{clamped < 0.0f ? -clamped : clamped, clamped < 0.0f};
}

}
