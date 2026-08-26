// 制御モードと目標値の組（仕様書 §3.3 / §5.4）。
//
// モードと目標値を別々の変数で持つと、「モードだけ切り替えて前の目標値が残る」経路が
// 書けてしまう。位置目標 90.0[deg] が duty 90.0（= 9000%）として解釈される事故が
// それで、仕様書 §3.3 が明示的に禁じている。両者を 1 つの状態にまとめ、
// 目標値を 0 に落とす規則をここだけが持つ。
//
// Arduino.h を include しないのは意図的で、native 環境でそのままテストできるようにするため。

#pragma once

#include "MotorCanProtocol.h"

namespace motorcan {

class ControlTarget {
   public:
    // 仕様書 §5.4: 電源投入直後は duty モード・目標 0。
    ControlTarget() : mode_(ControlType::Duty), value_(0.0f) {}

    ControlType mode() const { return mode_; }
    float value() const { return value_; }

    void setValue(float value) { value_ = value; }

    // 仕様書 §3.5: 緊急停止の解除直後は目標値 0 から始める（モードは変えない）。
    // 停止前の目標を復元すると、解除した瞬間にモータが動き出して人を巻き込む。
    void clearValue() { value_ = 0.0f; }

    // モードが実際に変わったときだけ目標値を 0 に落とし、true を返す。
    // PID の積分項クリアは呼び出し側が true のときに行う（PID を持たない基板もあるため）。
    //
    // 同じモードでの呼び出しで 0 に落とさないのは、SET_TARGET が毎フレーム制御タイプを
    // 載せてくるから（仕様書 §3.1）。落とすと 20Hz の再送のたびに目標値が振動する。
    bool switchMode(ControlType mode) {
        if (mode == mode_) {
            return false;
        }
        mode_ = mode;
        value_ = 0.0f;
        return true;
    }

   private:
    ControlType mode_;
    float value_;
};

}  // namespace motorcan
