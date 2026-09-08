#pragma once

#include <stdint.h>

namespace motorcan {

constexpr float kDefaultSlewRateDegPerSec = 90.0f;

constexpr float kDefaultServoReachedToleranceDeg = 0.0f;

struct ServoLimits {
    float angleMinDeg;
    float angleMaxDeg;
    float slewRateDegPerSec;
};

struct ServoPulseSpec {
    uint16_t minUs;
    uint16_t maxUs;
    float angleRangeDeg;
};

uint16_t angleToPulseUs(float angleDeg, const ServoPulseSpec &spec);

class ServoMotion {
   public:
    ServoMotion(float initialAngleDeg, const ServoLimits &limits);

    void setTarget(float angleDeg, uint32_t nowMs);

    void update(uint32_t nowMs);

    float currentAngleDeg() const { return currentAngleDeg_; }
    float targetAngleDeg() const { return targetAngleDeg_; }

    bool isReached() const { return reached_; }

    void holdHere(uint32_t nowMs);

    void setLimits(const ServoLimits &limits);
    const ServoLimits &limits() const { return limits_; }

    void setReachedToleranceDeg(float toleranceDeg);

   private:
    void anchorAt(uint32_t nowMs);
    float clampAngle(float angleDeg) const;

    ServoLimits limits_;
    float reachedToleranceDeg_;

    float startAngleDeg_;
    uint32_t startMs_;

    float currentAngleDeg_;
    float targetAngleDeg_;
    bool reached_;

    uint32_t lastNowMs_;
};

}
