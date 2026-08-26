#include "ServoChannel.h"

namespace motorcan {

ServoChannel::ServoChannel(float initialAngleDeg, const ServoLimits &limits,
                           uint32_t commandTimeoutMs)
    : safety_(commandTimeoutMs), motion_(initialAngleDeg, limits) {}

void ServoChannel::feed(uint32_t nowMs) { safety_.feed(nowMs); }

EStopAction ServoChannel::handleEStopFrame(const uint8_t *data, uint8_t length, uint32_t nowMs) {
    const EStopAction action = safety_.handleEStopFrame(data, length);
    if (action != EStopAction::None) {
        // 仕様書 §7.5 / §3.5: 停止でも解除でもその場で現在角へ凍結する。
        // 解除側でも凍結するのは「解除した瞬間に動き出さない」を成立させるため。
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

bool ServoChannel::isOutputAllowed(uint32_t nowMs) const { return safety_.isOutputAllowed(nowMs); }

uint8_t ServoChannel::safetyStatusFlags(uint32_t nowMs) const { return safety_.statusFlags(nowMs); }

bool ServoChannel::setTarget(float angleDeg, uint32_t nowMs) {
    if (!safety_.isOutputAllowed(nowMs)) {
        // 仕様書 §7.5: 出力禁止中は新しい角度指令を受け付けない。
        // 受け付けると、PC が §5.1 の契約どおり 20Hz で再送している間ずっと
        // 補間が再アンカーされ、緊急停止中でも 1 ティックぶんずつ進み続ける。
        return false;
    }
    motion_.setTarget(angleDeg, nowMs);
    return true;
}

void ServoChannel::hold(uint32_t nowMs) { motion_.holdHere(nowMs); }

void ServoChannel::tick(uint32_t nowMs) {
    if (!safety_.isOutputAllowed(nowMs)) {
        // 補間を進める**前に**凍結する。後ろに置くと、ウォッチドッグ満了のように
        // フレームを伴わない禁止では、満了の瞬間に slew_rate × 1 ティック分だけ
        // 進んでから凍結する（既定なら 90deg/s × 5ms = 0.45deg）。
        motion_.holdHere(nowMs);
    }
    motion_.update(nowMs);
}

void ServoChannel::setLimits(const ServoLimits &limits) { motion_.setLimits(limits); }

const ServoLimits &ServoChannel::limits() const { return motion_.limits(); }

void ServoChannel::setReachedToleranceDeg(float toleranceDeg) {
    motion_.setReachedToleranceDeg(toleranceDeg);
}

float ServoChannel::currentAngleDeg() const { return motion_.currentAngleDeg(); }

float ServoChannel::currentSlewDegPerSec() const { return motion_.currentSlewDegPerSec(); }

bool ServoChannel::isReached() const { return motion_.isReached(); }

}  // namespace motorcan
