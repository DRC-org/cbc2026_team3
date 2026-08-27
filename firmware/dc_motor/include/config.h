// DC モータ用自作モタドラの機体依存定数。
//
// ここに集約してあるのは「基板を見ないと確定できない値」と「チューニングで変わる値」。
// TODO(実機で確認) が付いた定数は仮置きであり、通電前に必ず基板・データシートと
// 突き合わせること。
//
// **この基板はフィードバックを一切持たない。** エンコーダ・電流センス・温度センサの
// いずれも非搭載で、制御は duty の開ループのみ（仕様書 §4 / §8）。位置・速度制御と
// PID は実装ごと存在しない。
//
// パラメータの一部は SET_PARAM で実行時に変更できるが、RAM 上のみで電源断で
// ここの既定値に戻る（仕様書 §3.4）。恒久的に変えたい値はこのファイルを直すこと。

#pragma once

#include <stdint.h>

#include "MotorCanProtocol.h"

// ===========================================================================
// ピン配置（チーム提供のサンプルコードの配線に準拠）
// ===========================================================================

// モータ出力: 1 チャンネルにつき PWM 1 本 + 方向 1 本。
// UNO R4 で PWM が出せるのは D3 / D5 / D6 / D9 / D10 / D11 で、そこから CAN の
// D4/D5 を除いた中から 3 本を PWM に充ててある。方向ピンは digitalWrite なので
// PWM 対応である必要はない。
constexpr uint8_t kPinPwm[3] = {11, 10, 9};
constexpr uint8_t kPinDir[3] = {12, 3, 7};

// TODO(実機で確認): 方向ピンの論理。サンプルの `digitalWrite(DIR, duty >= 0 ? LOW : HIGH)`
// に準拠して「LOW = 正転」と仮定している。逆だと全チャンネルが指令と反対に回る。
constexpr bool kDirForwardIsLow = true;

// 物理緊急停止スイッチの検知入力。**LOW = 押されている（停止中）。**
// INPUT_PULLUP で読むので、断線したときも LOW 側＝停止側へ倒れる。
//
// この基板にはゲートドライバの出力禁止（DIS）が無く、PC から止める手段は
// duty 0 だけしかない。REF はその一重の防壁に対する数少ない追加情報なので、
// 押下は緊急停止ラッチへ落として FEEDBACK bit3 で PC へ知らせる（仕様書 §5.2）。
constexpr uint8_t kPinRef = 2;
constexpr bool kRefActiveLow = true;

// CAN。UNO R4 Minima の CAN ペリフェラルは D4(TX) / D5(RX) に固定されており、
// Arduino_CAN の CAN インスタンスが variant の PIN_CAN0_TX / PIN_CAN0_RX を使う。
// ここの定数は配線確認用で、コードから直接は使わない。
// これらのピンを他用途へ割り当てると PC から止められない基板になるため、
// src/main.cpp の static_assert が衝突をビルド時に検出する。
constexpr uint8_t kPinCanTx = PIN_CAN0_TX;
constexpr uint8_t kPinCanRx = PIN_CAN0_RX;

constexpr uint8_t kPinLed = 13;  // オンボード LED
constexpr uint8_t kPinRgb = 6;   // シリアル RGB LED（1 個）

// DIP スイッチ **2bit**。INPUT_PULLUP の負論理で、LOW = 1。
// 添字がビット位置: {SW0=bit0, SW1=bit1}。
//
// D0/D1 はハードウェア UART(Serial1) と同じピンなので、デバッグ用シリアルには
// 必ず USB CDC の Serial を使うこと。Serial1 を開くと DIP が読めなくなり、
// デバイス ID が化けて別のアクチュエータが動く。
constexpr uint8_t kPinDip[2] = {1, 0};
constexpr uint8_t kDipBitCount = 2;

// ===========================================================================
// チャンネル表（仕様書 §2.2）
// ===========================================================================

// 1 枚の基板が 3 つの DC モータを駆動し、**チャンネルごとに独立したデバイス ID を持つ**。
// PC からは別々のモータとして見える（サーボ基板と同じ扱い）。
constexpr uint8_t kDcChannelCount = 3;

// デバイス ID は「基板種別 | 基板番号 | スロット番号」の固定ビット分割（仕様書 §2.2）。
// 帯も刻み幅も連続ブロック性も要らず、DIP は基板番号そのもの。
//
//   基板番号 | ch0  | ch1  | ch2
//   ---------+------+------+------
//      0     | 0x80 | 0x81 | 0x82
//      1     | 0x88 | 0x89 | 0x8A
//      2     | 0x90 | 0x91 | 0x92
//      3     | 0x98 | 0x99 | 0x9A
//
// candump に 0x8A が流れていれば「DC 基板 1 枚目の ch2」と直接読める。
constexpr motorcan::BoardKind kBoardKind = motorcan::BoardKind::Dc;

