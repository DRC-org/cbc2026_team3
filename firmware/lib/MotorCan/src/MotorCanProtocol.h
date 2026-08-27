// 自作モータドライバ CAN プロトコルの符号化・復号層。
// 単一情報源は docs/motor_driver_can_protocol.md であり、PC 側 lib/drivers/generic.py と
// 対になる。片方だけを変更してはならない。
//
// Arduino.h を include しないのは意図的で、native 環境（pio test -e native）で
// そのままコンパイルしてテストできるようにするため。DC 用とサーボ用のファームで共有する。

#pragma once

#include <stdint.h>

namespace motorcan {

// 仕様書 §2.1。0b100/0b101/0b110 は予約で、ここに載せてはならない。
// 予約値を有効扱いすると PC 側 parse_can_id が例外を投げ、そのバスの受信ループが
// 停止してバス上の全モータが STALE になる。
enum class CommandType : uint8_t {
    SetTarget = 0,
    Feedback = 1,
    SetMode = 2,
    SetParam = 3,
    EStop = 7,
};

// 仕様書 §4。duty は百分率ではなく -1.0～+1.0。
enum class ControlType : uint8_t {
    Position = 0,
    Velocity = 1,
    Duty = 2,
};

// 仕様書 §3.4 のパラメータ ID 表。
enum class ParamId : uint8_t {
    Kp = 0x00,
    Ki = 0x01,
    Kd = 0x02,
    MaxDuty = 0x03,
    CommandTimeoutMs = 0x04,
    FeedbackIntervalMs = 0x05,
    OvercurrentThresholdMa = 0x06,
    ReachedTolerance = 0x07,
};

// 仕様書 §3.2 FEEDBACK Byte7 の状態フラグ。
namespace status_flag {
constexpr uint8_t kReached = 1 << 0;
constexpr uint8_t kOvercurrent = 1 << 1;
constexpr uint8_t kOverheat = 1 << 2;
constexpr uint8_t kEStop = 1 << 3;
constexpr uint8_t kWatchdog = 1 << 4;
constexpr uint8_t kDeviceIdUnconfigured = 1 << 5;
}  // namespace status_flag

// 仕様書 §2.2。0x00 は「DIP 設定忘れ」とみなして駆動を拒否する。
constexpr uint8_t kDeviceIdUnconfigured = 0x00;
constexpr uint8_t kDeviceIdBroadcast = 0xFF;
constexpr uint16_t kBroadcastEStopCanId = 0x7FF;

// 全フレーム DLC = 8（仕様書 §3）。
constexpr uint8_t kFrameLength = 8;

// 仕様書 §3.5 の解除マジックバイト。1 バイトの値だけで安全装置が開かないようにする。
constexpr uint8_t kEStopClearMagic1 = 0x5A;
constexpr uint8_t kEStopClearMagic2 = 0xA5;

// 仕様書 §3.4 の既定値のうち、PC 側との契約になっているもの。
//
// command_timeout_ms は PC 側の目標値再送周期（この値の数分の 1）の根拠であり、
// feedback_interval_ms は PC 側の STALE 判定が前提にしている送信周期。どちらも
// 基板ごとに変えてよい値ではないので、基板の config.h ではなくここが単一定義を持つ。
// 同じ数字を両 config.h に書くと、仕様が動いたとき片方だけが古くなる。
//
// 一方 max_duty / overcurrent / kp・ki・kd / reached_tolerance は仕様書に既定値が
// 載っていてもアクチュエータ単位のチューニング値なので、各 config.h が持つ。
constexpr uint32_t kDefaultCommandTimeoutMs = 500;
constexpr uint32_t kDefaultFeedbackIntervalMs = 10;  // 100Hz

// SET_PARAM 0x04 / 0x05 で受け付ける範囲（仕様書 §3.4）。
//
// command_timeout_ms に上限が無いと、CAN の 1 フレームでウォッチドッグを実質無効に
// できてしまう。仕様書 §5.1 が「WATCHDOG_ENABLED に SET_PARAM の ID は無い」と書いて
// 最後の砦を守っているのに、猶予そのものを 49.7 日へ伸ばせば同じ結果になるため、
// 猶予の側にも上限が要る。上限は既定の 4 倍で、これを超えると「PC が落ちても
// コンベアが数秒回り続ける」ことになり最後の砦として機能しない。
// 下限は PC 側の再送周期の目安（既定 500ms に対して 50ms）。それより短い猶予は、
// 契約どおり再送している健全な機体を止めるだけで安全性を上げない。
constexpr uint32_t kMinCommandTimeoutMs = 50;
constexpr uint32_t kMaxCommandTimeoutMs = 2000;

// 0 は送信が詰まってバスを埋める。上限側は、極端に長い周期にすると PC からは
// 「基板が死んだ（STALE）」ようにしか見えず、原因の切り分けができなくなる。
constexpr uint32_t kMinFeedbackIntervalMs = 1;
constexpr uint32_t kMaxFeedbackIntervalMs = 1000;

// SET_PARAM の float 値を上の範囲へ丸める。範囲外は fallbackMs ではなく境界値に
// 倒す（書いた値に近い側で動かす方が現場で挙動を推測しやすい）。
// NaN / 無限大だけは fallbackMs（＝現在値）を返して指令ごと捨てる。
// 化けた float32 を uint32_t へ直接キャストするのは未定義動作で、RA4M1 では
// 負値が 0 に飽和して「永久に出力禁止」、他の処理系では 4294967295ms に化ける。
uint32_t sanitizeCommandTimeoutMs(float value, uint32_t fallbackMs);
uint32_t sanitizeFeedbackIntervalMs(float value, uint32_t fallbackMs);

// ---------------------------------------------------------------------------
// CAN ID
// ---------------------------------------------------------------------------

uint16_t buildCanId(CommandType command, uint8_t deviceId);

struct CanIdInfo {
    CommandType command;
    uint8_t deviceId;
    bool valid;  // 予約コマンド種別・11bit 超過の ID では false
};

CanIdInfo parseCanId(uint16_t canId);

// ---------------------------------------------------------------------------
// スカラのバイト列変換（すべてリトルエンディアン）
// ---------------------------------------------------------------------------

// ホストのバイト順を仮定せずに IEEE754 float32 LE を組み立てる。
// AVR/ARM/x86 いずれでも同じバイト列になることを保証するため、memcpy でビット列を
// 取り出したあとシフトでバイトを並べる。
void packFloatLe(uint8_t *dst, float value);
float unpackFloatLe(const uint8_t *src);

void packInt16Le(uint8_t *dst, int16_t value);
int16_t unpackInt16Le(const uint8_t *src);

// int16 に収まらない値は折り返さず飽和させる。
// キャストで折り返すと +4000deg が負値に化け、PC 側が逆方向へ位置制御しかねない。
int16_t saturateToInt16(int32_t value);

// ---------------------------------------------------------------------------
// 受信フレームの復号（PC → モタドラ）
// ---------------------------------------------------------------------------

struct SetTargetCommand {
    ControlType type;
    float value;
    bool valid;
};
SetTargetCommand decodeSetTarget(const uint8_t *data, uint8_t length);

struct SetModeCommand {
    ControlType type;
    bool valid;
};
SetModeCommand decodeSetMode(const uint8_t *data, uint8_t length);

struct SetParamCommand {
    ParamId id;
    float value;
    bool valid;  // 未知のパラメータ ID は false（仕様書 §3.4: 無視する）
};
SetParamCommand decodeSetParam(const uint8_t *data, uint8_t length);

enum class EStopAction : uint8_t {
    None = 0,
    Stop = 1,
    Clear = 2,
};
EStopAction decodeEStop(const uint8_t *data, uint8_t length);

// ---------------------------------------------------------------------------
// 送信フレームの構築（モタドラ → PC）
// ---------------------------------------------------------------------------

// out には kFrameLength バイト以上の領域が必要。
// 位置・速度・電流は int32 で受けて int16 に飽和させる（呼び出し側で丸め済みの値を渡す）。
void encodeFeedback(uint8_t *out, int32_t position_0p1deg, int32_t rpm, int32_t current_ma,
                    uint8_t temperature_c, uint8_t flags);

// ---------------------------------------------------------------------------
// duty
// ---------------------------------------------------------------------------

// duty を [-maxDuty, +maxDuty] に収める（仕様書 §5.3）。
// maxDuty 自体も 0.0–1.0 に丸め、NaN は 0 に落とす。
// サンプルコードは maxDuty 未初期化で常に 0 になりモータが回らないバグがあったため、
// 既定値は呼び出し側（config.h の kDefaultMaxDuty）が必ず持つこと。
float clampDuty(float duty, float maxDuty);

}  // namespace motorcan
