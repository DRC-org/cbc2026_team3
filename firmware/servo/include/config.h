// サーボ用自作モタドラの機体依存定数。
//
// ここに集約してあるのは「基板を見ないと確定できない値」と「機構が決まるまで動かせない値」。
// TODO(実機で確認) が付いた定数は仮置きであり、通電前に必ず基板・サーボのデータシート・
// 実測と突き合わせること。可動範囲を誤ったまま通電するとサーボがメカストッパに当たったまま
// 停動し、短時間で焼損する（仕様書 §7.2）。
//
// パラメータの一部は SET_PARAM で実行時に変更できるが、RAM 上のみで電源断で
// ここの既定値に戻る（仕様書 §3.4）。恒久的に変えたい値はこのファイルを直すこと。

#pragma once

#include <stdint.h>

#include "ServoMotion.h"

// ===========================================================================
// CAN ペリフェラルと衝突するピン
// ===========================================================================

// UNO R4 の CAN0 は variant ごとに別のピンへ固定されており、Arduino_CAN の CAN インスタンスが
// variant の PIN_CAN0_TX / PIN_CAN0_RX を使う。ここの定数は配線確認用で、コードから直接は
// 使わない（実際の値は pins_arduino.h が持つ）。
//
//   Minima : PIN_CAN0_TX = D4,  PIN_CAN0_RX = D5
//   WiFi   : PIN_CAN0_TX = D10, PIN_CAN0_RX = D13（D13 はオンボード LED と兼用）
//
// **チーム提供のサンプルは SV0..SV3 を D4〜D7 に置いているが、この配線はそのまま使えない。**
// Minima では D4/D5 が CAN ペリフェラルに固定されており、サーボ出力と正面衝突する。
// サーボ側が先にピンを握れば CAN が上がらず PC から止められない基板になり、
// CAN が先に握ればサーボが動かない。どちらにしても現場で原因が分かりにくい。
constexpr uint8_t kPinCanTxMinima = 4;
constexpr uint8_t kPinCanRxMinima = 5;
constexpr uint8_t kPinCanTxWifi = 10;
constexpr uint8_t kPinCanRxWifi = 13;

// ===========================================================================
// ピン配置
// ===========================================================================

// サーボ出力。UNO R4 で PWM が出せるのは D3 / D5 / D6 / D9 / D10 / D11 のみ。
// そこから上記 CAN のピンを除くと、両基板で安全に使えるのは D3 / D6 / D9 / D11。
//
// 既定は Minima を前提に D9 / D10 / D11。WiFi では D10 が CAN TX なので
// ch1 だけ D6 へ逃がしてある（基板が確定したら片方に寄せて条件分岐を消すこと）。
#if defined(ARDUINO_UNOWIFIR4)
constexpr uint8_t kPinServoCh0 = 9;   // TODO(実機で確認)
constexpr uint8_t kPinServoCh1 = 6;   // TODO(実機で確認): WiFi では D10 が CAN TX のため D6
constexpr uint8_t kPinServoCh2 = 11;  // TODO(実機で確認)
#else
constexpr uint8_t kPinServoCh0 = 9;   // TODO(実機で確認)
constexpr uint8_t kPinServoCh1 = 10;  // TODO(実機で確認)
constexpr uint8_t kPinServoCh2 = 11;  // TODO(実機で確認)
#endif

// DIP スイッチ 4bit。INPUT_PULLUP の負論理で、LOW = 1。
// 添字がビット位置: {SW0=bit0, SW1=bit1, SW2=bit2, SW3=bit3}。
//
// DC 用は D8/D9/D1/D0 を使っているが、サーボ用は D9〜D11 をサーボ出力に取られるので
// A0〜A3（14〜17）へ移してある。A0-A3 はデジタル入力として普通に使える。
// D0/D1 を避けているのはハードウェア UART(Serial1) と兼用だから（DC 用 config.h の注記と同じ）。
// TODO(実機で確認): 基板の DIP がどのピンに落ちているか。
constexpr uint8_t kPinDip[4] = {14, 15, 16, 17};  // A0, A1, A2, A3

