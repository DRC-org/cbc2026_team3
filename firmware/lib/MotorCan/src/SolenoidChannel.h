// 電磁弁 1 チャンネル分の「安全機構 + ON/OFF 目標」の結線（仕様書 §9）。
//
// DC 用の DcChannel・サーボ用の ServoChannel と同じ役割で、3 枚が同じ規則を持つことを
// 保証するために存在する。安全機構（MotorSafety）と目標値を別々に app.cpp が持つと、
// 両者をどう組み合わせるかがペリフェラルに埋まって native テストが 1 件も掛からない。
// 実際サーボ側では「緊急停止ラッチ中でも SET_TARGET が通り、ラッチ中にサーボが動く」
// バグがその形で出た。
//
// この 2 つを組み合わせる規則はここだけが持つ。
//   - 出力が許可されていない間は新しい ON/OFF 指令を受け付けない
//   - 停止時は目標そのものを OFF に落とす（仕様書 §3.5: 解除した瞬間に動き出さない）
//
// **止める = 消磁であり、それが唯一の安全側**（仕様書 §9.4）。サーボの「現在角を保持」に
// 相当する扱いは持たない。吸着で保持しているワークは落ちるが、断線・PC 停止・操縦者の
// 緊急停止のいずれでも確実に無通電へ倒れることを優先している。
//
// **物理非常停止入力（DC 用の REF）はこの基板に無い**ので applyPhysicalStop も持たない。
// 配線の無いピンに対する API を残すと、実機で効かない安全機構を「ある」と読ませてしまう。
//
// Arduino.h も STM32 HAL も include しないのは意図的で、native 環境（pio test -e native）で
// そのままテストできるようにするため。
//
// **CubeMX + CMake の firmware/solenoid/ からも、PlatformIO の native テストからも
// 同じソースを参照する。** 片方だけを直せる形にしてはならない。

#pragma once

#include <stdint.h>

#include "MotorCanProtocol.h"
#include "MotorSafety.h"

namespace motorcan {

class SolenoidChannel {
   public:
    explicit SolenoidChannel(uint32_t commandTimeoutMs);

    // ---- 安全機構（仕様書 §5.1 / §5.2）----

    // 自分宛の SET_TARGET を受信したときに呼ぶ。制御タイプが on_off でなくても、
    // 緊急停止ラッチ中でも呼ぶこと（仕様書 §6: 通信自体は生きている）。
    void feed(uint32_t nowMs);

    // E_STOP フレームを解釈する。停止でも解除でも目標を OFF へ落とす（§3.5）。
    EStopAction handleEStopFrame(const uint8_t *data, uint8_t length);

    // CAN が上がらなかったときなど、PC から止められない状態で通電させないための停止。
    void stop();

    void setWatchdogEnabled(bool enabled);
    void setCommandTimeoutMs(uint32_t timeoutMs);
    uint32_t commandTimeoutMs() const;

    bool isOutputAllowed(uint32_t nowMs) const;

    // FEEDBACK Byte0 の緊急停止 / ウォッチドッグのビット（他は呼び出し側で OR する）。
    uint8_t safetyStatusFlags(uint32_t nowMs) const;

    // ---- 目標 ON/OFF（仕様書 §9.2）----

    // 出力が許可されていない間は受け付けず false を返す。受け付けると、PC が §5.1 の
    // 契約どおり再送している間ずっとラッチ中の目標が更新され続け、解除した瞬間に
    // その状態で通電する。**feed() を先に呼ぶこと**（起動直後は §5.4 により未受信＝
    // 出力禁止なので、順序を逆にすると最初の 1 通を捨てる）。
    bool setOn(bool on, uint32_t nowMs);

    // シリアルデバッグの 's' 等、その場で消磁したいとき。
    void hold();

    // 出力段（GPIO）へ渡す状態。出力禁止中は目標に関わらず false を返すので、
    // 呼び出し側が安全機構を迂回する経路を書けない。
    // **出力へ至る経路はこの 1 本だけにすること**（仕様書 §9.4）。
    bool outputOn(uint32_t nowMs) const;

   private:
    MotorSafety safety_;
    bool on_;
};

}  // namespace motorcan
