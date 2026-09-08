// 電磁弁用自作モタドラのファームウェア本体（仕様書 §9）。
//
// 基板は STM32F303K8T6。CAN は内蔵 bxCAN、出力は GPIO 6 本の ON/OFF だけで、
// PWM も方向ピンもフィードバック回路も無い。
//
// **プロトコルと安全機構は firmware/lib/MotorCan/ が持ち、DC 用・サーボ用と共有する。**
// ここに書くのはペリフェラルの操作と、そのライブラリを呼ぶ順序だけ。規則そのもの
// （出力ゲート・ウォッチドッグ・宛先判定・デバイス ID の組み立て）をこちらへ写すと
// native テストが掛からなくなり、3 枚のうち 1 枚だけ挙動が違う状態が作れてしまう。

#include "app.h"

#include <stdlib.h>

#include "MotorCanProtocol.h"
#include "MotorCanRouter.h"
#include "MotorLoopTimer.h"
#include "MotorPinTable.h"
#include "MotorTxHealth.h"
#include "SerialLineBuffer.h"
#include "SerialOverride.h"
#include "SolenoidChannel.h"
#include "config.h"
#include "main.h"

extern "C" {
extern CAN_HandleTypeDef hcan;
extern UART_HandleTypeDef huart1;
}

using namespace motorcan;

