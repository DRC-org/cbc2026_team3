// サーボ 1 チャンネル分の「安全機構 + 角度補間」の結線（仕様書 §7.1 / §7.5）。
//
// MotorSafety（緊急停止ラッチ・ウォッチドッグ）と ServoMotion（補間）を別々に
// 持たせると、両者をどう組み合わせるかが servo/main.cpp の中だけに書かれ、
// ペリフェラルに埋まって native テストが 1 件も掛からない。実際そこで
// **緊急停止ラッチ中でも SET_TARGET が通り、ラッチ中にサーボが動く**バグが出た
// （再送のたびに補間が再アンカーされ、次のティックで slew_rate × 5ms ずつ進む）。
//
// この 2 つを組み合わせる規則はここだけが持つ。
//   - 出力が許可されていない間は新しい目標角を受け付けない
//   - 出力が許可されていない間は、補間を進める**前に**現在角で凍結する
// main.cpp には ServoMotion / MotorSafety を直に触る経路を残さない（残すと
// 「凍結しない setTarget」「凍結より先に進む update」を書き直せてしまう）。
//
// Arduino.h を include しないのは意図的で、native 環境でそのままテストできるようにするため。

#pragma once

#include <stdint.h>

#include "MotorCanProtocol.h"
#include "MotorSafety.h"
#include "ServoMotion.h"

namespace motorcan {

class ServoChannel {
   public:
    ServoChannel(float initialAngleDeg, const ServoLimits &limits, uint32_t commandTimeoutMs);

    // ---- 安全機構（仕様書 §5.1 / §5.2 / §7.5）----

    // 自分宛の SET_TARGET を受信したときに呼ぶ。制御タイプが position でなくても、
    // 緊急停止ラッチ中でも呼ぶこと（仕様書 §6: 通信自体は生きている）。
    void feed(uint32_t nowMs);

    // E_STOP フレームを解釈する。停止でも解除でも、その場で現在角へ凍結する（§7.5）。
    // 解除で凍結するのは、「解除した瞬間に動き出さない」を成立させるため（§3.5）。
    EStopAction handleEStopFrame(const uint8_t *data, uint8_t length, uint32_t nowMs);

    // CAN が上がらなかったときなど、PC から止められない状態で駆動させないための停止。
    void stop(uint32_t nowMs);

    void setWatchdogEnabled(bool enabled);
    void setCommandTimeoutMs(uint32_t timeoutMs);
    uint32_t commandTimeoutMs() const;

    bool isOutputAllowed(uint32_t nowMs) const;

    // FEEDBACK Byte7 の bit3 / bit4（他のビットは呼び出し側で OR する）。
    uint8_t safetyStatusFlags(uint32_t nowMs) const;

    // ---- 目標角（仕様書 §7.2 / §7.5）----

    // 出力が許可されていない間は受け付けず false を返す（仕様書 §7.5:
    // 「新しい角度指令の受け付けを止め、その時点の角度を保持し続ける」）。
    // 受け付けると、ラッチ中の再送のたびに補間が再アンカーされて緊急停止中に動き、
    // ラッチ中の指令が解除後に実行される。**feed() を先に呼ぶこと**（起動直後は
    // §5.4 により未受信＝出力禁止なので、順序を逆にすると最初の 1 通を捨てる）。
    bool setTarget(float angleDeg, uint32_t nowMs);

    // シリアルデバッグの 's' 等、その場で現在角へ凍結したいとき。
    void hold(uint32_t nowMs);

    // 補間を 1 ティック進める。出力禁止中は進める前に凍結する。
    void tick(uint32_t nowMs);

    // ---- SET_PARAM 0x07 / 0x10-0x12（仕様書 §7.6）----

    void setLimits(const ServoLimits &limits);
    const ServoLimits &limits() const;
    void setReachedToleranceDeg(float toleranceDeg);

    // ---- 観測値（FEEDBACK 用。仕様書 §7.4）----

    float currentAngleDeg() const;
    float currentSlewDegPerSec() const;
    bool isReached() const;

   private:
    MotorSafety safety_;
    ServoMotion motion_;
};

}  // namespace motorcan
