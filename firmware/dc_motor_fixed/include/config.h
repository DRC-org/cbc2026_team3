// DC 用自作モタドラ（Arduino UNO R4 Minima）固定 duty 版の機体依存定数。
// TODO(実機で確認) が付いた定数は仮置きであり、通電前に必ず基板・データシートと
// 突き合わせること。

#pragma once

#include <stdint.h>

// ===========================================================================
// ピン配置（チーム提供のサンプルコードの配線に準拠）
// ===========================================================================

// モータ出力: 1 チャンネルにつき PWM 1 本 + 方向 1 本。
// UNO R4 で PWM が出せるのは D3 / D5 / D6 / D9 / D10 / D11 で、CAN の D4/D5 を除いた
// 3 本を PWM に充ててある。方向ピンは digitalWrite なので PWM 対応でなくてよい。
constexpr uint8_t kPinPwm[3] = {11, 10, 9};
constexpr uint8_t kPinDir[3] = {12, 3, 7};

// TODO(実機で確認): 方向ピンの論理。サンプルの `digitalWrite(DIR, duty >= 0 ? LOW : HIGH)`
// に準拠して「LOW = 正転」と仮定している。逆だと全チャンネルが指令と反対に回る。
constexpr bool kDirForwardIsLow = true;

// 物理緊急停止スイッチの検知入力。LOW = 押されている（停止中）。
// INPUT_PULLUP で読むので、断線したときも LOW 側＝停止側へ倒れる。
// この版ではこれが唯一の停止手段。
constexpr uint8_t kPinRef = 2;
constexpr bool kRefActiveLow = true;

constexpr uint8_t kPinLed = 13;  // オンボード LED
constexpr uint8_t kPinRgb = 6;   // シリアル RGB LED（1 個）

// ===========================================================================
// チャンネル表
// ===========================================================================

constexpr uint8_t kDcChannelCount = 3;

// 各チャンネルが常時出し続ける duty。符号が回転方向（正 = 正転）、絶対値が 0.0〜1.0。
// この版に指令入力は無いので、出力を変える手段はここを書き換えて焼き直すことだけ。
constexpr float kFixedDuty[kDcChannelCount] = {-0.15f, 0.95f, 0.0f};

struct DcChannelConfig {
    uint8_t pwmPin;
    uint8_t dirPin;
    float maxDuty;
};

// TODO(実機で確認): max_duty はモータとギヤ比が決まってから詰めること。
// ポンプが必要な流量を出せる上限として 95% を置いている。
constexpr float kDefaultMaxDuty = 0.95f;

constexpr DcChannelConfig kDcChannels[kDcChannelCount] = {
    {kPinPwm[0], kPinDir[0], kDefaultMaxDuty},  // ch0 = conveyor（メインハンド）
    {kPinPwm[1], kPinDir[1], kDefaultMaxDuty},  // ch1 = pump_vac（サブハンド）
    {kPinPwm[2], kPinDir[2], kDefaultMaxDuty},  // ch2 = pump_blow（サブハンド）
};

// ===========================================================================
// モータ出力
// ===========================================================================

// PWM 30kHz。サンプルの `begin(30000.0f, 0.0f)`（周波数 [Hz] を取る float
// オーバーロード）と同じ値。uint32_t の版は「周期 [us]」を取る別物。
//
// TODO(実機で確認): duty 0 のときハーフブリッジがコーストになるかブレーキになるか。
constexpr float kPwmFrequencyHz = 30000.0f;

// ===========================================================================
// 表示
// ===========================================================================

#define HAS_RGB_LED 1
constexpr uint8_t kRgbBrightness = 30;

constexpr uint32_t kHeartbeatIntervalMs = 1000;
