// DC 用自作モタドラ（Arduino UNO R4 Minima）の機体依存定数。
// TODO(実機で確認) が付いた定数は仮置きであり、通電前に必ず基板・データシートと
// 突き合わせること。

#pragma once

#include <stdint.h>

#include "MotorCanProtocol.h"

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
constexpr uint8_t kPinRef = 2;
constexpr bool kRefActiveLow = true;

// CAN は内蔵ペリフェラルで D4(TX) / D5(RX) 固定。正は variant の
// PIN_CAN0_TX / PIN_CAN0_RX で、写しはここに置かない。

constexpr uint8_t kPinLed = 13;  // オンボード LED
constexpr uint8_t kPinRgb = 6;   // シリアル RGB LED（1 個）

// DIP スイッチ 2bit。INPUT_PULLUP の負論理で、LOW = 1。
// 添字がビット位置: {SW0=bit0, SW1=bit1}。D0/D1 はハードウェア UART(Serial1) と
// 同じピンなので、デバッグ用シリアルには必ず USB CDC の Serial を使うこと。
constexpr uint8_t kPinDip[2] = {1, 0};
constexpr uint8_t kDipBitCount = 2;

// ===========================================================================
// チャンネル表（仕様書 §2.2）
// ===========================================================================

constexpr uint8_t kDcChannelCount = 3;

// デバイス ID は「基板種別 | 基板番号 | スロット番号」の固定ビット分割（仕様書 §2.2）。
//
//   基板番号 | ch0  | ch1  | ch2
//   ---------+------+------+------
//      0     | 0x80 | 0x81 | 0x82
//      1     | 0x88 | 0x89 | 0x8A
//      2     | 0x90 | 0x91 | 0x92
//      3     | 0x98 | 0x99 | 0x9A
constexpr motorcan::BoardKind kBoardKind = motorcan::BoardKind::Dc;

// 版番号（仕様書 §3.4）。上げたら config/**/*.yaml の expected_firmware も
// 同じコミットで揃えること。
//
// 2: デバイス ID 未設定のチャンネルが FEEDBACK / INFO を 1 通も送らなくなった（§2.2）。
constexpr uint8_t kFirmwareVersion = 2;

struct DcChannelConfig {
    uint8_t pwmPin;
    uint8_t dirPin;
    float maxDuty;
};

// TODO(実機で確認): max_duty はモータとギヤ比が決まってから詰めること。
// サンプルは 50% を上限にしている。ここは安全側に 30% から始める。
constexpr float kDefaultMaxDuty = 0.30f;

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
// 制御ループ
// ===========================================================================

#define WATCHDOG_ENABLED 1

// ===========================================================================
// 表示
// ===========================================================================

#define HAS_RGB_LED 1
constexpr uint8_t kRgbBrightness = 30;

constexpr uint32_t kUnconfiguredBlinkIntervalMs = 200;

constexpr uint32_t kHeartbeatIntervalMs = 1000;

constexpr uint16_t kCanTxFailStreakAlarm = 50;

constexpr uint32_t kInfoIntervalMs = 1000;

// ===========================================================================
// デバッグ用シリアル
// ===========================================================================

#define ENABLE_SERIAL_DEBUG 1
constexpr uint32_t kSerialBaud = 115200;
