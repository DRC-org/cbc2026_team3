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

bool SolenoidChannel::applySetTarget(const SetTargetCommand &cmd, uint32_t nowMs) {
    if (!cmd.valid) {
        return false;
    }
    // 仕様書 §9.2: この基板は on_off のみ受理する。position の 90.0[deg] や duty の 0.3 を
    // 「非 0 = ON」として解釈すると、別の基板宛のつもりで書いた値で弁が開く。
    if (cmd.type != ControlType::OnOff) {
        return false;
    }
    // **on_off に固定小数点のスケールは掛からない**（仕様書 §4 の表）。0 か非 0 かだけを見る。
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

bool SolenoidChannel::outputOn(uint32_t nowMs) const {
    return safety_.isOutputAllowed(nowMs) && on_;
}

}  // namespace motorcan
