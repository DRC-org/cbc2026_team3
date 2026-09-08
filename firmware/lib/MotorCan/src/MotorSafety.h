#pragma once

#include <stdint.h>

#include "MotorCanProtocol.h"

namespace motorcan {

class MotorSafety {
   public:
    explicit MotorSafety(uint32_t timeoutMs);

    void stop();

    void clear();

    bool isLatched() const { return latched_; }

    void applyPhysicalStop(bool active) {
        if (active) {
            stop();
        }
    }

    EStopAction handleEStopFrame(const uint8_t *data, uint8_t length);

    void feed(uint32_t nowMs);

    bool isExpired(uint32_t nowMs) const;

    bool isCommandLost(uint32_t nowMs) const;

    void setTimeoutMs(uint32_t timeoutMs) { timeoutMs_ = timeoutMs; }
    uint32_t timeoutMs() const { return timeoutMs_; }

    void setWatchdogEnabled(bool enabled) { watchdogEnabled_ = enabled; }

    bool isOutputAllowed(uint32_t nowMs) const {
        return !latched_ && everFed_ && !(watchdogEnabled_ && isExpired(nowMs));
    }

    uint8_t statusFlags(uint32_t nowMs) const;

   private:
    uint32_t timeoutMs_;
    uint32_t lastFedMs_;
    bool everFed_;
    bool latched_;
    bool watchdogEnabled_;
};

}
