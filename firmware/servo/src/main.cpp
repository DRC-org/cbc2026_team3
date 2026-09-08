// サーボ用自作モタドラのファームウェア本体（Arduino Nano / UNO R4 Minima）。
//
// プロトコルの単一情報源は docs/motor_driver_can_protocol.md（特に §7）。
// 機体依存の定数はすべて include/config.h にある。
//
// 責務の分割:
//   MotorCan（Arduino 非依存）… フレームの符号化・復号、宛先判定、緊急停止ラッチ、
//                                ウォッチドッグ、角度補間、**両者の結線**（ServoChannel）、
//                                周期タイマ、シリアル行組み立て
//   can_backend.h の実装      … MCU ごとの CAN ペリフェラルの扱い（MCP2515 / R4 内蔵）
//   このファイル              … ペリフェラル初期化、スロット管理、CAN 送受信の配線
//
// 「出力禁止中は角度指令を受け付けず、補間より先に現在角で凍結する」（§7.5）は
// ServoChannel が持つ。ここで ServoMotion / MotorSafety を直に触ると、その規則を
// 迂回する経路（＝緊急停止中に動くサーボ）が書けてしまう。DC 用（firmware/dc_motor）と
// 同じ判断をする箇所も MotorCan 側に置くこと —— 両 main.cpp が同じ分岐を各自で持つと、
// 片方だけ直したことに誰も気付けない。
//
// **サーボ基板は 3 枚あって MCU が 2 種類ある**（config.h 冒頭を参照）。CAN の扱いだけが
// 違うので servo_can（can_backend.h）へ切り出してあり、このファイルに MCU 依存の #if が
// 残るのは**ピンの表と静的検査だけ**である。
//   - 基板 #0 / #1: Arduino Nano。CAN は MCP2515 を SPI（D11/D12/D13）で外付け。
//     D13 は SCK なのでステータス LED に使えず RGB LED が担う
//   - 基板 #2: UNO R4 Minima。CAN は内蔵で D4/D5 固定
//   - PWM はどちらも Servo ライブラリを使う（DC 用の R4 専用 PwmOut は使わない）
//
// サーボ固有の扱い（DC 用との違い）:
//   - 1 枚が 5 スロットを持ち、各スロットはサーボにもセンサにもなる（§7.1）
//   - position モードのみ受理する（§7.2）
//   - 到達フラグは実測ではなくファームの推定値（§7.3）
//   - 緊急停止・ウォッチドッグでは脱力させず現在角を保持する（§7.5）

#include <Arduino.h>
#include <Servo.h>
#include <stdlib.h>

#include "MotorCanProtocol.h"
#include "MotorCanRouter.h"
#include "MotorLoopTimer.h"
#include "MotorTxHealth.h"
#include "SerialLineBuffer.h"
#include "SerialOverride.h"
#include "ServoChannel.h"
#include "ServoMotion.h"
#include "can_backend.h"
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
//
// **MCU で中身が違う。** Nano は SPI の 3 本と MCP2515 の 2 本を含める（そこを他用途へ
// 割り当てると CAN が丸ごと死ぬ）。R4 の CAN は D4/D5 固定で variant のマクロが正なので、
// ここには写さず下の pinsAvoidCan() が PIN_CAN0_TX / PIN_CAN0_RX を直接見る。
//
// **UART の 2 本（D0/D1）はどちらの MCU でも ENABLE_SERIAL_DEBUG に依らず常に入れる。**
// 理由は config.h の kPinUartRx / kPinUartTx のコメントにある（シリアルを切っても
// その 2 本は使えるようにならないこと / kServoBoards のゼロ埋めの捕獲）。
#if defined(ARDUINO_ARCH_RENESAS)
static constexpr uint8_t kFixedPins[] = {
    kPinUartRx, kPinUartTx, kPinRgb, kPinDip[0], kPinDip[1], kPinDip[2], kPinDip[3],
};
#else
static constexpr uint8_t kFixedPins[] = {
    kPinUartRx, kPinUartTx, kPinMcpInt,  kPinMcpCs,  kPinSpiMosi, kPinSpiMiso,
    kPinSpiSck, kPinRgb,    kPinDip[0],  kPinDip[1], kPinDip[2],  kPinDip[3],
};
#endif
static constexpr uint8_t kFixedPinCount = sizeof(kFixedPins) / sizeof(kFixedPins[0]);

