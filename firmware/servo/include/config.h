// サーボ用自作モタドラの機体依存定数。
//
// 基板 #0 / #1 … Arduino Nano（ATmega328P / 8bit / 5V / 16MHz / Flash 32KB / SRAM 2KB）
// 基板 #2      … Arduino UNO R4 Minima（RA4M1 / 32bit）

#pragma once

#include <stdint.h>

#include "MotorCanProtocol.h"
#include "ServoMotion.h"

// ===========================================================================
// ピン配置
// ===========================================================================

#if defined(ARDUINO_ARCH_RENESAS)

// ---------------------------------------------------------------------------
// 基板 #2: Arduino UNO R4 Minima
// ---------------------------------------------------------------------------

// CAN は内蔵ペリフェラルで D4(TX) / D5(RX) 固定。正は variant の
// PIN_CAN0_TX / PIN_CAN0_RX で、写しはここに置かない。

// UART（D0=RX / D1=TX）。ハードウェア UART（Serial1）と同じピン。
constexpr uint8_t kPinUartRx = 0;
constexpr uint8_t kPinUartTx = 1;

// シリアル RGB LED（1 個）。この基板では D9 がサーボ SV0 なので D8。
constexpr uint8_t kPinRgb = 8;
constexpr uint8_t kRgbBrightness = 30;

// DIP スイッチ 4bit。INPUT_PULLUP の負論理で、LOW = 1。
// 添字がビット位置: {SW0=bit0, SW1=bit1, SW2=bit2, SW3=bit3}。
// A0〜A3 は R4 でも 14〜17。
constexpr uint8_t kPinDip[4] = {14, 15, 16, 17};
constexpr uint8_t kDipBitCount = 4;

#else

// ---------------------------------------------------------------------------
// 基板 #0 / #1: Arduino Nano
// ---------------------------------------------------------------------------

// MCP2515（CAN コントローラ）。INT は受信バッファが埋まっている間 LOW になる。
constexpr uint8_t kPinMcpInt = 3;
constexpr uint8_t kPinMcpCs = 10;

// ハードウェア SPI が占有する 3 本。
constexpr uint8_t kPinSpiMosi = 11;
constexpr uint8_t kPinSpiMiso = 12;
constexpr uint8_t kPinSpiSck = 13;

// UART（D0=RX / D1=TX）。基板上の USB-シリアル変換に直結している。
constexpr uint8_t kPinUartRx = 0;
constexpr uint8_t kPinUartTx = 1;

// シリアル RGB LED（1 個）。D13 は SPI の SCK なのでオンボード LED は使えない。
constexpr uint8_t kPinRgb = 9;
constexpr uint8_t kRgbBrightness = 30;

// DIP スイッチ 4bit。INPUT_PULLUP の負論理で、LOW = 1。
// 添字がビット位置: {SW0=bit0, SW1=bit1, SW2=bit2, SW3=bit3}。
// A0〜A3 は Nano では 14〜17。
constexpr uint8_t kPinDip[4] = {14, 15, 16, 17};
constexpr uint8_t kDipBitCount = 4;

#endif  // ARDUINO_ARCH_RENESAS

// ===========================================================================
// スロット表（仕様書 §7.1）
// ===========================================================================

constexpr uint8_t kServoSlotCount = 5;

enum class SlotRole : uint8_t {
    Unused,       // 何も繋がない。pinMode すら触らない
    Servo,        // サーボ出力。deviceId 宛の SET_TARGET で動く
    TouchSensor,  // デジタル入力。自分のデバイス ID で FEEDBACK を送り bit4 で報告する
};

struct ServoSlotConfig {
    SlotRole role;
    uint8_t pin;
    float initialAngleDeg;
    motorcan::ServoLimits limits;
    motorcan::ServoPulseSpec pulse;
    bool sensorActiveLow;  // TouchSensor のとき LOW を「入力あり」とみなすか
};

// TODO(実機で確認): 角度 → パルス幅の対応。サンプルの attach(pin, 500, 2400) に合わせてある。
constexpr motorcan::ServoPulseSpec kServoPulse270{500, 2400, 270.0f};

// TODO(実機で確認): 180 度サーボを挿すときのパルス幅。下の値は仮置きである。
// 180 度品のパルス幅は 500-2400 とは限らず、1000-2000 や 500-2500 も普通にある。
constexpr motorcan::ServoPulseSpec kServoPulse180{500, 2400, 180.0f};

