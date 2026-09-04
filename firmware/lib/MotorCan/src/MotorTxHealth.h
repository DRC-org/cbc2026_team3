// CAN 送信の連続失敗を数え、閾値で警告へ倒すかを決める（3 枚で共通）。
//
// 送信 API の戻り値を捨てると、送信が 1 通も出ていない基板が平常時と同じ
// ハートビートを出し続ける。実機では DC 基板の INFO が 4 秒間止まっていても
// LED にもログにも現れなかった。数える規則をここに 1 つだけ置き、
// 3 枚の main.cpp / app.cpp が各自の変数で持ち直さないようにする。
//
// **閾値そのものはここに持たない。** 何通で警告に倒すかは送信の周期と
// 1 通あたりの待ち時間（サーボ基板の mcp_can は最大 5ms）に依存し、基板ごとに違う。
// 判定に使う値は呼び出し側の config.h が持つ。
//
// Arduino.h を include しないのは意図的で、native 環境で検証できるようにするため。

#pragma once

#include <stdint.h>

namespace motorcan {

class TxFailCounter {
   public:
    TxFailCounter() : streak_(0) {}

    void onSuccess() { streak_ = 0; }

    // 飽和させるのは、折り返して 0 に戻ると**警告が消える**ため。
    // バス不通のように失敗が続く状況こそ数えたい場面なので、上限で張り付かせる。
    void onFailure() {
        if (streak_ < 0xFFFF) {
            ++streak_;
        }
    }

    bool isAlarming(uint16_t threshold) const { return streak_ >= threshold; }

    uint16_t streak() const { return streak_; }

   private:
    uint16_t streak_;
};

}  // namespace motorcan