// **このビルドが担う全基板・全スロットのピンを見る。** Unused のスロットを検査から外すと、
// 別の基板でそのスロットを使い始めた瞬間に検査を通っていない配線が動き出す（基板 #1 の
// 行だけ衝突していても症状はその 1 枚にしか出ず、実機を挿すまで気付けない）。
//
// **constexpr のループで continue を使わないこと。** avr-gcc 7.3 は constexpr 評価中の
// continue で増分式を飛ばし、無限ループになってビルドが落ちる。条件は if の入れ子で書く。
static constexpr bool slotPinsAreSane() {
    for (uint8_t b = 0; b < kServoBoardCount; ++b) {
        for (uint8_t i = 0; i < kServoSlotCount; ++i) {
            for (uint8_t f = 0; f < kFixedPinCount; ++f) {
                if (kServoBoards[b].slots[i].pin == kFixedPins[f]) {
                    return false;
                }
            }
            for (uint8_t j = static_cast<uint8_t>(i + 1); j < kServoSlotCount; ++j) {
                if (kServoBoards[b].slots[i].pin == kServoBoards[b].slots[j].pin) {
                    return false;
                }
            }
        }
    }
    return true;
}
static_assert(slotPinsAreSane(),
              "config.h のスロットのピンが UART/SPI/CAN/RGB/DIP と衝突しているか、重複している"
              "（pin が 0 なら kServoBoards の要素の書き忘れでゼロ埋めされている）");

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
static_assert(fixedPinsAreUnique(), "config.h の固定ピン（UART/SPI/CAN/RGB/DIP）が重複している");

#if defined(ARDUINO_ARCH_RENESAS)
// R4 の CAN は D4(TX) / D5(RX) 固定。**config.h に写しを置かない**ため、正である
// variant のマクロをここで直接見る（DC 用 main.cpp の pinsAvoidCan() と同じ理由）。
// CAN 線を他用途に奪われた基板は PC から停止できなくなる。
static constexpr bool pinsAvoidCan() {
    for (uint8_t i = 0; i < kFixedPinCount; ++i) {
        if (kFixedPins[i] == PIN_CAN0_TX || kFixedPins[i] == PIN_CAN0_RX) {
            return false;
        }
    }
    for (uint8_t b = 0; b < kServoBoardCount; ++b) {
        for (uint8_t i = 0; i < kServoSlotCount; ++i) {
            if (kServoBoards[b].slots[i].pin == PIN_CAN0_TX ||
                kServoBoards[b].slots[i].pin == PIN_CAN0_RX) {
                return false;
            }
        }
    }
    return true;
}
static_assert(pinsAvoidCan(), "config.h のピンが CAN(D4/D5) と衝突している");
#endif

// makeDeviceId が「基板種別 | 基板番号 | スロット番号」で組み立てるので（§2.2）、
// スロット間の重複も帯からのはみ出しも構造的に起こらない —— 基準 ID の表も、重複・
// 連続ブロック性・帯の static_assert も要らない。

static_assert(kServoSlotCount <= motorcan::kMaxSlotNumber + 1,
              "スロット数がデバイス ID のスロット番号（3bit）に収まらない");

// 宛先判定の結果はスロットのビットマスク（uint8_t）で返ってくる。
static_assert(kServoSlotCount <= motorcan::kMaxChannels,
              "スロット数が FrameRoute::channelMask のビット数を超えている");

static_assert(kDipBitCount == sizeof(kPinDip) / sizeof(kPinDip[0]),
              "kDipBitCount と kPinDip の要素数が一致していない");

