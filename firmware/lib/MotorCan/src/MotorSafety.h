// 緊急停止ラッチとコマンドウォッチドッグ（仕様書 §5.1 / §5.2 / §6）。
//
// 現在時刻を millis() で内部取得せず uint32_t nowMs で受け取るのは、
// native 環境で満了・折り返しを実時間を待たずに検証できるようにするため。
// DC 用とサーボ用のファームで共有する。

#pragma once

#include <stdint.h>

#include "MotorCanProtocol.h"

namespace motorcan {

class MotorSafety {
   public:
    explicit MotorSafety(uint32_t timeoutMs);

    // ---- 緊急停止ラッチ（仕様書 §3.5 / §5.2）----

    // 出力を止めてラッチする。以後 SET_TARGET を受け取っても駆動しない。
    void stop();

    // ラッチを解除する。解除の可否判定（マジックバイト）は handleEStopFrame 側の責務。
    // 戻り値は「解除によって状態が変わったか」ではなく「解除後に非ラッチか」。
    bool tryClear();

    bool isLatched() const { return latched_; }

    // E_STOP フレームを解釈してラッチ状態へ反映する。
    // マジックバイトが揃わない解除要求ではラッチを維持する（仕様書 §3.5）。
    EStopAction handleEStopFrame(const uint8_t *data, uint8_t length);

    // ---- コマンドウォッチドッグ（仕様書 §5.1）----

    // 自分宛の SET_TARGET を受信したときに呼ぶ。
    // 緊急停止ラッチ中でも呼ぶこと。呼ばないと解除した直後にウォッチドッグが満了済みで、
    // 操縦者が解除しても一切動かない状態になる（仕様書 §6）。
    void feed(uint32_t nowMs);

    // 満了していれば true。ラッチはしないので、新しい feed() で自動的に復帰する。
    bool isExpired(uint32_t nowMs) const;

    void setTimeoutMs(uint32_t timeoutMs) { timeoutMs_ = timeoutMs; }
    uint32_t timeoutMs() const { return timeoutMs_; }

    // ---- 総合判定 ----

    // 緊急停止ラッチ中でもウォッチドッグ満了中でもなければ true。
    bool isOutputAllowed(uint32_t nowMs) const { return !latched_ && !isExpired(nowMs); }

    // FEEDBACK Byte7 の bit3 / bit4 を返す（他のビットは呼び出し側で OR する）。
    uint8_t statusFlags(uint32_t nowMs) const;

   private:
    uint32_t timeoutMs_;
    uint32_t lastFedMs_;
    bool everFed_;
    bool latched_;
};

}  // namespace motorcan
