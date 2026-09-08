#pragma once

#include <stdint.h>

#include "MotorCanProtocol.h"
#include "MotorCanRouter.h"

namespace motorcan {

constexpr uint32_t kSerialOverrideHoldMs = kMaxCommandTimeoutMs;

class SerialOverride {
   public:
    void note(uint8_t channel, uint32_t nowMs) {
        if (channel >= kMaxChannels) {
            return;
        }
        if (!active(nowMs)) {
            mask_ = 0;
        }
        mask_ = static_cast<uint8_t>(mask_ | (1u << channel));
        lastMs_ = nowMs;
    }

    void clear() { mask_ = 0; }

    bool shouldFeed(uint8_t channel, uint32_t nowMs) const {
        if (channel >= kMaxChannels) {
            return false;
        }
        return active(nowMs) && (mask_ & static_cast<uint8_t>(1u << channel)) != 0;
    }

    bool active(uint32_t nowMs) const {
        return mask_ != 0 && (nowMs - lastMs_) <= kSerialOverrideHoldMs;
    }

   private:
    uint8_t mask_ = 0;
    uint32_t lastMs_ = 0;
};

}