// 行を足して kServoBoardCount を上げ忘れると、その基板は「表に無い番号」として全スロットが
// 未設定へ倒れる —— 症状は「新しい基板だけ何も動かず LED が赤く点滅する」だけで、
// 原因が config.h の 1 行の数字だと気付くまでに時間を食う。
static_assert(sizeof(kServoBoards) / sizeof(kServoBoards[0]) == kServoBoardCount,
              "kServoBoards の行数と kServoBoardCount が一致していない");

// **基板番号が重複していると、線形探索で先に見つかった行が黙って勝つ。**
// 基板番号は行の添字ではなく値で持つので、重複はここでしか止められない。症状は
// 「DIP を合わせたのに別の基板の役割で動く」で、しかも 2 枚のうち片方でしか出ない。
static constexpr bool boardNumbersAreUnique() {
    for (uint8_t i = 0; i < kServoBoardCount; ++i) {
        for (uint8_t j = static_cast<uint8_t>(i + 1); j < kServoBoardCount; ++j) {
            if (kServoBoards[i].boardNumber == kServoBoards[j].boardNumber) {
                return false;
            }
        }
    }
    return true;
}
static_assert(boardNumbersAreUnique(), "kServoBoards の boardNumber が重複している");

// 基板番号はデバイス ID の bit5-3（仕様書 §2.2）なので 0-7 しか表せない。
// **はみ出した行は DIP をどう回しても選ばれない** —— readDipSwitch は 4bit を読むが
// makeDeviceId が 8 以上を未設定へ倒すので、その基板は永久に「全スロット未設定」になる。
// 書いた本人には「表に足したのに動かない」としか見えない。
static constexpr bool boardNumbersFitInDeviceId() {
    for (uint8_t i = 0; i < kServoBoardCount; ++i) {
        if (kServoBoards[i].boardNumber > motorcan::kMaxBoardNumber) {
            return false;
        }
    }
    return true;
}
static_assert(boardNumbersFitInDeviceId(),
              "kServoBoards の boardNumber がデバイス ID の基板番号（3bit）に収まらない");

// ===========================================================================
// ペリフェラルと状態（すべてスロット単位）
// ===========================================================================

// CAN が上がらなかった基板は PC から止められない。LED でそれと分かるようにする。
static bool g_canFailed = false;

// 送信の連続失敗数。数える規則は TxFailCounter が持つ（native テスト圏内）。
// **servo_can::send() の戻り値を捨ててはならない** —— 捨てると「誰にも何も届かない基板」が
// 平常時と同じ青のハートビートを出し続ける。失敗の出方は MCU で違う（MCP2515 は 1 通あたり
// 最大 5ms 待たされてから失敗、R4 は mailbox が空いていなければ即失敗）が、数え方は同じ。
static TxFailCounter g_txFail;

static Servo g_servo[kServoSlotCount];

// 仕様書 §5.4: 起動時の緊急停止ラッチは解除済み。§7.1 のとおり宛先がスロットなので
// ウォッチドッグも補間もスロットごとに独立して動く。
//
// **初期角と可動範囲は setup() の begin() で入れる。** 静的初期化子では書けない ——
// どちらも config.h の kServoBoards から取るが、行を選ぶ DIP の値は実行時にしか
// 読めない。begin() 前は既定値（幅 0 の可動範囲・出力禁止）なので、Unused のスロットが
// begin() されないまま残っても駆動側へ倒れない。
static ServoChannel g_channel[kServoSlotCount];

// DIP の基板番号を反映した実効デバイス ID。
// **Unused のスロットだけ 0x00 にする。** そうしておくと routeFrame が自分宛と
// 判定しないので、そのスロット宛のフレームで何かが動く経路が構造的に無くなる。
// センサは自分の ID で FEEDBACK を送るので 0x00 にしてはならない。
static uint8_t g_deviceId[kServoSlotCount] = {};

