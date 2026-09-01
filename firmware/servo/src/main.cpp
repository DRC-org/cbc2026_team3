// サーボ用自作モタドラのファームウェア本体（Arduino Nano / ATmega328P）。
//
// プロトコルの単一情報源は docs/motor_driver_can_protocol.md（特に §7）。
// 機体依存の定数はすべて include/config.h にある。
//
// 責務の分割:
//   MotorCan（Arduino 非依存）… フレームの符号化・復号、宛先判定、緊急停止ラッチ、
//                                ウォッチドッグ、角度補間、**両者の結線**（ServoChannel）、
//                                周期タイマ、シリアル行組み立て
//   このファイル              … ペリフェラル初期化、スロット管理、CAN 送受信の配線
//
// 「出力禁止中は角度指令を受け付けず、補間より先に現在角で凍結する」（§7.5）は
// ServoChannel が持つ。ここで ServoMotion / MotorSafety を直に触ると、その規則を
// 迂回する経路（＝緊急停止中に動くサーボ）が書けてしまう。
//
// DC 用（firmware/dc_motor）と同じ判断をする箇所は MotorCan 側に置くこと。
// 両 main.cpp が同じ分岐を各自で持つと、片方だけ直したことに誰も気付けない。
//
// **DC 用とは MCU も CAN の持ち方も違う**（config.h 冒頭を参照）。
//   - Arduino Nano（ATmega328P / 8bit）。R4 専用の PwmOut は使えず Servo ライブラリを使う
//   - CAN は MCP2515 を SPI で外付け。INT ピンのレベルで受信を知る
//   - D11/D12/D13 が SPI。D13 は SCK なのでステータス LED に使えず、RGB LED が担う
//
// サーボ固有の扱い（DC 用との違い）:
//   - 1 枚が 5 スロットを持ち、各スロットはサーボにもセンサにもなる（§7.1）
//   - position モードのみ受理する（§7.2）
//   - 到達フラグは実測ではなくファームの推定値（§7.3）
//   - 緊急停止・ウォッチドッグでは脱力させず現在角を保持する（§7.5）

#include <Arduino.h>
#include <SPI.h>
#include <Servo.h>
#include <mcp_can.h>
#include <stdlib.h>

#include "MotorCanProtocol.h"
#include "MotorCanRouter.h"
#include "MotorLoopTimer.h"
#include "SerialLineBuffer.h"
#include "SerialOverride.h"
#include "ServoChannel.h"
#include "ServoMotion.h"
#include "config.h"

#if HAS_RGB_LED
#include <Adafruit_NeoPixel.h>
#endif

using namespace motorcan;

// ===========================================================================
// 配線の静的検証
// ===========================================================================

// 使うピンをすべて 1 つの表にまとめてから検証する。DC 用でこれを怠り、config.h の
// 想定と実基板の配線がまるごと食い違ってもビルドが通る状態を作った経緯がある。
// SPI の 3 本を含めるのは、そこを他用途へ割り当てると CAN が丸ごと死ぬため。
static constexpr uint8_t kFixedPins[] = {
    kPinMcpInt, kPinMcpCs, kPinSpiMosi, kPinSpiMiso, kPinSpiSck,
    kPinRgb,    kPinDip[0], kPinDip[1], kPinDip[2],  kPinDip[3],
};
static constexpr uint8_t kFixedPinCount = sizeof(kFixedPins) / sizeof(kFixedPins[0]);

// **constexpr のループで continue を使わないこと。** avr-gcc 7.3 は constexpr 評価中の
// continue で増分式を飛ばし、無限ループになって
// 「constexpr loop iteration count exceeds limit」でビルドが落ちる。
// 実際 Unused スロットを 1 つ置いただけでこれを踏んだ。条件は if の入れ子で書く。
static constexpr bool slotPinsAreSane() {
    for (uint8_t i = 0; i < kServoSlotCount; ++i) {
        if (kServoSlots[i].role != SlotRole::Unused) {
            for (uint8_t f = 0; f < kFixedPinCount; ++f) {
                if (kServoSlots[i].pin == kFixedPins[f]) {
                    return false;
                }
            }
            for (uint8_t j = static_cast<uint8_t>(i + 1); j < kServoSlotCount; ++j) {
                if (kServoSlots[j].role != SlotRole::Unused &&
                    kServoSlots[i].pin == kServoSlots[j].pin) {
                    return false;
                }
            }
        }
    }
    return true;
}
static_assert(slotPinsAreSane(),
              "config.h のスロットのピンが SPI/CAN/RGB/DIP と衝突しているか、重複している");

static constexpr bool fixedPinsAreUnique() {
    for (uint8_t i = 0; i < kFixedPinCount; ++i) {
        for (uint8_t j = static_cast<uint8_t>(i + 1); j < kFixedPinCount; ++j) {
            if (kFixedPins[i] == kFixedPins[j]) {
                return false;
            }
        }
    }
    return true;
}
static_assert(fixedPinsAreUnique(), "config.h の固定ピン（SPI/CAN/RGB/DIP）が重複している");

