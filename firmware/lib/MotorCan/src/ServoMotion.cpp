#include "ServoMotion.h"

#include <math.h>

#include "MotorCanProtocol.h"

namespace motorcan {

namespace {

constexpr float kTravelEpsilonDeg = 1e-4f;

float clampFloat(float value, float low, float high) {
    if (value < low) {
        return low;
    }
    if (value > high) {
        return high;
    }
    return value;
}

}

uint16_t angleToPulseUs(float angleDeg, const ServoPulseSpec &spec) {
    if (spec.angleRangeDeg <= 0.0f) {
        return spec.minUs;
    }

    const float span = static_cast<float>(spec.maxUs) - static_cast<float>(spec.minUs);
    const float ratio = clampFloat(angleDeg / spec.angleRangeDeg, 0.0f, 1.0f);
    const float us = static_cast<float>(spec.minUs) + span * ratio;

    const uint16_t low = spec.minUs < spec.maxUs ? spec.minUs : spec.maxUs;
    const uint16_t high = spec.minUs < spec.maxUs ? spec.maxUs : spec.minUs;
    const long rounded = lroundf(us);
    if (rounded <= static_cast<long>(low)) {
        return low;
    }
    if (rounded >= static_cast<long>(high)) {
        return high;
    }
    return static_cast<uint16_t>(rounded);
}

ServoMotion::ServoMotion(float initialAngleDeg, const ServoLimits &limits)
    : limits_{0.0f, 0.0f, kDefaultSlewRateDegPerSec},
      reachedToleranceDeg_(kDefaultServoReachedToleranceDeg),
      startAngleDeg_(0.0f),
      startMs_(0),
      currentAngleDeg_(0.0f),
      targetAngleDeg_(0.0f),
      reached_(true),
      lastNowMs_(0) {
    setLimits(limits);

    const float initial = initialAngleDeg;
    currentAngleDeg_ = clampAngle(initial);
    targetAngleDeg_ = currentAngleDeg_;
    startAngleDeg_ = currentAngleDeg_;
}

float ServoMotion::clampAngle(float angleDeg) const {
    return clampFloat(angleDeg, limits_.angleMinDeg, limits_.angleMaxDeg);
}

void ServoMotion::anchorAt(uint32_t nowMs) {
    startAngleDeg_ = currentAngleDeg_;
    startMs_ = nowMs;
    lastNowMs_ = nowMs;
}

void ServoMotion::setTarget(float angleDeg, uint32_t nowMs) {
    anchorAt(nowMs);
    targetAngleDeg_ = clampAngle(angleDeg);

    const float remaining = targetAngleDeg_ - currentAngleDeg_;
    reached_ = fabsf(remaining) <= reachedToleranceDeg_;
}

void ServoMotion::update(uint32_t nowMs) {
    lastNowMs_ = nowMs;

    const uint32_t elapsedMs = nowMs - startMs_;

    const float distance = targetAngleDeg_ - startAngleDeg_;
    const float absDistance = fabsf(distance);
    const float travel = limits_.slewRateDegPerSec * (static_cast<float>(elapsedMs) * 0.001f);

    if (travel + kTravelEpsilonDeg >= absDistance) {
        currentAngleDeg_ = targetAngleDeg_;
    } else {
        currentAngleDeg_ = startAngleDeg_ + (distance < 0.0f ? -travel : travel);
    }

    reached_ = fabsf(targetAngleDeg_ - currentAngleDeg_) <= reachedToleranceDeg_;
}

void ServoMotion::holdHere(uint32_t nowMs) {
    anchorAt(nowMs);
    targetAngleDeg_ = currentAngleDeg_;
    reached_ = true;
}

void ServoMotion::setLimits(const ServoLimits &limits) {
    ServoLimits next = limits;

    if (next.angleMinDeg > next.angleMaxDeg) {
        const float swapped = next.angleMinDeg;
        next.angleMinDeg = next.angleMaxDeg;
        next.angleMaxDeg = swapped;
    }
    if (!(next.slewRateDegPerSec > 0.0f)) {
        next.slewRateDegPerSec = limits_.slewRateDegPerSec;
    }

    limits_ = next;

    anchorAt(lastNowMs_);

    targetAngleDeg_ = clampAngle(targetAngleDeg_);
    reached_ = fabsf(targetAngleDeg_ - currentAngleDeg_) <= reachedToleranceDeg_;
}

void ServoMotion::setReachedToleranceDeg(float toleranceDeg) {
    if (toleranceDeg < 0.0f) {
        return;
    }
    reachedToleranceDeg_ = toleranceDeg;
}

}
