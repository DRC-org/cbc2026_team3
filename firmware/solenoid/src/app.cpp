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
#include "SerialLineBuffer.h"
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
// **CAN との衝突だけを見てはならない。** かつて DC 用の config.h は CAN ピンとの
// 衝突しか検査しておらず、`DIS` として LOW/HIGH を振っていた D7 が実機では ch2 の
// 方向ピンだった、という食い違いをビルドが素通しにした。
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
PeriodicTimer g_blinkTimer;

// FEEDBACK の送信周期は基板全体で 1 つ（チャンネルごとに変えると位相の分散が崩れる）。
uint16_t g_feedbackIntervalMs = kDefaultFeedbackIntervalMs;

bool g_canFailed = false;
bool g_ledOn = false;

#if ENABLE_SERIAL_DEBUG
char g_serialStorage[24];
SerialLineBuffer g_serialLine(g_serialStorage, sizeof(g_serialStorage));
bool g_serialOverride = false;
#endif

// ===========================================================================
// GPIO
// ===========================================================================

// Port（config.h の HAL 非依存な enum）から HAL のポートへの変換はここだけが持つ。
GPIO_TypeDef *portOf(Port port) { return port == Port::A ? GPIOA : GPIOB; }

// 仕様書 §2.2: オフセット適用後のデバイス ID が 0x00 のチャンネルは駆動しない。
bool isChannelConfigured(uint8_t ch) { return g_deviceId[ch] != kDeviceIdUnconfigured; }

