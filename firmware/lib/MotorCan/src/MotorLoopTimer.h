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

    // FEEDBACK の送信位相をチャンネルごとにずらす（3 枚とも setup() で使う）。
    //
    // 全チャンネルが同じ周期で同時に送るとフレームのバーストになり、他バスの周期送信と
    // 重なったときに調停待ちが伸びて FEEDBACK の間隔が波打つ。周期を等分した位相を
    // 割り当てて平準化する。**起点は次回満了が index/count の位置に来るよう過去へ置く。**
    //
    // 3 枚の main.cpp / app.cpp がこの式とその理由を各自で持っていた。1 箇所に置いて
    // native テスト（test_board）で固定する。**起点を直接置く口はここだけにしてある** ——
    // 素の setter を残すと、式を手で書き直した基板が 1 枚だけ同時送信に戻る。
    void stagger(uint32_t startMs, uint32_t intervalMs, uint8_t index, uint8_t count) {
        if (count == 0) {
            reset(startMs);
            return;
        }
        lastMs_ = startMs - intervalMs + (intervalMs * index) / count;
    }

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

// 「今どの速さで点滅すべきか」を決めるための、基板全体の状態（3 枚で共通）。
//
// **色や GPIO の叩き方はここに持たない。** DC 用とサーボ用は RGB LED 1 個、
// 電磁弁用は単色 LED 1 本で、出し方がまるごと違う。共通なのは
// 「全チャンネルを走査して unconfigured と stopped を集める → 間隔を選ぶ」までで、
// そこだけを引き上げてある。
struct BoardIndication {
    // CAN が上がらなかった基板は PC から止められない。ID 未設定と同じ「今すぐ
    // 直さないと使えない」側に入れる（仕様書 §2.2）。
    explicit BoardIndication(bool canFailed) : urgent(canFailed), stopped(false) {}

    // チャンネル 1 つぶんを足す。**どのチャンネルを数えるかは呼び出し側が決める** ——
    // サーボ基板は Unused スロットを数えず、緊急停止はサーボスロットからしか見ない。
    void observe(bool configured, bool latched) {
        if (!configured) {
            urgent = true;
        }
        if (latched) {
            stopped = true;
        }
    }

    bool urgent;
    bool stopped;
};

// 点滅間隔を選ぶ。**3 通りの規則を引数で受ける** —— 電磁弁基板は LED が 1 本しか
// 無いので緊急停止を専用の速さ（500ms）で示すが、DC 用とサーボ用は色で示すので
// stoppedMs に heartbeatMs と同じ値を渡す。ここで基板を判別して分岐すると、
// LED の本数という機体側の事情がライブラリに入り込む。
inline uint32_t blinkIntervalFor(const BoardIndication &indication, uint32_t urgentMs,
                                 uint32_t stoppedMs, uint32_t heartbeatMs) {
    if (indication.urgent) {
        return urgentMs;
    }
    if (indication.stopped) {
        return stoppedMs;
    }
    return heartbeatMs;
}

}  // namespace motorcan