// デバイス ID は makeDeviceId が「基板種別 | 基板番号 | スロット番号」で組み立てるので、
// スロット間の重複も帯からのはみ出しも構造的に起こらない（仕様書 §2.2）。
// かつては基準 ID の表を持ち、重複・連続ブロック性・帯・センサのビット割り当ての
// 4 つを static_assert で見張っていたが、ビット分割と 1 デバイス 1 ビットで規則ごと消えた。

// 下の g_servo / g_channel は各スロットを明示的に初期化している。
// スロットを増やすときはそれらの初期化子も一緒に足すこと。
static_assert(kServoSlotCount == 5,
              "スロット数を変えたら g_servo / g_channel の初期化子も更新すること");
static_assert(kServoSlotCount <= motorcan::kMaxSlotNumber + 1,
              "スロット数がデバイス ID のスロット番号（3bit）に収まらない");

// 宛先判定の結果はスロットのビットマスク（uint8_t）で返ってくる。
static_assert(kServoSlotCount <= motorcan::kMaxChannels,
              "スロット数が FrameRoute::channelMask のビット数を超えている");

static_assert(kDipBitCount == sizeof(kPinDip) / sizeof(kPinDip[0]),
              "kDipBitCount と kPinDip の要素数が一致していない");

// ===========================================================================
// ペリフェラルと状態（すべてスロット単位）
// ===========================================================================

static MCP_CAN g_can(kPinMcpCs);
static bool g_canFailed = false;

// 連続して送信に失敗した回数。**sendMsgBuf の戻り値を捨ててはならない** ——
// mcp_can の sendMsg() は空き TX バッファ待ちと TXREQ クリア待ちの二段で
// TIMEOUTVALUE(2500us) まで回るので、バス不通・bus-off・調停混雑では 1 通あたり
// 最大 5ms を食う。捨てると「loop() だけが伸び続けて誰にも何も届かない基板」が
// 平常時と同じ緑のハートビートを出し続ける。
static uint16_t g_txFailStreak = 0;

static Servo g_servo[kServoSlotCount];

// 仕様書 §5.4: 起動時の緊急停止ラッチは解除済み。§7.1 のとおり宛先がスロットなので
// ウォッチドッグも補間もスロットごとに独立して動く。
static ServoChannel g_channel[kServoSlotCount] = {
    ServoChannel(kServoSlots[0].initialAngleDeg, kServoSlots[0].limits, kDefaultCommandTimeoutMs),
    ServoChannel(kServoSlots[1].initialAngleDeg, kServoSlots[1].limits, kDefaultCommandTimeoutMs),
    ServoChannel(kServoSlots[2].initialAngleDeg, kServoSlots[2].limits, kDefaultCommandTimeoutMs),
    ServoChannel(kServoSlots[3].initialAngleDeg, kServoSlots[3].limits, kDefaultCommandTimeoutMs),
    ServoChannel(kServoSlots[4].initialAngleDeg, kServoSlots[4].limits, kDefaultCommandTimeoutMs),
};

// DIP ブロックオフセット適用後の実効デバイス ID。
// **サーボ以外のスロットは常に 0x00 にする。** そうしておくと routeFrame が
// 自分宛と判定しないので、予約 ID 宛のフレームで何かが動く経路が構造的に無くなる。
static uint8_t g_deviceId[kServoSlotCount] = {0, 0, 0, 0, 0};

// Servo::attach() を通したかどうか。ID 未設定スロットは attach すらしない。
static bool g_attached[kServoSlotCount] = {false, false, false, false, false};

// センサの現在値。スロットごとに独立で、そのスロット自身の FEEDBACK で報告する。
static bool g_sensorActive[kServoSlotCount] = {false, false, false, false, false};

// 送信周期は基板全体で 1 つ（スロットごとに変えると送信位相の分散が崩れる）。
static uint16_t g_feedbackIntervalMs = kDefaultFeedbackIntervalMs;
static PeriodicTimer g_feedbackTimer[kServoSlotCount];

static PeriodicTimer g_motionTimer;
static PeriodicTimer g_infoTimer;
static PeriodicTimer g_blinkTimer;
static bool g_ledOn = false;

#if HAS_RGB_LED
static Adafruit_NeoPixel g_strip(1, kPinRgb, NEO_GRB + NEO_KHZ800);
#endif

#if ENABLE_SERIAL_DEBUG
// シリアルから角度を入力しているスロットと、その期限（規則は SerialOverride.h）。
// CAN の SET_TARGET を受けたら解除して、PC の指令とシリアルが競合しないようにする。
static SerialOverride g_serialOverride;
static char g_serialStorage[kSerialLineCapacity];
static SerialLineBuffer g_serialLine(g_serialStorage, sizeof(g_serialStorage));
#endif

// ===========================================================================
// スロットの役割
// ===========================================================================

static bool isServoSlot(uint8_t slot) { return kServoSlots[slot].role == SlotRole::Servo; }
static bool isSensorSlot(uint8_t slot) { return kServoSlots[slot].role == SlotRole::TouchSensor; }

// Unused 以外はすべて CAN デバイスとして FEEDBACK を送る。
static bool isDeviceSlot(uint8_t slot) { return kServoSlots[slot].role != SlotRole::Unused; }