namespace {

// ===========================================================================
// config.h の妥当性検査（ビルド時）
// ===========================================================================

// ピン 1 本を「ポート + ピン」の組で表す。GPIOA / GPIOB はポインタへのキャストを
// 含むマクロで constexpr 文脈に持ち込めないため、検査用の型を別に持つ。
struct PinRef {
    Port port;
    uint16_t pin;
};

// 基板が使うピンをすべて 1 つの表に集める。
//
// **CAN との衝突だけを見てはならない。** それだけでは config.h の想定と実基板の
// 配線がまるごと食い違っていてもビルドが通る。
// UART と CAN は「コードから触らないが奪われると死ぬ」ピンなので必ず入れる。
constexpr PinRef kAllPins[] = {
    {kSolenoidChannels[0].port, kSolenoidChannels[0].pin},
    {kSolenoidChannels[1].port, kSolenoidChannels[1].pin},
    {kSolenoidChannels[2].port, kSolenoidChannels[2].pin},
    {kSolenoidChannels[3].port, kSolenoidChannels[3].pin},
    {kSolenoidChannels[4].port, kSolenoidChannels[4].pin},
    {kSolenoidChannels[5].port, kSolenoidChannels[5].pin},
    {kPortLed, kPinLed},
    {kDipPorts[0], kDipPins[0]},
    {kDipPorts[1], kDipPins[1]},
    {kDipPorts[2], kDipPins[2]},
    {kDipPorts[3], kDipPins[3]},
    {kPortCanRx, kPinCanRx},
    {kPortCanTx, kPinCanTx},
    {kPortUartTx, kPinUartTx},
    {kPortUartRx, kPinUartRx},
};

constexpr uint8_t kAllPinCount = sizeof(kAllPins) / sizeof(kAllPins[0]);

// **constexpr のループで continue を使わないこと。** avr-gcc 7.3 は constexpr 評価中の
// continue で増分式を飛ばして無限ループになる。arm-none-eabi では踏まないが、
// MotorCan を共有している以上、書き方も 3 枚で揃えておく。
constexpr bool pinsAreUnique() {
    for (uint8_t i = 0; i < kAllPinCount; ++i) {
        for (uint8_t j = static_cast<uint8_t>(i + 1); j < kAllPinCount; ++j) {
            if (kAllPins[i].port == kAllPins[j].port && kAllPins[i].pin == kAllPins[j].pin) {
                return false;
            }
        }
    }
    return true;
}

static_assert(pinsAreUnique(), "config.h のピンが重複している（CAN / UART / LED / DIP 含む）");

// **上の kAllPins[] と下の g_channel[] は各チャンネルを明示的に初期化している。**
// チャンネル数だけ増やすと、ch6 / ch7 のピンが衝突検査から静かに漏れ（重複した
// ピンを 2 つの弁に割り当ててもビルドが通る）、g_channel[] の初期化子も足りなくなる。
// DC 用 main.cpp とサーボ用 main.cpp には同じ番人が居るので、ここだけ無いと
// 「3 枚のうち 1 枚だけ検査が抜けている」状態になる。
static_assert(kSolenoidChannelCount == 6,
              "チャンネル数を変えたら kAllPins / g_channel の初期化子も更新すること");
static_assert(kSolenoidChannelCount <= motorcan::kMaxSlotNumber + 1,
              "チャンネル数がデバイス ID のスロット幅（3bit）を超えている");
static_assert(kSolenoidChannelCount <= motorcan::kMaxChannels,
              "チャンネル数が FrameRoute の channelMask（8bit）を超えている");
static_assert(kDipBitCount == sizeof(kDipPins) / sizeof(kDipPins[0]),
              "kDipBitCount と kDipPins の数が食い違っている");
static_assert(kDipBitCount == sizeof(kDipPorts) / sizeof(kDipPorts[0]),
              "kDipBitCount と kDipPorts の数が食い違っている");

// **CubeMX 生成の main.h と config.h のチャンネル表が一致していること。**
// 片方だけを書き換えると、GPIO は初期化されているのに別のピンを叩くファームになり、
// 症状は「その弁だけ動かない」か「違う弁が開く」だけで CAN 越しには見えない。
static_assert(kSolenoidChannels[0].pin == PUMP1_SW_Pin, "ch0 が PUMP1_SW と食い違っている");
static_assert(kSolenoidChannels[1].pin == PUMP2_SW_Pin, "ch1 が PUMP2_SW と食い違っている");
static_assert(kSolenoidChannels[2].pin == PUMP3_SW_Pin, "ch2 が PUMP3_SW と食い違っている");
static_assert(kSolenoidChannels[3].pin == PUMP4_SW_Pin, "ch3 が PUMP4_SW と食い違っている");
static_assert(kSolenoidChannels[4].pin == PUMP5_SW_Pin, "ch4 が PUMP5_SW と食い違っている");
static_assert(kSolenoidChannels[5].pin == PUMP6_SW_Pin, "ch5 が PUMP6_SW と食い違っている");
static_assert(kPinLed == LED_BI_Pin, "LED が main.h と食い違っている");
static_assert(kDipPins[0] == DIP1_Pin, "DIP1 が main.h と食い違っている");
static_assert(kDipPins[1] == DIP2_Pin, "DIP2 が main.h と食い違っている");
static_assert(kDipPins[2] == DIP3_Pin, "DIP3 が main.h と食い違っている");
static_assert(kDipPins[3] == DIP4_Pin, "DIP4 が main.h と食い違っている");

// ===========================================================================
// 状態
// ===========================================================================

SolenoidChannel g_channel[kSolenoidChannelCount] = {
    SolenoidChannel(kDefaultCommandTimeoutMs), SolenoidChannel(kDefaultCommandTimeoutMs),
    SolenoidChannel(kDefaultCommandTimeoutMs), SolenoidChannel(kDefaultCommandTimeoutMs),
    SolenoidChannel(kDefaultCommandTimeoutMs), SolenoidChannel(kDefaultCommandTimeoutMs),
};

uint8_t g_deviceId[kSolenoidChannelCount] = {0};
PeriodicTimer g_feedbackTimer[kSolenoidChannelCount];
PeriodicTimer g_infoTimer;

// INFO は 1Hz で全チャンネル分を送るが、bxCAN のメールボックスは 3 本しかなく、
// 6 ch を同じ反復で連続送信すると 4 通目以降が必ず落ちる。しかも kInfoIntervalMs が
// feedback_interval_ms の整数倍なので毎回同じ位相で落ち、後ろの ch は永久に 1 通も
// 出ない（実機で 6ch 中 2ch しか出ていないことを観測）。
// 1 反復 1 通に割り、落ちた ch はそのまま次の反復で送り直す。
uint8_t g_infoPendingCh = kSolenoidChannelCount;  // kSolenoidChannelCount = 送信待ちなし

PeriodicTimer g_blinkTimer;

// FEEDBACK の送信周期は基板全体で 1 つ（チャンネルごとに変えると位相の分散が崩れる）。
uint16_t g_feedbackIntervalMs = kDefaultFeedbackIntervalMs;

bool g_canFailed = false;

// config.h のピン割当が CubeMX 生成の main.h と食い違っている。
// **弁を 1 つも開かせず、LED でそれと分かるようにする** ——
// CAN が上がらない基板と同じ「今すぐ直さないと使えない」扱い（仕様書 §2.2）。
//
// 駆動を封じているのは実際には「デバイス ID を確定させない」ことで（setup() の
// コメント）、このフラグが担うのは LED の表示だけ。
bool g_configMismatch = false;

bool g_ledOn = false;

// 送信の連続失敗数。数える規則は TxFailCounter が持つ（native テスト圏内）。
TxFailCounter g_txFail;

#if ENABLE_SERIAL_DEBUG
char g_serialStorage[kSerialLineCapacity];
SerialLineBuffer g_serialLine(g_serialStorage, sizeof(g_serialStorage));
// シリアルで開閉しているチャンネルと、その期限（規則は SerialOverride.h）。
SerialOverride g_serialOverride;
#endif

// ===========================================================================
// GPIO
// ===========================================================================

// Port（config.h の HAL 非依存な enum）から HAL のポートへの変換はここだけが持つ。
GPIO_TypeDef *portOf(Port port) { return port == Port::A ? GPIOA : GPIOB; }

// HAL のポート → 比較用のポート番号（config.h の enum と同じ値）。
//
// **portOf() の逆だが「A でなければ B」に丸めてはならない。** CubeMX が
// ピンを 3 つ目のポートへ動かしたとき、B と読み替えると config.h が B と
// 書いてあるだけで一致してしまい、照合そのものが素通りする。
uint8_t portIndexOf(GPIO_TypeDef *port) {
    if (port == GPIOA) {
        return static_cast<uint8_t>(Port::A);
    }
    if (port == GPIOB) {
        return static_cast<uint8_t>(Port::B);
    }
    return kPortIndexUnknown;
}

// config.h のピン割当が CubeMX 生成の main.h と一致しているか。
//
// **ピン番号は static_assert が見ているが、ポートはビルド時に見られない。**
// `PUMP5_SW_GPIO_Port` は `GPIOA` へ、`GPIOA` は `((GPIO_TypeDef *) GPIOA_BASE)` へ
// 展開されるポインタキャストなので、constexpr 文脈にも `##` の連結にも持ち込めない。
// 電源投入時に必ず通る setup() で照合する。
//
// **比較の規則そのものは MotorPinTable.h（HAL 非依存）が持つ。** ここへループを
// 書き戻すと、この翻訳単位は native テストの対象外なので `!=` を `==` に
// 書き換えても全ケース緑になる。ここに残すのは HAL ポインタ → ポート番号の
// 逆引きと、2 つの表を同じ順序で並べることだけ。
//
// **CAN（PA11 / PA12）と UART（PA9 / PA10）はここでは照合できない。**
// この 2 つは CubeMX が `HAL_CAN_MspInit` / `HAL_UART_MspInit` の中で
// `GPIO_PIN_11|GPIO_PIN_12` のようにその場で書いており、main.h に
// `*_GPIO_Port` / `*_Pin` のマクロが生成されない（＝突き合わせる相手が居ない）。
// この 4 本を守るのは kAllPins の重複検査（static_assert）だけである。
bool portsMatchCubeMx() {
    // config.h 側。**下の expected[] と 1 対 1 の順序**で並べる。
    // チャンネルを増やしたら上の static_assert(kSolenoidChannelCount == 6) が先に落ちる。
    constexpr PortPin actual[] = {
        {static_cast<uint8_t>(kSolenoidChannels[0].port), kSolenoidChannels[0].pin},
        {static_cast<uint8_t>(kSolenoidChannels[1].port), kSolenoidChannels[1].pin},
        {static_cast<uint8_t>(kSolenoidChannels[2].port), kSolenoidChannels[2].pin},
        {static_cast<uint8_t>(kSolenoidChannels[3].port), kSolenoidChannels[3].pin},
        {static_cast<uint8_t>(kSolenoidChannels[4].port), kSolenoidChannels[4].pin},
        {static_cast<uint8_t>(kSolenoidChannels[5].port), kSolenoidChannels[5].pin},
        {static_cast<uint8_t>(kPortLed), kPinLed},
        // DIP は読み違えると基板番号ごと変わる（別の基板のデバイス ID を名乗る）
        {static_cast<uint8_t>(kDipPorts[0]), kDipPins[0]},
        {static_cast<uint8_t>(kDipPorts[1]), kDipPins[1]},
        {static_cast<uint8_t>(kDipPorts[2]), kDipPins[2]},
        {static_cast<uint8_t>(kDipPorts[3]), kDipPins[3]},
    };
    // **CubeMX 側 (main.h) が正。** MX_GPIO_Init が実際に初期化したのはこちら。
    const PortPin expected[] = {
        {portIndexOf(PUMP1_SW_GPIO_Port), PUMP1_SW_Pin},
        {portIndexOf(PUMP2_SW_GPIO_Port), PUMP2_SW_Pin},
        {portIndexOf(PUMP3_SW_GPIO_Port), PUMP3_SW_Pin},
        {portIndexOf(PUMP4_SW_GPIO_Port), PUMP4_SW_Pin},
        {portIndexOf(PUMP5_SW_GPIO_Port), PUMP5_SW_Pin},
        {portIndexOf(PUMP6_SW_GPIO_Port), PUMP6_SW_Pin},
        {portIndexOf(LED_BI_GPIO_Port), LED_BI_Pin},
        {portIndexOf(DIP1_GPIO_Port), DIP1_Pin},
        {portIndexOf(DIP2_GPIO_Port), DIP2_Pin},
        {portIndexOf(DIP3_GPIO_Port), DIP3_Pin},
        {portIndexOf(DIP4_GPIO_Port), DIP4_Pin},
    };
    constexpr uint8_t kCheckedPinCount = sizeof(actual) / sizeof(actual[0]);
    static_assert(sizeof(expected) / sizeof(expected[0]) == kCheckedPinCount,
                  "config.h 側と main.h 側の行数が食い違っている（1 対 1 に並べること）");
    static_assert(kCheckedPinCount == kSolenoidChannelCount + 1 + kDipBitCount,
                  "照合していないピンがある（チャンネル + LED + DIP の全部を並べること）");

    return pinTablesMatch(actual, expected, kCheckedPinCount);
}

// LED を叩く唯一の口。**config.h ではなく CubeMX 生成の main.h を正として書く。**
//
// ポートの食い違いを見つけたときに残る通知経路は LED だけなのに、その LED を
// 疑いの対象そのもの（config.h の kPortLed）で叩くと、食い違いの中身次第で
// **止めたはずの弁のピンを 200ms ごとに叩く**（kPinLed = 1 << 5 のポートを B と
// 書き間違えれば PB5 = ch2 = valve_3 で、MX_GPIO_Init が出力に設定済みのピンである）。
// 両者が一致していることは portsMatchCubeMx() が別途見るので、config.h の
// kPortLed / kPinLed が死んだ定数になるわけではない。
void writeLed(bool on) {
    HAL_GPIO_WritePin(LED_BI_GPIO_Port, LED_BI_Pin, on ? GPIO_PIN_SET : GPIO_PIN_RESET);
}

// 仕様書 §2.2: オフセット適用後のデバイス ID が 0x00 のチャンネルは駆動しない。
bool isChannelConfigured(uint8_t ch) { return g_deviceId[ch] != kDeviceIdUnconfigured; }

// **電磁弁への出力はすべてこの関数を通す。** ID 未設定と安全機構のどちらも
// 迂回する経路を作らないため（仕様書 §9.4）。
void applyChannelOutput(uint8_t ch, uint32_t nowMs) {
    if (!isChannelConfigured(ch)) {
        // 設定ミスで意図しない弁が開くより、開かない方が安全。
        return;
    }
    // **出力を読む前に目標を畳む。** outputOn() は出力禁止中に false を返すだけで
    // 目標を残すので、これが無いとウォッチドッグ満了や緊急停止で消磁した後に
    // 「受理できない SET_TARGET」が 1 通届いただけで途絶前に開いていた弁が
    // 再通電する（§3.1 / §6 のとおり受理できないフレームでもウォッチドッグは養われる）。
    g_channel[ch].tick(nowMs);

    // outputOn() は出力禁止中に false を返す。この基板に出力禁止ピンは無く、
    // GPIO を LOW にすることだけが止める手段なので、ここを通さない経路を作らないこと。
    const bool on = g_channel[ch].outputOn(nowMs);
    HAL_GPIO_WritePin(portOf(kSolenoidChannels[ch].port), kSolenoidChannels[ch].pin,
                      on ? GPIO_PIN_SET : GPIO_PIN_RESET);
}

void applyAllOutputs(uint32_t nowMs) {
    for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
        applyChannelOutput(ch, nowMs);
    }
}

