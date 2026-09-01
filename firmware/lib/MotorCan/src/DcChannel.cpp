#include "DcChannel.h"

namespace motorcan {

// 仕様書 §5.4: 電源投入直後は目標 0・出力停止・ラッチ解除済み。
DcChannel::DcChannel(uint32_t commandTimeoutMs) : safety_(commandTimeoutMs), duty_(0.0f) {}

void DcChannel::feed(uint32_t nowMs) { safety_.feed(nowMs); }

EStopAction DcChannel::handleEStopFrame(const uint8_t *data, uint8_t length) {
    const EStopAction action = safety_.handleEStopFrame(data, length);
    if (action != EStopAction::None) {
        // 仕様書 §3.5: 停止でも解除でも目標を 0 に落とす。
        // 解除側でも落とすのは「解除した瞬間に動き出さない」を成立させるため。
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
    // 仕様書 §4: この基板はフィードバックを持たないので duty のみ受理する。
    // position の 90.0[deg] を duty として解釈すると 9000% の全力指令になるため、
    // position / velocity / on_off は黙って捨てる。
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

float DcChannel::outputDuty(uint32_t nowMs) const {
    return safety_.isOutputAllowed(nowMs) ? duty_ : 0.0f;
}

}  // namespace motorcan