// オンボード LED。
// **WiFi 基板では D13 が CAN RX と兼用**で、pinMode/digitalWrite で握ると CAN 受信が死ぬ。
// PC から止められない基板になるので、WiFi では LED を使わない。
// TODO(実機で確認): WiFi 基板を採用する場合は LED マトリクス等、CAN と兼用でない
// 表示手段へ差し替えること（Arduino_LED_Matrix はコアに同梱されている）。
#if defined(ARDUINO_UNOWIFIR4)
#define HAS_STATUS_LED 0
constexpr uint8_t kPinLed = 13;
#else
#define HAS_STATUS_LED 1
constexpr uint8_t kPinLed = 13;
#endif

// シリアル RGB LED による状態表示。外部ライブラリ（FastLED_NeoPixel 等）が必要なので既定は無効。
// 有効にするには platformio.ini の lib_deps に追加すること。
#define HAS_RGB_LED 0
constexpr uint8_t kPinRgb = 3;  // TODO(実機で確認)

// ===========================================================================
// サーボ PWM
// ===========================================================================

// アナログサーボの標準的なフレーム周期 50Hz。
// デジタルサーボなら 200〜333Hz まで上げられるが、対応していない個体に速い周期を
// 与えると発熱・ジッタの原因になるので、既定は全個体で安全な 50Hz にしてある。
// TODO(実機で確認): サーボのデータシートで許容周期を確認する。
constexpr uint32_t kServoPwmPeriodUs = 20000;

// TODO(実機で確認): 角度 → パルス幅の対応。270 度サーボの一般的な値を仮置きしてある。
// **サンプルコードのように 180/270 を掛けて write() の 0-180 に押し込む変換はしない。**
// 分解能が 2/3 に落ち、可動範囲の端が表現できなくなるため（ServoMotion.h 参照）。
constexpr motorcan::ServoPulseSpec kServoPulse270{500, 2500, 270.0f};

// ===========================================================================
// チャンネル表（仕様書 §7.1）
// ===========================================================================

// 1 枚の基板が複数のサーボを駆動し、**チャンネルごとに独立したデバイス ID を持つ**。
// PC からは別々のモータとして見える（config/main_hand.yaml の gripper / wall_f / wall_r が
// それぞれ別の can_id を持つのはこのため）。
constexpr uint8_t kServoChannelCount = 3;

struct ServoChannelConfig {
    uint8_t deviceId;              // DIP オフセットを足す前の基準デバイス ID
    uint8_t pin;                   // サーボ信号線
    float initialAngleDeg;         // 起動時に持っていく角度（仕様書 §5.4）
    motorcan::ServoLimits limits;  // 可動範囲とスルーレート（SET_PARAM 0x10-0x12 で変更可）
    motorcan::ServoPulseSpec pulse;
    const char *name;  // シリアルデバッグ表示用。CAN の挙動には影響しない
};

// 既定は config/main_hand.yaml の実構成に合わせてある。
//
//   ch | デバイス ID | モータ  | ピン
//   ---+------------+---------+------
//    0 | 0x01       | gripper | D9
//    1 | 0x03       | wall_f  | D10（WiFi では D6）
//    2 | 0x04       | wall_r  | D11
//
// TODO(実機で確認): angle_min / angle_max は機構が付いた状態で「当たらない範囲」を
// 実測して入れること。現状は config/main_hand_positions.yaml が 0〜6deg の微小ストロークしか
// 使わないのに合わせた安全側の仮値で、広げるのは機構確定後。**狭すぎる分にはクランプで
// 止まるだけだが、広すぎるとメカストッパに当たったまま停動して焼損する。**
constexpr ServoChannelConfig kServoChannels[kServoChannelCount] = {
    // gripper: ワークを把持する。閉 0deg / 開 5deg（positions.gripper）。
    // 把持側は機構の当たりが近いので可動範囲を狭めに取る。
    {0x01, kPinServoCh0, 0.0f, {0.0f, 30.0f, 90.0f}, kServoPulse270, "gripper"},
    // wall_f: 前側の壁。初期 0deg / 閉 3deg / 開 6deg（positions.wall_f）。
    {0x03, kPinServoCh1, 0.0f, {0.0f, 30.0f, 90.0f}, kServoPulse270, "wall_f"},
    // wall_r: 後側の壁。wall_f と同一仕様。
    {0x04, kPinServoCh2, 0.0f, {0.0f, 30.0f, 90.0f}, kServoPulse270, "wall_r"},
};

