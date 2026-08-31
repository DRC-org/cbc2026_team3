// DC モータ用自作モタドラのファームウェア本体（Arduino UNO R4 / RA4M1）。
//
// プロトコルの単一情報源は docs/motor_driver_can_protocol.md。
// 機体依存の定数はすべて include/config.h にある。
//
// 責務の分割:
//   MotorCan（Arduino 非依存）… フレームの符号化・復号、宛先判定、緊急停止ラッチ、
//                                ウォッチドッグ、**両者の結線**（DcChannel）、
//                                duty の分解、周期タイマ、シリアル行組み立て
//   このファイル              … ペリフェラル初期化、チャンネル管理、CAN 送受信の配線
//
// 「出力禁止中は duty 指令を受け付けない」（仕様書 §3.5 / §5.2）は DcChannel が持つ。
// ここで MotorSafety を直に触ると、その規則を迂回する経路（＝緊急停止中に回るモータ）が
// 書けてしまう。
//
// サーボ用（firmware/servo）と同じ判断をする箇所は MotorCan 側に置くこと。
// 両 main.cpp が同じ分岐を各自で持つと、片方だけ直したことに誰も気付けない。
//
// この基板の性質（仕様書 §4 / §8）:
//   - 1 枚が 3 チャンネルを持ち、チャンネルごとに独立したデバイス ID を持つ
//   - **フィードバックを一切持たない。** エンコーダ・電流センス・温度センサとも非搭載で、
//     FEEDBACK の位置・速度はすべて 0、到達フラグも立てない
//   - duty モードのみ受理する（位置・速度制御は実装ごと存在しない）
//   - ゲートドライバの出力禁止（DIS）が無く、止める手段は PWM 0% だけ。その代わり
//     物理緊急停止スイッチの状態を REF ピンで読める

#include <Arduino.h>
#include <Arduino_CAN.h>
#include <stdlib.h>

#include "DcChannel.h"
#include "MotorCanProtocol.h"
#include "MotorCanRouter.h"
#include "MotorLoopTimer.h"
#include "SerialLineBuffer.h"
#include "config.h"
#include "pwm.h"

#if HAS_RGB_LED
#include <Adafruit_NeoPixel.h>
#endif

using namespace motorcan;

// ===========================================================================
// 配線の静的検証
// ===========================================================================

// 使うピンをすべて 1 つの表にまとめてから検証する。以前は CAN ピンとの衝突しか
// 見ておらず、**config.h の想定と実基板の配線がまるごと食い違っていてもビルドが通った**
// （DIS として LOW/HIGH を振っていた D7 が、実機では ch2 の方向ピンだった）。
static constexpr uint8_t kAllPins[] = {
    kPinPwm[0], kPinPwm[1], kPinPwm[2], kPinDir[0], kPinDir[1], kPinDir[2],
    kPinRef,    kPinLed,    kPinRgb,    kPinDip[0], kPinDip[1],
};
static constexpr uint8_t kAllPinCount = sizeof(kAllPins) / sizeof(kAllPins[0]);

// CAN 線を他用途に奪われた基板は PC から停止できなくなる。
static constexpr bool pinsAvoidCan() {
    for (uint8_t i = 0; i < kAllPinCount; ++i) {
        if (kAllPins[i] == PIN_CAN0_TX || kAllPins[i] == PIN_CAN0_RX) {
            return false;
        }
    }
    return true;
}
static_assert(pinsAvoidCan(), "config.h のピンが CAN(D4/D5) と衝突している");

// 同じピンを 2 つの役割に割り当てると、入力として読みたいピンを出力が駆動する
// （REF が方向ピンに潰されれば、押しても止まらない基板になる）。
static constexpr bool pinsAreUnique() {
    for (uint8_t i = 0; i < kAllPinCount; ++i) {
        for (uint8_t j = static_cast<uint8_t>(i + 1); j < kAllPinCount; ++j) {
            if (kAllPins[i] == kAllPins[j]) {
                return false;
            }
        }
    }
    return true;
}
static_assert(pinsAreUnique(), "config.h のピンが重複している");

// デバイス ID は makeDeviceId が「基板種別 | 基板番号 | スロット番号」で組み立てるので、
// チャンネル間の重複も帯からのはみ出しも構造的に起こらない（仕様書 §2.2）。
// かつては基準 ID の表を持ち、重複・連続ブロック性・帯の 3 つを static_assert で
// 見張っていたが、ビット分割にしたことで規則ごと消えた。

