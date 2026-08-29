// サーボ用自作モタドラの機体依存定数。
//
// 基板は **Arduino Nano**（ATmega328P / 8bit / 5V / 16MHz）。DC 用の UNO R4 Minima とは
// MCU も CAN の持ち方も違うので、ピン配置を DC 用から類推してはならない。
//   - CAN は MCP2515 を SPI で外付け（内蔵 CAN ペリフェラルは無い）
//   - D11/D12/D13 をハードウェア SPI が占有する
//   - **D13 は SCK。オンボード LED はステータス表示に使えない**（RGB LED がその役目）
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

#include "MotorCanProtocol.h"
#include "ServoMotion.h"

// ===========================================================================
// ピン配置
// ===========================================================================

// MCP2515（CAN コントローラ）。INT は受信バッファが埋まっている間 LOW になる。
constexpr uint8_t kPinMcpInt = 3;
constexpr uint8_t kPinMcpCs = 10;

// ハードウェア SPI。コードからは直接使わないが、他用途へ割り当てると CAN が死ぬので
// 衝突検査の対象に入れてある（src/main.cpp の static_assert）。
constexpr uint8_t kPinSpiMosi = 11;
constexpr uint8_t kPinSpiMiso = 12;
constexpr uint8_t kPinSpiSck = 13;

// シリアル RGB LED（1 個）。D13 が SPI の SCK に取られているため、
// 状態表示はこれが唯一の手段になる。
constexpr uint8_t kPinRgb = 9;
constexpr uint8_t kRgbBrightness = 30;

// DIP スイッチ 4bit。INPUT_PULLUP の負論理で、LOW = 1。
// 添字がビット位置: {SW0=bit0, SW1=bit1, SW2=bit2, SW3=bit3}。
// A0〜A3 は Nano では 14〜17。Arduino.h に依存しないよう数値で書いてある。
constexpr uint8_t kPinDip[4] = {14, 15, 16, 17};
constexpr uint8_t kDipBitCount = 4;

// ===========================================================================
// スロット表（仕様書 §7.1）
// ===========================================================================

// **この基板は 5 本の信号線（SV0〜SV4）を持ち、どれもサーボにもセンサにもなる。**
// どのスロットを何に使うかは配線で決まるので、役割を表に持たせて 1 行で切り替えられる
// ようにしてある。**サーボ・センサ・空きが何個ずつでも動く**（センサだけの基板も可）。
//
// これが成立するのは **1 スロット = 1 CAN デバイス**にしてあるからで、センサも自分の
// デバイス ID で FEEDBACK を送る。サーボのフレームに相乗りさせると、相乗り先の無い
// 「センサだけの基板」が成立せず、載せられる個数も予約ビットの数で頭打ちになる。
constexpr uint8_t kServoSlotCount = 5;

enum class SlotRole : uint8_t {
    Servo,        // サーボ出力。deviceId 宛の SET_TARGET で動く
    TouchSensor,  // デジタル入力。自分のデバイス ID で FEEDBACK を送り bit6 で報告するだけ
    Unused,       // 何も繋がない。pinMode すら触らない
};

struct ServoSlotConfig {
    SlotRole role;
    // デバイス ID は表に持たない。**スロットの添字がそのままデバイス ID の下位 3bit**
    // になるので（仕様書 §2.2）、配線で役割を変えても ID は動かない。
    uint8_t pin;
    float initialAngleDeg;         // 起動時に持っていく角度（仕様書 §5.4）
    motorcan::ServoLimits limits;  // 可動範囲とスルーレート（SET_PARAM 0x04-0x06 で変更可）
    motorcan::ServoPulseSpec pulse;
    // TouchSensor のとき、LOW を「入力あり」とみなすか。
    // 報告ビットは常に FEEDBACK のセンサ入力（自分のデバイス ID で送るので 1 つで足りる）。
    bool sensorActiveLow;
    const char *name;     // シリアルデバッグ表示用。CAN の挙動には影響しない
};

// TODO(実機で確認): 角度 → パルス幅の対応。サンプルの attach(pin, 500, 2400) に合わせてある。
// **サンプルのように 180/270 を掛けて write() の 0-180 に押し込む変換はしない。**
// 分解能が 2/3 に落ち、可動範囲の端が表現できなくなるため（ServoMotion.h 参照）。
// ファームは Servo::writeMicroseconds() でパルス幅を直接指令する。
constexpr motorcan::ServoPulseSpec kServoPulse270{500, 2400, 270.0f};