// ===========================================================================
// 制御ループ
// ===========================================================================

// 補間の更新周期。サーボ自身が内部でパルス幅へ追従するので、DC 用の 1kHz ほど速くなくてよい。
// PWM 周期（50Hz = 20ms）より速く、FEEDBACK 周期（100Hz）と同等の 5ms にしてある。
constexpr uint32_t kMotionIntervalMs = 5;

// コマンドウォッチドッグ（仕様書 §5.1）。**宛先がデバイス ID ＝ チャンネルなので、
// ウォッチドッグもチャンネルごとに独立して動く。** 1 チャンネルへの指令が途絶えても
// 他のチャンネルは動き続ける（片方の壁だけ通信が切れる、という状況が実在するため）。
//
// PC 側の目標値定期再送が未実装（仕様書 §8）のため、有効のままだと SET_TARGET から
// kDefaultCommandTimeoutMs 後に新しい角度指令を受け付けなくなる。**サーボは満了しても
// 現在角を保持するので機構が落ちることはない**が、そこから先は動かせない。
// PC 側が入るまでの暫定運用としてここを 0 にするか、command_timeout_ms を大きく設定すること。
#define WATCHDOG_ENABLED 1

// 仕様書 §3.4 の既定値。
constexpr uint32_t kDefaultCommandTimeoutMs = 500;
constexpr uint32_t kDefaultFeedbackIntervalMs = 10;  // 100Hz

// 仕様書 §7.3 / §7.6。0 は「補間が完了した時点で到達」を意味する。
// サーボは実測値を持たないので「目標角 - 指令角」がそのまま補間の残りになる。
constexpr float kDefaultReachedToleranceDeg = motorcan::kDefaultServoReachedToleranceDeg;

// ===========================================================================
// 緊急停止・ウォッチドッグ時の振る舞い（仕様書 §7.5）
// ===========================================================================

// true にすると緊急停止・ウォッチドッグ満了で PWM を止めてサーボを脱力させる。
//
// **既定は false（現在角を保持）。** サーボは PWM を止めると back-drivable になり、
// 壁が自重で倒れ、グリッパが把持中のワークを落とす。DC 用のコースト（脱力）と
// 意図的に振る舞いを変えている点であり、変更するときは機構side の影響を必ず確認すること。
constexpr bool kEStopDetach = false;

// ===========================================================================
// 表示
// ===========================================================================

// DIP オフセット適用後のデバイス ID が 0x00 になったチャンネルがあるときの速い点滅
// （仕様書 §2.2 / §7.1）。
constexpr uint32_t kUnconfiguredBlinkIntervalMs = 200;

// 正常時のハートビート点滅周期。ファームが生きていることを目視で確認するため。
constexpr uint32_t kHeartbeatIntervalMs = 1000;

// ===========================================================================
// デバッグ用シリアル
// ===========================================================================

// USB CDC の Serial から「<ch> <角度>」で角度を直接指令できるようにする（0 で無効）。
// 緊急停止ラッチ中はシリアルからも駆動できない（applyChannelOutput が一括で禁止する）。
#define ENABLE_SERIAL_DEBUG 1
constexpr uint32_t kSerialBaud = 115200;