// ===========================================================================
// CAN
// ===========================================================================

// 状態フラグの組み立て規則そのものは composeFeedbackFlags が持つ（native テスト圏内）。
// **ここで OR を足してはならない。** 到達フラグを立てないこと（仕様書 §9.3: 弁が
// 開いたかを観測する手段が 1 つも無い）は board == Solenoid から導かれるので、
// この呼び出しが規則の全てになる。ここで組み立てると規則が HAL の翻訳単位へ移り、
// `flags |= kReached;` を足しても native テストが 1 件も落ちなくなる。
uint8_t buildStatusFlags(uint8_t ch, uint32_t nowMs) {
    return composeFeedbackFlags(kBoardKind, SlotKind::Actuator,
                                g_channel[ch].safetyStatusFlags(nowMs), isChannelConfigured(ch),
                                /*reached=*/false, /*sensorActive=*/false);
}

// 送信は空きメールボックスが無ければ諦める。**待ってはならない** — 詰まった
// バスの上で loop() が止まると、ウォッチドッグ満了の反映も出力の更新も止まる。
// FEEDBACK は次の周期でまた送られるので、1 通落ちても PC 側の STALE 判定
// （既定 500ms）には遠く届かない。
//
// 諦めた結果は捨てずに数える。**戻り値を捨てないための唯一の口**にしてあるので、
// HAL_CAN_AddTxMessage() を直に呼ぶ経路を作らないこと。捨てると、6ch 中 4ch の
// INFO が 1 通も出ていないことが LED にもログにも現れない。
//
// **この数え方は自動再送 (main.c の AutoRetransmission = ENABLE) に依存している。**
// NART (再送しない) だと、1 回の送信試行が成功・エラー・調停負けのどれで終わっても
// メールボックスが解放されるので、`GetTxMailboxesFreeLevel() == 0` も
// `AddTxMessage != HAL_OK` も**成立しない** —— トランシーバが死んでいてもバスから
// 外れていても ACK が返らなくても、g_txFail は 0 のまま LED は平常のハートビートを
// 出し続ける。PC 側からは 6 本の弁が全部 STALE になるだけで、現場の切り分け手段が
// 両側とも消える。DC 基板 (R4 内蔵 CAN は既定で再送する) では同じ状況でメールボックス
// が埋まり続けて規則どおり赤へ倒れるので、**その非対称は意図されたものではない**。
//
// 再送を有効にしても loop() は止まらない —— ここは空きが無ければ即座に諦める
// (規則②「空きを待たない」) ので、詰まったバスの上でも周期は回り続ける。
bool sendFrame(uint16_t canId, uint8_t *data, uint8_t length) {
    if (HAL_CAN_GetTxMailboxesFreeLevel(&hcan) == 0) {
        g_txFail.onFailure();
        return false;
    }
    CAN_TxHeaderTypeDef header{};
    header.StdId = canId;
    header.ExtId = 0;
    header.IDE = CAN_ID_STD;
    header.RTR = CAN_RTR_DATA;
    header.DLC = length;
    header.TransmitGlobalTime = DISABLE;

    uint32_t mailbox = 0;
    if (HAL_CAN_AddTxMessage(&hcan, &header, data, &mailbox) != HAL_OK) {
        g_txFail.onFailure();
        return false;
    }
    g_txFail.onSuccess();
    return true;
}

