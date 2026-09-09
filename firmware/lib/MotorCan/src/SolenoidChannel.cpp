#include "SolenoidChannel.h"

namespace motorcan {

SolenoidChannel::SolenoidChannel(uint32_t commandTimeoutMs)
    : safety_(commandTimeoutMs), on_(false) {}

void SolenoidChannel::feed(uint32_t nowMs) { safety_.feed(nowMs); }

EStopAction SolenoidChannel::handleEStopFrame(const uint8_t *data, uint8_t length) {
    const EStopAction action = safety_.handleEStopFrame(data, length);
    if (action != EStopAction::None) {
        on_ = false;
    }
    return action;
}

void SolenoidChannel::stop() {
    safety_.stop();
    on_ = false;
}

void SolenoidChannel::setWatchdogEnabled(bool enabled) { safety_.setWatchdogEnabled(enabled); }

void SolenoidChannel::setCommandTimeoutMs(uint32_t timeoutMs) { safety_.setTimeoutMs(timeoutMs); }

uint32_t SolenoidChannel::commandTimeoutMs() const { return safety_.timeoutMs(); }

bool SolenoidChannel::isOutputAllowed(uint32_t nowMs) const {
    return safety_.isOutputAllowed(nowMs);
}

uint8_t SolenoidChannel::safetyStatusFlags(uint32_t nowMs) const {
    return safety_.statusFlags(nowMs);
}

bool SolenoidChannel::applySetTarget(const SetTargetCommand &cmd, uint32_t nowMs) {
    if (!cmd.valid) {
        return false;
    }
    if (cmd.type != ControlType::OnOff) {
        return false;
    }
    return setOn(cmd.raw != 0, nowMs);
}

bool SolenoidChannel::setOn(bool on, uint32_t nowMs) {
    if (!safety_.isOutputAllowed(nowMs)) {
        return false;
    }
    on_ = on;
    return true;
}

void SolenoidChannel::hold() { on_ = false; }

void SolenoidChannel::tick(uint32_t nowMs) {
    if (!safety_.isOutputAllowed(nowMs)) {
        on_ = false;
    }
}

bool SolenoidChannel::outputOn(uint32_t nowMs) const {
    return safety_.isOutputAllowed(nowMs) && on_;
}

}