// 下の g_pwm / g_channel は各チャンネルを明示的に初期化している。
// チャンネルを増やすときはそれらの初期化子も一緒に足すこと。
static_assert(kDcChannelCount == 3,
              "チャンネル数を変えたら g_pwm / g_channel の初期化子も更新すること");
static_assert(kDcChannelCount <= motorcan::kMaxSlotNumber + 1,
              "チャンネル数がデバイス ID のスロット番号（3bit）に収まらない");

// 宛先判定の結果はチャンネルのビットマスク（uint8_t）で返ってくる。
static_assert(kDcChannelCount <= motorcan::kMaxChannels,
              "チャンネル数が FrameRoute::channelMask のビット数を超えている");

// DIP のビット数と kPinDip の要素数がずれると、読まないピンが出るか配列外を読む。
static_assert(kDipBitCount == sizeof(kPinDip) / sizeof(kPinDip[0]),
              "kDipBitCount と kPinDip の要素数が一致していない");

// ===========================================================================
// ペリフェラルと状態（すべてチャンネル単位）
// ===========================================================================

static PwmOut g_pwm[kDcChannelCount] = {
    PwmOut(kDcChannels[0].pwmPin),
    PwmOut(kDcChannels[1].pwmPin),
    PwmOut(kDcChannels[2].pwmPin),
};

// 仕様書 §5.4: 起動時は目標 0・出力停止・緊急停止ラッチは解除済み。
// 宛先がチャンネルなので、ウォッチドッグもチャンネルごとに独立して動く
// （1 チャンネルへの指令が途絶えても他は動き続ける）。
static DcChannel g_channel[kDcChannelCount] = {
    DcChannel(kDefaultCommandTimeoutMs),
    DcChannel(kDefaultCommandTimeoutMs),
    DcChannel(kDefaultCommandTimeoutMs),
};

// DIP ブロックオフセット適用後の実効デバイス ID。0x00 なら駆動しない（§2.2）。
static uint8_t g_deviceId[kDcChannelCount] = {0, 0, 0};

// PwmOut::begin() を通したかどうか。ID 未設定チャンネルは begin() すらしない。
static bool g_pwmStarted[kDcChannelCount] = {false, false, false};

// SET_PARAM 0x00（max_duty）はチャンネル（＝デバイス）ごとの値。
static float g_maxDuty[kDcChannelCount] = {
    kDcChannels[0].maxDuty,
    kDcChannels[1].maxDuty,
    kDcChannels[2].maxDuty,
};

static uint32_t g_feedbackIntervalMs = kDefaultFeedbackIntervalMs;
static PeriodicTimer g_feedbackTimer[kDcChannelCount];

static PeriodicTimer g_infoTimer;
static PeriodicTimer g_blinkTimer;
static bool g_ledOn = false;

// CAN が上がらなかった基板は PC から止められない。LED でそれと分かるようにする。
static bool g_canFailed = false;

#if HAS_RGB_LED
static Adafruit_NeoPixel g_strip(1, kPinRgb, NEO_GRB + NEO_KHZ800);
#endif

#if ENABLE_SERIAL_DEBUG
// シリアルから duty を入力している間だけ true。
// CAN の SET_TARGET を受けたら解除して、PC の指令とシリアルが競合しないようにする。
static bool g_serialOverride = false;
static char g_serialStorage[24];
static SerialLineBuffer g_serialLine(g_serialStorage, sizeof(g_serialStorage));
#endif

// ===========================================================================
// 入力
// ===========================================================================

// 物理緊急停止スイッチが押されているか。INPUT_PULLUP で読むので、
// kRefActiveLow が真なら断線時も「押されている」側へ倒れる。
static bool isPhysicalStopPressed() {
    const int level = digitalRead(kPinRef);
    return kRefActiveLow ? (level == LOW) : (level == HIGH);
}

// ===========================================================================
// 出力
// ===========================================================================

// 仕様書 §2.2: オフセット適用後のデバイス ID が 0x00 のチャンネルは駆動しない。
static bool isChannelConfigured(uint8_t ch) {
    return g_deviceId[ch] != kDeviceIdUnconfigured;
}

