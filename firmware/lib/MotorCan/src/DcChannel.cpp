#include "DcChannel.h"

namespace motorcan {

DcChannel::DcChannel(uint32_t commandTimeoutMs) : safety_(commandTimeoutMs), duty_(0.0f) {}

void DcChannel::feed(uint32_t nowMs) { safety_.feed(nowMs); }

EStopAction DcChannel::handleEStopFrame(const uint8_t *data, uint8_t length) {
    const EStopAction action = safety_.handleEStopFrame(data, length);
    if (action != EStopAction::None) {
        duty_ = 0.0f;
    }
    return action;
}

void DcChannel::stop() {
    safety_.stop();
    duty_ = 0.0f;
}

void DcChannel::applyPhysicalStop(bool active) {
    if (active) {
        duty_ = 0.0f;
    }
    safety_.applyPhysicalStop(active);
}

void DcChannel::setWatchdogEnabled(bool enabled) { safety_.setWatchdogEnabled(enabled); }

void DcChannel::setCommandTimeoutMs(uint32_t timeoutMs) { safety_.setTimeoutMs(timeoutMs); }

uint32_t DcChannel::commandTimeoutMs() const { return safety_.timeoutMs(); }

bool DcChannel::isOutputAllowed(uint32_t nowMs) const { return safety_.isOutputAllowed(nowMs); }

uint8_t DcChannel::safetyStatusFlags(uint32_t nowMs) const { return safety_.statusFlags(nowMs); }

bool DcChannel::applySetTarget(const SetTargetCommand &cmd, uint32_t nowMs) {
    if (!cmd.valid) {
        return false;
    }
    if (cmd.type != ControlType::Duty) {
        return false;
    }
    return setDuty(fromRaw(cmd.raw, kDutyScale), nowMs);
}

bool DcChannel::setDuty(float duty, uint32_t nowMs) {
    if (!safety_.isOutputAllowed(nowMs)) {
        return false;
    }
    duty_ = duty;
    return true;
}

void DcChannel::hold() { duty_ = 0.0f; }

void DcChannel::tick(uint32_t nowMs) {
    if (!safety_.isOutputAllowed(nowMs)) {
        duty_ = 0.0f;
    }
}

float DcChannel::outputDuty(uint32_t nowMs) const {
    return safety_.isOutputAllowed(nowMs) ? duty_ : 0.0f;
}

}
