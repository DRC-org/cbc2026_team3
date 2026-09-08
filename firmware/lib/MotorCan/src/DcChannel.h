#pragma once

#include <stdint.h>

#include "MotorCanProtocol.h"
#include "MotorSafety.h"

namespace motorcan {

class DcChannel {
   public:
    explicit DcChannel(uint32_t commandTimeoutMs);

    void feed(uint32_t nowMs);

    EStopAction handleEStopFrame(const uint8_t *data, uint8_t length);

    void stop();

    void applyPhysicalStop(bool active);

    void setWatchdogEnabled(bool enabled);
    void setCommandTimeoutMs(uint32_t timeoutMs);
    uint32_t commandTimeoutMs() const;

    bool isOutputAllowed(uint32_t nowMs) const;

    uint8_t safetyStatusFlags(uint32_t nowMs) const;

    bool applySetTarget(const SetTargetCommand &cmd, uint32_t nowMs);

    bool setDuty(float duty, uint32_t nowMs);

    void hold();

    void tick(uint32_t nowMs);

    float outputDuty(uint32_t nowMs) const;

   private:
    MotorSafety safety_;
    float duty_;
};

}