void sendFeedback(uint8_t ch, uint32_t nowMs) {
    // **この基板は位置を持たないので状態フラグ 1 バイトだけ**（仕様書 §3.2）。
    // 常に 0 の位置を詰めても、PC には「測ったように見える 0」が届くだけ。
    uint8_t data[kFeedbackFlagsOnlyLength];
    const uint8_t len = encodeFeedback(data, buildStatusFlags(ch, nowMs));

    // 緊急停止中・ウォッチドッグ作動中も送り続ける。止めると PC 側が STALE になり、
    // なぜ動かないのかを操縦者が判別できなくなる。
    // **ID 未設定チャンネルはここへ来ない**（呼び出し側が isChannelConfigured で弾く。§2.2）。
    sendFrame(buildCanId(CommandType::Feedback, g_deviceId[ch]), data, len);
}

// 仕様書 §3.4: 焼き忘れた基板をセッティングタイムに見つけるための自己申告。
// 低頻度（1Hz）で送るので、PC が後から起動しても拾える。
// **送れたかを返す。** 落とすと 1 秒欠けるので、呼び出し側が次の反復で送り直す。
bool sendInfo(uint8_t ch) {
    uint8_t data[kInfoBaseLength];
    const uint8_t len = encodeInfo(data, kFirmwareVersion, kBoardKind, SlotKind::Actuator);
    return sendFrame(buildCanId(CommandType::Info, g_deviceId[ch]), data, len);
}

