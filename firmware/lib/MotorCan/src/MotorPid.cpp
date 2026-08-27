#include "MotorPid.h"

namespace motorcan {

MotorPid::MotorPid(float kp, float ki, float kd, float integralLimit)
    : kp_(kp),
      ki_(ki),
      kd_(kd),
      integralLimit_(integralLimit),
      integral_(0.0f),
      previousError_(0.0f),
      hasPreviousError_(false) {}

void MotorPid::setKi(float ki) {
    // ki を動かした瞬間に既存の積分値が別の大きさの出力に化けて急発進しないよう、
    // ゲイン変更時は積分を捨てる。
    if (ki != ki_) {
        integral_ = 0.0f;
    }
    ki_ = ki;
}

void MotorPid::reset() {
    integral_ = 0.0f;
    previousError_ = 0.0f;
    hasPreviousError_ = false;
}

float MotorPid::update(float error, float dtSec) {
    if (error != error) {
        // NaN の誤差を積分項・前回誤差へ入れると、以後**正常な目標に対しても**
        // 出力が NaN のままになる（ワインドアップ制限は NaN を素通しする。
        // integral_ > limit も < -limit も NaN では false）。内部状態を汚さずに 0 を返し、
        // 次の正常な誤差でそのまま復帰できるようにする。
        return 0.0f;
    }
    if (!(dtSec > 0.0f)) {
        // dt が 0 や NaN だと微分が発散する。制御量を変えずに前回値を保つのが安全。
        return kp_ * error;
    }

    integral_ += error * dtSec;
    if (integral_ != integral_) {
        // 誤差・dt が有限でも積分は inf - inf などで NaN になりうる。
        // 一度なると自力では戻らないので、その場で捨てて自己回復させる。
        integral_ = 0.0f;
    }

    // ワインドアップ制限は ki を掛けた後の寄与で見る。
    // 目標に長く届かない状態（機械的な突き当たりなど）で積分が育ち切ると、
    // 拘束が外れた瞬間にフルパワーで飛び出す。
    if (ki_ != 0.0f) {
        const float limit = integralLimit_ / (ki_ < 0.0f ? -ki_ : ki_);
        if (integral_ > limit) {
            integral_ = limit;
        } else if (integral_ < -limit) {
            integral_ = -limit;
        }
    } else {
        integral_ = 0.0f;
    }

    // 初回は前回誤差が無く、微分項が誤差そのもの / dt になって巨大なキックを出す。
    float derivative = 0.0f;
    if (hasPreviousError_) {
        derivative = (error - previousError_) / dtSec;
    }
    previousError_ = error;
    hasPreviousError_ = true;

    return kp_ * error + ki_ * integral_ + kd_ * derivative;
}

}  // namespace motorcan