// TODO(実機で確認): angle_min / angle_max は機構が付いた状態で「当たらない範囲」を
// 実測して入れること。現状は config/main_hand_positions.yaml が 0〜6deg の微小ストロークしか
// 使わないのに合わせた安全側の仮値で、広げるのは機構確定後。**狭すぎる分にはクランプで
// 止まるだけだが、広すぎるとメカストッパに当たったまま停動して焼損する。**
constexpr motorcan::ServoLimits kProvisionalLimits{0.0f, 30.0f, 90.0f};

// 既定は config/main_hand.yaml / config/sub_hand.yaml の実構成に合わせてある。
// **4ch あるので自作サーボはこの 1 枚で全部まかなえる。**
//
// 以下のデバイス ID は **DIP の基板番号が 0（全 OFF）のとき**の値。基板番号を N に
// すると 8 ずつずれる（N=1 なら 0x48〜0x4C）ので、PC 側 yaml の can_id もそちらへ合わせる。
// 実際の値は candump が教えてくれる: FEEDBACK の CAN ID は 0x300 + デバイス ID。
//
//   スロット | ピン | 役割        | デバイス ID | PC 側のモータ / 用途
//   ---------+------+-------------+------------+---------------------------
//   SV0      | D4   | Servo       | 0x40       | gripper       (メインハンド)
//   SV1      | D5   | Servo       | 0x41       | wall_f        (メインハンド)
//   SV2      | D6   | Servo       | 0x42       | wall_r        (メインハンド)
//   SV3      | D7   | Servo       | 0x43       | sub_gripper   (サブハンド)
//   SV4      | D8   | TouchSensor | 0x44       | origin_sensor (原点合わせ用)
//
// **Unused 以外のスロットはすべて CAN デバイスとして FEEDBACK を送る。**
// センサも PC 側 yaml に 1 モータとして登録すること（登録しないと受信ループが
// そのフレームを誰にも配らない）。目標値を持たないので SET_TARGET は飛ばず、
// motor_check.magnitude: 0 で動作確認からは外れるが、途絶は STALE として検出される。
//
// 役割を変えるときは、その行の SlotRole と最後の引数（sensorActiveLow）だけを
// 書き換える。**デバイス ID は動かさないこと** — スロットに固定しておくと、
// 配線を差し替えても PC 側 yaml の can_id が無変更で済む。
//
// デバイス ID が PC 側 yaml と一致していることが唯一の接点で、照合する仕組みは無い。
// ずれるとそのモータは指令を受け取らず FEEDBACK も来ない（PC からは STALE に見える）。
constexpr ServoSlotConfig kServoSlots[kServoSlotCount] = {
    {SlotRole::Servo, 4, 0.0f, kProvisionalLimits, kServoPulse270, false, "gripper"},
    {SlotRole::Servo, 5, 0.0f, kProvisionalLimits, kServoPulse270, false, "wall_f"},
    {SlotRole::Servo, 6, 0.0f, kProvisionalLimits, kServoPulse270, false, "wall_r"},
    {SlotRole::Servo, 7, 0.0f, kProvisionalLimits, kServoPulse270, false, "sub_gripper"},
    // TODO(実機で確認): 接触時に導通して LOW になる想定（サンプル準拠）。
    // 極性が逆だと「触れていないのに触れている」と報告し続け、原点合わせが即座に終わる。
    {SlotRole::TouchSensor, 8, 0.0f, kProvisionalLimits, kServoPulse270, true, "origin_sensor"},
};

// ===========================================================================
// デバイス ID（仕様書 §2.2）
// ===========================================================================

// デバイス ID は「基板種別 | 基板番号 | スロット番号」の固定ビット分割。
// **帯も刻み幅も連続ブロック性も要らない。** DIP は基板番号そのもので、
// スロットの添字がそのまま ID の下位 3bit になる。
//
//   基板番号 | SV0  | SV1  | SV2  | SV3  | SV4
//   ---------+------+------+------+------+------
//      0     | 0x40 | 0x41 | 0x42 | 0x43 | 0x44
//      1     | 0x48 | 0x49 | 0x4A | 0x4B | 0x4C
//
// candump に 0x4A が流れていれば「サーボ基板 1 枚目の SV2」と直接読める。
// DIP は 4bit だが基板番号は 3bit なので、8 以上を設定した基板は全スロットが
// 未設定になる（LED 赤点滅・駆動拒否）。黙って丸めると別の基板の ID を名乗る。
constexpr motorcan::BoardKind kBoardKind = motorcan::BoardKind::Servo;

