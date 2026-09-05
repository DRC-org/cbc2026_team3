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
//   - 出力が許可されていない間は SET_PARAM 0x03-0x06 も効かせない（保留する）
//   - 受理する制御タイプは position だけ（仕様書 §7.2）
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
    // **begin() を呼ぶまで出力を許可しない。** サーボ基板のスロット設定は DIP の基板番号で
    // 選ぶので実行時にしか確定せず、g_channel[] は静的初期化子を持てない（config.h の
    // kSlotsByBoard）。既定の可動範囲は幅 0 で、どんな目標角も初期角へクランプされる。
    //
    // **未初期化のまま駆動側へ倒れないことがこのコンストラクタの要件。** 空きスロットは
    // begin() されないまま残るので、そこへ SET_TARGET が届いても（PC 側 yaml の can_id を
    // 書き間違えれば届く）1 度もパルスが出ない側に落ちる必要がある。
    ServoChannel();

    // 役割とスロット設定が確定した後に呼ぶ初期化（setup() から 1 回）。
    // **setWatchdogEnabled() より先に呼ぶこと** —— 安全機構ごと作り直すので、
    // 順序を逆にすると config.h の WATCHDOG_ENABLED 0 が既定の「有効」で上書きされる。
    void begin(float initialAngleDeg, const ServoLimits &limits, uint32_t commandTimeoutMs);

    // 初期値が静的に決まる呼び出し側（テストと、スロット設定を持たない基板）のための
    // 短縮形。**規則の実装は begin() ただ 1 つ**で、ここはそこへ委譲するだけにする
    // （2 通りに書くと、begin() だけに足した条件が構築経路から漏れる）。
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

    // begin() 前は常に false。駆動ゲートはすべてここを通るので、
    // 未初期化のチャンネルは setTarget も tick も動かせない。
    bool isOutputAllowed(uint32_t nowMs) const;

    // FEEDBACK Byte0 の緊急停止 / ウォッチドッグのビット（他は呼び出し側で OR する）。
    uint8_t safetyStatusFlags(uint32_t nowMs) const;

    // ---- 目標角（仕様書 §7.2 / §7.5）----

    // 自分宛の SET_TARGET 1 通をそのまま渡す。**受理できる制御タイプの判定はここが
    // 唯一の持ち主**（仕様書 §7.2: position のみ）。main.cpp 側で判定すると、
    // ペリフェラルの翻訳単位は native テストの対象外（common.ini の `test_ignore = *`）
    // なので、その 1 行を消しても全ケース緑のままになる。
    // **feed() を先に呼ぶこと**（§6: 受理できないタイプでも通信自体は生きている）。
    bool applySetTarget(const SetTargetCommand &cmd, uint32_t nowMs);

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

    // ---- SET_PARAM 0x03-0x06（仕様書 §7.6）----

    // **出力禁止中は効かせず、範囲だけを覚えて解除時に取り込む**（仕様書 §7.5）。
    // ServoMotion::setLimits は目標角を新しい範囲へクランプするので、ラッチ中に
    // 通すと「新しい角度指令の受け付けを止める」というゲートの外側から目標が動く
    // （setTarget が入口で拒否しているのに、SET_PARAM が同じことをできてしまう）。
    void setLimits(const ServoLimits &limits, uint32_t nowMs);

    // 保留中なら保留値を返す。**返さないと、ラッチ中に angle_min → angle_max と
    // 2 通届いたとき、呼び出し側の read-modify-write が 1 通目を取りこぼす。**
    const ServoLimits &limits() const;

    void setReachedToleranceDeg(float toleranceDeg, uint32_t nowMs);

    // ---- 観測値（FEEDBACK 用。仕様書 §7.4）----

    float currentAngleDeg() const;
    bool isReached() const;

   private:
    // 保留していた SET_PARAM を取り込む。出力が許可されている文脈でのみ呼ぶこと。
    void applyPendingParams(uint32_t nowMs);

    MotorSafety safety_;
    ServoMotion motion_;

    // 出力禁止中に届いた SET_PARAM 0x03-0x06 の保留値（仕様書 §7.5）。
    ServoLimits pendingLimits_;
    float pendingToleranceDeg_;
    bool hasPendingLimits_;
    bool hasPendingTolerance_;

    // begin() 済みか。**everFed_ とは別の条件**なので MotorSafety へ寄せない ——
    // あちらは「PC から 1 通も指令が来ていない」で、こちらは「このスロットは
    // そもそも使わない」。混ぜると、空きスロットが最初の SET_TARGET 1 通で駆動側へ回る。
    bool begun_;
};

}  // namespace motorcan