// **電磁弁への出力はすべてこの関数を通す。** ID 未設定と安全機構のどちらも
// 迂回する経路を作らないため（仕様書 §9.4）。
void applyChannelOutput(uint8_t ch, uint32_t nowMs) {
    if (!isChannelConfigured(ch)) {
        // 設定ミスで意図しない弁が開くより、開かない方が安全。
        return;
    }
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

uint8_t buildStatusFlags(uint8_t ch, uint32_t nowMs) {
    // 緊急停止ラッチ / ウォッチドッグのビットの判定は MotorSafety に集約されている。
    // ここで手で組み立て直すと、他の 2 枚と同じ条件で立つ保証が無くなる。
    uint8_t flags = g_channel[ch].safetyStatusFlags(nowMs);
    if (!isChannelConfigured(ch)) {
        flags |= status_flag::kDeviceIdUnconfigured;
    }
    // **到達フラグは立てない**（仕様書 §9.3）。弁が実際に開いたかを観測する手段が
    // 1 つも無いので、「指令したから到達した」と報告するのは実測でも推定でもない嘘になる。
    // 断線したソレノイドも抜けたコネクタも「到達」と報告されてしまう。
    return flags;
}

// 送信は空きメールボックスが無ければ諦める。**待ってはならない** — 詰まった
// バスの上で loop() が止まると、ウォッチドッグ満了の反映も出力の更新も止まる。
// FEEDBACK は次の周期でまた送られるので、1 通落ちても PC 側の STALE 判定
// （既定 500ms）には遠く届かない。
void sendFrame(uint16_t canId, uint8_t *data, uint8_t length) {
    if (HAL_CAN_GetTxMailboxesFreeLevel(&hcan) == 0) {
        return;
    }
    CAN_TxHeaderTypeDef header{};
    header.StdId = canId;
    header.ExtId = 0;
    header.IDE = CAN_ID_STD;
    header.RTR = CAN_RTR_DATA;
    header.DLC = length;
    header.TransmitGlobalTime = DISABLE;

    uint32_t mailbox = 0;
    HAL_CAN_AddTxMessage(&hcan, &header, data, &mailbox);
}

void sendFeedback(uint8_t ch, uint32_t nowMs) {
    // **この基板は位置を持たないので状態フラグ 1 バイトだけ**（仕様書 §3.2）。
    // 常に 0 の位置を詰めても、PC には「測ったように見える 0」が届くだけ。
    uint8_t data[kFeedbackFlagsOnlyLength];
    const uint8_t len = encodeFeedback(data, buildStatusFlags(ch, nowMs));

    // 緊急停止中・ウォッチドッグ作動中も送り続ける。止めると PC 側が STALE になり、
    // なぜ動かないのかを操縦者が判別できなくなる。
    sendFrame(buildCanId(CommandType::Feedback, g_deviceId[ch]), data, len);
}

// 仕様書 §3.4: 焼き忘れた基板をセッティングタイムに見つけるための自己申告。
// 低頻度（1Hz）で送るので、PC が後から起動しても拾える。
void sendInfo(uint8_t ch) {
    uint8_t data[kInfoLength];
    const uint8_t len = encodeInfo(data, kFirmwareVersion, kBoardKind, SlotKind::Actuator);
    sendFrame(buildCanId(CommandType::Info, g_deviceId[ch]), data, len);
}

void applyParam(uint8_t ch, const SetParamCommand &cmd) {
    switch (cmd.id) {
        case ParamId::CommandTimeoutMs:
            // 猶予に上限が無いと、仕様書 §5.1 が守っている最後の砦が SET_PARAM
            // 1 フレームで実質外れる。範囲の根拠は MotorCanProtocol が持つ。
            g_channel[ch].setCommandTimeoutMs(clampCommandTimeoutMs(cmd.raw));
            break;
        case ParamId::FeedbackIntervalMs:
            g_feedbackIntervalMs = clampFeedbackIntervalMs(cmd.raw);
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
            // 仕様書 §9.2: この基板は on_off のみ受理する。
            // position の 90.0[deg] や duty の 0.3 を「非 0 = ON」として解釈すると、
            // 別の基板宛のつもりで書いた値で弁が開く。
            if (cmd.type != ControlType::OnOff) {
                return;
            }
#if ENABLE_SERIAL_DEBUG
            g_serialOverride = false;
#endif
            // setOn は緊急停止ラッチ中・ウォッチドッグ満了中は受け付けない。
            g_channel[ch].setOn(cmd.raw != 0, nowMs);
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
                g_serialOverride = false;
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

// **PC → モタドラ方向のフレームだけを通す**（コマンド種別 0-2）。
//
// 全通過にすると、共有バス上の FEEDBACK（自作モタドラ 14 台 × 100Hz）まで受信
// FIFO（深さ 3）へ流れ込み、loop() が一瞬でも伸びた隙に取りこぼす。落ちるのが
// E_STOP や SET_TARGET だと、症状は「たまに指令が効かない」という最も追いにくい形になる。
//
// Standard ID は上位 3bit がコマンド種別（仕様書 §2.1）。1 本のマスクでは 0/1/2 の
// 3 値を表せないので、0x000-0x1FF と 0x200-0x2FF の 2 バンクに分ける。
// bxCAN の 32bit スケールでは STID が FilterIdHigh の bit15..5 に載る。
bool configureCanFilters() {
    CAN_FilterTypeDef filter{};
    filter.FilterMode = CAN_FILTERMODE_IDMASK;
    filter.FilterScale = CAN_FILTERSCALE_32BIT;
    filter.FilterFIFOAssignment = CAN_FILTER_FIFO0;
    filter.FilterActivation = CAN_FILTER_ENABLE;
    filter.SlaveStartFilterBank = 14;

    // E_STOP(0b000) と SET_TARGET(0b001): 上位 2bit が 00
    filter.FilterBank = 0;
    filter.FilterIdHigh = static_cast<uint16_t>(0x000 << 5);
    filter.FilterIdLow = 0;
    filter.FilterMaskIdHigh = static_cast<uint16_t>(0x600 << 5);
    filter.FilterMaskIdLow = 0;
    if (HAL_CAN_ConfigFilter(&hcan, &filter) != HAL_OK) {
        return false;
    }

    // SET_PARAM(0b010): 上位 3bit が 010
    filter.FilterBank = 1;
    filter.FilterIdHigh = static_cast<uint16_t>(0x200 << 5);
    filter.FilterMaskIdHigh = static_cast<uint16_t>(0x700 << 5);
    return HAL_CAN_ConfigFilter(&hcan, &filter) == HAL_OK;
}

// ===========================================================================
// デバイス ID（DIP スイッチ = チャンネル表全体へのブロックオフセット）
// ===========================================================================

// DIP の添字を readDipSwitch へ渡し、読み出しはラムダが (port, pin) へ引き直す。
// 負論理とビット順の対応、はみ出しの扱いは MotorCanRouter / makeDeviceId が持つ
// （native テストで守られている）。
const uint8_t kDipIndices[kDipBitCount] = {0, 1, 2, 3};

void resolveDeviceIds() {
    const uint8_t boardNumber = readDipSwitch(
        kDipIndices, kDipBitCount,
        [](uint8_t index) {
            return static_cast<int>(HAL_GPIO_ReadPin(portOf(kDipPorts[index]), kDipPins[index]));
        },
        kDipActiveLevel);

    for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
        g_deviceId[ch] = makeDeviceId(kBoardKind, boardNumber, ch);
    }
}

// ===========================================================================
// LED
// ===========================================================================

void updateLed(uint32_t nowMs) {
#if HAS_STATUS_LED
    bool unconfigured = false;
    bool stopped = false;
    for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
        if (!isChannelConfigured(ch)) {
            unconfigured = true;
        }
        if ((g_channel[ch].safetyStatusFlags(nowMs) & status_flag::kEStop) != 0) {
            stopped = true;
        }
    }

    // LED は 1 本しかないので、色ではなく点滅の速さが状態を伝える唯一の手段になる。
    //   速い（200ms）  … CAN 不通 / デバイス ID 未設定。今すぐ直さないと使えない
    //   中間（500ms）  … 緊急停止ラッチ中。直す対象ではないが動かない
    //   遅い（1000ms） … 平常のハートビート
    uint32_t interval = kHeartbeatIntervalMs;
    if (g_canFailed || unconfigured) {
        interval = kUnconfiguredBlinkIntervalMs;
    } else if (stopped) {
        interval = kEStopBlinkIntervalMs;
    }

    if (!g_blinkTimer.due(nowMs, interval)) {
        return;
    }
    g_ledOn = !g_ledOn;
    HAL_GPIO_WritePin(portOf(kPortLed), kPinLed, g_ledOn ? GPIO_PIN_SET : GPIO_PIN_RESET);
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
        const char *line = g_serialLine.line();

        if (line[0] == 's' || line[0] == 'S') {
            g_serialOverride = false;
            for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
                g_channel[ch].hold();
            }
        } else {
            // チャンネル番号と状態が空白で区切られていない行は捨てる。
            // 番号を読み違えると別の弁が開くので、曖昧な入力は指令にしない。
            char *sep = nullptr;
            const long ch = strtol(line, &sep, 10);
            if (sep != line && *sep == ' ' && ch >= 0 &&
                ch < static_cast<long>(kSolenoidChannelCount)) {
                g_channel[ch].setOn(strtol(sep + 1, nullptr, 10) != 0, nowMs);
                g_serialOverride = true;
            }
        }
    }

    // シリアル操作中はウォッチドッグを養い続ける。1 回だけ養う実装だと
    // command_timeout_ms 後に必ず止まってデバッグにならない。
    // 'S' 入力・CAN の SET_TARGET・電源断のいずれでもこのモードは解除される。
    if (g_serialOverride) {
        for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
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
    // 何よりも先に出力を消磁側へ倒す。GPIO は MX_GPIO_Init() が RESET で初期化
    // しているが、ここを省くと初期化順を変えたときに「通電したまま起動する」
    // 経路が黙って生まれる。
    for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
        HAL_GPIO_WritePin(portOf(kSolenoidChannels[ch].port), kSolenoidChannels[ch].pin,
                          GPIO_PIN_RESET);
    }
    HAL_GPIO_WritePin(portOf(kPortLed), kPinLed, GPIO_PIN_RESET);

    // 実効デバイス ID を確定させてから CAN を開ける。
    // ID 未設定のチャンネルは applyChannelOutput が触らないので、
    // 設定ミスの基板は 1 本も弁を開かない（仕様書 §2.2）。
    resolveDeviceIds();

    const uint32_t startMs = HAL_GetTick();

    for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
        // 全チャンネルが同じ周期で同時に送ると 6 フレームのバーストになり、他バスの
        // 周期送信と重なったときに調停待ちが伸びて FEEDBACK の間隔が波打つ。
        // 周期を等分した位相をチャンネルごとにずらして平準化する。
        // ID 未設定のチャンネルも「デバイス ID 未設定」を知らせるために送る。
        g_feedbackTimer[ch].setLastMs(startMs - g_feedbackIntervalMs +
                                      (g_feedbackIntervalMs * ch) / kSolenoidChannelCount);
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
        if (g_feedbackTimer[ch].due(nowMs, g_feedbackIntervalMs)) {
            sendFeedback(ch, nowMs);
        }
    }

    // 仕様書 §3.4: 版番号の自己申告。起動時 1 回ではなく低頻度で送り続けるのは、
    // PC が基板より後から起動しても拾えるようにするため。
    if (g_infoTimer.due(nowMs, kInfoIntervalMs)) {
        for (uint8_t ch = 0; ch < kSolenoidChannelCount; ++ch) {
            if (isChannelConfigured(ch)) {
                sendInfo(ch);
            }
        }
    }

    updateLed(nowMs);
}
