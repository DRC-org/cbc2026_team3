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

    // 基板上の物理緊急停止入力（DC 基板の REF）を毎ループ反映する。
    //
    // **押下でラッチし、離しても自動復帰しない。** レベル追従にすると、PC が §5.1 の
    // 契約どおり 20Hz で目標値を再送し続けている以上、スイッチを離した瞬間に機体が
    // 動き出す。解除は CAN の E_STOP 解除フレーム（操縦者の明示操作）だけに限る。
    //
    // 逆に、押している間は解除フレームが届いても次のループでここが再ラッチするので、
    // 「押している間は絶対に動かない」が呼び出し順序に依らず成立する。
    void applyPhysicalStop(bool active) {
        if (active) {
            stop();
        }
    }

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

    // 「一度でも指令を受けたうえで満了した」= 本物の CAN 通信途絶なら true。
    // 起動直後の未受信（isExpired が true になる）と区別するために要る。
    // 出力の可否は isExpired 側で判断し、こちらはウォッチドッグの報告にだけ使う。
    bool isCommandLost(uint32_t nowMs) const;

    void setTimeoutMs(uint32_t timeoutMs) { timeoutMs_ = timeoutMs; }
    uint32_t timeoutMs() const { return timeoutMs_; }

    // ウォッチドッグそのものの有効/無効（仕様書 §5.1 / §8）。無効にすると途絶しても
    // 駆動を許可し、ウォッチドッグの報告もしない。書き換えてよいのは setup() が
    // config.h の WATCHDOG_ENABLED を写すときだけで、CAN の SET_PARAM からは触らせない
    // （PC 側の 1 フレームで最後の砦が外れる経路を作らないため）。
    //
    // ビルド時の #if ではなく実行時フラグにしてあるのは、両ファームの main.cpp が
    // 同じ #if 分岐を各自で持つと片方に入れ忘れられるため。実際 WATCHDOG_ENABLED は
    // servo にだけ効き、dc_motor では「設定しても効かないフラグ」だった時期がある。
    void setWatchdogEnabled(bool enabled) { watchdogEnabled_ = enabled; }

    // ---- 総合判定 ----

    // 緊急停止ラッチ中でもウォッチドッグ満了中でもなければ true。
    // 駆動ゲートはすべてこれを通すこと（isExpired を直に見ると無効化フラグを迂回する）。
    //
    // everFed_ を watchdogEnabled_ の外に出してあるのは、仕様書 §5.4 の
    // 「SET_TARGET を 1 通も受け取るまで出力を許可しない」が**ウォッチドッグの
    // 有効/無効とは別の条件**だから。中に入れると WATCHDOG_ENABLED 0 の基板が
    // CAN 通信ゼロのまま setup() でゲートドライバを開く。無効化で外れるのは
    // 「途絶したら止める」ことだけで、最初の 1 通を待つゲートは外れない
    // （ベンチ確認の逃げ道は残る。最初の cansend でゲートが開く）。
    bool isOutputAllowed(uint32_t nowMs) const {
        return !latched_ && everFed_ && !(watchdogEnabled_ && isExpired(nowMs));
    }

    // FEEDBACK Byte7 の緊急停止 / ウォッチドッグのビットを返す（他は呼び出し側で OR する）。
    // ウォッチドッグのビットは isCommandLost() に従うので、起動直後の未受信では立たない。
    uint8_t statusFlags(uint32_t nowMs) const;

   private:
    uint32_t timeoutMs_;
    uint32_t lastFedMs_;
    bool everFed_;
    bool latched_;
    bool watchdogEnabled_;
};

}  // namespace motorcan
