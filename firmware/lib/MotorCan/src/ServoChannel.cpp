#include "ServoChannel.h"

namespace motorcan {

ServoChannel::ServoChannel(float initialAngleDeg, const ServoLimits &limits,
                           uint32_t commandTimeoutMs)
    : safety_(commandTimeoutMs),
      motion_(initialAngleDeg, limits),
      pendingLimits_(motion_.limits()),
      pendingToleranceDeg_(kDefaultServoReachedToleranceDeg),
      hasPendingLimits_(false),
      hasPendingTolerance_(false) {}

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

bool ServoChannel::applySetTarget(const SetTargetCommand &cmd, uint32_t nowMs) {
    if (!cmd.valid) {
        return false;
    }
    // 仕様書 §7.2: サーボは position のみ受理する。duty の 0.3 を角度として解釈すると
    // 想定外の位置へ飛ぶので、velocity / duty / on_off は黙って捨てる。
    if (cmd.type != ControlType::Position) {
        return false;
    }
    return setTarget(fromRaw(cmd.raw, kAngleScale), nowMs);
}

bool ServoChannel::setTarget(float angleDeg, uint32_t nowMs) {
    if (!safety_.isOutputAllowed(nowMs)) {
        // 仕様書 §7.5: 出力禁止中は新しい角度指令を受け付けない。
        // 受け付けると、PC が §5.1 の契約どおり 20Hz で再送している間ずっと
        // 補間が再アンカーされ、緊急停止中でも 1 ティックぶんずつ進み続ける。
        return false;
    }
    // 保留していた可動範囲は、この指令をクランプする**前に**取り込む。
    // 次のティック任せにすると、解除直後の 1 通だけが古い範囲でクランプされる。
    applyPendingParams(nowMs);
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
    } else {
        // 出力が許可された最初のティックで、禁止中に届いた SET_PARAM を取り込む。
        applyPendingParams(nowMs);
    }
    motion_.update(nowMs);
}

void ServoChannel::setLimits(const ServoLimits &limits, uint32_t nowMs) {
    if (!safety_.isOutputAllowed(nowMs)) {
        // 仕様書 §7.5: 出力禁止中に効かせない。ServoMotion::setLimits は目標角を
        // 新しい範囲へクランプするので、ここを素通しにすると setTarget が入口で
        // 拒否しているのと同じことを SET_PARAM 経由でできてしまう。
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
    if (!safety_.isOutputAllowed(nowMs)) {
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
        // 取り込んだ範囲で**現在角を動かさない**（仕様書 §3.5「解除した瞬間に
        // 動き出さない」）。setLimits は目標角を新しい範囲へクランプするので、
        // ここで凍結し直さないと解除の瞬間に補間が走り出す。狭めた範囲は
        // 次の SET_TARGET から効く（そちらは setTarget がクランプする）。
        motion_.holdHere(nowMs);
    }
}

float ServoChannel::currentAngleDeg() const { return motion_.currentAngleDeg(); }

bool ServoChannel::isReached() const { return motion_.isReached(); }

}  // namespace motorcan
