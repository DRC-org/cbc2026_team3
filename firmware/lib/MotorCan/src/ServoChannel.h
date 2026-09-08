#pragma once

#include <stdint.h>

#include "MotorCanProtocol.h"
#include "MotorSafety.h"
#include "ServoMotion.h"

namespace motorcan {

class ServoChannel {
   public:
    ServoChannel();

    void begin(float initialAngleDeg, const ServoLimits &limits, uint32_t commandTimeoutMs);

    ServoChannel(float initialAngleDeg, const ServoLimits &limits, uint32_t commandTimeoutMs);

    void feed(uint32_t nowMs);

    EStopAction handleEStopFrame(const uint8_t *data, uint8_t length, uint32_t nowMs);

    void stop(uint32_t nowMs);

    void setWatchdogEnabled(bool enabled);
    void setCommandTimeoutMs(uint32_t timeoutMs);
    uint32_t commandTimeoutMs() const;

    bool isOutputAllowed(uint32_t nowMs) const;

    uint8_t safetyStatusFlags(uint32_t nowMs) const;

    bool applySetTarget(const SetTargetCommand &cmd, uint32_t nowMs);

    bool setTarget(float angleDeg, uint32_t nowMs);

    void hold(uint32_t nowMs);

    void tick(uint32_t nowMs);

    void setLimits(const ServoLimits &limits, uint32_t nowMs);

    const ServoLimits &limits() const;

    void setReachedToleranceDeg(float toleranceDeg, uint32_t nowMs);

    float currentAngleDeg() const;
    bool isReached() const;

   private:
    void applyPendingParams(uint32_t nowMs);

    MotorSafety safety_;
    ServoMotion motion_;

    ServoLimits pendingLimits_;
    float pendingToleranceDeg_;
    bool hasPendingLimits_;
    bool hasPendingTolerance_;

    bool begun_;
};

}