// 焼き忘れた基板をセッティングタイムに見つけるための版番号（仕様書 §3.6）。
// **プロトコルかピン配置を変えたら必ず上げること。**
constexpr uint8_t kFirmwareVersion = 1;

struct DcChannelConfig {
    uint8_t pwmPin;
    uint8_t dirPin;
    float maxDuty;     // 仕様書 §5.3 の duty 上限（SET_PARAM 0x00 で変更可）
    const char *name;  // シリアルデバッグ表示用。CAN の挙動には影響しない
};

// TODO(実機で確認): max_duty はモータとギヤ比が決まってから詰めること。
// サンプルは 50% を上限にしている。ここは安全側に 30% から始める。
constexpr float kDefaultMaxDuty = 0.30f;

// 既定は config/main_hand.yaml の実構成に合わせてある。ch1 / ch2 は現在未使用で、
// PC 側の yaml にモータとして登録されていない（指令が来ないので回らない）。
constexpr DcChannelConfig kDcChannels[kDcChannelCount] = {
    {kPinPwm[0], kPinDir[0], kDefaultMaxDuty, "conveyor"},
    {kPinPwm[1], kPinDir[1], kDefaultMaxDuty, "ch1"},
    {kPinPwm[2], kPinDir[2], kDefaultMaxDuty, "ch2"},
};

// ===========================================================================
// モータ出力
// ===========================================================================

// PWM 30kHz。可聴域を外しつつ MOSFET のスイッチング損失を抑える。
// サンプルの `begin(30000.0f, 0.0f)`（周波数 [Hz] を取る float オーバーロード）と
// 同じ値。uint32_t の版は「周期 [us]」を取る別物なので取り違えないこと。
constexpr float kPwmFrequencyHz = 30000.0f;

// TODO(実機で確認): duty 0 のときハーフブリッジがコーストになるかブレーキになるか。
// この基板には出力禁止（DIS）が無いので、停止＝PWM 0% であり、そのときの
// 挙動は出力段の構成そのもので決まる。機構の噛み込みからの復帰性に効く。

// ===========================================================================
// 制御ループ
// ===========================================================================

// コマンドウォッチドッグ（仕様書 §5.1）。PC 側は最後に指令した目標値を
// kDefaultCommandTimeoutMs 以内に再送し続ける契約なので、途絶は PC の停止か
// ケーブル断を意味する。止まらない基板は PC から止められない基板でもある。
//
// 0 にすると途絶しても駆動を続け、FEEDBACK の bit4 も報告しなくなる。これは
// 手で cansend を打つようなベンチ確認（20Hz の再送を用意できない場合）のための
// 逃げ道であって、試合では既定の 1 のまま使う。再送が間に合わない状態は運用上の
// 異常なので、ここや command_timeout_ms を触って覆い隠してはならない（仕様書 §8）。
//
// この値は setup() が DcChannel::setWatchdogEnabled() へ写す。判定を #if で
// main.cpp 側に置くと、同じ分岐を両ファームが各自で持つことになり、片方に入れ忘れても
// 誰も気付けない。有効/無効の判定は MotorSafety にだけある。
#define WATCHDOG_ENABLED 1

// command_timeout_ms / feedback_interval_ms（仕様書 §3.4 の既定値）は PC 側との契約なので
// MotorCanProtocol.h の kDefaultCommandTimeoutMs / kDefaultFeedbackIntervalMs が持つ。
// 基板ごとに変えてよい値ではなく、両基板の config.h に同じ数字を書くと片方だけ古くなる。

// ===========================================================================
// 表示
// ===========================================================================

// シリアル RGB LED（1 個）による状態表示。基板に実装されている。
// 無効にするとオンボード LED の点滅だけになる（状態の区別は付かなくなる）。
#define HAS_RGB_LED 1
constexpr uint8_t kRgbBrightness = 30;

// DIP オフセット適用後のデバイス ID が 0x00 になったチャンネルがあるとき、および
// CAN が上がらなかったときの速い点滅（仕様書 §2.2）。
constexpr uint32_t kUnconfiguredBlinkIntervalMs = 200;

// 正常時のハートビート点滅周期。ファームが生きていることを目視で確認するため。
constexpr uint32_t kHeartbeatIntervalMs = 1000;

// INFO（版番号の自己申告）の送信周期。1Hz なら 8 デバイスでもバス負荷は無視できる。
constexpr uint32_t kInfoIntervalMs = 1000;

// ===========================================================================
// デバッグ用シリアル
// ===========================================================================

// USB CDC の Serial から「<ch> <duty>」で duty を直接入力できるようにする（0 で無効）。
// 緊急停止ラッチ中はシリアルからも駆動できない（DcChannel が指令を拒否する）。
#define ENABLE_SERIAL_DEBUG 1
constexpr uint32_t kSerialBaud = 115200;