// 仕様書 §2.2 / §7.1: オフセット適用後のデバイス ID が 0x00 のスロットは
// 駆動も報告もしない（設定ミスで意図しないアクチュエータが動くより安全）。
static bool isSlotConfigured(uint8_t slot) {
    return isDeviceSlot(slot) && g_deviceId[slot] != kDeviceIdUnconfigured;
}

// ===========================================================================
// センサ
// ===========================================================================

// 判断は一切持たせない。仕様書 §5.2 / §7 のとおり基板は状態を報告するだけで、
// 原点合わせも停止も PC 側が決める（基板に閾値やリトライを持たせると、機構の
// 調整のたびにファームを焼き直すことになる）。
static void readSensors() {
    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        if (!isSensorSlot(slot)) {
            continue;
        }
        const int level = digitalRead(kServoSlots[slot].pin);
        g_sensorActive[slot] =
            kServoSlots[slot].sensorActiveLow ? (level == LOW) : (level == HIGH);
    }
}

// ===========================================================================
// 出力
// ===========================================================================

// サーボへのパルス出力はすべてこの関数を通す。ID 未設定・可動範囲クランプ・脱力設定の
// いずれかを迂回する経路を作らないため（dc_motor の applyChannelOutput() と同じ方針）。
//
// 仕様書 §7.5 の「新しい角度指令を受け付けず現在角を保持する」は ServoChannel が持つ。
// ここで凍結すると補間より後ろになり、凍結する前に 1 ティック分だけ進んでしまう。
//
// write() ではなく writeMicroseconds() を使うのは、サンプルのように 180/270 を掛けて
// 0-180 に押し込むと分解能が 2/3 に落ち、可動範囲の端が表現できなくなるため。
static void applyChannelOutput(uint8_t slot, uint32_t nowMs) {
    if (!isServoSlot(slot) || !isSlotConfigured(slot) || !g_attached[slot]) {
        // 設定ミスで意図しないアクチュエータが動くより、動かない方が安全。
        // ID 未設定スロットは attach していないのでパルスは 1 発も出ない。
        return;
    }

    if (kEStopDetach && !g_channel[slot].isOutputAllowed(nowMs)) {
        // 脱力させたい機構のための切り替え。既定は false。
        // detach でサーボへのフレームが消え、出力軸が back-drivable になる。
        if (g_servo[slot].attached()) {
            g_servo[slot].detach();
        }
        return;
    }
    if (kEStopDetach && !g_servo[slot].attached()) {
        // 脱力から復帰するときは、現在角のパルスを持って繋ぎ直す。
        g_servo[slot].attach(kServoSlots[slot].pin, kServoSlots[slot].pulse.minUs,
                             kServoSlots[slot].pulse.maxUs);
    }

    // ここに来る角度は ServoMotion が angle_min / angle_max でクランプ済み（仕様書 §7.2）。
    // angleToPulseUs 側でもパルス幅を [minUs, maxUs] に収める二重の防壁になっている。
    const uint16_t pulseUs =
        angleToPulseUs(g_channel[slot].currentAngleDeg(), kServoSlots[slot].pulse);
    g_servo[slot].writeMicroseconds(static_cast<int>(pulseUs));
}

// ===========================================================================
// 補間
// ===========================================================================

static void updateMotion(uint32_t nowMs) {
    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        if (!isServoSlot(slot)) {
            continue;
        }
        // tick() が「出力禁止なら補間より先に現在角で凍結する」まで面倒を見る（§7.5）。
        g_channel[slot].tick(nowMs);
        applyChannelOutput(slot, nowMs);
    }
}

// ===========================================================================
// CAN
// ===========================================================================

// 状態フラグの組み立て規則そのものは composeFeedbackFlags が持つ（native テスト圏内）。
// **ここで OR を足してはならない。** 「センサスロットは緊急停止・ウォッチドッグ・到達を
// 立てない」（仕様書 §5.2）は SlotKind::Sensor から導かれ、以前はこの early-return が
// 唯一の実装だったので `return flags;` を消してもテストが 1 件も落ちなかった。
static uint8_t buildStatusFlags(uint8_t slot, uint32_t nowMs) {
    const SlotKind kind = isSensorSlot(slot) ? SlotKind::Sensor : SlotKind::Actuator;
    return composeFeedbackFlags(kBoardKind, kind, g_channel[slot].safetyStatusFlags(nowMs),
                                isSlotConfigured(slot), g_channel[slot].isReached(),
                                g_sensorActive[slot]);
}

// CAN 送信 1 通ぶんの結果を記録する。**戻り値を捨てないための唯一の口**にしてあるので、
// g_can.sendMsgBuf() を直に呼ぶ経路を作らないこと。
static void sendFrame(uint16_t canId, uint8_t length, uint8_t *data) {
    if (g_can.sendMsgBuf(canId, 0, length, data) == CAN_OK) {
        g_txFailStreak = 0;
        return;
    }
    if (g_txFailStreak < 0xFFFF) {
        ++g_txFailStreak;
    }
}

