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
constexpr motorcan::ServoLimits kSubRotateRLimits{0.0f, 270.0f, 90.0f};
constexpr motorcan::ServoLimits kSubRotateLLimits{0.0f, 270.0f, 90.0f};
constexpr motorcan::ServoLimits kSubPitchRLimits{0.0f, 270.0f, 90.0f};
constexpr motorcan::ServoLimits kSubPitchLLimits{0.0f, 270.0f, 90.0f};
constexpr motorcan::ServoLimits kSubOffsetLimits{0.0f, 270.0f, 90.0f};

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
//    #0  | SV0      | 0x40       | gripper                 (メインハンド)
//    #0  | SV1      | 0x41       | wall_f                  (メインハンド)
//    #0  | SV2      | 0x42       | wall_r                  (メインハンド)
//    #0  | SV3      | 0x43       | rotate の原点スイッチ    (メインハンド)
//    #0  | SV4      | 0x44       | y_axis 右の原点スイッチ  (メインハンド)
//    #1  | SV0      | 0x48       | y_axis 左の原点スイッチ  (メインハンド)
//    #1  | SV1      | 0x49       | sub_y_axis 前端スイッチ  (サブハンド)
//    #1  | SV2      | 0x4A       | sub_y_axis 後端スイッチ  (サブハンド)
//    #1  | SV3      | 0x4B       | sub_lift 上端スイッチ    (サブハンド)
//    #1  | SV4      | 0x4C       | sub_lift 下端スイッチ    (サブハンド)
//    #2  | SV0      | 0x50       | sub_rotate_r            (サブハンド)
//    #2  | SV1      | 0x51       | sub_rotate_l            (サブハンド)
//    #2  | SV2      | 0x52       | sub_pitch_r             (サブハンド)
//    #2  | SV3      | 0x53       | sub_pitch_l             (サブハンド)
//    #2  | SV4      | 0x54       | sub_offset              (サブハンド)
//
// 基板 #1 は SV0 がメインハンド・SV1〜SV4 がサブハンドで、1 枚が 2 つのロボットに
// またがる（配線はメインハンドからこの基板まで引く必要がある）。

// boardNumber は DIP で選ばれる番号そのもの（仕様書 §2.2 の bit5-3）。
struct ServoBoardConfig {
    uint8_t boardNumber;
    ServoSlotConfig slots[kServoSlotCount];
};

#if defined(ARDUINO_ARCH_RENESAS)

constexpr uint8_t kServoBoardCount = 1;

constexpr ServoBoardConfig kServoBoards[] = {
    // 基板 #2（DIP=2）: サブハンドの回転 2 軸 / ピッチ 2 軸 / オフセット 1 軸
    //
    // sub_rotate_r/l と sub_pitch_r/l は機構的に直結した左右ペアで、折り返し
    // （scale: -1.0 / offset: 270.0）は PC 側の位置定数 yaml が持つ。
    //
    // TODO(実機で確認): initialAngleDeg は 5 スロットとも仮値。通電と再起動のたび
    // ここへ駆動するので、機構を付ける前に「当たらない角度」を実測して入れること。
    {2,
     {
         {SlotRole::Servo, 9, 0.0f, kSubRotateRLimits, kServoPulse270, true},   // SV0 sub_rotate_r
         {SlotRole::Servo, 11, 0.0f, kSubRotateLLimits, kServoPulse270, true},  // SV1 sub_rotate_l
         {SlotRole::Servo, 10, 0.0f, kSubPitchRLimits, kServoPulse270, true},   // SV2 sub_pitch_r
         {SlotRole::Servo, 6, 0.0f, kSubPitchLLimits, kServoPulse270, true},    // SV3 sub_pitch_l
         {SlotRole::Servo, 3, 0.0f, kSubOffsetLimits, kServoPulse270, true},    // SV4 sub_offset
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
         // TODO(実機で確認): sensorActiveLow は仮値。極性はスロットごとの配線で決まるので
         // 同じ基板の SV3 へ合わせてはならない。
         {SlotRole::TouchSensor, 8, 0.0f, kProvisionalLimits, kServoPulse270, true},  // SV4 y_axis 右
     }},
    // 基板 #1（DIP=1）: 5 スロットとも TouchSensor で、駆動するモータは 1 台も無い。
    // 焼き忘れ検出は PC 側 sensors: の expected_firmware が担う。
    //
    // TODO(実機で確認): SV0〜SV4 の sensorActiveLow は仮値。
    {1,
     {
         {SlotRole::TouchSensor, 4, 0.0f, kProvisionalLimits, kServoPulse270, true},  // SV0 y_axis 左 (メイン)
         {SlotRole::TouchSensor, 5, 0.0f, kProvisionalLimits, kServoPulse270, true},  // SV1 sub_y_axis 前端
         {SlotRole::TouchSensor, 6, 0.0f, kProvisionalLimits, kServoPulse270, true},  // SV2 sub_y_axis 後端
         {SlotRole::TouchSensor, 7, 0.0f, kProvisionalLimits, kServoPulse270, true},  // SV3 sub_lift 上端
         {SlotRole::TouchSensor, 8, 0.0f, kProvisionalLimits, kServoPulse270, true},  // SV4 sub_lift 下端
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
// 6: 零点確定用のスイッチ 6 本とサブハンドのサーボ 5 本を割り当て、3 枚とも役割が
//    変わった。#0 SV4 は TouchSensor へ戻り、#1 は 5 スロットとも TouchSensor、
//    #2 は 5 スロットとも Servo になった。
//
// 基板 #2（UNO R4 Minima）の追加自体では上げていない。既存 2 枚の CAN 上の振る舞いが
// 変わっていないため。R4 バイナリも Nano と同じ番号を名乗る。
constexpr uint8_t kFirmwareVersion = 6;

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