void applyParam(uint8_t ch, const SetParamCommand &cmd) {
    // command_timeout_ms / feedback_interval_ms は 3 枚に共通なので MotorCan が持つ。
    if (applyCommonParam(cmd, g_channel[ch], g_feedbackIntervalMs)) {
        return;
    }
    switch (cmd.id) {
        case ParamId::CommandTimeoutMs:
        case ParamId::FeedbackIntervalMs:
            // applyCommonParam が処理済み。ここへは来ない。
            break;
        case ParamId::MaxDuty:
        case ParamId::ReachedTolerance:
        case ParamId::SlewRate:
        case ParamId::AngleMin:
        case ParamId::AngleMax:
            // 仕様書 §9.5: 出力が 2 値のこの基板には意味を持たない。
            // 受け付けて内部に持つと、PC 側から「設定できたのに効かない値」に見える。
            break;
    }
}

void handleChannelFrame(uint8_t ch, CommandType command, const uint8_t *data, uint8_t length,
                        uint32_t nowMs) {
    switch (command) {
        case CommandType::SetTarget: {
            // 仕様書 §6: 緊急停止ラッチ中でもウォッチドッグは養う。
            // 養わないと解除した直後に満了済みで動かない。制御タイプが on_off で
            // なくても養うのは、通信自体は生きているため。
            // 受理判定（SolenoidChannel::setOn）より必ず先に呼ぶこと。起動直後は
            // §5.4 により未受信＝出力禁止なので、順序を逆にすると最初の 1 通を捨てる。
            g_channel[ch].feed(nowMs);

            const SetTargetCommand cmd = decodeSetTarget(data, length);
            if (!cmd.valid) {
                return;
            }
#if ENABLE_SERIAL_DEBUG
            // PC がこのチャンネルへ指令を出している以上、シリアルの上書きは終わり。
            g_serialOverride.clear();
#endif
            // **制御タイプの受理判定（§9.2: on_off のみ）は SolenoidChannel が持つ。**
            // ここに `if (cmd.type != ...)` を戻すと、この翻訳単位は native テストの
            // 対象外なので消しても全ケース緑になる。
            // 続けて緊急停止ラッチ中・ウォッチドッグ満了中は受け付けない。
            g_channel[ch].applySetTarget(cmd, nowMs);
            break;
        }
        case CommandType::SetParam: {
            const SetParamCommand cmd = decodeSetParam(data, length);
            if (cmd.valid) {
                // 未知のパラメータ ID は decodeSetParam が弾く（仕様書 §3.3）。
                applyParam(ch, cmd);
            }
            break;
        }
        case CommandType::EStop: {
            const EStopAction action = g_channel[ch].handleEStopFrame(data, length);
            if (action != EStopAction::None) {
                // 停止はその場で GPIO へ反映する。次のループを待つと、その間だけ
                // 通電し続ける（loop() の周期は保証されていない）。
                applyChannelOutput(ch, nowMs);
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

void handleFrame(uint16_t canId, bool isStandardId, const uint8_t *data, uint8_t length) {
    // Standard Frame 判定・予約コマンド種別・宛先判定（チャンネル表との突き合わせ /
    // ブロードキャスト E_STOP は全チャンネル / ID 未設定チャンネルに自分宛は無い）は
    // 他の 2 枚と同じ規則なので MotorCanRouter に集約してある。
    const FrameRoute route = routeFrame(canId, isStandardId, g_deviceId, kSolenoidChannelCount);
    if (!route.accepted) {
        return;
    }

    const uint32_t nowMs = HAL_GetTick();
    for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
        if ((route.channelMask & static_cast<uint8_t>(1u << ch)) != 0) {
            handleChannelFrame(ch, route.command, data, length, nowMs);
        }
    }
}

void pollCan() {
    CAN_RxHeaderTypeDef header{};
    uint8_t data[8] = {0};

    while (HAL_CAN_GetRxFifoFillLevel(&hcan, CAN_RX_FIFO0) > 0) {
        if (HAL_CAN_GetRxMessage(&hcan, CAN_RX_FIFO0, &header, data) != HAL_OK) {
            return;
        }
        handleFrame(static_cast<uint16_t>(header.StdId), header.IDE == CAN_ID_STD, data,
                    static_cast<uint8_t>(header.DLC));
    }
}

// **PC → モタドラ方向のフレームだけを通す**（E_STOP / SET_TARGET / SET_PARAM）。
//
// 全通過にすると、共有バス上の FEEDBACK（自作モタドラ 14 台 × 100Hz）まで受信
// FIFO（深さ 3）へ流れ込み、loop() が一瞬でも伸びた隙に取りこぼす。落ちるのが
// E_STOP や SET_TARGET だと、症状は「たまに指令が効かない」という最も追いにくい形になる。
//
// **どの ID を通すかは CommandType から導く**（`kEStopAndSetTargetFilter` /
// `kSetParamFilter`）。`0x000` / `0x600` / `0x200` / `0x700` のリテラルを並べると
// CommandType への参照が 1 つも無くなり、種別のビット配置を動かしたときに**電磁弁だけが
// 緊急停止を落とし始める**（DC 用とサーボ用は parseCanId 経由で自動追従する）。
//
// **1 本のマスクでは 3 値を表せない**ので 2 バンクに分ける。ここに残っているのは
// bxCAN 固有の事情だけ —— 32bit スケールでは STID が FilterIdHigh の bit15..5 に載る。
constexpr uint16_t kStdIdShiftInFilterReg = 5;

constexpr uint16_t toFilterReg(uint16_t stdId) {
    return static_cast<uint16_t>(stdId << kStdIdShiftInFilterReg);
}

bool configureCanFilters() {
    CAN_FilterTypeDef filter{};
    filter.FilterMode = CAN_FILTERMODE_IDMASK;
    filter.FilterScale = CAN_FILTERSCALE_32BIT;
    filter.FilterFIFOAssignment = CAN_FILTER_FIFO0;
    filter.FilterActivation = CAN_FILTER_ENABLE;
    filter.SlaveStartFilterBank = 14;

    filter.FilterBank = 0;
    filter.FilterIdHigh = toFilterReg(kEStopAndSetTargetFilter.id);
    filter.FilterIdLow = 0;
    filter.FilterMaskIdHigh = toFilterReg(kEStopAndSetTargetFilter.mask);
    filter.FilterMaskIdLow = 0;
    if (HAL_CAN_ConfigFilter(&hcan, &filter) != HAL_OK) {
        return false;
    }

    filter.FilterBank = 1;
    filter.FilterIdHigh = toFilterReg(kSetParamFilter.id);
    filter.FilterMaskIdHigh = toFilterReg(kSetParamFilter.mask);
    return HAL_CAN_ConfigFilter(&hcan, &filter) == HAL_OK;
}

// ===========================================================================
// デバイス ID（DIP スイッチ = チャンネル表全体へのブロックオフセット）
// ===========================================================================

// DIP の添字を readDipSwitch へ渡し、読み出しはラムダが (port, pin) へ引き直す。
// 負論理とビット順の対応、はみ出しの扱いは MotorCanRouter / makeDeviceId が持つ
// （native テストで守られている）。
//
// **添字を並べた表なので、kDipBitCount を増やしたらここも足すこと。**
// 足し忘れると読まないビットが出て、DIP を回しても基板番号が変わらなくなる。
const uint8_t kDipIndices[kDipBitCount] = {0, 1, 2, 3};
static_assert(kDipBitCount == 4, "kDipBitCount を変えたら kDipIndices も更新すること");

void resolveDeviceIds() {
    const uint8_t boardNumber = readDipSwitch(
        kDipIndices, kDipBitCount,
        [](uint8_t index) {
            return static_cast<int>(HAL_GPIO_ReadPin(portOf(kDipPorts[index]), kDipPins[index]));
        },
        kDipActiveLevel);

    motorcan::resolveDeviceIds(g_deviceId, kSolenoidChannelCount, kBoardKind, boardNumber,
                               nullptr);
}

// ===========================================================================
// LED
// ===========================================================================

void updateLed(uint32_t nowMs) {
#if HAS_STATUS_LED
    BoardIndication indication(g_canFailed || g_configMismatch ||
                               g_txFail.isAlarming(kCanTxFailStreakAlarm));
    for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
        indication.observe(isChannelConfigured(ch),
                           (g_channel[ch].safetyStatusFlags(nowMs) & status_flag::kEStop) != 0);
    }

    // **LED は 1 本しかないので、点滅の速さが状態を伝える唯一の手段になる。**
    // 他の 2 枚と違って緊急停止に専用の速さを割り当てるのはそのため。
    //   速い（200ms）  … CAN 不通 / デバイス ID 未設定。今すぐ直さないと使えない
    //   中間（500ms）  … 緊急停止ラッチ中。直す対象ではないが動かない
    //   遅い（1000ms） … 平常のハートビート
    const uint32_t interval = blinkIntervalFor(indication, kUnconfiguredBlinkIntervalMs,
                                               kEStopBlinkIntervalMs, kHeartbeatIntervalMs);

    if (!g_blinkTimer.due(nowMs, interval)) {
        return;
    }
    g_ledOn = !g_ledOn;
    writeLed(g_ledOn);
#else
    (void)nowMs;
#endif
}

// ===========================================================================
// デバッグ用シリアル
// ===========================================================================

#if ENABLE_SERIAL_DEBUG

// 「<ch> <0|1>」で 1 チャンネルを開閉する。's' で全チャンネル消磁。
// 緊急停止ラッチ中は SolenoidChannel が指令を拒否するため、ここから安全機構を
// 迂回することはできない（仕様書 §5.2 の要求）。
//
// **HAL_UART_Receive() は使わない。** タイムアウト 0 では受信済みの 1 バイトも
// 取らずに戻るうえ、HAL の RxState を握るので loop() の中で回す用途に合わない。
// RXNE を直接見て RDR を読む。
void pollSerial(uint32_t nowMs) {
    // オーバーランを放置すると RXNE が立たなくなり、シリアルが二度と応答しなくなる。
    // デバッグ経路なので取りこぼしそのものは許容し、フラグだけ落とす。
    if (__HAL_UART_GET_FLAG(&huart1, UART_FLAG_ORE)) {
        __HAL_UART_CLEAR_OREFLAG(&huart1);
    }

    while (__HAL_UART_GET_FLAG(&huart1, UART_FLAG_RXNE)) {
        const char c = static_cast<char>(huart1.Instance->RDR & 0xFF);
        if (!g_serialLine.push(c)) {
            continue;
        }
        // 行の骨格の解釈（'s' / '<番号> <値>'）は parseSerialCommand が持つ。
        // 値の読み取りだけを基板ごとに行う（電磁弁は 0/1 の整数）。
        const SerialCommand cmd = parseSerialCommand(g_serialLine.line(), kSolenoidChannelCount);
        if (cmd.kind == SerialCommand::Kind::StopAll) {
            g_serialOverride.clear();
            for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
                g_channel[ch].hold();
            }
        } else if (cmd.kind == SerialCommand::Kind::Channel) {
            g_channel[cmd.channel].setOn(strtol(cmd.value, nullptr, 10) != 0, nowMs);
            g_serialOverride.note(cmd.channel, nowMs);
        }
    }

    // シリアル操作中はウォッチドッグを養い続ける。1 回だけ養う実装だと
    // command_timeout_ms 後に必ず止まってデバッグにならない。
    // **養う範囲と期限の規則は SerialOverride が持つ**（打ったチャンネルだけ /
    // 最後の入力から kSerialOverrideHoldMs）。ここで全チャンネルを養うと、
    // `2 1` と 1 行打っただけで 6ch すべての最後の砦が無期限に外れ、その後 PC が
    // 落ちても**触っていない 5ch まで通電したまま**残る（CAN も PC も死んでいるので
    // E_STOP も届かない）。
    for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
        if (g_serialOverride.shouldFeed(ch, nowMs)) {
            g_channel[ch].feed(nowMs);
        }
    }
}