static void sendFeedback(uint8_t slot, uint32_t nowMs) {
    uint8_t data[kFeedbackWithPositionLength];
    const uint8_t flags = buildStatusFlags(slot, nowMs);

    // **センサは位置を持たないので状態フラグ 1 バイトだけ**（仕様書 §3.2）。
    // サーボが返す位置は補間中の「指令角」であって実測ではないが、
    // **angle_min / angle_max でクランプされた結果が分かる唯一の手段**なので送る
    // （reached はクランプ後の目標に対して立つため、クランプに気付けない）。
    const uint8_t len =
        isSensorSlot(slot)
            ? encodeFeedback(data, flags)
            : encodeFeedback(data, flags,
                             static_cast<int32_t>(
                                 lroundf(g_channel[slot].currentAngleDeg() * kAngleScale)));

    // 緊急停止中・ウォッチドッグ作動中も送り続ける。
    // 止めると PC 側が STALE になり、なぜ動かないのかを操縦者が判別できなくなる。
    // **ID 未設定スロットはここへ来ない**（呼び出し側が isSlotConfigured で弾く。§2.2）。
    sendFrame(buildCanId(CommandType::Feedback, g_deviceId[slot]), len, data);
}

// 仕様書 §3.4: 焼き忘れた基板をセッティングタイムに見つけるための自己申告。
// 低頻度（1Hz）で送るので、PC が後から起動しても拾える。
static void sendInfo(uint8_t slot) {
    uint8_t data[kInfoWithServoRangeLength];
    // **可動レンジを足すのはサーボスロットだけ**（仕様書 §3.4）。センサスロットは
    // 角度そのものを持たないので、載せると PC 側に「測ったように見える 0」が届く。
    // この申告だけが、config.h の ServoPulseSpec と実物の型（180/270）の食い違いを
    // CAN 越しに見える形にしている（仕様書 §7.7）。
    if (isServoSlot(slot)) {
        const uint8_t len = encodeInfo(data, kFirmwareVersion, kBoardKind, SlotKind::Actuator,
                                       kServoSlots[slot].pulse.angleRangeDeg);
        sendFrame(buildCanId(CommandType::Info, g_deviceId[slot]), len, data);
        return;
    }

    const SlotKind kind = isSensorSlot(slot) ? SlotKind::Sensor : SlotKind::Actuator;
    const uint8_t len = encodeInfo(data, kFirmwareVersion, kBoardKind, kind);
    sendFrame(buildCanId(CommandType::Info, g_deviceId[slot]), len, data);
}

// **nowMs を受け取るのは、出力禁止中の SET_PARAM を ServoChannel が保留するため**
// （仕様書 §7.5）。ここで判断すると main.cpp にゲートが増え、native テストの
// 圏外で規則が 2 箇所に分かれる。
static void applyParam(uint8_t slot, const SetParamCommand &cmd, uint32_t nowMs) {
    // command_timeout_ms / feedback_interval_ms は 3 枚に共通なので MotorCan が持つ。
    if (applyCommonParam(cmd, g_channel[slot], g_feedbackIntervalMs)) {
        return;
    }
    switch (cmd.id) {
        case ParamId::CommandTimeoutMs:
        case ParamId::FeedbackIntervalMs:
            // applyCommonParam が処理済み。ここへは来ない。
            break;
        case ParamId::ReachedTolerance:
            g_channel[slot].setReachedToleranceDeg(fromRaw(cmd.raw, kAngleScale), nowMs);
            break;
        case ParamId::SlewRate: {
            ServoLimits limits = g_channel[slot].limits();
            limits.slewRateDegPerSec = fromRaw(cmd.raw, kRateScale);
            g_channel[slot].setLimits(limits, nowMs);
            break;
        }
        case ParamId::AngleMin: {
            ServoLimits limits = g_channel[slot].limits();
            limits.angleMinDeg = fromRaw(cmd.raw, kAngleScale);
            g_channel[slot].setLimits(limits, nowMs);
            break;
        }
        case ParamId::AngleMax: {
            ServoLimits limits = g_channel[slot].limits();
            limits.angleMaxDeg = fromRaw(cmd.raw, kAngleScale);
            g_channel[slot].setLimits(limits, nowMs);
            break;
        }
        case ParamId::MaxDuty:
            // 仕様書 §3.3: DC 固有。この基板は制御則を持たないので無視する。
            break;
    }
}

