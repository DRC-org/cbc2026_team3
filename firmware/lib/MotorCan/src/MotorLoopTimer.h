// 周期実行の時刻管理（FEEDBACK 送信・LED 点滅・補間更新）。
//
// millis() は約 49.7 日で 0 に戻る。符号つきの差や「次回時刻」を持つ実装だと
// 折り返し直後に永久に満了しなくなり、FEEDBACK が止まって PC 側が全モータ STALE、
// LED も消えたままになる。符号なしの差分で判定する形をここに 1 つだけ置く。
//
// Arduino.h を include しないのは意図的で、native 環境で折り返しを実時間を待たずに
// 検証できるようにするため。

#pragma once

#include <stdint.h>

namespace motorcan {

class PeriodicTimer {
   public:
    PeriodicTimer() : lastMs_(0) {}

    void reset(uint32_t nowMs) { lastMs_ = nowMs; }

    // 起点を直接置く。サーボ基板が全チャンネル同時送信を避けて位相をずらすのに使う。
    void setLastMs(uint32_t ms) { lastMs_ = ms; }

    // 満了していれば起点を nowMs へ進めて true を返す。
    // 起点を「前回 + interval」ではなく nowMs にするのは、処理が詰まって遅れたときに
    // 取り戻そうとして連続発火するのを避けるため（送信バーストでバスを埋める）。
    bool due(uint32_t nowMs, uint32_t intervalMs) {
        if (nowMs - lastMs_ < intervalMs) {
            return false;
        }
        lastMs_ = nowMs;
        return true;
    }

   private:
    uint32_t lastMs_;
};

}  // namespace motorcan
