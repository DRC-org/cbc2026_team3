#include "ServoChannel.h"

namespace motorcan {

ServoChannel::ServoChannel()
    : safety_(kDefaultCommandTimeoutMs),
      motion_(0.0f, ServoLimits{0.0f, 0.0f, kDefaultSlewRateDegPerSec}),
      pendingLimits_(motion_.limits()),
      pendingToleranceDeg_(kDefaultServoReachedToleranceDeg),
      hasPendingLimits_(false),
      hasPendingTolerance_(false),
      begun_(false) {}

ServoChannel::ServoChannel(float initialAngleDeg, const ServoLimits &limits,
                           uint32_t commandTimeoutMs)
    : ServoChannel() {
    begin(initialAngleDeg, limits, commandTimeoutMs);
}

void ServoChannel::begin(float initialAngleDeg, const ServoLimits &limits,
                         uint32_t commandTimeoutMs) {
    safety_ = MotorSafety(commandTimeoutMs);
    motion_ = ServoMotion(initialAngleDeg, limits);
    pendingLimits_ = motion_.limits();
    pendingToleranceDeg_ = kDefaultServoReachedToleranceDeg;
    hasPendingLimits_ = false;
    hasPendingTolerance_ = false;
    begun_ = true;
}

void ServoChannel::feed(uint32_t nowMs) { safety_.feed(nowMs); }

EStopAction ServoChannel::handleEStopFrame(const uint8_t *data, uint8_t length, uint32_t nowMs) {
    const EStopAction action = safety_.handleEStopFrame(data, length);
    if (action != EStopAction::None) {
        motion_.holdHere(nowMs);
    }
    return action;
}

void ServoChannel::stop(uint32_t nowMs) {
    safety_.stop();
    motion_.holdHere(nowMs);
}

void ServoChannel::setWatchdogEnabled(bool enabled) { safety_.setWatchdogEnabled(enabled); }

void ServoChannel::setCommandTimeoutMs(uint32_t timeoutMs) { safety_.setTimeoutMs(timeoutMs); }

uint32_t ServoChannel::commandTimeoutMs() const { return safety_.timeoutMs(); }

bool ServoChannel::isOutputAllowed(uint32_t nowMs) const {
    return begun_ && safety_.isOutputAllowed(nowMs);
}

uint8_t ServoChannel::safetyStatusFlags(uint32_t nowMs) const { return safety_.statusFlags(nowMs); }

bool ServoChannel::applySetTarget(const SetTargetCommand &cmd, uint32_t nowMs) {
    if (!cmd.valid) {
        return false;
    }
    if (cmd.type != ControlType::Position) {
        return false;
    }
    return setTarget(fromRaw(cmd.raw, kAngleScale), nowMs);
}

bool ServoChannel::setTarget(float angleDeg, uint32_t nowMs) {
    if (!isOutputAllowed(nowMs)) {
        return false;
    }
    applyPendingParams(nowMs);
    motion_.setTarget(angleDeg, nowMs);
    return true;
}

void ServoChannel::hold(uint32_t nowMs) { motion_.holdHere(nowMs); }

void ServoChannel::tick(uint32_t nowMs) {
    if (!isOutputAllowed(nowMs)) {
        motion_.holdHere(nowMs);
    } else {
        applyPendingParams(nowMs);
    }
    motion_.update(nowMs);
}

void ServoChannel::setLimits(const ServoLimits &limits, uint32_t nowMs) {
    if (!isOutputAllowed(nowMs)) {
        pendingLimits_ = limits;
        hasPendingLimits_ = true;
        return;
    }
    motion_.setLimits(limits);
    hasPendingLimits_ = false;
}

const ServoLimits &ServoChannel::limits() const {
    return hasPendingLimits_ ? pendingLimits_ : motion_.limits();
}

void ServoChannel::setReachedToleranceDeg(float toleranceDeg, uint32_t nowMs) {
    if (!isOutputAllowed(nowMs)) {
        pendingToleranceDeg_ = toleranceDeg;
        hasPendingTolerance_ = true;
        return;
    }
    motion_.setReachedToleranceDeg(toleranceDeg);
    hasPendingTolerance_ = false;
}

void ServoChannel::applyPendingParams(uint32_t nowMs) {
    if (hasPendingTolerance_) {
        motion_.setReachedToleranceDeg(pendingToleranceDeg_);
        hasPendingTolerance_ = false;
    }
    if (hasPendingLimits_) {
        motion_.setLimits(pendingLimits_);
        hasPendingLimits_ = false;
        motion_.holdHere(nowMs);
    }
}

float ServoChannel::currentAngleDeg() const { return motion_.currentAngleDeg(); }

bool ServoChannel::isReached() const { return motion_.isReached(); }

}