static void handleSlotFrame(uint8_t slot, CommandType command, const uint8_t *data, uint8_t len,
                            uint32_t nowMs) {
    // センサは駆動されないので、どのコマンドも受け付けない（自分宛の E_STOP も
    // ブロードキャストも含む）。止める出力が無く、ラッチしても意味が無いため。
    // 未使用スロットにはそもそも自分宛の ID が無い（g_deviceId が 0x00）。
    if (!isServoSlot(slot)) {
        return;
    }

    switch (command) {
        case CommandType::SetTarget: {
            // 仕様書 §6: 緊急停止ラッチ中でもウォッチドッグは養う。
            // 養わないと解除した直後に満了済みで動かない。
            // 制御タイプが position でなくても養うのは、通信自体は生きているため。
            // 受理判定（ServoChannel::setTarget）より必ず先に呼ぶこと。起動直後は
            // §5.4 により未受信＝出力禁止なので、順序を逆にすると最初の 1 通を捨てる。
            g_channel[slot].feed(nowMs);

            const SetTargetCommand cmd = decodeSetTarget(data, len);
            if (!cmd.valid) {
                return;
            }
#if ENABLE_SERIAL_DEBUG
            // PC がこのスロットへ指令を出している以上、シリアルの上書きは終わり。
            g_serialOverride.clear();
#endif
            // **制御タイプの受理判定（§7.2: position のみ）は ServoChannel が持つ。**
            // ここに `if (cmd.type != ...)` を戻すと、この翻訳単位は native テストの
            // 対象外なので消しても全ケース緑になる。
            // 続けて angle_min / angle_max でクランプし（§7.2）、緊急停止ラッチ中・
            // ウォッチドッグ満了中は受け付けない（§7.5）。
            g_channel[slot].applySetTarget(cmd, nowMs);
            break;
        }
        case CommandType::SetParam: {
            // 仕様書 §7.6: command_timeout_ms(0x01) / feedback_interval_ms(0x02) /
            // reached_tolerance(0x03) / slew_rate(0x04) / angle_min(0x05) /
            // angle_max(0x06) を処理し、max_duty(0x00) は制御則を持たないので無視する。
            const SetParamCommand cmd = decodeSetParam(data, len);
            if (cmd.valid) {
                applyParam(slot, cmd, nowMs);
            }
            break;
        }
        case CommandType::EStop: {
            // 停止でも解除でも、その場で現在角の保持へ倒す（§7.5）。凍結そのものは
            // ServoChannel が行い、ここは脱力設定のときにパルスを切るために出力を回す。
            const EStopAction action = g_channel[slot].handleEStopFrame(data, len, nowMs);
            if (action != EStopAction::None) {
                applyChannelOutput(slot, nowMs);
#if ENABLE_SERIAL_DEBUG
                g_serialOverride.clear();
#endif
            }
            break;
        }
        case CommandType::Feedback:
        case CommandType::Info:
            // 他基板がモタドラ → PC 方向へ送るフレーム。受信フィルタで落としているが、
            // フィルタ設定を変えたときに素通りしないよう明示的に無視する。
            break;
    }
}

static void handleFrame(uint32_t rxId, const uint8_t *data, uint8_t len) {
    // mcp_can は拡張フレームを bit31、RTR を bit30 で返す。どちらも我々のプロトコルには
    // 存在しないので、ID の下位 11bit だけを見て routeFrame に判定させる。
    const bool extended = (rxId & 0x80000000UL) != 0;
    const bool remote = (rxId & 0x40000000UL) != 0;
    const uint16_t canId = static_cast<uint16_t>(rxId & 0x7FFUL);

    // Standard Frame 判定・予約コマンド種別・宛先判定（スロット表との突き合わせ /
    // ブロードキャスト E_STOP は全スロット / ID 未設定スロットに自分宛は無い）は
    // DC 用と同じ規則なので MotorCanRouter に集約してある。
    const FrameRoute route =
        routeFrame(canId, !extended && !remote, g_deviceId, kServoSlotCount);
    if (!route.accepted) {
        return;
    }

    const uint32_t nowMs = millis();
    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        if ((route.channelMask & static_cast<uint8_t>(1u << slot)) != 0) {
            handleSlotFrame(slot, route.command, data, len, nowMs);
        }
    }
}

// **PC → モタドラ方向のフレームだけを通す**（E_STOP / SET_TARGET / SET_PARAM）。
//
// 全通過（MCP_ANY）にすると、共有バス上の FEEDBACK（自作モタドラ 14 台 × 100Hz）と
// INFO まで MCP2515 の受信バッファ（**RXB0 / RXB1 の 2 段しかない**）へ流れ込み、
// SPI で読み出しては捨てるだけの仕事が loop() に乗る。この基板の loop() は
// sendMsgBuf が 1 通あたり最大 5ms（空き TX 待ち + TXREQ クリア待ち）まで伸びうるので、
// 2 段は 1.4ms 相当で溢れる。落ちるのがブロードキャスト E_STOP だと、症状は
// 「たまに緊急停止が効かないサーボ基板」という最も追いにくい形になる。
//
// **どの ID を通すかは CommandType から導く**（`kEStopAndSetTargetFilter` /
// `kSetParamFilter`）。ここに残しているのは MCP2515 固有の事情だけ ——
// mcp2515_write_mf は ext=0 のとき ulData の **bit16 以降**を SIDH/SIDL へ詰めるので、
// 標準 ID はそこへ載せる（低位 16bit は拡張 ID のマスクになるので 0 にする。
// 非 0 にすると標準フレームでもデータ 1-2 バイト目と比較され始める）。
//
// **1 本のマスクでは 3 値を表せない**ので 2 バンクに分ける。
//   RXB0（マスク 0）… E_STOP + SET_TARGET。時間に厳しい方を優先度の高い RXB0 に置く
//   RXB1（マスク 1）… SET_PARAM
// RXB0 は BUKT（ロールオーバー）付きなので、RXB0 が埋まっている間に来た E_STOP は
// RXB1 のフィルタに関係なく RXB1 へ落ちる。
constexpr uint32_t kStdIdShiftInFilterReg = 16;