#endif  // ENABLE_SERIAL_DEBUG

}  // namespace

// ===========================================================================
// setup / loop
// ===========================================================================

extern "C" void setup() {
    // **config.h と CubeMX のピン割当が一致しているかを何よりも先に確かめる。**
    // 上の static_assert 群はピン番号 (`*_Pin`) しか突き合わせていないので、
    // `kSolenoidChannels[4]` を `{Port::A, 1 << 3}` と書き間違えても
    // `PUMP5_SW_Pin == GPIO_PIN_3` なら通り、`pinsAreUnique()` も PA3 が他と
    // 衝突しなければ通る。症状は「その弁だけ動かない」か「MX_GPIO_Init が出力設定
    // していないピンを叩く」だけで、CAN 越しには一切見えない。
    //
    // **ビルド時に見られない理由**: `PUMP5_SW_GPIO_Port` は `GPIOA` へ、`GPIOA` は
    // `((GPIO_TypeDef *) GPIOA_BASE)` へ展開されるポインタキャストなので、
    // constexpr 文脈にも `##` の連結にも持ち込めない。**電源投入時に必ず通る**
    // ここが次善の場所になる。
    //
    // **消磁ループより先に置く。** 後ろに置くと、食い違ったポート指定のまま
    // HAL_GPIO_WritePin を 6 回叩いてから検査することになり、「間違ったピンを
    // 叩く経路を残さない」というこの検査自身の目的と噛み合わない。
    g_configMismatch = !portsMatchCubeMx();

    if (!g_configMismatch) {
        // 出力を消磁側へ倒す。GPIO は MX_GPIO_Init() が RESET で初期化しているが、
        // ここを省くと初期化順を変えたときに「通電したまま起動する」経路が黙って生まれる。
        for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
            HAL_GPIO_WritePin(portOf(kSolenoidChannels[ch].port), kSolenoidChannels[ch].pin,
                              GPIO_PIN_RESET);
        }
    }
    // LED だけは食い違っていても叩く（唯一の通知経路）。main.h を正として書くので
    // config.h の表が間違っていても別のピンには当たらない（writeLed のコメント）。
    writeLed(false);

    // **食い違ったらデバイス ID を確定させない。** g_deviceId[] は 0x00
    // （kDeviceIdUnconfigured）のまま残るので、仕様書 §2.2 の既存の経路にそのまま乗る:
    // applyChannelOutput は入口で return して GPIO へ 1 度も届かず、FEEDBACK も INFO も
    // 1 通も出ず、LED は BoardIndication が urgent（赤の速い点滅）へ倒す。
    // docs/invariants.md の「表に無い基板番号は全スロット Unused のまま据え置く」と同じ形で、
    // 新しいゲートを 1 つも増やさずに「弁を 1 つも開かせない」が成立する。
    //
    // **緊急停止ラッチ（MotorSafety::stop()）で止めてはならない。** ラッチは CAN の
    // E_STOP 解除フレームで外れ、PC 側は励磁のたびにそれを送る（lib/drivers/generic.py の
    // activation_steps() が encode_e_stop_clear() を返し、加えて lib/server.py の
    // _send_e_stop_clear_broadcast が全バスへブロードキャスト解除を流す）。つまり
    // ラッチに預けると、PC を起動して励磁した時点で剥がれ、その後の SET_TARGET で
    // 間違ったポートの GPIO を叩く —— 止まっているのは電源投入から最初の励磁までだけになる。
    // 下の CAN 失敗時に stop() が有効なのは、**CAN が上がっていないので解除フレームが
    // 物理的に届かない**からであって、ポートの食い違いにその前提は無い（CAN は正常に上がる）。
    if (!g_configMismatch) {
        // 実効デバイス ID を確定させてから CAN を開ける。
        resolveDeviceIds();
    }

    const uint32_t startMs = HAL_GetTick();

    for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
        // 送信位相の分散は PeriodicTimer::stagger が持つ（式と理由は MotorLoopTimer.h）。
        // ID 未設定のチャンネルは 1 通も送らない（§2.2）が、位相の割り当てはチャンネルの
        // 添字で決まるので、ここは全チャンネル分やる（飛ばすと隣とずれ方が変わる）。
        g_feedbackTimer[ch].stagger(startMs, g_feedbackIntervalMs, ch, kSolenoidChannelCount);
    }

    // 仕様書 §1: 1 Mbps（ビットタイミングは solenoid.ioc が持つ）。
    // **CAN が上がらない基板を駆動させると PC から止められない**ので、
    // 失敗時は緊急停止ラッチに落として出力を封じる。
    if (!configureCanFilters() || HAL_CAN_Start(&hcan) != HAL_OK) {
        g_canFailed = true;
        for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
            g_channel[ch].stop();
        }
    }

    // config.h のビルド時フラグを実行時フラグへ写す（仕様書 §5.1）。
    // 判定そのものは MotorSafety にしか無いので、写し忘れれば有効のまま動く。
    for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
        g_channel[ch].setWatchdogEnabled(WATCHDOG_ENABLED != 0);
    }

    g_infoTimer.reset(startMs);
    g_blinkTimer.reset(startMs);

    applyAllOutputs(startMs);
}

