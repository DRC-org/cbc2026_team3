#include "ServoMotion.h"

#include <math.h>

#include "MotorCanProtocol.h"

namespace motorcan {

namespace {

// 補間完了の判定に使う許容誤差 [deg]。
// 経過時間 × スルーレートを float で計算すると、ちょうど到達時刻でも
// 数 ULP だけ距離に届かないことがあり、そのまま比較すると到達が 1 周期遅れる。
// 0.0001deg はどのサーボの分解能よりも細かく、機械的には無視できる。
constexpr float kTravelEpsilonDeg = 1e-4f;

float clampFloat(float value, float low, float high) {
    if (value < low) {
        return low;
    }
    if (value > high) {
        return high;
    }
    return value;
}

}  // namespace

uint16_t angleToPulseUs(float angleDeg, const ServoPulseSpec &spec) {
    if (spec.angleRangeDeg <= 0.0f) {
        // 変換が定義できない。可動範囲の下端へ倒す方が、未定義の値を出すより読みやすい。
        return spec.minUs;
    }

    const float span = static_cast<float>(spec.maxUs) - static_cast<float>(spec.minUs);
    const float ratio = clampFloat(angleDeg / spec.angleRangeDeg, 0.0f, 1.0f);
    const float us = static_cast<float>(spec.minUs) + span * ratio;

    const uint16_t low = spec.minUs < spec.maxUs ? spec.minUs : spec.maxUs;
    const uint16_t high = spec.minUs < spec.maxUs ? spec.maxUs : spec.minUs;
    const long rounded = lroundf(us);
    if (rounded <= static_cast<long>(low)) {
        return low;
    }
    if (rounded >= static_cast<long>(high)) {
        return high;
    }
    return static_cast<uint16_t>(rounded);
}

ServoMotion::ServoMotion(float initialAngleDeg, const ServoLimits &limits)
    : limits_{0.0f, 0.0f, kDefaultSlewRateDegPerSec},
      reachedToleranceDeg_(kDefaultServoReachedToleranceDeg),
      startAngleDeg_(0.0f),
      startMs_(0),
      currentAngleDeg_(0.0f),
      targetAngleDeg_(0.0f),
      reached_(true),
      lastNowMs_(0) {
    // setLimits の正規化（min/max の入れ替え・非正 slew_rate の拒否）を一箇所に集めるため、
    // コンストラクタでもそれを通す。
    setLimits(limits);

    const float initial = initialAngleDeg;
    currentAngleDeg_ = clampAngle(initial);
    targetAngleDeg_ = currentAngleDeg_;
    startAngleDeg_ = currentAngleDeg_;
}

float ServoMotion::clampAngle(float angleDeg) const {
    return clampFloat(angleDeg, limits_.angleMinDeg, limits_.angleMaxDeg);
}

void ServoMotion::anchorAt(uint32_t nowMs) {
    startAngleDeg_ = currentAngleDeg_;
    startMs_ = nowMs;
    lastNowMs_ = nowMs;
}

void ServoMotion::setTarget(float angleDeg, uint32_t nowMs) {
    // NaN の防御はここには無い。**CAN は int16 しか運ばず**（仕様書 §4）、float が
    // 入る唯一の経路であるシリアルデバッグは motorcan::toRaw を通す。
    anchorAt(nowMs);
    targetAngleDeg_ = clampAngle(angleDeg);

    const float remaining = targetAngleDeg_ - currentAngleDeg_;
    reached_ = fabsf(remaining) <= reachedToleranceDeg_;
}

void ServoMotion::update(uint32_t nowMs) {
    lastNowMs_ = nowMs;

    // millis() は約 49.7 日で 0 に戻る。符号なしの引き算で経過時間を出すことで、
    // 折り返しの瞬間に補間が巻き戻ってサーボが逆走するのを避ける。
    const uint32_t elapsedMs = nowMs - startMs_;

    const float distance = targetAngleDeg_ - startAngleDeg_;
    const float absDistance = fabsf(distance);
    const float travel = limits_.slewRateDegPerSec * (static_cast<float>(elapsedMs) * 0.001f);

    if (travel + kTravelEpsilonDeg >= absDistance) {
        currentAngleDeg_ = targetAngleDeg_;
    } else {
        currentAngleDeg_ = startAngleDeg_ + (distance < 0.0f ? -travel : travel);
    }

    reached_ = fabsf(targetAngleDeg_ - currentAngleDeg_) <= reachedToleranceDeg_;
}

void ServoMotion::holdHere(uint32_t nowMs) {
    anchorAt(nowMs);
    targetAngleDeg_ = currentAngleDeg_;
    reached_ = true;
}

void ServoMotion::setLimits(const ServoLimits &limits) {
    ServoLimits next = limits;

    if (next.angleMinDeg > next.angleMaxDeg) {
        const float swapped = next.angleMinDeg;
        next.angleMinDeg = next.angleMaxDeg;
        next.angleMaxDeg = swapped;
    }
    if (!(next.slewRateDegPerSec > 0.0f)) {
        // 0 や負値は「即座に飛ぶ」とも「永久に到達しない」とも読め、どちらも危険。
        // NaN もここで弾かれる（比較が常に false になるため）。
        next.slewRateDegPerSec = limits_.slewRateDegPerSec;
    }

    limits_ = next;

    // 直近に観測した時刻へアンカーし直す。し直さないと、変更後のスルーレートが
    // 変更前の経過時間にさかのぼって効いて角度が飛ぶ。
    anchorAt(lastNowMs_);

    // **現在角には手を付けない。** 現在角はサーボが実際に居る位置の推定値であって、
    // 範囲を狭めた瞬間にそこへ書き換えると、スルーレート制限の外側で 1 ティックも
    // 待たずに飛ぶ（可動範囲を 20deg 上げただけで指令パルスが 20deg 分ジャンプする）。
    // §7.6 の変更は**目標に対する制約**なので、クランプするのは目標角だけにして、
    // 範囲の外に出た現在角はクランプ後の目標へ向かって補間で戻す。
    targetAngleDeg_ = clampAngle(targetAngleDeg_);
    reached_ = fabsf(targetAngleDeg_ - currentAngleDeg_) <= reachedToleranceDeg_;
}

void ServoMotion::setReachedToleranceDeg(float toleranceDeg) {
    if (toleranceDeg < 0.0f) {
        return;
    }
    reachedToleranceDeg_ = toleranceDeg;
}


}  // namespace motorcan