constexpr uint32_t toFilterReg(uint16_t stdId) {
    return static_cast<uint32_t>(stdId) << kStdIdShiftInFilterReg;
}

// **ビット位置を取り違えるとフィルタが全通過にも全遮断にもなる。**
// 全遮断なら「CAN が生きているのに指令が 1 通も効かない」、全通過なら
// この修正そのものが無効になり、どちらも実機でしか気付けない。
static_assert(toFilterReg(0x7FF) == 0x07FF0000UL,
              "標準 ID がマスク/フィルタレジスタの想定位置（bit16 以降）に載っていない");
static_assert((toFilterReg(kEStopAndSetTargetFilter.mask) & 0xFFFFUL) == 0,
              "拡張 ID 側のマスクが 0 でない（標準フレームがデータ 1-2 バイト目と比較される）");

static bool configureCanFilters() {
    // RXB0: E_STOP + SET_TARGET。フィルタ 2 本とも同じ値にしないと、
    // 初期化時の 0x000 が別のマスクで残って予期しない ID を通す。
    if (g_can.init_Mask(0, 0, toFilterReg(kEStopAndSetTargetFilter.mask)) != MCP2515_OK) {
        return false;
    }
    for (uint8_t f = 0; f <= 1; ++f) {
        if (g_can.init_Filt(f, 0, toFilterReg(kEStopAndSetTargetFilter.id)) != MCP2515_OK) {
            return false;
        }
    }

    // RXB1: SET_PARAM。フィルタは 4 本あるので全部同じ値で埋める。
    if (g_can.init_Mask(1, 0, toFilterReg(kSetParamFilter.mask)) != MCP2515_OK) {
        return false;
    }
    for (uint8_t f = 2; f <= 5; ++f) {
        if (g_can.init_Filt(f, 0, toFilterReg(kSetParamFilter.id)) != MCP2515_OK) {
            return false;
        }
    }
    return true;
}

static void pollCan() {
    // MCP2515 の INT は受信バッファが空になるまで LOW のまま。mcp_can は RX 割り込みだけを
    // 有効にするので、LOW = 受信あり と見てよい。
    // 回数に上限を置くのは、万一 INT が別の理由で張り付いても loop() を止めないため
    // （止まると補間もフィードバックも凍り、PC からは STALE にしか見えない）。
    for (uint8_t guard = 0; guard < 8; ++guard) {
        if (digitalRead(kPinMcpInt) != LOW) {
            return;
        }
        unsigned long rxId = 0;
        unsigned char len = 0;
        unsigned char buf[8];
        if (g_can.readMsgBuf(&rxId, &len, buf) != CAN_OK) {
            return;
        }
        handleFrame(static_cast<uint32_t>(rxId), buf, static_cast<uint8_t>(len));
    }
}

// ===========================================================================
// デバイス ID（DIP スイッチ = スロット表全体へのブロックオフセット）
// ===========================================================================

// 1 枚がスロットごとに別のデバイス ID を持つため（§7.1）、DIP は
// **スロット表全体に加えるブロックオフセット**として働く。同一ファームの基板を
// 複数枚使うとき、2 枚目の DIP を 1 段上げるだけで全スロットの ID がまとめて
// 次のブロックへ移る。刻み幅がスロット数でないとブロックが重なる理由、
// 帯からはみ出したときの扱いは MotorCanRouter が持つ（native テストで守られている）。
//
// **Unused のスロットだけ 0x00 のままにする。** センサは自分のデバイス ID で
// FEEDBACK を送るので ID を持つ（持たせないと「センサだけの基板」が何も報告できない）。
static void resolveDeviceIds() {
    const uint8_t boardNumber = readDipSwitch(
        kPinDip, kDipBitCount, [](uint8_t pin) { return static_cast<int>(digitalRead(pin)); },
        LOW);
    motorcan::resolveDeviceIds(g_deviceId, kServoSlotCount, kBoardKind, boardNumber,
                               isDeviceSlot);
}

// ===========================================================================
// LED
// ===========================================================================

static void updateLed(uint32_t nowMs) {
    // **送信が続けて失敗している基板も「今すぐ直さないと使えない」側に入れる。**
    // FEEDBACK も INFO も出ていないので PC 側からは STALE にしか見えず、
    // 配線不良と区別が付かない。ここが唯一の切り分け手段になる（仕様書 §2.2 と同じ扱い）。
    BoardIndication indication(g_canFailed || g_txFailStreak >= kCanTxFailStreakAlarm);
    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        // Unused スロットは ID を名乗らないので「未設定」に数えない（数えると
        // 空きスロットのある基板が常に赤く点滅する）。緊急停止はサーボスロットだけが持つ。
        if (!isDeviceSlot(slot)) {
            continue;
        }
        indication.observe(
            isSlotConfigured(slot),
            isServoSlot(slot) &&
                (g_channel[slot].safetyStatusFlags(nowMs) & status_flag::kEStop) != 0);
    }

    // CAN が上がらない・ID 未設定は「この基板は今すぐ直さないと使えない」状態なので、
    // 平常のハートビートと区別が付くよう速い点滅にする（仕様書 §2.2）。
    // **この基板は緊急停止を色（橙）で示す**ので、点滅の速さは平常と同じにする。
    const uint32_t interval = blinkIntervalFor(indication, kUnconfiguredBlinkIntervalMs,
                                               kHeartbeatIntervalMs, kHeartbeatIntervalMs);
    if (!g_blinkTimer.due(nowMs, interval)) {
        return;
    }
    g_ledOn = !g_ledOn;

