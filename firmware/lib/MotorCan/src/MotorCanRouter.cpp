#include "MotorCanRouter.h"

namespace motorcan {

namespace {

FrameRoute rejected() { return FrameRoute{false, CommandType::Feedback, 0}; }

}

FrameRoute routeFrame(uint16_t canId, bool isStandardId, const uint8_t *deviceIds,
                      uint8_t channelCount) {
    if (!isStandardId || deviceIds == nullptr || channelCount == 0) {
        return rejected();
    }
    if (channelCount > kMaxChannels) {
        return rejected();
    }

    const CanIdInfo info = parseCanId(canId);
    if (!info.valid) {
        return rejected();
    }

    if (info.deviceId == kDeviceIdBroadcast) {
        if (info.command != CommandType::EStop) {
            return rejected();
        }
        const uint8_t all = static_cast<uint8_t>((1u << channelCount) - 1u);
        return FrameRoute{true, info.command, all};
    }

    if (info.deviceId == kDeviceIdUnconfigured) {
        return rejected();
    }

    uint8_t mask = 0;
    for (uint8_t ch = 0; ch < channelCount; ++ch) {
        if (deviceIds[ch] == info.deviceId) {
            mask |= static_cast<uint8_t>(1u << ch);
        }
    }
    if (mask == 0) {
        return rejected();
    }
    return FrameRoute{true, info.command, mask};
}

void resolveDeviceIds(uint8_t *out, uint8_t count, BoardKind board, uint8_t boardNumber,
                      bool (*isDevice)(uint8_t slot)) {
    if (out == nullptr) {
        return;
    }
    for (uint8_t slot = 0; slot < count; ++slot) {
        const bool device = (isDevice == nullptr) || isDevice(slot);
        out[slot] = device ? makeDeviceId(board, boardNumber, slot) : kDeviceIdUnconfigured;
    }
}

uint8_t readDipSwitch(const uint8_t *pins, uint8_t count, int (*readPin)(uint8_t pin),
                      int activeLevel) {
    if (pins == nullptr || readPin == nullptr) {
        return 0;
    }
    uint8_t value = 0;
    for (uint8_t bit = 0; bit < count && bit < 8; ++bit) {
        if (readPin(pins[bit]) == activeLevel) {
            value |= static_cast<uint8_t>(1u << bit);
        }
    }
    return value;
}

}