// モータへの出力はすべてこの関数を通す。ID 未設定・duty 上限・安全機構の
// いずれかを迂回する経路を作らないため。
//
// duty を「大きさ」と「向き」に分ける規則は splitDuty が持つ。ここで符号を見て
// 分岐すると、duty 0 のときに方向ピンが反転する実装を書き直せてしまう
// （停止指令は毎ループ流れるので、機構に絶えず衝撃が入る）。
static void applyChannelOutput(uint8_t ch, uint32_t nowMs) {
    if (!isChannelConfigured(ch) || !g_pwmStarted[ch]) {
        // 設定ミスで意図しないアクチュエータが動くより、動かない方が安全。
        // ID 未設定チャンネルは PwmOut::begin() を通していないのでパルスは 1 発も出ない。
        // begin() が失敗したチャンネル（PWM チャンネルの取り合い等）も同様に触らない。
        return;
    }

    // outputDuty() は出力禁止中に 0 を返す。この基板には出力禁止ピン（DIS）が無く、
    // PWM を 0% にすることだけが止める手段なので、ここを通さない経路を作らないこと。
    const DutyOutput out = splitDuty(g_channel[ch].outputDuty(nowMs), g_maxDuty[ch]);
    digitalWrite(kDcChannels[ch].dirPin, (out.reverse != kDirForwardIsLow) ? LOW : HIGH);
    g_pwm[ch].pulse_perc(out.magnitude * 100.0f);
}

// ===========================================================================
// CAN
// ===========================================================================

// 状態フラグの組み立て規則そのものは composeFeedbackFlags が持つ（native テスト圏内）。
// **ここで OR を足してはならない。** かつて 3 枚がそれぞれフラグを組み立てており、
// この基板に `flags |= kReached;` を 1 行足しても native テストは 1 件も落ちなかった。
// 到達フラグを立てないこと（仕様書 §3.2 / §8: 観測手段が 1 つも無い）は
// board == Dc から導かれるので、この呼び出しが規則の全てになる。
static uint8_t buildStatusFlags(uint8_t ch, uint32_t nowMs) {
    return composeFeedbackFlags(kBoardKind, SlotKind::Actuator,
                                g_channel[ch].safetyStatusFlags(nowMs), isChannelConfigured(ch),
                                /*reached=*/false, /*sensorActive=*/false);
}

static void sendFeedback(uint8_t ch, uint32_t nowMs) {
    // **この基板は位置を持たないので状態フラグ 1 バイトだけ**（仕様書 §3.2）。
    // 位置・速度に常に 0 を詰めても、PC には「測ったように見える 0」が届くだけ。
    uint8_t data[kFeedbackFlagsOnlyLength];
    const uint8_t len = encodeFeedback(data, buildStatusFlags(ch, nowMs));

    // 緊急停止中・ウォッチドッグ作動中も送り続ける。
    // 止めると PC 側が STALE になり、なぜ動かないのかを操縦者が判別できなくなる。
    // ID 未設定チャンネルは CAN ID 0x100 で送ることになり、複数チャンネルが未設定だと
    // 同じ ID のフレームが重複するが、PC 側へ「デバイス ID 未設定」を届ける方を優先する。
    const CanMsg msg(CanStandardId(buildCanId(CommandType::Feedback, g_deviceId[ch])), len,
                     data);
    CAN.write(msg);
}

// 仕様書 §3.4: 焼き忘れた基板をセッティングタイムに見つけるための自己申告。
// 低頻度（1Hz）で送るので、PC が後から起動しても拾える。
static void sendInfo(uint8_t ch) {
    uint8_t data[kInfoBaseLength];
    const uint8_t len = encodeInfo(data, kFirmwareVersion, kBoardKind, SlotKind::Actuator);
    const CanMsg msg(CanStandardId(buildCanId(CommandType::Info, g_deviceId[ch])), len, data);
    CAN.write(msg);
}

