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

static uint32_t g_feedbackIntervalMs = kDefaultFeedbackIntervalMs;
static PeriodicTimer g_feedbackTimer[kServoSlotCount];

static PeriodicTimer g_motionTimer;
static PeriodicTimer g_infoTimer;
static PeriodicTimer g_blinkTimer;
static bool g_ledOn = false;

#if HAS_RGB_LED
static Adafruit_NeoPixel g_strip(1, kPinRgb, NEO_GRB + NEO_KHZ800);
#endif

#if ENABLE_SERIAL_DEBUG
// シリアルから角度を入力している間だけ true。
// CAN の SET_TARGET を受けたら解除して、PC の指令とシリアルが競合しないようにする。
static bool g_serialOverride = false;
static char g_serialStorage[24];
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

static uint8_t buildStatusFlags(uint8_t slot, uint32_t nowMs) {
    uint8_t flags = 0;
    if (!isSlotConfigured(slot)) {
        flags |= status_flag::kDeviceIdUnconfigured;
    }

    if (isSensorSlot(slot)) {
        // センサは駆動されないので、緊急停止もウォッチドッグも到達も意味を持たない。
        // 立てると PC 側 check_safety_error() が「駆動できない状態」と読んで
        // 動作確認を打ち切る（センサに駆動できない状態は無い）。
        if (g_sensorActive[slot]) {
            flags |= status_flag::kSensor;
        }
        return flags;
    }

    // 緊急停止ラッチ / ウォッチドッグのビットの判定は MotorSafety に集約されている。
    // ウォッチドッグのビットは指令を一度でも受けた後の満了でのみ立ち、
    // 無効化した基板では立たない。
    // ここで手で組み立て直すと、DC 用と同じ条件で立つ保証が無くなる。
    flags |= g_channel[slot].safetyStatusFlags(nowMs);
    if (g_channel[slot].isReached()) {
        // 仕様書 §7.3: これは実測ではなく補間の完了を示す推定値。
        // 脱調・過負荷・メカ干渉で実際には動いていなくても立つ。
        flags |= status_flag::kReached;
    }
    return flags;
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
    // ID 未設定スロットは CAN ID 0x100 で送ることになるが、PC 側へ「デバイス ID 未設定」を
    // 届ける方を優先する。
    g_can.sendMsgBuf(buildCanId(CommandType::Feedback, g_deviceId[slot]), 0, len, data);
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
        g_can.sendMsgBuf(buildCanId(CommandType::Info, g_deviceId[slot]), 0, len, data);
        return;
    }

    const SlotKind kind = isSensorSlot(slot) ? SlotKind::Sensor : SlotKind::Actuator;
    const uint8_t len = encodeInfo(data, kFirmwareVersion, kBoardKind, kind);
    g_can.sendMsgBuf(buildCanId(CommandType::Info, g_deviceId[slot]), 0, len, data);
}

static void applyParam(uint8_t slot, const SetParamCommand &cmd) {
    switch (cmd.id) {
        case ParamId::CommandTimeoutMs:
            // 猶予に上限が無いと、仕様書 §5.1 が守っている最後の砦が
            // SET_PARAM 1 フレームで実質外れる。範囲の根拠は MotorCanProtocol が持つ。
            g_channel[slot].setCommandTimeoutMs(clampCommandTimeoutMs(cmd.raw));
            break;
        case ParamId::FeedbackIntervalMs:
            // 周期は基板全体で 1 つ（スロットごとに変えると位相の分散が崩れる）。
            g_feedbackIntervalMs = clampFeedbackIntervalMs(cmd.raw);
            break;
        case ParamId::ReachedTolerance:
            g_channel[slot].setReachedToleranceDeg(fromRaw(cmd.raw, kAngleScale));
            break;
        case ParamId::SlewRate: {
            ServoLimits limits = g_channel[slot].limits();
            limits.slewRateDegPerSec = fromRaw(cmd.raw, kRateScale);
            g_channel[slot].setLimits(limits);
            break;
        }
        case ParamId::AngleMin: {
            ServoLimits limits = g_channel[slot].limits();
            limits.angleMinDeg = fromRaw(cmd.raw, kAngleScale);
            g_channel[slot].setLimits(limits);
            break;
        }
        case ParamId::AngleMax: {
            ServoLimits limits = g_channel[slot].limits();
            limits.angleMaxDeg = fromRaw(cmd.raw, kAngleScale);
            g_channel[slot].setLimits(limits);
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
            // 仕様書 §7.2: サーボは position のみ受理する。duty 値を角度として解釈すると
            // 想定外の位置へ飛ぶので、velocity / duty は黙って捨てる。
            if (cmd.type != ControlType::Position) {
                return;
            }
#if ENABLE_SERIAL_DEBUG
            g_serialOverride = false;
#endif
            // setTarget が angle_min / angle_max でクランプし（§7.2）、緊急停止ラッチ中・
            // ウォッチドッグ満了中は受け付けない（§7.5）。
            g_channel[slot].setTarget(fromRaw(cmd.raw, kAngleScale), nowMs);
            break;
        }
        case CommandType::SetParam: {
            // 仕様書 §7.6: command_timeout_ms(0x01) / feedback_interval_ms(0x02) /
            // reached_tolerance(0x03) / slew_rate(0x04) / angle_min(0x05) /
            // angle_max(0x06) を処理し、max_duty(0x00) は制御則を持たないので無視する。
            const SetParamCommand cmd = decodeSetParam(data, len);
            if (cmd.valid) {
                applyParam(slot, cmd);
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
                g_serialOverride = false;
#endif
            }
            break;
        }
        case CommandType::Feedback:
        case CommandType::Info:
            // 他基板がモタドラ → PC 方向へ送るフレーム。ID は衝突しないが念のため無視する。
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
    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        g_deviceId[slot] =
            isDeviceSlot(slot) ? makeDeviceId(kBoardKind, boardNumber, slot)
                               : kDeviceIdUnconfigured;
    }
}