#if HAS_RGB_LED
    // DC 用と同じ表示規則にしてある。基板が違うたびに色の意味が変わると、
    // 現場で 2 種類の対応表を覚えることになる。
    // 赤（速い点滅）= CAN 不通 / 送信失敗 / ID 未設定、橙 = 緊急停止ラッチ中、緑 = 平常。
    uint8_t r = 0;
    uint8_t g = 0;
    if (indication.urgent) {
        r = g_ledOn ? 255 : 0;
    } else if (indication.stopped) {
        r = 255;
        g = 96;
    } else {
        g = g_ledOn ? 255 : 32;
    }

    // **AVR 版 Adafruit_NeoPixel::show() は 1 LED あたり約 30us 割り込みを禁止する。**
    // その窓に Servo ライブラリの Timer1 割り込み（パルス終端）が当たると、
    // **そのパルスだけが最大 30us 伸びる** —— kServoPulse270 は 7.04us/deg なので
    // 約 4.3deg のヒゲになる（grip 5deg / 壁 6deg の微小ストロークではほぼ全域に相当）。
    // 次のフレーム（20ms 後）で正しい幅に戻るので機構への影響は一瞬だが、
    // **呼ぶ回数を増やしてはならない**（updateMotion の直後や毎ループの位置へ
    // 動かすと、当たる確率がそのまま比例して上がる）。色が変わらないときに
    // 送らないのは、緊急停止ラッチ中（橙で固定）に毎秒 1 回叩き続けないため。
    static uint8_t lastR = 0xFF;
    static uint8_t lastG = 0xFF;
    if (r != lastR || g != lastG) {
        lastR = r;
        lastG = g;
        g_strip.setPixelColor(0, g_strip.Color(r, g, 0));
        g_strip.show();
    }
#endif
}

// ===========================================================================
// デバッグ用シリアル
// ===========================================================================

#if ENABLE_SERIAL_DEBUG

// 「<スロット> <角度[deg]>」で 1 スロットへ角度を指令する。's' で全スロットを現在角に凍結。
// 角度は setTarget が可動範囲でクランプし、緊急停止ラッチ中は ServoChannel が拒否するため、
// ここから安全機構を迂回することはできない（仕様書 §5.2 の要求）。
static void pollSerial(uint32_t nowMs) {
    while (Serial.available() > 0) {
        if (!g_serialLine.push(static_cast<char>(Serial.read()))) {
            continue;
        }
        // 行の骨格の解釈（'s' / '<番号> <値>'）は parseSerialCommand が持つ。
        // 値の読み取りと「サーボスロットか」の判定だけを基板ごとに行う。
        const SerialCommand cmd = parseSerialCommand(g_serialLine.line(), kServoSlotCount);
        if (cmd.kind == SerialCommand::Kind::StopAll) {
            g_serialOverride.clear();
            for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
                if (isServoSlot(slot)) {
                    g_channel[slot].hold(nowMs);
                }
            }
        } else if (cmd.kind == SerialCommand::Kind::Channel && isServoSlot(cmd.channel)) {
            // 緊急停止ラッチ中はシリアルからも角度を通さない（ServoChannel が拒否する）。
            // **avr-libc に strtof は無い。** AVR では double が 32bit float なので strtod で足りる。
            // **float が入る唯一の経路。** toRaw が NaN と範囲外を飽和させるので、
            // ここから先には CAN 経路と同じ値しか流れない（仕様書 §4）。
            const float deg = fromRaw(
                toRaw(static_cast<float>(strtod(cmd.value, nullptr)), kAngleScale), kAngleScale);
            g_channel[cmd.channel].setTarget(deg, nowMs);
            g_serialOverride.note(cmd.channel, nowMs);
        }
    }

    // シリアル操作中はウォッチドッグを養い続ける。1 回だけ養う実装だと
    // command_timeout_ms 後に必ず止まってデバッグにならない。
    // **養う範囲と期限の規則は SerialOverride が持つ**（打ったスロットだけ /
    // 最後の入力から kSerialOverrideHoldMs）。ここで全スロットを養うと、
    // 1 行打っただけで基板ぜんぶの最後の砦が無期限に外れる。
    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        if (isServoSlot(slot) && g_serialOverride.shouldFeed(slot, nowMs)) {
            g_channel[slot].feed(nowMs);
        }
    }
}

#endif  // ENABLE_SERIAL_DEBUG

// ===========================================================================
// setup / loop
// ===========================================================================