// Servo::attach() を通したかどうか。ID 未設定スロットは attach すらしない。
static bool g_attached[kServoSlotCount] = {};

// センサの現在値。スロットごとに独立で、そのスロット自身の FEEDBACK で報告する。
static bool g_sensorActive[kServoSlotCount] = {};

// 送信周期は基板全体で 1 つ（スロットごとに変えると送信位相の分散が崩れる）。
static uint16_t g_feedbackIntervalMs = kDefaultFeedbackIntervalMs;
static PeriodicTimer g_feedbackTimer[kServoSlotCount];

static PeriodicTimer g_motionTimer;
static PeriodicTimer g_infoTimer;

// INFO は 1Hz で全スロット分を送る。**共通の規則は MotorTxHealth.h**（1 反復 1 通・
// 空きを待たない・戻り値を捨てない）。この基板が同時に満たすべき事情は MCU で 2 つある:
//
//   基板 #0/#1（Nano / MCP2515）: TX 3 本だが sendMsgBuf は空きと TXREQ のクリアを
//     TIMEOUTVALUE(2500us) まで待つ。4〜5 通を連続送信すると最大 20〜25ms loop() が
//     止まり、その間 **RXB0/RXB1 の 2 段しかない受信バッファが溢れる** ——
//     落ちたのがブロードキャスト E_STOP なら「たまに緊急停止が効かない基板」になる。
//   基板 #2（UNO R4 Minima）: Arduino_CAN は標準 ID の mailbox を 1 本しか使わない
//     （R7FA4M1_CAN.cpp の write が CAN_MAILBOX_ID_0 固定）ので 2 通目以降が必ず落ち、
//     kInfoIntervalMs(1000) が feedback_interval_ms(10) の整数倍で位相が固定される
//     ため**毎回同じスロットだけが出て残り 4 本は永久に 1 通も出ない**。
//
// PC から見える振る舞い（1Hz・内容・CAN ID）は変わらないので **kFirmwareVersion は
// 上げない**（§3.4 の版番号は「CAN 上の振る舞いが変わったか」だけを指す）。
static uint8_t g_infoPendingSlot = kServoSlotCount;  // kServoSlotCount = 送信待ちなし

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
// スロット設定（実行時に選ばれた 1 行）
// ===========================================================================

// DIP の基板番号で config.h の kServoBoards から 1 行を選ぶので、実行時にしか確定しない。
// **既定の nullptr が「表に無い基板番号」をそのまま表す。** 判定を別のフラグに持たせると、
// ポインタとフラグが食い違う状態が作れてしまう（役割だけ Unused でピンは基板 #0 の値、
// のような中間状態）。ピン・パルス仕様・可動範囲を読む側はすべて下の 3 つを先に通すので、
// 行が選ばれていない基板では 1 本も pinMode されず 1 通も送らない。
static const ServoSlotConfig *g_slots = nullptr;

static SlotRole slotRole(uint8_t slot) {
    return g_slots == nullptr ? SlotRole::Unused : g_slots[slot].role;
}

static bool isServoSlot(uint8_t slot) { return slotRole(slot) == SlotRole::Servo; }
static bool isSensorSlot(uint8_t slot) { return slotRole(slot) == SlotRole::TouchSensor; }

// Unused 以外はすべて CAN デバイスとして FEEDBACK を送る。
static bool isDeviceSlot(uint8_t slot) { return slotRole(slot) != SlotRole::Unused; }

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
        const int level = digitalRead(g_slots[slot].pin);
        g_sensorActive[slot] =
            g_slots[slot].sensorActiveLow ? (level == LOW) : (level == HIGH);
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
        g_servo[slot].attach(g_slots[slot].pin, g_slots[slot].pulse.minUs,
                             g_slots[slot].pulse.maxUs);
    }

    // ここに来る角度は ServoMotion が angle_min / angle_max でクランプ済み（仕様書 §7.2）。
    // angleToPulseUs 側でもパルス幅を [minUs, maxUs] に収める二重の防壁になっている。
    const uint16_t pulseUs =
        angleToPulseUs(g_channel[slot].currentAngleDeg(), g_slots[slot].pulse);
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