// TODO(実機で確認): angle_min / angle_max は機構が付いた状態で「当たらない範囲」を
// 実測して入れること。
// !!! 現在この値は 0〜270deg = サーボの全可動域で、クランプは実質効いていない !!!
constexpr motorcan::ServoLimits kGripperLimits{0.0f, 270.0f, 90.0f};
constexpr motorcan::ServoLimits kWallFLimits{0.0f, 270.0f, 90.0f};
constexpr motorcan::ServoLimits kWallRLimits{0.0f, 270.0f, 90.0f};
constexpr motorcan::ServoLimits kSubGripperLimits{0.0f, 270.0f, 90.0f};

// TouchSensor / Unused は駆動しないので共有のままでよい。
constexpr motorcan::ServoLimits kProvisionalLimits{0.0f, 270.0f, 90.0f};

// ===========================================================================
// スロット設定（基板番号ごと）
// ===========================================================================

// 実配線。基板ごとにピンが違う。
//
//   基板  | SV0 | SV1 | SV2 | SV3 | SV4
//   ------+-----+-----+-----+-----+-----
//   #0/#1 | D4  | D5  | D6  | D7  | D8
//   #2    | D9  | D11 | D10 | D6  | D3
//
// デバイス ID とその割当（FEEDBACK の CAN ID は 0x300 + デバイス ID）。
//
//   基板 | スロット | デバイス ID | PC 側のモータ / 用途
//   -----+----------+------------+--------------------------------
//    #0  | SV0      | 0x40       | gripper        (メインハンド)
//    #0  | SV1      | 0x41       | wall_f         (メインハンド)
//    #0  | SV2      | 0x42       | wall_r         (メインハンド)
//    #0  | SV3      | 0x43       | rotate の原点スイッチ
//    #0  | SV4      | ―          | 未使用 (y_axis の原点スイッチ用に予約)
//    #1  | SV0      | 0x48       | sub_gripper    (サブハンド)
//    #1  | SV1〜SV4 | ―          | 未使用
//    #2  | SV0〜SV4 | ―          | 用途未定 (0x50〜0x54 を予約)

// boardNumber は DIP で選ばれる番号そのもの（仕様書 §2.2 の bit5-3）。
struct ServoBoardConfig {
    uint8_t boardNumber;
    ServoSlotConfig slots[kServoSlotCount];
};

#if defined(ARDUINO_ARCH_RENESAS)

constexpr uint8_t kServoBoardCount = 1;

constexpr ServoBoardConfig kServoBoards[] = {
    // 基板 #2（DIP=2）: UNO R4 Minima
    //
    // 5 スロットとも用途未定なので Unused。この状態の基板は正しいファームを焼いて
    // DIP を 2 に合わせても RGB LED が赤の速い点滅になる（故障ではない）。
    //
    // TODO(実機で確認): 用途が決まったら role・initialAngleDeg・limits・pulse を
    // 実物へ合わせ、sensorActiveLow はそのスロットの配線で実測すること。
    {2,
     {
         {SlotRole::Unused, 9, 0.0f, kProvisionalLimits, kServoPulse270, true},   // SV0
         {SlotRole::Unused, 11, 0.0f, kProvisionalLimits, kServoPulse270, true},  // SV1
         {SlotRole::Unused, 10, 0.0f, kProvisionalLimits, kServoPulse270, true},  // SV2
         {SlotRole::Unused, 6, 0.0f, kProvisionalLimits, kServoPulse270, true},   // SV3
         {SlotRole::Unused, 3, 0.0f, kProvisionalLimits, kServoPulse270, true},   // SV4
     }},
};

#else

constexpr uint8_t kServoBoardCount = 2;