void setup() {
#if ENABLE_SERIAL_DEBUG
    Serial.begin(kSerialBaud);
#endif

    pinMode(kPinMcpInt, INPUT);

    for (uint8_t bit = 0; bit < kDipBitCount; ++bit) {
        pinMode(kPinDip[bit], INPUT_PULLUP);
    }

    // センサは attach より先に入力へ倒す。出力のまま放置すると、サーボ用の
    // ピンとして駆動されてセンサ回路を叩く。
    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        if (kServoSlots[slot].role == SlotRole::TouchSensor) {
            pinMode(kServoSlots[slot].pin, INPUT_PULLUP);
        }
    }

#if HAS_RGB_LED
    g_strip.begin();
    g_strip.setBrightness(kRgbBrightness);
#endif

    // attach する前に実効デバイス ID を確定させる。
    // ID 未設定のスロットには Servo::attach() すら通さず、パルスを 1 発も出さない
    // （仕様書 §2.2: 設定ミスで意図しないアクチュエータが動くより動かない方が安全）。
    resolveDeviceIds();

    const uint32_t startMs = millis();

    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        // 送信位相の分散は PeriodicTimer::stagger が持つ（式と理由は MotorLoopTimer.h）。
        // ID 未設定のスロットは 1 通も送らない（§2.2）が、位相の割り当てはスロットの
        // 添字で決まるので、ここは全スロット分やる（飛ばすと隣とずれ方が変わる）。
        g_feedbackTimer[slot].stagger(startMs, g_feedbackIntervalMs, slot, kServoSlotCount);

        if (!isServoSlot(slot) || !isSlotConfigured(slot)) {
            continue;
        }
        // 仕様書 §5.4 / §7: 起動時は config.h の初期角へ。
        // attach の直後に角度を書かないと、Servo ライブラリの既定パルス幅で
        // サーボが意図しない位置へ飛ぶ。
        g_servo[slot].attach(kServoSlots[slot].pin, kServoSlots[slot].pulse.minUs,
                             kServoSlots[slot].pulse.maxUs);
        g_attached[slot] = g_servo[slot].attached();
        if (g_attached[slot]) {
            g_servo[slot].writeMicroseconds(static_cast<int>(
                angleToPulseUs(g_channel[slot].currentAngleDeg(), kServoSlots[slot].pulse)));
        }
    }

    // 仕様書 §1: 1 Mbps。MCP2515 の水晶は 16MHz。
    // CAN が上がらない基板を駆動させると PC から止められないので、
    // begin / フィルタ設定が失敗したら緊急停止ラッチに落として新しい角度指令を
    // 受け付けなくする（§7.5 のとおり出力は切らず、初期角を保持したままになる）。
    //
    // **MCP_ANY ではなく MCP_STDEXT を渡す。** MCP_ANY はマスク／フィルタを丸ごと
    // 無効化するので configureCanFilters() が効かない。**MCP_STD は使えない** ——
    // mcp_can では「シリコンのバグ」としてコメントアウトされており、渡すと
    // begin() が MCP2515_FAIL を返して基板がまるごと止まる。MCP_STDEXT は
    // フィルタを有効にしたまま標準・拡張の両方を各フィルタの EXIDE で判別する設定で、
    // 下で全 6 本を標準 ID として書き直すので拡張フレームは 1 通も通らない。
    if (g_can.begin(MCP_STDEXT, CAN_1000KBPS, MCP_16MHZ) != CAN_OK || !configureCanFilters()) {
        g_canFailed = true;
        for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
            g_channel[slot].stop(startMs);
        }
    } else {
        g_can.setMode(MCP_NORMAL);
    }

    // config.h のビルド時フラグを実行時フラグへ写す（仕様書 §5.1 / §8）。
    // 判定そのものは MotorSafety にしか無いので、写し忘れれば有効のまま動く。
    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        g_channel[slot].setWatchdogEnabled(WATCHDOG_ENABLED != 0);
    }

    readSensors();
    g_motionTimer.reset(startMs);
    g_infoTimer.reset(startMs);
    g_blinkTimer.reset(startMs);
}

void loop() {
    const uint32_t nowMs = millis();

    pollCan();
#if ENABLE_SERIAL_DEBUG
    pollSerial(nowMs);
#endif

    if (g_motionTimer.due(nowMs, kMotionIntervalMs)) {
        // センサは補間と同じ周期で読む。FEEDBACK より速く読んでおかないと、
        // 送信の直前に取った値と実際の接触時刻が最大 1 周期ぶんずれる。
        readSensors();
        updateMotion(nowMs);
    }

    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        // **ID を名乗れるスロットだけが FEEDBACK を送る**（仕様書 §2.2）。
        // Unused 以外なら役割は問わない（センサだけの基板でも PC が読める）。
        if (isSlotConfigured(slot) && g_feedbackTimer[slot].due(nowMs, g_feedbackIntervalMs)) {
            sendFeedback(slot, nowMs);
        }
    }

    // 仕様書 §3.4: 版番号の自己申告。起動時 1 回ではなく低頻度で送り続けるのは、
    // PC が基板より後から起動しても拾えるようにするため。
    if (g_infoTimer.due(nowMs, kInfoIntervalMs)) {
        for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
            if (isSlotConfigured(slot)) {
                sendInfo(slot);
            }
        }
    }

    updateLed(nowMs);
}