static void applyParam(uint8_t ch, const SetParamCommand &cmd) {
    switch (cmd.id) {
        case ParamId::MaxDuty:
            g_maxDuty[ch] = clampDuty(fromRaw(cmd.raw, kDutyScale), 1.0f);
            break;
        case ParamId::CommandTimeoutMs:
            // 猶予に上限が無いと、仕様書 §5.1 が守っている最後の砦が
            // SET_PARAM 1 フレームで実質外れる（49.7 日の猶予 = 無効化）。
            // 範囲の根拠と NaN の扱いは MotorCanProtocol が持つ。
            g_channel[ch].setCommandTimeoutMs(clampCommandTimeoutMs(cmd.raw));
            break;
        case ParamId::FeedbackIntervalMs:
            // 周期は基板全体で 1 つ（チャンネルごとに変えると位相の分散が崩れる）。
            g_feedbackIntervalMs = clampFeedbackIntervalMs(cmd.raw);
            break;
        case ParamId::ReachedTolerance:
        case ParamId::SlewRate:
        case ParamId::AngleMin:
        case ParamId::AngleMax:
            // 仕様書 §3.3: サーボ固有のパラメータ。この基板は持たないので無視する。
            // 受け付けて内部に持つと、PC 側から「設定できたのに効かない値」に見える。
            break;
    }
}