extern "C" void loop() {
    const uint32_t nowMs = HAL_GetTick();

    pollCan();
#if ENABLE_SERIAL_DEBUG
    pollSerial(nowMs);
#endif

    // 出力は毎ループ書き直す。**ウォッチドッグ満了のようにフレームを伴わない
    // 出力禁止は、ここを通らなければ GPIO に反映されない**（この基板に出力禁止ピンは
    // 無く、GPIO を LOW にすることだけが止める手段）。
    applyAllOutputs(nowMs);

    for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
        // **ID を名乗れるチャンネルだけが FEEDBACK を送る**（仕様書 §2.2）。
        if (isChannelConfigured(ch) && g_feedbackTimer[ch].due(nowMs, g_feedbackIntervalMs)) {
            sendFeedback(ch, nowMs);
        }
    }

    // 仕様書 §3.4: 版番号の自己申告。起動時 1 回ではなく低頻度で送り続けるのは、
    // PC が基板より後から起動しても拾えるようにするため。
    // **1 反復 1 通**（理由は g_infoPendingCh の宣言に付けてある）。
    if (g_infoTimer.due(nowMs, kInfoIntervalMs)) {
        g_infoPendingCh = 0;
    }
    if (g_infoPendingCh < kSolenoidChannelCount) {
        // **ID を名乗れないチャンネルは 1 通も送らない**（仕様書 §2.2）。飛ばして次へ進む。
        if (!isChannelConfigured(g_infoPendingCh)) {
            ++g_infoPendingCh;
        } else if (sendInfo(g_infoPendingCh)) {
            ++g_infoPendingCh;
        }
        // 送信に失敗した ch はインデックスを進めず、次の反復で送り直す。
    }

    updateLed(nowMs);
}
