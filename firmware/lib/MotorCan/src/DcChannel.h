// DC モータ 1 チャンネル分の「安全機構 + duty 目標」の結線。
//
// サーボ用の ServoChannel と同じ役割で、両者が同じ規則を持つことを保証するために
// 存在する。安全機構（MotorSafety）と目標値を別々に main.cpp が持つと、両者をどう
// 組み合わせるかがペリフェラルに埋まって native テストが 1 件も掛からない。実際
// サーボ側では「緊急停止ラッチ中でも SET_TARGET が通り、ラッチ中にサーボが動く」
// バグがその形で出た。
//
// この 2 つを組み合わせる規則はここだけが持つ。
//   - 出力が許可されていない間は新しい duty 指令を受け付けない
//   - 停止時は目標そのものを 0 に落とす（仕様書 §3.5: 解除した瞬間に動き出さない）
//
// DC 基板はエンコーダを持たないので ServoChannel のような補間・到達推定は無い。
// duty をそのまま出力段へ渡すだけで、位置・速度・電流の観測値も存在しない。
//
// Arduino.h を include しないのは意図的で、native 環境でそのままテストできるようにするため。

#pragma once

#include <stdint.h>

#include "MotorCanProtocol.h"
#include "MotorSafety.h"

namespace motorcan {

class DcChannel {
   public:
    explicit DcChannel(uint32_t commandTimeoutMs);

    // ---- 安全機構（仕様書 §5.1 / §5.2）----

    // 自分宛の SET_TARGET を受信したときに呼ぶ。制御タイプが duty でなくても、
    // 緊急停止ラッチ中でも呼ぶこと（仕様書 §6: 通信自体は生きている）。
    void feed(uint32_t nowMs);

    // E_STOP フレームを解釈する。停止でも解除でも目標 duty を 0 へ落とす（§3.5）。
    EStopAction handleEStopFrame(const uint8_t *data, uint8_t length);

    // CAN が上がらなかったときなど、PC から止められない状態で駆動させないための停止。
    void stop();

    // 基板上の物理緊急停止入力（REF）。押下でラッチし、離しても自動復帰しない。
    // 判断は MotorSafety::applyPhysicalStop が持つ。
    void applyPhysicalStop(bool active);

    void setWatchdogEnabled(bool enabled);
    void setCommandTimeoutMs(uint32_t timeoutMs);
    uint32_t commandTimeoutMs() const;

    bool isOutputAllowed(uint32_t nowMs) const;

    // FEEDBACK Byte7 の bit3 / bit4（他のビットは呼び出し側で OR する）。
    uint8_t safetyStatusFlags(uint32_t nowMs) const;

    // ---- 目標 duty（仕様書 §4 / §5.3）----

    // 出力が許可されていない間は受け付けず false を返す。受け付けると、PC が
    // §5.1 の契約どおり再送している間ずっとラッチ中の目標が更新され続け、
    // 解除した瞬間にその duty で回り出す。**feed() を先に呼ぶこと**（起動直後は
    // §5.4 により未受信＝出力禁止なので、順序を逆にすると最初の 1 通を捨てる）。
    bool setDuty(float duty, uint32_t nowMs);

    // シリアルデバッグの 's' 等、その場で止めたいとき。
    void hold();

    // 出力段へ渡す duty。出力禁止中は目標に関わらず 0 を返すので、
    // 呼び出し側が安全機構を迂回する経路を書けない。
    float outputDuty(uint32_t nowMs) const;

   private:
    MotorSafety safety_;
    float duty_;
};

}  // namespace motorcan