static void handleChannelFrame(uint8_t ch, CommandType command, const CanMsg &msg,
                               uint32_t nowMs) {
    switch (command) {
        case CommandType::SetTarget: {
            // 仕様書 §6: 緊急停止ラッチ中でもウォッチドッグは養う。
            // 養わないと解除した直後に満了済みで動かない。
            // 制御タイプが duty でなくても養うのは、通信自体は生きているため。
            // 受理判定（DcChannel::setDuty）より必ず先に呼ぶこと。起動直後は
            // §5.4 により未受信＝出力禁止なので、順序を逆にすると最初の 1 通を捨てる。
            g_channel[ch].feed(nowMs);

            const SetTargetCommand cmd = decodeSetTarget(msg.data, msg.data_length);
            if (!cmd.valid) {
                return;
            }
            // 仕様書 §4: この基板はフィードバックを持たないので duty のみ受理する。
            // position の 90.0[deg] を duty として解釈すると 9000% の全力指令になるため、
            // position / velocity は黙って捨てる。
            if (cmd.type != ControlType::Duty) {
                return;
            }
#if ENABLE_SERIAL_DEBUG
            g_serialOverride = false;
#endif
            // setDuty は緊急停止ラッチ中・ウォッチドッグ満了中は受け付けない。
            g_channel[ch].setDuty(fromRaw(cmd.raw, kDutyScale), nowMs);
            break;
        }
        case CommandType::SetParam: {
            const SetParamCommand cmd = decodeSetParam(msg.data, msg.data_length);
            if (cmd.valid) {
                // 未知のパラメータ ID は decodeSetParam が弾く（仕様書 §3.3）。
                applyParam(ch, cmd);
            }
            break;
        }
        case CommandType::EStop: {
            const EStopAction action = g_channel[ch].handleEStopFrame(msg.data, msg.data_length);
            if (action != EStopAction::None) {
                // 停止はその場で PWM へ反映する。次のループを待つと、その間だけ
                // 回り続ける（loop() の周期は保証されていない）。
                applyChannelOutput(ch, nowMs);
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

static void handleFrame(const CanMsg &msg) {
    // Standard Frame 判定・予約コマンド種別・宛先判定（チャンネル表との突き合わせ /
    // ブロードキャスト E_STOP は全チャンネル / ID 未設定チャンネルに自分宛は無い）は
    // サーボ用と同じ規則なので MotorCanRouter に集約してある。
    const FrameRoute route = routeFrame(static_cast<uint16_t>(msg.getStandardId()),
                                        msg.isStandardId(), g_deviceId, kDcChannelCount);
    if (!route.accepted) {
        return;
    }

    const uint32_t nowMs = millis();
    for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
        if ((route.channelMask & static_cast<uint8_t>(1u << ch)) != 0) {
            handleChannelFrame(ch, route.command, msg, nowMs);
        }
    }
}

static void pollCan() {
    while (CAN.available()) {
        const CanMsg msg = CAN.read();
        handleFrame(msg);
    }
}

// ===========================================================================
// デバイス ID（DIP スイッチ = チャンネル表全体へのブロックオフセット）
// ===========================================================================

// 1 枚がチャンネルごとに別のデバイス ID を持つため、DIP は
// **チャンネル表全体に加えるブロックオフセット**として働く。同一ファームの基板を
// 複数枚使うとき、2 枚目の DIP を 1 段上げるだけで全チャンネルの ID がまとめて
// 次のブロックへ移る。刻み幅がチャンネル数でないとブロックが重なる理由、
// 負論理とビット順の対応、はみ出しの扱いは MotorCanRouter が持つ
// （native テストで守られている）。
static void resolveDeviceIds() {
    const uint8_t boardNumber = readDipSwitch(
        kPinDip, kDipBitCount, [](uint8_t pin) { return static_cast<int>(digitalRead(pin)); },
        LOW);
    for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
        g_deviceId[ch] = makeDeviceId(kBoardKind, boardNumber, ch);
    }
}

// ===========================================================================
// LED
// ===========================================================================

static void updateLed(uint32_t nowMs) {
    bool unconfigured = false;
    bool stopped = false;
    for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
        if (!isChannelConfigured(ch)) {
            unconfigured = true;
        }
        if ((g_channel[ch].safetyStatusFlags(nowMs) & status_flag::kEStop) != 0) {
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
    digitalWrite(kPinLed, g_ledOn ? HIGH : LOW);

#if HAS_RGB_LED
    // 赤（速い点滅）= CAN 不通 / ID 未設定、橙 = 緊急停止ラッチ中、緑 = 平常。
    // 緊急停止だけ消灯を挟まないのは、「止まっている」ことを見落とさせないため。
    uint8_t r = 0;
    uint8_t g = 0;
    uint8_t b = 0;
    if (urgent) {
        r = g_ledOn ? 255 : 0;
    } else if (stopped) {
        r = 255;
        g = 96;
    } else {
        g = g_ledOn ? 255 : 32;
    }
    g_strip.setPixelColor(0, g_strip.Color(r, g, b));
    g_strip.show();
#else
    (void)stopped;
#endif
}

// ===========================================================================
// デバッグ用シリアル
// ===========================================================================

#if ENABLE_SERIAL_DEBUG

// 「<ch> <duty>」で 1 チャンネルへ duty を指令する。's' で全チャンネル停止。
// duty は splitDuty が max_duty でクランプし、緊急停止ラッチ中は DcChannel が
// 指令を拒否するため、ここから安全機構を迂回することはできない（仕様書 §5.2 の要求）。
static void pollSerial(uint32_t nowMs) {
    while (Serial.available() > 0) {
        if (!g_serialLine.push(static_cast<char>(Serial.read()))) {
            continue;
        }
        const char *line = g_serialLine.line();

        if (line[0] == 's' || line[0] == 'S') {
            g_serialOverride = false;
            for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
                g_channel[ch].hold();
            }
        } else {
            // チャンネル番号と duty が空白で区切られていない行は捨てる。
            // 番号を読み違えると別のモータが回るので、曖昧な入力は指令にしない。
            char *sep = nullptr;
            const long ch = strtol(line, &sep, 10);
            if (sep != line && *sep == ' ' && ch >= 0 &&
                ch < static_cast<long>(kDcChannelCount)) {
                // **float が入る唯一の経路。** toRaw が NaN と範囲外を飽和させるので、
                // ここから先には CAN 経路と同じ値しか流れない（仕様書 §4）。
                g_channel[ch].setDuty(
                    fromRaw(toRaw(strtof(sep + 1, nullptr), kDutyScale), kDutyScale), nowMs);
                g_serialOverride = true;
            }
        }
    }

    // シリアル操作中はウォッチドッグを養い続ける。
    // 1 回だけ養う実装だと command_timeout_ms 後に必ず止まってデバッグにならない。
    // 'S' 入力・CAN の SET_TARGET・電源断のいずれでもこのモードは解除される。
    if (g_serialOverride) {
        for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
            g_channel[ch].feed(nowMs);
        }
    }
}

#endif  // ENABLE_SERIAL_DEBUG

// ===========================================================================
// setup / loop
// ===========================================================================

void setup() {
    // 何よりも先に出力段を停止側へ倒す。この基板には出力禁止ピンが無いので、
    // PWM ピンが不定のまま電源が入るとモータが回り出しうる。
    for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
        pinMode(kDcChannels[ch].pwmPin, OUTPUT);
        digitalWrite(kDcChannels[ch].pwmPin, LOW);
        pinMode(kDcChannels[ch].dirPin, OUTPUT);
        digitalWrite(kDcChannels[ch].dirPin, kDirForwardIsLow ? LOW : HIGH);
    }

    pinMode(kPinRef, INPUT_PULLUP);
    pinMode(kPinLed, OUTPUT);
    digitalWrite(kPinLed, LOW);

    for (uint8_t bit = 0; bit < kDipBitCount; ++bit) {
        pinMode(kPinDip[bit], INPUT_PULLUP);
    }

#if ENABLE_SERIAL_DEBUG
    // USB CDC。D0/D1 は DIP に使っているので Serial1 は開かない（config.h 参照）。
    Serial.begin(kSerialBaud);
#endif

#if HAS_RGB_LED
    g_strip.begin();
    g_strip.setBrightness(kRgbBrightness);
#endif

    // PWM を立ち上げる前に実効デバイス ID を確定させる。
    // ID 未設定のチャンネルには PwmOut::begin() すら通さず、パルスを 1 発も出さない
    // （仕様書 §2.2: 設定ミスで意図しないアクチュエータが動くより動かない方が安全）。
    resolveDeviceIds();

    const uint32_t startMs = millis();

    for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
        // 全チャンネルが同じ周期で同時に送ると 3 フレームのバーストになり、他バスの
        // 周期送信と重なったときに調停待ちが伸びて FEEDBACK の間隔が波打つ。
        // 周期を等分した位相をチャンネルごとにずらして平準化する。
        // ID 未設定のチャンネルも「デバイス ID 未設定」を知らせるために送るので、ここは全チャンネル分やる。
        g_feedbackTimer[ch].setLastMs(startMs - g_feedbackIntervalMs +
                                      (g_feedbackIntervalMs * ch) / kDcChannelCount);

        if (!isChannelConfigured(ch)) {
            continue;
        }
        // PwmOut::begin() の引数無し版は 490Hz・duty 50% で始まる。
        // それではモータが一瞬回るので、周波数とデューティを明示して 0% から立ち上げる。
        g_pwmStarted[ch] = g_pwm[ch].begin(kPwmFrequencyHz, 0.0f);
    }

    // 仕様書 §1: 1 Mbps。
    // CAN が上がらない基板を駆動させると PC から止められないので、
    // begin 失敗時は緊急停止ラッチに落として出力を封じる。
    if (!CAN.begin(CanBitRate::BR_1000k)) {
        g_canFailed = true;
        for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
            g_channel[ch].stop();
        }
    }

    // config.h のビルド時フラグを実行時フラグへ写す（仕様書 §5.1 / §8）。
    // 判定そのものは MotorSafety にしか無いので、写し忘れれば有効のまま動く。
    for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
        g_channel[ch].setWatchdogEnabled(WATCHDOG_ENABLED != 0);
    }

    // 電源投入時点で物理緊急停止が押されているなら、その状態から始める。
    // ここを省くと、押されたまま起動した基板が FEEDBACK の緊急停止ビットを立てず、
    // PC からは「解除済み」に見える。
    const bool pressed = isPhysicalStopPressed();
    for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
        g_channel[ch].applyPhysicalStop(pressed);
    }

    g_infoTimer.reset(startMs);
    g_blinkTimer.reset(startMs);
}

