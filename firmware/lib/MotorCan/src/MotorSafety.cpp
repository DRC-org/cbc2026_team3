#include "MotorSafety.h"

namespace motorcan {

MotorSafety::MotorSafety(uint32_t timeoutMs)
    : timeoutMs_(timeoutMs),
      lastFedMs_(0),
      everFed_(false),
      latched_(false),
      watchdogEnabled_(true) {}

void MotorSafety::stop() { latched_ = true; }

void MotorSafety::clear() { latched_ = false; }

EStopAction MotorSafety::handleEStopFrame(const uint8_t *data, uint8_t length) {
    const EStopAction action = decodeEStop(data, length);
    if (action == EStopAction::Stop) {
        stop();
    } else if (action == EStopAction::Clear) {
        clear();
    }
    return action;
}

void MotorSafety::feed(uint32_t nowMs) {
    lastFedMs_ = nowMs;
    everFed_ = true;
}

bool MotorSafety::isExpired(uint32_t nowMs) const {
    if (!everFed_) {
        return true;
    }
    const uint32_t elapsed = nowMs - lastFedMs_;
    return elapsed >= timeoutMs_;
}

bool MotorSafety::isCommandLost(uint32_t nowMs) const {
    return everFed_ && isExpired(nowMs);
}

uint8_t MotorSafety::statusFlags(uint32_t nowMs) const {
    uint8_t flags = 0;
    if (latched_) {
        flags |= status_flag::kEStop;
    }
    if (!everFed_) {
        flags |= status_flag::kNeverCommanded;
    }
    if (watchdogEnabled_ && isCommandLost(nowMs)) {
        flags |= status_flag::kWatchdog;
    }
    return flags;
}

}