constexpr ServoBoardConfig kServoBoards[] = {
    // 基板 #0（DIP=0）: メインハンド
    {0,
     {
         {SlotRole::Servo, 4, 0.0f, kGripperLimits, kServoPulse270, false},  // SV0 gripper
         {SlotRole::Servo, 5, 270.0f, kWallFLimits, kServoPulse270, false},  // SV1 wall_f
         {SlotRole::Servo, 6, 90.0f, kWallRLimits, kServoPulse270, false},  // SV2 wall_r
         // 実測（CAN ID 0x343 の FEEDBACK）: 非接触で LOW、接触で HIGH。
         {SlotRole::TouchSensor, 7, 0.0f, kProvisionalLimits, kServoPulse270, false},  // SV3 rotate
         // スイッチ未装着のため Unused。付けたら TouchSensor へ戻し、同時に
         // config/main_hand.yaml の sensors: と
         // config/main_hand_positions.yaml の axes.y_axis.homing も戻すこと。
         //
         // TODO(実機で確認): sensorActiveLow は仮値。
         {SlotRole::Unused, 8, 0.0f, kProvisionalLimits, kServoPulse270, true},  // SV4 y_axis (未装着)
     }},
    // 基板 #1（DIP=1）: サブハンド
    //
    // TODO(実機で確認): SV1〜SV4 の sensorActiveLow は仮値。
    {1,
     {
         {SlotRole::Servo, 4, 0.0f, kSubGripperLimits, kServoPulse270, false},  // SV0 sub_gripper
         {SlotRole::Unused, 5, 0.0f, kProvisionalLimits, kServoPulse270, false},  // SV1
         {SlotRole::Unused, 6, 0.0f, kProvisionalLimits, kServoPulse270, false},  // SV2
         {SlotRole::Unused, 7, 0.0f, kProvisionalLimits, kServoPulse270, true},   // SV3
         {SlotRole::Unused, 8, 0.0f, kProvisionalLimits, kServoPulse270, true},   // SV4
     }},
};

#endif  // ARDUINO_ARCH_RENESAS

// ===========================================================================
// デバイス ID（仕様書 §2.2）
// ===========================================================================

//   基板番号 | SV0  | SV1  | SV2  | SV3  | SV4
//   ---------+------+------+------+------+------
//      0     | 0x40 | 0x41 | 0x42 | 0x43 | 0x44
//      1     | 0x48 | 0x49 | 0x4A | 0x4B | 0x4C
//      2     | 0x50 | 0x51 | 0x52 | 0x53 | 0x54
constexpr motorcan::BoardKind kBoardKind = motorcan::BoardKind::Servo;

// 版番号（仕様書 §3.4）。上げたら config/**/*.yaml の expected_firmware も
// 同じコミットで揃えること。
//
// 2: INFO にサーボ可動レンジ（Byte3-4）を追加。
// 3: デバイス ID 未設定のスロットが FEEDBACK / INFO を 1 通も送らなくなった（§2.2）。
// 4: スロットの役割を基板番号ごとに持つようにした。基板 #0 の SV3 が Servo
//    （sub_gripper）から TouchSensor（rotate の原点スイッチ）になり、sub_gripper は
//    基板 #1 の SV0（0x48）へ移った。
// 5: 基板 #0 の SV4（y_axis の原点スイッチ用）が TouchSensor から Unused になった。
//    スイッチを付けたら TouchSensor へ戻して版番号をまた上げること。
//
// 基板 #2（UNO R4 Minima）の追加では上げていない。既存 2 枚の CAN 上の振る舞いが
// 変わっていないため。R4 バイナリも v5 を名乗る。
constexpr uint8_t kFirmwareVersion = 5;

constexpr uint32_t kInfoIntervalMs = 1000;

// ===========================================================================
// 制御ループ
// ===========================================================================

// 補間の更新周期。Servo ライブラリのフレーム周期（20ms）より速く、FEEDBACK 周期
// （100Hz）と同等。ATmega328P は float がソフトウェア実装なので、詰めすぎると
// CAN 受信が痩せる。
constexpr uint32_t kMotionIntervalMs = 5;

// setup() が MotorSafety::setWatchdogEnabled() へ写す。
#define WATCHDOG_ENABLED 1

// command_timeout_ms / feedback_interval_ms の既定値は MotorCanProtocol.h、
// 到達許容差の既定値は ServoMotion.h が持つ。

// ===========================================================================
// 緊急停止・ウォッチドッグ時の振る舞い（仕様書 §7.5）
// ===========================================================================

constexpr bool kEStopDetach = false;

// ===========================================================================
// 表示
// ===========================================================================

#define HAS_RGB_LED 1

constexpr uint32_t kUnconfiguredBlinkIntervalMs = 200;

constexpr uint32_t kHeartbeatIntervalMs = 1000;

// FEEDBACK は 5 スロット × 100Hz = 500 通/秒 出るので、50 連続失敗は約 100ms 分。
constexpr uint16_t kCanTxFailStreakAlarm = 50;

// ===========================================================================
// デバッグ用シリアル
// ===========================================================================

// USB シリアル（115200 baud）から「<スロット番号> <角度>」で角度を直接指令できる（0 で無効）。
#define ENABLE_SERIAL_DEBUG 1
constexpr uint32_t kSerialBaud = 115200;