void loop() {
    const uint32_t nowMs = millis();

    pollCan();
#if ENABLE_SERIAL_DEBUG
    pollSerial(nowMs);
#endif

    // **pollCan() より後に読むこと。** 押している間に解除フレームが届いても、
    // ここで再ラッチされて「押している間は絶対に動かない」が成立する。
    const bool pressed = isPhysicalStopPressed();
    for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
        g_channel[ch].applyPhysicalStop(pressed);
    }

    // 出力は毎ループ書き直す。ウォッチドッグ満了のようにフレームを伴わない出力禁止は、
    // ここを通らなければ PWM に反映されない（この基板には出力禁止ピンが無く、
    // PWM を 0% にすることだけが止める手段）。
    for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
        applyChannelOutput(ch, nowMs);
    }

    for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
        if (g_feedbackTimer[ch].due(nowMs, g_feedbackIntervalMs)) {
            sendFeedback(ch, nowMs);
        }
    }

    // 仕様書 §3.4: 版番号の自己申告。起動時 1 回ではなく低頻度で送り続けるのは、
    // PC が基板より後から起動しても拾えるようにするため。
    if (g_infoTimer.due(nowMs, kInfoIntervalMs)) {
        for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
            if (isChannelConfigured(ch)) {
                sendInfo(ch);
            }
        }
    }

    updateLed(nowMs);
}
