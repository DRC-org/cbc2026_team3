// シリアルデバッグ中のウォッチドッグ上書き（3 枚共通。仕様書 §5.1）。
//
// シリアルから 1 行入力すると、そのチャンネルのコマンドウォッチドッグを毎ループ
// 養い続ける。1 回だけ養う実装だと command_timeout_ms 後に必ず止まってデバッグに
// ならないので、上書きモードそのものは必要である。問題はその**範囲と期限**で、
// ここが持つ規則は 2 つ。どちらも「最後の砦を基板単位で外さない」ためにある。
//
//   - **養うのは操作したチャンネルだけ。** かつては 3 枚とも全チャンネルを養って
//     いたので、電磁弁基板の UART へ `2 1` と 1 行打った後に PC が落ちると、
//     本来 500ms で全 6ch が消磁するはずのところ、**触っていない 5ch も含めて
//     通電したまま**残った（CAN も PC も死んでいるので E_STOP も届かない）。
//     DC 基板なら同じ経路でコンベアが回り続ける
//   - **最後の入力から kSerialOverrideHoldMs で自動解除する。** 解除が 's' 入力・
//     CAN の SET_TARGET・電源断だけだと時間切れが 1 つも無く、打ったまま席を
//     離れた基板が無期限にウォッチドッグを外したままになる
//
// **3 枚の main.cpp / app.cpp に書き写してはならない。** ペリフェラルの翻訳単位は
// native テストの対象外（common.ini の `test_ignore = *`）なので、写しがそこに
// あると 1 枚だけ規則が抜けても全ケース緑のままになる。
//
// Arduino.h も HAL も include しないのは意図的で、native 環境で満了を実時間を
// 待たずに検証できるようにするため。

#pragma once

#include <stdint.h>

#include "MotorCanProtocol.h"
#include "MotorCanRouter.h"

namespace motorcan {

// 上書きが生きている時間。**PC が SET_PARAM で設定できる猶予の上限と同じにする。**
// シリアルのデバッグモードが、CAN 経由で設定できる最大の猶予より長く砦を外せて
// よい理由は無い（kMaxCommandTimeoutMs の根拠がそのまま効く: これを超えると
// 「PC が落ちてもコンベアが数秒回り続ける」ことになり砦として機能しない）。
constexpr uint32_t kSerialOverrideHoldMs = kMaxCommandTimeoutMs;

class SerialOverride {
   public:
    // シリアルで 1 チャンネルを操作した。
    //
    // **満了していた分は復活させない。** 復活させると、ch2 を放置して満了させた後に
    // ch3 を 1 行打っただけで ch2 まで養われ直し、期限が期限として機能しなくなる。
    void note(uint8_t channel, uint32_t nowMs) {
        if (channel >= kMaxChannels) {
            return;
        }
        if (!active(nowMs)) {
            mask_ = 0;
        }
        mask_ = static_cast<uint8_t>(mask_ | (1u << channel));
        lastMs_ = nowMs;
    }

    // 's' 入力・CAN の SET_TARGET / E_STOP で明示的に降りる。
    void clear() { mask_ = 0; }

    // このチャンネルのウォッチドッグを養ってよいか。
    bool shouldFeed(uint8_t channel, uint32_t nowMs) const {
        if (channel >= kMaxChannels) {
            return false;
        }
        return active(nowMs) && (mask_ & static_cast<uint8_t>(1u << channel)) != 0;
    }

    // 1 チャンネルでも上書きが生きているか。
    // millis() の折り返しは符号なしの差分で吸収する（MotorLoopTimer.h と同じ形）。
    bool active(uint32_t nowMs) const {
        return mask_ != 0 && (nowMs - lastMs_) <= kSerialOverrideHoldMs;
    }

   private:
    uint8_t mask_ = 0;
    uint32_t lastMs_ = 0;
};

}  // namespace motorcan