// 焼き忘れた基板をセッティングタイムに見つけるための版番号（仕様書 §3.6）。
// **プロトコルかピン配置を変えたら必ず上げること。**
constexpr uint8_t kFirmwareVersion = 1;

// INFO（版番号の自己申告）の送信周期。1Hz なら 8 デバイスでもバス負荷は無視できる。
constexpr uint32_t kInfoIntervalMs = 1000;

// ===========================================================================
// 制御ループ
// ===========================================================================

// 補間の更新周期。サーボ自身が内部でパルス幅へ追従するので、速くする意味は薄い。
// Servo ライブラリのフレーム周期（20ms）より速く、FEEDBACK 周期（100Hz）と同等。
// **ATmega328P は float がソフトウェア実装**なので、ここを詰めすぎると CAN 受信が痩せる。
constexpr uint32_t kMotionIntervalMs = 5;

// コマンドウォッチドッグ（仕様書 §5.1）。**宛先がデバイス ID ＝ スロットなので、
// ウォッチドッグもスロットごとに独立して動く。** 1 チャンネルへの指令が途絶えても
// 他のチャンネルは動き続ける（片方の壁だけ通信が切れる、という状況が実在するため）。
//
// PC 側は最後に指令した角度を kDefaultCommandTimeoutMs 以内に再送し続ける契約なので、
// 満了は PC の停止かケーブル断を意味する。**サーボは満了しても現在角を保持するので
// 機構が落ちることはない**が、そこから先は動かせない。
//
// 0 にすると途絶しても新しい角度指令を受け付け続け、FEEDBACK の bit4 も報告しなくなる。
// 手で cansend を打つようなベンチ確認のための逃げ道であって、試合では既定の 1 のまま
// 使う。再送が間に合わない状態は運用上の異常なので、ここや command_timeout_ms を
// 触って覆い隠してはならない（仕様書 §8）。
//
// この値は setup() が MotorSafety::setWatchdogEnabled() へ写す。判定を #if で
// main.cpp 側に置くと、同じ分岐を両ファームが各自で持つことになり、片方に入れ忘れても
// 誰も気付けない。有効/無効の判定は MotorSafety にだけある。
#define WATCHDOG_ENABLED 1

// command_timeout_ms / feedback_interval_ms（仕様書 §3.4 の既定値）は PC 側との契約なので
// MotorCanProtocol.h の kDefaultCommandTimeoutMs / kDefaultFeedbackIntervalMs が持つ。
// 到達許容差の既定値（§7.3 / §7.6 の 0）は ServoMotion.h の
// kDefaultServoReachedToleranceDeg が持ち、ServoMotion が自分で適用する。

// ===========================================================================
// 緊急停止・ウォッチドッグ時の振る舞い（仕様書 §7.5）
// ===========================================================================

// true にすると緊急停止・ウォッチドッグ満了で PWM を止めて（detach して）サーボを脱力させる。
//
// **既定は false（現在角を保持）。** サーボは PWM を止めると back-drivable になり、
// 壁が自重で倒れ、グリッパが把持中のワークを落とす。DC 用の「PWM 0%」と
// 意図的に振る舞いを変えている点であり、変更するときは機構側の影響を必ず確認すること。
constexpr bool kEStopDetach = false;

// ===========================================================================
// 表示
// ===========================================================================

// D13 が SPI の SCK に取られているため、状態表示は RGB LED だけが担う。
// 0 にすると状態を知る手段が丸ごと無くなる（現場で切り分けができない）。
#define HAS_RGB_LED 1

// デバイス ID が未設定のスロットがあるとき、および CAN が上がらなかったときの速い点滅
// （仕様書 §2.2 / §7.1）。
constexpr uint32_t kUnconfiguredBlinkIntervalMs = 200;

// 正常時のハートビート点滅周期。ファームが生きていることを目視で確認するため。
constexpr uint32_t kHeartbeatIntervalMs = 1000;

// ===========================================================================
// デバッグ用シリアル
// ===========================================================================

// USB シリアル（115200 baud）から「<スロット番号> <角度>」で角度を直接指令できる（0 で無効）。
// DIP は A0〜A3 なので、DC 用と違って UART との兼用による制約は無い。
// 緊急停止ラッチ中はシリアルからも駆動できない（ServoChannel が指令を拒否する）。
//
// **Flash 32KB / SRAM 2KB しかないので、容量が足りなくなったらここを 0 にする。**
#define ENABLE_SERIAL_DEBUG 1
constexpr uint32_t kSerialBaud = 115200;