// 状態フラグの組み立て規則は composeFeedbackFlags が持つ（規則と理由はその宣言。
// native テスト圏内）。**ここで OR を足してはならない** —— 「センサスロットは緊急停止・
// ウォッチドッグ・到達を立てない」（§5.2）は SlotKind::Sensor から導かれるので、
// ここを唯一の実装にすると規則を消しても native テストが 1 件も落ちない。
static uint8_t buildStatusFlags(uint8_t slot, uint32_t nowMs) {
    const SlotKind kind = isSensorSlot(slot) ? SlotKind::Sensor : SlotKind::Actuator;
    return composeFeedbackFlags(kBoardKind, kind, g_channel[slot].safetyStatusFlags(nowMs),
                                isSlotConfigured(slot), g_channel[slot].isReached(),
                                g_sensorActive[slot]);
}

// CAN 送信 1 通ぶんの結果を記録する。**戻り値を捨てないための唯一の口**にしてあるので、
// servo_can::send() を直に呼ぶ経路を作らないこと。
// **送れたかを呼び出し側へも返す** —— INFO は落ちると 1 秒欠けるので送り直しが要る。
static bool sendFrame(uint16_t canId, uint8_t length, const uint8_t *data) {
    if (servo_can::send(canId, length, data)) {
        g_txFail.onSuccess();
        return true;
    }
    g_txFail.onFailure();
    return false;
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
// **送れたかを返す**（DC 用・電磁弁用と同じ）。落ちた slot は添字を進めず、
// 次の反復で送り直すために呼び出し側が結果を見る。
static bool sendInfo(uint8_t slot) {
    uint8_t data[kInfoWithServoRangeLength];
    // **可動レンジを足すのはサーボスロットだけ**（仕様書 §3.4）。センサスロットは
    // 角度そのものを持たないので、載せると PC 側に「測ったように見える 0」が届く。
    // この申告だけが、config.h の ServoPulseSpec と実物の型（180/270）の食い違いを
    // CAN 越しに見える形にしている（仕様書 §7.7）。
    if (isServoSlot(slot)) {
        const uint8_t len = encodeInfo(data, kFirmwareVersion, kBoardKind, SlotKind::Actuator,
                                       g_slots[slot].pulse.angleRangeDeg);
        return sendFrame(buildCanId(CommandType::Info, g_deviceId[slot]), len, data);
    }

    const SlotKind kind = isSensorSlot(slot) ? SlotKind::Sensor : SlotKind::Actuator;
    const uint8_t len = encodeInfo(data, kFirmwareVersion, kBoardKind, kind);
    return sendFrame(buildCanId(CommandType::Info, g_deviceId[slot]), len, data);
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
            // 仕様書 §6: 緊急停止ラッチ中でも、制御タイプが position でなくてもウォッチ
            // ドッグは養う（通信自体は生きているので、養わないと解除直後に満了済みで
            // 動かない）。**受理判定より必ず先に呼ぶこと** —— 起動直後は §5.4 により
            // 未受信＝出力禁止なので、順序を逆にすると最初の 1 通を捨てる。
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
            // 他基板がモタドラ → PC 方向へ送るフレーム。Nano 版は MCP2515 の受信フィルタで
            // 落としているが、**R4 版にはフィルタが無いので実際にここまで届く**
            // （can_r4.cpp 冒頭）。routeFrame より手前で捨てられる保証は無いものとして、
            // 明示的に無視する。
            break;
    }
}

