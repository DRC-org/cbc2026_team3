#include "SolenoidChannel.h"

namespace motorcan {

// 仕様書 §5.4: 電源投入直後は目標 OFF・出力停止・ラッチ解除済み。
SolenoidChannel::SolenoidChannel(uint32_t commandTimeoutMs)
    : safety_(commandTimeoutMs), on_(false) {}

void SolenoidChannel::feed(uint32_t nowMs) { safety_.feed(nowMs); }

EStopAction SolenoidChannel::handleEStopFrame(const uint8_t *data, uint8_t length) {
    const EStopAction action = safety_.handleEStopFrame(data, length);
    if (action != EStopAction::None) {
        // 仕様書 §3.5: 停止でも解除でも目標を OFF に落とす。
        // 解除側でも落とすのは「解除した瞬間に通電しない」を成立させるため。
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

bool SolenoidChannel::setOn(bool on, uint32_t nowMs) {
    if (!safety_.isOutputAllowed(nowMs)) {
        return false;
    }
    on_ = on;
    return true;
}

void SolenoidChannel::hold() { on_ = false; }

bool SolenoidChannel::outputOn(uint32_t nowMs) const {
    return safety_.isOutputAllowed(nowMs) && on_;
}

}  // namespace motorcan
