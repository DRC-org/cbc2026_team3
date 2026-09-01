// サーボ用モタドラの角度補間・可動範囲クランプ・到達推定。
// 単一情報源は docs/motor_driver_can_protocol.md §7 であり、PC 側 lib/drivers/generic.py と
// 対になる。片方だけを変更してはならない。
//
// Arduino.h を include しないのは意図的で、native 環境（pio test -e native）で
// そのままコンパイルしてテストできるようにするため。時刻は nowMs を引数で受け取り、
// millis() を内部で呼ばない（満了や折り返しを実時間を待たずに検証できる）。

#pragma once

#include <stdint.h>

namespace motorcan {

// 仕様書 §7.6 の slew_rate 既定値。到達推定時間の根拠でもある。
constexpr float kDefaultSlewRateDegPerSec = 90.0f;

// 仕様書 §7.3 の到達判定は「補間が完了した時点」。既定の許容差を 0 にしてあるのは、
// サーボが実測値を持たず「目標角 - 指令角」がそのまま補間の残りだから。
// SET_PARAM 0x03（reached_tolerance）で 0 以外にすると、補間完了より手前で到達を報告する。
constexpr float kDefaultServoReachedToleranceDeg = 0.0f;

// 仕様書 §7.2 の可動範囲と §7.6 のスルーレート。チャンネル単位で持つ。
struct ServoLimits {
    float angleMinDeg;
    float angleMaxDeg;
    float slewRateDegPerSec;
};

// サーボの角度 → パルス幅の対応。データシート由来の機体依存値で config.h が持つ。
struct ServoPulseSpec {
    uint16_t minUs;
    uint16_t maxUs;
    float angleRangeDeg;
};

// angleDeg=0 で minUs、angleDeg=angleRangeDeg で maxUs の線形変換。
// 結果は必ず [minUs, maxUs] に収める。
//
// 270 度サーボを Arduino Servo の write()（0-180 しか取れない）で扱うために
// 角度へ 180/270 を掛けるサンプルが出回っているが、それをすると分解能が 2/3 に落ち、
// 可動範囲の端が表現できない。ここではスケール変換をせず角度から直接パルス幅を出す。
//
// angleRangeDeg が非正、または角度が NaN の場合は minUs を返す。NaN は
// ServoMotion::setTarget が先に弾くので、ここへ来るのは config.h の書き損じのときだけ。
uint16_t angleToPulseUs(float angleDeg, const ServoPulseSpec &spec);

// 1 チャンネル分の角度補間。
//
// 位置フィードバックを持たないサーボの「現在角」は、指令角をスルーレートで
// 補間した推定値でしかない（仕様書 §7.3 / §7.4）。
class ServoMotion {
   public:
    ServoMotion(float initialAngleDeg, const ServoLimits &limits);

    // 仕様書 §7.2: 可動範囲外はメカストッパで停動して焼損するため必ずクランプする。
    // nowMs を取るのは、補間の起点をここでアンカーし直すため。前回 update() からの
    // 差分方式にすると、指令が来ない間に溜まった経過時間で一気に飛ぶ。
    void setTarget(float angleDeg, uint32_t nowMs);

    // スルーレート制限つきで指令角を目標角へ近づける。毎ループ呼ぶ。
    void update(uint32_t nowMs);

    float currentAngleDeg() const { return currentAngleDeg_; }
    float targetAngleDeg() const { return targetAngleDeg_; }

    // 仕様書 §7.3 の到達推定。**実測ではなく推定であり、脱調・過負荷・メカ干渉で
    // 実際には動いていなくても true を返す。** PC 側 move_to はこのフラグで次の
    // ステップへ進むため、機構が引っかかっていてもシーケンスは進んでしまう。
    // 危険な動作には require_trigger / auto_stop で人間の目視確認を挟むこと。
    bool isReached() const { return reached_; }

    // 現在角で凍結する（緊急停止・ウォッチドッグ満了時に使う）。
    // 仕様書 §7.5: サーボは PWM を止めると脱力して back-drivable になり、壁が自重で
    // 倒れ、グリッパが把持中のワークを落とす。出力を切らずに目標角を現在角へ落とす。
    void holdHere(uint32_t nowMs);

    // SET_PARAM 0x04-0x06（slew_rate / angle_min / angle_max）。angle_min > angle_max は入れ替えて正規化し、
    // 非正の slew_rate は採用せず従来値を維持する（どちらの解釈でも危険なため）。
    // 現在の目標角は新しい可動範囲へクランプし直すが、**現在角は動かさない**
    // （動かすとスルーレート制限の外側で指令パルスが飛ぶ。範囲外へ出た現在角は補間で戻る）。
    //
    // **出力禁止中に呼んでよいかの判断はここには無い。** それは安全機構との結線なので
    // ServoChannel が持つ（ここで判断すると main.cpp から直に呼ぶ迂回路が書ける）。
    void setLimits(const ServoLimits &limits);
    const ServoLimits &limits() const { return limits_; }

    // SET_PARAM 0x03（reached_tolerance）。
    void setReachedToleranceDeg(float toleranceDeg);

   private:
    void anchorAt(uint32_t nowMs);
    float clampAngle(float angleDeg) const;

    ServoLimits limits_;
    float reachedToleranceDeg_;

    // 補間の起点。current を毎周期積算せず起点からの経過時間で出すのは、
    // 浮動小数の累積誤差で到達時刻が 1 周期ずれるのを避けるため。
    float startAngleDeg_;
    uint32_t startMs_;

    float currentAngleDeg_;
    float targetAngleDeg_;
    bool reached_;

    // setLimits() が補間をアンカーし直すために使う、直近に観測した時刻。
    uint32_t lastNowMs_;
};

}  // namespace motorcan
