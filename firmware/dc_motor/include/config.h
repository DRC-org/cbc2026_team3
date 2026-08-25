// DC モータ用自作モタドラの機体依存定数。
//
// ここに集約してあるのは「基板を見ないと確定できない値」と「チューニングで変わる値」。
// TODO(実機で確認) が付いた定数は仮置きであり、通電前に必ず基板・データシートと
// 突き合わせること。誤ったまま通電するとモータ・ドライバ IC を壊しうる。
//
// パラメータの一部は SET_PARAM で実行時に変更できるが、RAM 上のみで電源断で
// ここの既定値に戻る（仕様書 §3.4）。恒久的に変えたい値はこのファイルを直すこと。

#pragma once

#include <stdint.h>

// ===========================================================================
// ピン配置（チーム提供のサンプルコードの配線に準拠）
// ===========================================================================

// モータ出力: 符号-絶対値方式のハーフブリッジ 2 系統。
// 正転で PWM_N に duty・PWM_L を 0、逆転でその逆、停止で両方 0（コースト）。
constexpr uint8_t kPinPwmN = 11;
constexpr uint8_t kPinPwmL = 10;

// ゲートドライバの出力禁止。緊急停止・ID 未設定・フォールト時にアサートする。
constexpr uint8_t kPinDis = 7;

// エンコーダ。ENC_A / ENC_B は外部割込み対応ピンであること（RA4M1 の IRQ0/IRQ1）。
constexpr uint8_t kPinEncA = 2;
constexpr uint8_t kPinEncB = 3;
constexpr uint8_t kPinEncX = 15;  // A1。Z 相（原点）。現状は未使用

constexpr uint8_t kPinSwA = 16;  // A2。リミットスイッチ想定。現状は未使用
constexpr uint8_t kPinSwB = 17;  // A3。リミットスイッチ想定。現状は未使用
constexpr uint8_t kPinSens = 14;  // A0。電流センス
constexpr uint8_t kPinInt = 12;   // ゲートドライバのフォールト出力想定。現状は未使用

// CAN。CAN ペリフェラルのピンは variant 固定で、Arduino_CAN の CAN インスタンスが
// PIN_CAN0_TX / PIN_CAN0_RX を使う。ここの定数は配線確認用で、コードから直接は使わない。
//
// !!! Minima と WiFi でピンが違う !!!
//   Minima : TX=D4,  RX=D5
//   WiFi   : TX=D10, RX=D13   ← D13 はオンボード LED と同じピン
// チーム提供のサンプルが CAN_RX 5 / CAN_TX 4 を前提にしているので基板は Minima と
// 見ているが、WiFi でビルドした場合に LED が CAN 線を奪わないよう下でガードしている。
constexpr uint8_t kPinCanRx = PIN_CAN0_RX;
constexpr uint8_t kPinCanTx = PIN_CAN0_TX;

constexpr uint8_t kPinScl = 19;  // A5。現状は未使用
constexpr uint8_t kPinSda = 18;  // A4。現状は未使用

// ステータス LED を駆動してよいか。
// WiFi では D13 が CAN RX なので、pinMode(OUTPUT) にした時点で受信が死ぬ。
// 「LED が光らない」より「PC から止められない基板」の方が明らかに危険なため、
// 衝突する構成では LED 側を自動的に諦める。
//
// constexpr ではなくマクロで判定しているのは、#if がプリプロセッサの段階で評価され
// constexpr 変数を参照できない（未定義識別子として 0 に潰れ、ガードが黙って
// 無効化される）ため。
#define PIN_STATUS_LED 13

#if !defined(PIN_CAN0_TX) || !defined(PIN_CAN0_RX)
#error "PIN_CAN0_TX / PIN_CAN0_RX が未定義。config.h は Arduino.h の後に include すること"
#endif

#if (PIN_STATUS_LED == PIN_CAN0_TX) || (PIN_STATUS_LED == PIN_CAN0_RX)
#define HAS_STATUS_LED 0
#else
#define HAS_STATUS_LED 1
#endif

constexpr uint8_t kPinLed = PIN_STATUS_LED;  // オンボード LED
constexpr uint8_t kPinRgb = 6;               // シリアル RGB LED（HAS_RGB_LED 時のみ）

// DIP スイッチ 4bit（デバイス ID）。INPUT_PULLUP の負論理で、LOW = 1。
// 添字がビット位置: {SW0=bit0, SW1=bit1, SW2=bit2, SW3=bit3}。
//
// SW2/SW3 は D1/D0 = ハードウェア UART(Serial1) と同じピンなので、
// デバッグ用シリアルには必ず USB CDC の Serial を使うこと。Serial1 を開くと
// DIP が読めなくなり、デバイス ID が化けて別のアクチュエータが動く。
constexpr uint8_t kPinDip[4] = {8, 9, 1, 0};

// ===========================================================================
// モータ出力
// ===========================================================================

// PWM 周期 30us（約 33kHz）。可聴域を外しつつ MOSFET のスイッチング損失を抑える。
constexpr uint32_t kPwmPeriodUs = 30;

// TODO(実機で確認): DIS の論理。アクティブ HIGH（HIGH で出力禁止）と仮定している。
// ゲートドライバのデータシートで反転していたらここを false にする。
// 逆にすると「緊急停止でモータが全力で回る」最悪の事故になるため、通電前に必ず確認。
constexpr bool kDisActiveHigh = true;

