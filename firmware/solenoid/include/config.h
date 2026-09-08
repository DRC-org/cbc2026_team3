// 電磁弁用自作モタドラの機体依存定数（仕様書 §9）。
//
// 基板は STM32F303K8T6（Cortex-M4F / 32bit / 3.3V / 64KB Flash / 12KB SRAM）。
// CAN は内蔵 bxCAN（PA11 = RX / PA12 = TX 固定。LQFP32 に代替ピンは無い）。
// 出力は GPIO の ON/OFF が 6 本で、PWM も方向ピンも無い。
//
// !!! 実機は PA11 / PA12 がトランシーバの TXD / RXD と逆に配線されている。
//     ファームでは直せない。修正するまで長時間通電しないこと
//     （出力同士がぶつかる）。切り分け手順は firmware/README.md !!!
//
// TODO(実機で確認) が付いた定数は仮置きであり、通電前に必ず基板・回路図・実測と
// 突き合わせること。
//
// STM32 HAL を include しない。HAL の GPIOA / GPIOB は
// `((GPIO_TypeDef *) GPIOA_BASE)` へ展開されるポインタキャストなので constexpr
// 文脈では比較できず、取り込むと重複検査をビルド時に回せなくなる。

#pragma once

#include <stdint.h>

#include "MotorCanProtocol.h"

// ===========================================================================
// ピン配置
// ===========================================================================

// GPIO ポート。HAL の GPIO_TypeDef* への変換は src/app.cpp が 1 箇所で持つ。
enum class Port : uint8_t {
    A = 0,
    B = 1,
};

// CAN（PA11 / PA12）・SWD（PA13 / PA14）・USART1（PA9 / PA10）はペリフェラルが占有する。
constexpr Port kPortCanRx = Port::A;
constexpr uint16_t kPinCanRx = 1u << 11;
constexpr Port kPortCanTx = Port::A;
constexpr uint16_t kPinCanTx = 1u << 12;
constexpr Port kPortUartTx = Port::A;
constexpr uint16_t kPinUartTx = 1u << 9;
constexpr Port kPortUartRx = Port::A;
constexpr uint16_t kPinUartRx = 1u << 10;

constexpr Port kPortLed = Port::A;
constexpr uint16_t kPinLed = 1u << 5;

// DIP スイッチ 4bit。内部プルアップの負論理で、LOW = 1。
// 添字がビット位置: {DIP1=bit0, DIP2=bit1, DIP3=bit2, DIP4=bit3}。
//
// TODO(実機で確認): 基板の DIP がコモンを GND へ落とす配線であること。VCC 側へ引く
// 配線なら kDipActiveLevel と solenoid.ioc の GPIO_PuPd を揃えて反転すること。
constexpr uint8_t kDipBitCount = 4;
constexpr Port kDipPorts[kDipBitCount] = {Port::B, Port::B, Port::A, Port::A};
constexpr uint16_t kDipPins[kDipBitCount] = {1u << 1, 1u << 0, 1u << 7, 1u << 6};

constexpr int kDipActiveLevel = 0;

// ===========================================================================
// チャンネル表（仕様書 §9.1）
// ===========================================================================

constexpr uint8_t kSolenoidChannelCount = 6;

struct SolenoidChannelConfig {
    Port port;
    uint16_t pin;
};

constexpr SolenoidChannelConfig kSolenoidChannels[kSolenoidChannelCount] = {
    {Port::B, 1u << 7},   // ch0 = valve_1 (PB7 / PUMP1_SW)
    {Port::B, 1u << 6},   // ch1 = valve_2 (PB6 / PUMP2_SW)
    {Port::B, 1u << 5},   // ch2 = valve_3 (PB5 / PUMP3_SW)
    {Port::B, 1u << 4},   // ch3 = valve_4 (PB4 / PUMP4_SW)
    {Port::B, 1u << 3},   // ch4 = valve_5 (PB3 / PUMP5_SW)
    {Port::A, 1u << 15},  // ch5 = valve_6 (PA15 / PUMP6_SW)
};

// ===========================================================================
// デバイス ID（仕様書 §2.2）
// ===========================================================================

// デバイス ID は「基板種別 | 基板番号 | スロット番号」の固定ビット分割。
//
//   基板番号 | ch0  | ch1  | ch2  | ch3  | ch4  | ch5
//   ---------+------+------+------+------+------+------
//      0     | 0xC0 | 0xC1 | 0xC2 | 0xC3 | 0xC4 | 0xC5
//      1     | 0xC8 | 0xC9 | 0xCA | 0xCB | 0xCC | 0xCD
constexpr motorcan::BoardKind kBoardKind = motorcan::BoardKind::Solenoid;

// 版番号（仕様書 §3.4）。上げたら config/**/*.yaml の expected_firmware も
// 同じコミットで揃えること。
//
// 2: デバイス ID 未設定のチャンネルが FEEDBACK / INFO を 1 通も送らなくなった（§2.2）。
constexpr uint8_t kFirmwareVersion = 2;

constexpr uint32_t kInfoIntervalMs = 1000;

// ===========================================================================
// 制御ループ
// ===========================================================================

#define WATCHDOG_ENABLED 1

// ===========================================================================
// 表示
// ===========================================================================

#define HAS_STATUS_LED 1

constexpr uint32_t kUnconfiguredBlinkIntervalMs = 200;

constexpr uint32_t kEStopBlinkIntervalMs = 500;

constexpr uint32_t kHeartbeatIntervalMs = 1000;

constexpr uint16_t kCanTxFailStreakAlarm = 50;

// ===========================================================================
// デバッグ用シリアル
// ===========================================================================

#define ENABLE_SERIAL_DEBUG 1