// ===========================================================================
// LED
// ===========================================================================

static void updateLed(uint32_t nowMs) {
    bool unconfigured = false;
    bool stopped = false;
    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        if (!isDeviceSlot(slot)) {
            continue;
        }
        if (!isSlotConfigured(slot)) {
            unconfigured = true;
        }
        if (isServoSlot(slot) &&
            (g_channel[slot].safetyStatusFlags(nowMs) & status_flag::kEStop) != 0) {
            stopped = true;
        }
    }

    // CAN が上がらない・ID 未設定は「この基板は今すぐ直さないと使えない」状態なので、
    // 平常のハートビートと区別が付くよう速い点滅にする（仕様書 §2.2）。
    const bool urgent = g_canFailed || unconfigured;
    if (!g_blinkTimer.due(nowMs, urgent ? kUnconfiguredBlinkIntervalMs : kHeartbeatIntervalMs)) {
        return;
    }
    g_ledOn = !g_ledOn;

#if HAS_RGB_LED
    // DC 用と同じ表示規則にしてある。基板が違うたびに色の意味が変わると、
    // 現場で 2 種類の対応表を覚えることになる。
    // 赤（速い点滅）= CAN 不通 / ID 未設定、橙 = 緊急停止ラッチ中、緑 = 平常。
    uint8_t r = 0;
    uint8_t g = 0;
    if (urgent) {
        r = g_ledOn ? 255 : 0;
    } else if (stopped) {
        r = 255;
        g = 96;
    } else {
        g = g_ledOn ? 255 : 32;
    }
    g_strip.setPixelColor(0, g_strip.Color(r, g, 0));
    g_strip.show();
#else
    (void)stopped;
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
        const char *line = g_serialLine.line();

        if (line[0] == 's' || line[0] == 'S') {
            g_serialOverride = false;
            for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
                if (isServoSlot(slot)) {
                    g_channel[slot].hold(nowMs);
                }
            }
        } else {
            // スロット番号と角度が空白で区切られていない行は捨てる。
            // 番号を読み違えると別のサーボが動くので、曖昧な入力は指令にしない。
            char *sep = nullptr;
            const long slot = strtol(line, &sep, 10);
            if (sep != line && *sep == ' ' && slot >= 0 &&
                slot < static_cast<long>(kServoSlotCount) &&
                isServoSlot(static_cast<uint8_t>(slot))) {
                // 緊急停止ラッチ中はシリアルからも角度を通さない（ServoChannel が拒否する）。
                // avr-libc に strtof は無い。AVR では double が 32bit float なので strtod で足りる
                // **float が入る唯一の経路。** toRaw が NaN と範囲外を飽和させるので、
                // ここから先には CAN 経路と同じ値しか流れない（仕様書 §4）。
                const float deg = fromRaw(
                    toRaw(static_cast<float>(strtod(sep + 1, nullptr)), kAngleScale),
                    kAngleScale);
                g_channel[slot].setTarget(deg, nowMs);
                g_serialOverride = true;
            }
        }
    }

    // シリアル操作中はウォッチドッグを養い続ける。
    // 1 回だけ養う実装だと command_timeout_ms 後に必ず止まってデバッグにならない。
    // 'S' 入力・CAN の SET_TARGET・電源断のいずれでもこのモードは解除される。
    if (g_serialOverride) {
        for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
            if (isServoSlot(slot)) {
                g_channel[slot].feed(nowMs);
            }
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
        // 全スロットが同じ周期で同時に送るとフレームのバーストになり、他バスの
        // 周期送信と重なったときに調停待ちが伸びて FEEDBACK の間隔が波打つ。
        // 周期を等分した位相をスロットごとにずらして平準化する。
        // ID 未設定のスロットも「デバイス ID 未設定」を知らせるために送るので、ここは全スロット分やる。
        g_feedbackTimer[slot].setLastMs(startMs - g_feedbackIntervalMs +
                                        (g_feedbackIntervalMs * slot) / kServoSlotCount);

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
    // begin 失敗時は緊急停止ラッチに落として新しい角度指令を受け付けなくする
    // （§7.5 のとおり出力は切らず、初期角を保持したままになる）。
    if (g_can.begin(MCP_ANY, CAN_1000KBPS, MCP_16MHZ) != CAN_OK) {
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
        // Unused 以外はすべて FEEDBACK を送る。センサだけの基板でも PC が読めるようにする。
        if (isDeviceSlot(slot) && g_feedbackTimer[slot].due(nowMs, g_feedbackIntervalMs)) {
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