// 仕様書 §5.3。サンプルコードは maxDuty 未初期化で常に 0 になりモータが回らなかったため、
// 既定値を明示的に持つ。安全側に低め。
constexpr float kDefaultMaxDuty = 0.30f;

// ===========================================================================
// エンコーダ（位置・速度フィードバック）
// ===========================================================================

// エンコーダ無しの基板では 0 にする。位置・速度制御は使えなくなり duty のみになる。
#define HAS_ENCODER 1

// TODO(実機で確認): エンコーダの 1 相あたりパルス数（モータ軸）。
constexpr float kEncoderPulsesPerMotorRev = 500.0f;

// A/B 両相の立上り・立下りを数える 4 逓倍。
constexpr float kEncoderQuadratureMultiplier = 4.0f;

// TODO(実機で確認): 減速比（モータ軸回転 : 出力軸回転）。
// 仕様書 §3.2 のとおり FEEDBACK の位置・速度は出力軸換算で送る（PC 側は換算を知らない）。
constexpr float kGearRatio = 30.0f;

// TODO(実機で確認): 配線次第で A/B が入れ替わり回転方向が反転する。
// 「正の duty を与えたとき速度が正になる」ようにここで合わせる。
constexpr float kEncoderDirectionSign = 1.0f;

constexpr float kEncoderCountsPerOutputRev =
    kEncoderPulsesPerMotorRev * kEncoderQuadratureMultiplier * kGearRatio;

// ===========================================================================
// 電流センス
// ===========================================================================

// 電流センスを実装していない基板では 0 にする。FEEDBACK の電流は常に 0 になり、
// 過電流フラグも立たなくなる。
#define HAS_CURRENT_SENSE 1

// TODO(実機で確認): SENS の換算係数（ADC カウント → mA）。
// 双方向センスを想定し、無電流時のカウントを 0 点として差分から電流を出す。
// analogReadResolution は既定の 10bit（0–1023）のまま使う。
constexpr uint16_t kCurrentSenseZeroCount = 512;
constexpr float kCurrentSenseMaPerCount = 10.0f;

// TODO(実機で確認): 過電流しきい値。モータとドライバ IC の連続定格から決める。
constexpr float kDefaultOvercurrentThresholdMa = 5000.0f;

// ===========================================================================
// 温度
// ===========================================================================

// この基板は温度センサを持たない。FEEDBACK の温度は常に 0 を送る（仕様書 §3.2 / §7）。
// PC 側の温度警告（既定 65℃）は発火しない。基板改版で載ったらここを 1 にする。
#define HAS_TEMPERATURE_SENSOR 0

// ===========================================================================
// 制御ループ
// ===========================================================================

// 1kHz。PWM 周期（33kHz）より十分遅く、FEEDBACK 周期（100Hz）より十分速い。
constexpr uint32_t kControlIntervalUs = 1000;

// コマンドウォッチドッグ（仕様書 §5.1）。0 にすると SET_TARGET が途絶えても停止しない。
//
// PC 側の目標値定期再送が未実装（仕様書 §7）のため、有効のままだとコンベアに run を
// 指令しても kDefaultCommandTimeoutMs 後に止まる。PC 側が入るまでの暫定運用として
// ここを 0 にするか、command_timeout_ms を大きく設定すること。
// 安全側の既定は「有効」であり、無効化は必ず意識的に行う。
#define WATCHDOG_ENABLED 1

// 仕様書 §3.4 の既定値。
constexpr uint32_t kDefaultCommandTimeoutMs = 500;
constexpr uint32_t kDefaultFeedbackIntervalMs = 10;  // 100Hz

// TODO(実機で確認): PID ゲイン。
// 仕様書 §3.4 の kp/ki/kd は position と velocity で共有する 1 組であり、
// 実際に使うモードに合わせて調整すること（モード切替時に積分項はクリアされる）。
// 出力は duty 次元（-1.0～+1.0）で、誤差の単位は position=deg / velocity=rpm。
constexpr float kDefaultKp = 0.01f;
constexpr float kDefaultKi = 0.0f;
constexpr float kDefaultKd = 0.0f;

// 積分項の飽和上限（duty 次元）。ワインドアップで解除直後に急発進するのを防ぐ。
constexpr float kIntegralLimit = 1.0f;

// 仕様書 §3.4 の reached_tolerance 既定値。SET_PARAM はそのとき有効なモード側を書き換える。
constexpr float kDefaultReachedToleranceDeg = 1.0f;
constexpr float kDefaultReachedToleranceRpm = 5.0f;

// ===========================================================================
// 表示
// ===========================================================================

// シリアル RGB LED による状態表示。
// FastLED_NeoPixel が lib_deps に無い環境でもビルドが通るよう、main.cpp 側で
// __has_include を見てオンボード LED のみのフォールバックへ落とす。
// 実際に発光させるには platformio.ini の lib_deps にライブラリを追加すること。
#define HAS_RGB_LED 1

// DIP が 0x00（設定忘れ）のときの赤点滅周期（仕様書 §2.2）。
constexpr uint32_t kUnconfiguredBlinkIntervalMs = 200;

// 正常時のハートビート点滅周期。ファームが生きていることを目視で確認するため。
constexpr uint32_t kHeartbeatIntervalMs = 1000;

// ===========================================================================
// デバッグ用シリアル
// ===========================================================================

// USB CDC の Serial から duty を直接入力できるようにする（0 で無効）。
// 緊急停止ラッチ中はシリアルからも駆動できない（applyOutput が一括で禁止する）。
#define ENABLE_SERIAL_DEBUG 1
constexpr uint32_t kSerialBaud = 115200;
