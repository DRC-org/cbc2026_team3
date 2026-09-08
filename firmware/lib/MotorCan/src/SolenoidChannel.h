#pragma once

#include <stdint.h>

#include "MotorCanProtocol.h"
#include "MotorSafety.h"

namespace motorcan {

class SolenoidChannel {
   public:
    explicit SolenoidChannel(uint32_t commandTimeoutMs);

    void feed(uint32_t nowMs);

    EStopAction handleEStopFrame(const uint8_t *data, uint8_t length);

    void stop();

    void setWatchdogEnabled(bool enabled);
    void setCommandTimeoutMs(uint32_t timeoutMs);
    uint32_t commandTimeoutMs() const;

    bool isOutputAllowed(uint32_t nowMs) const;

    uint8_t safetyStatusFlags(uint32_t nowMs) const;

    bool applySetTarget(const SetTargetCommand &cmd, uint32_t nowMs);

    bool setOn(bool on, uint32_t nowMs);

    void hold();

    void tick(uint32_t nowMs);

    bool outputOn(uint32_t nowMs) const;

   private:
    MotorSafety safety_;
    bool on_;
};

}
