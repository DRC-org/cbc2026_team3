#include "MotorCanRouter.h"

namespace motorcan {

namespace {

FrameRoute rejected() { return FrameRoute{false, CommandType::Feedback, 0}; }

}  // namespace

FrameRoute routeFrame(uint16_t canId, bool isStandardId, const uint8_t *deviceIds,
                      uint8_t channelCount) {
    if (!isStandardId || deviceIds == nullptr || channelCount == 0) {
        return rejected();
    }
    if (channelCount > kMaxChannels) {
        channelCount = kMaxChannels;
    }

    const CanIdInfo info = parseCanId(canId);
    if (!info.valid) {
        return rejected();
    }

    if (info.deviceId == kDeviceIdBroadcast) {
        // 0xFF は E_STOP のブロードキャスト専用（仕様書 §2.2）。
        // SET_TARGET を 0xFF で送っても誰も動かしてはならない。
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

uint8_t applyDeviceIdOffset(uint8_t baseDeviceId, uint8_t offset) {
    if (baseDeviceId == kDeviceIdUnconfigured) {
        return kDeviceIdUnconfigured;
    }
    const uint8_t id = static_cast<uint8_t>(baseDeviceId + offset);
    if (id == kDeviceIdUnconfigured || id == kDeviceIdBroadcast) {
        return kDeviceIdUnconfigured;
    }
    return id;
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

}  // namespace motorcan