// servo_can::poll() が 1 通ごとに呼ぶ。**拡張フレーム / RTR の解釈はバックエンドが
// 済ませている**ので（MCP2515 は bit31/bit30、R4 は CanMsg::isStandardId）、ここは
// MCU に依らず「標準 ID か」の真偽だけを受け取る。
static void handleFrame(uint16_t canId, bool standard, const uint8_t *data, uint8_t len) {
    // Standard Frame 判定・予約コマンド種別・宛先判定（スロット表との突き合わせ /
    // ブロードキャスト E_STOP は全スロット / ID 未設定スロットに自分宛は無い）は
    // DC 用と同じ規則なので MotorCanRouter に集約してある。
    const FrameRoute route = routeFrame(canId, standard, g_deviceId, kServoSlotCount);
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

// ===========================================================================
// スロット設定とデバイス ID（どちらも DIP の基板番号で決まる）
// ===========================================================================

// デバイス ID は「基板種別 2bit | 基板番号 3bit | スロット番号 3bit」（§2.2）。DIP の
// 基板番号は bit5-3 に入るので **1 枚あたりの刻み幅はスロット数（5）ではなく 8** ——
// 基板 #1 の SV0 は 0x45 ではなく 0x48 である。組み立てと、3bit からはみ出した番号を
// 未設定へ倒す扱いは MotorCanRouter / makeDeviceId が持つ（native テスト圏内）。
//
// 同じ DIP が**スロット設定の行**も選ぶ（config.h の kServoBoards）。**行の添字ではなく
// boardNumber の一致で探す** —— R4 ビルドが持つ唯一の行の boardNumber は 2 なので、
// 添字で引くと「DIP=0 で基板 #2 の役割が動く」ことになる。表に無い番号では g_slots を
// nullptr のまま据え置き、全スロットを Unused として扱う（既存の「デバイス ID 未設定 →
// LED 赤の速い点滅・駆動拒否」へそのまま乗る）。
//
// **Unused のスロットだけ 0x00 のままにする。** センサは自分のデバイス ID で
// FEEDBACK を送るので ID を持つ（持たせないと「センサだけの基板」が何も報告できない）。
static void resolveSlotsAndDeviceIds() {
    const uint8_t boardNumber = readDipSwitch(
        kPinDip, kDipBitCount, [](uint8_t pin) { return static_cast<int>(digitalRead(pin)); },
        LOW);
    for (uint8_t b = 0; b < kServoBoardCount; ++b) {
        if (kServoBoards[b].boardNumber == boardNumber) {
            g_slots = kServoBoards[b].slots;
            break;
        }
    }
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
    BoardIndication indication(g_canFailed || g_txFail.isAlarming(kCanTxFailStreakAlarm));
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
    // 赤（速い点滅）= CAN 不通 / 送信失敗 / ID 未設定、橙 = 緊急停止ラッチ中、青 = 平常。
    // **平常に緑を使ってはならない（緑のランプの使用が禁止されている）。**
    // 橙は緑ダイを 96 で点けるが発色はオレンジなので、緑のランプには当たらない。
    uint8_t r = 0;
    uint8_t g = 0;
    uint8_t b = 0;
    if (indication.urgent()) {
        r = g_ledOn ? 255 : 0;
    } else if (indication.stopped()) {
        r = 255;
        g = 96;
    } else {
        b = g_ledOn ? 255 : 32;
    }

    // **AVR 版 Adafruit_NeoPixel::show() は 1 LED あたり約 30us 割り込みを禁止する。**
    // その窓に Servo ライブラリの Timer1 割り込み（パルス終端）が当たると **そのパルス
    // だけが最大 30us 伸びる** —— kServoPulse270 は 7.04us/deg なので約 4.3deg のヒゲに
    // なる（grip 5deg / 壁 6deg の微小ストロークではほぼ全域）。次のフレーム（20ms 後）で
    // 戻るので影響は一瞬だが、**呼ぶ回数を増やしてはならない**（毎ループの位置へ動かすと
    // 当たる確率が比例して上がる）。
    // **キャッシュは 3 成分すべてを持つこと。** ここが show() を呼ぶ唯一の条件なので、
    // 平常色が載っている青を外すと「色を計算しているのに一度も反映されない LED」になる。
    static uint8_t lastR = 0xFF;
    static uint8_t lastG = 0xFF;
    static uint8_t lastB = 0xFF;
    if (r != lastR || g != lastG || b != lastB) {
        lastR = r;
        lastG = g;
        lastB = b;
        g_strip.setPixelColor(0, g_strip.Color(r, g, b));
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

    for (uint8_t bit = 0; bit < kDipBitCount; ++bit) {
        pinMode(kPinDip[bit], INPUT_PULLUP);
    }

#if HAS_RGB_LED
    g_strip.begin();
    g_strip.setBrightness(kRgbBrightness);
#endif

    // attach する前にスロット設定と実効デバイス ID を確定させる。
    // ID 未設定のスロットには Servo::attach() すら通さず、パルスを 1 発も出さない
    // （仕様書 §2.2: 設定ミスで意図しないアクチュエータが動くより動かない方が安全）。
    // **DIP の読み取りより前には置けない**（役割もピンも基板番号で決まるため）。
    resolveSlotsAndDeviceIds();

    // 初期角と可動範囲を入れる。**サーボスロットだけ**を begin() する ——
    // Unused とセンサは駆動しないので、既定のまま（幅 0 の可動範囲・出力禁止）に
    // しておけば、そのスロット宛に何かが届いても 1 発もパルスが出ない。
    // **setWatchdogEnabled より先**（begin は安全機構ごと作り直すので、後から呼ぶと
    // config.h の WATCHDOG_ENABLED が既定の「有効」で上書きされる）。
    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        if (isServoSlot(slot)) {
            g_channel[slot].begin(g_slots[slot].initialAngleDeg, g_slots[slot].limits,
                                  kDefaultCommandTimeoutMs);
        }
    }

    // センサは attach より先に入力へ倒す。出力のまま放置すると、サーボ用の
    // ピンとして駆動されてセンサ回路を叩く。
    for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
        if (isSensorSlot(slot)) {
            pinMode(g_slots[slot].pin, INPUT_PULLUP);
        }
    }

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
        g_servo[slot].attach(g_slots[slot].pin, g_slots[slot].pulse.minUs,
                             g_slots[slot].pulse.maxUs);
        g_attached[slot] = g_servo[slot].attached();
        if (g_attached[slot]) {
            g_servo[slot].writeMicroseconds(static_cast<int>(
                angleToPulseUs(g_channel[slot].currentAngleDeg(), g_slots[slot].pulse)));
        }
    }

    // 仕様書 §1: 1 Mbps。ビットレート・フィルタ・ピンの面倒は servo_can が MCU ごとに見る。
    // CAN が上がらない基板を駆動させると PC から止められないので、
    // 失敗したら緊急停止ラッチに落として新しい角度指令を受け付けなくする
    // （§7.5 のとおり出力は切らず、初期角を保持したままになる）。
    if (!servo_can::begin()) {
        g_canFailed = true;
        for (uint8_t slot = 0; slot < kServoSlotCount; ++slot) {
            g_channel[slot].stop(startMs);
        }
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

    servo_can::poll(handleFrame);
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
    // **1 反復 1 通**（理由は g_infoPendingSlot の宣言に付けてある）。
    if (g_infoTimer.due(nowMs, kInfoIntervalMs)) {
        g_infoPendingSlot = 0;
    }
    if (g_infoPendingSlot < kServoSlotCount) {
        // **ID を名乗れないスロットは 1 通も送らない**（仕様書 §2.2）。飛ばして次へ進む。
        if (!isSlotConfigured(g_infoPendingSlot)) {
            ++g_infoPendingSlot;
        } else if (sendInfo(g_infoPendingSlot)) {
            ++g_infoPendingSlot;
        }
        // 送信に失敗した slot はインデックスを進めず、次の反復で送り直す。
    }

    updateLed(nowMs);
}
