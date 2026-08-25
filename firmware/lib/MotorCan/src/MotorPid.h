// position / velocity モード用の PID。
// SET_MODE でのモード切替時に reset() で積分項をクリアする必要があるため
// （仕様書 §3.3）、内部状態を持つクラスとして切り出してある。
//
// Arduino 非依存。dt を引数で受け取り内部で時刻を取らないので native でテストできる。
// サーボ用ファームでも同じものを使う。

#pragma once

namespace motorcan {

class MotorPid {
   public:
    MotorPid(float kp, float ki, float kd, float integralLimit);

    void setKp(float kp) { kp_ = kp; }
    void setKi(float ki);
    void setKd(float kd) { kd_ = kd; }

    float kp() const { return kp_; }
    float ki() const { return ki_; }
    float kd() const { return kd_; }

    // 積分項と微分の履歴を捨てる。モード切替・緊急停止・ウォッチドッグ満了で呼ぶ。
    void reset();

    // 出力は duty 次元（-1.0～+1.0 相当）。最終的なクランプは clampDuty 側の責務。
    float update(float error, float dtSec);

   private:
    float kp_;
    float ki_;
    float kd_;
    float integralLimit_;
    float integral_;
    float previousError_;
    bool hasPreviousError_;
};

}  // namespace motorcan
