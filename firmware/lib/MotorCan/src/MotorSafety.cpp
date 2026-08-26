#include "MotorSafety.h"

namespace motorcan {

MotorSafety::MotorSafety(uint32_t timeoutMs)
    : timeoutMs_(timeoutMs),
      lastFedMs_(0),
      everFed_(false),
      // 仕様書 §5.4: 電源投入直後のラッチは解除済み。
      // PC からの解除を待たないと動けない設計だと、PC 側の起動順序次第で
      // 現場で「電源を入れ直しても動かない」状態になる。
      latched_(false) {}

void MotorSafety::stop() { latched_ = true; }

bool MotorSafety::tryClear() {
    latched_ = false;
    return true;
}

EStopAction MotorSafety::handleEStopFrame(const uint8_t *data, uint8_t length) {
    const EStopAction action = decodeEStop(data, length);
    if (action == EStopAction::Stop) {
        stop();
    } else if (action == EStopAction::Clear) {
        tryClear();
    }
    return action;
}

void MotorSafety::feed(uint32_t nowMs) {
    lastFedMs_ = nowMs;
    everFed_ = true;
}

bool MotorSafety::isExpired(uint32_t nowMs) const {
    // 一度も SET_TARGET を受けていない起動直後は「満了」として扱い、出力停止側に倒す。
    // 仕様書 §5.4 の起動時状態（目標 0・出力停止）と矛盾せず、
    // 通信が始まる前に何かの拍子で駆動されるのを防げる。
    if (!everFed_) {
        return true;
    }
    // millis() は約 49.7 日で 0 に戻る。符号なしの引き算で経過時間を出すことで、
    // 折り返し直後に永久満了して原因不明の停止になるのを避ける。
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
    // bit4 は「CAN 通信が途絶した」ことの報告なので、指令をまだ 1 通も受けていない
    // 起動直後には立てない。立てると PC 側 check_safety_error() がセッティングタイムの
    // 動作確認を指令送信前に打ち切り、健全な基板の配線を疑わせる誤誘導になる。
    // 出力禁止（isOutputAllowed）は従来どおり未受信でも掛かったままにする。
    if (isCommandLost(nowMs)) {
        flags |= status_flag::kWatchdog;
    }
    return flags;
}

}  // namespace motorcan
