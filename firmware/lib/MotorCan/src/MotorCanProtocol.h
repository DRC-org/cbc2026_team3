// 自作モータドライバ CAN プロトコルの符号化・復号層。
// 単一情報源は docs/motor_driver_can_protocol.md であり、PC 側 lib/drivers/generic.py と
// 対になる。片方だけを変更してはならない。
//
// Arduino.h を include しないのは意図的で、native 環境（pio test -e native）で
// そのままコンパイルしてテストできるようにするため。DC 用・サーボ用・電磁弁用のファームで共有する。

#pragma once

#include <stdint.h>

namespace motorcan {

// 仕様書 §2.1。**値は CAN の調停順（小さいほど優先）に合わせてある。**
// 止めるためのフレームが目標値やフィードバックに追い越されてはならない。
// 0b101-0b111 は予約で、ここに載せてはならない（予約値を有効扱いすると PC 側
// parse_can_id が例外を投げ、そのバスの受信ループが停止して全モータが STALE になる）。
enum class CommandType : uint8_t {
    EStop = 0,      // PC → モタドラ。最優先
    SetTarget = 1,  // PC → モタドラ
    SetParam = 2,   // PC → モタドラ
    Feedback = 3,   // モタドラ → PC
    Info = 4,       // モタドラ → PC。低頻度の自己申告（仕様書 §3.4）
};

// 仕様書 §4。
enum class ControlType : uint8_t {
    Position = 0,
    Velocity = 1,
    Duty = 2,
    // 電磁弁用。0 = OFF / 非 0 = ON でしきい値は無い（仕様書 §9.2）。
    // duty を流用しないのは、「duty 0.3 の電磁弁」という意味を持たない指令を
    // 構造的に作れなくするため。duty を受け付けると、DC 基板宛のつもりで書いた
    // 値が電磁弁を全開にし、しかも PC 側の config からは正当な値に見える。
    OnOff = 3,
};

// 仕様書 §3.3 のパラメータ ID。**穴を空けずに詰める。**
// 「将来のための予約」を挟むと、対応表を読むたびに使われていない ID を数えることになる。
// 必要になった時点で末尾へ足せばよい。
enum class ParamId : uint8_t {
    MaxDuty = 0x00,             // DC 用のみ
    CommandTimeoutMs = 0x01,    // 両方
    FeedbackIntervalMs = 0x02,  // 両方
    ReachedTolerance = 0x03,    // サーボ用のみ
    SlewRate = 0x04,            // サーボ用のみ
    AngleMin = 0x05,            // サーボ用のみ
    AngleMax = 0x06,            // サーボ用のみ
};

// 仕様書 §3.2 FEEDBACK Byte0 の状態フラグ。**頭から詰める。**
// 空きを挟むと、報告できる項目が増えたときに「空いているビットがあるのに末尾へ足す」
// ことになり、対応表が読みにくくなる。
namespace status_flag {
constexpr uint8_t kReached = 1 << 0;
constexpr uint8_t kEStop = 1 << 1;
constexpr uint8_t kWatchdog = 1 << 2;
constexpr uint8_t kDeviceIdUnconfigured = 1 << 3;
// 基板上のセンサ入力（タッチセンサ等）。**センサは自分のデバイス ID で FEEDBACK を送る。**
// サーボのフレームに相乗りさせると、相乗り先の無い「センサだけの基板」が成立しない。
constexpr uint8_t kSensor = 1 << 4;
// **電源投入後まだ SET_TARGET を 1 通も受けていない**（仕様書 §5.1 / §5.4）。
// これが無いと基板の再起動が PC から見えない。サーボ基板は setup() で config.h の
// 初期角へ駆動するので、試合中の瞬断は「機構が勝手に飛ぶ」形で現れるのに、
// FEEDBACK 上は何事もなかったように見える（ウォッチドッグのビットは
// 「一度でも受けた後の満了」でしか立たない）。
constexpr uint8_t kNeverCommanded = 1 << 5;
// bit6-7 は予約。
}  // namespace status_flag

// 仕様書 §2.2。デバイス ID は固定ビット分割で、帯も刻み幅も要らない。
//
//   Bit7..6 : 基板種別（0=予約 / 1=サーボ / 2=DC / 3=電磁弁）
//   Bit5..3 : 基板番号（DIP そのもの。0-7）
//   Bit2..0 : スロット番号（0-7）
//
// 種別 0 を予約にしてあるので 0x00（未設定）は自動的に空く。ID を見ればどの基板の
// どのスロットかが直接読めるので、candump を眺めているときに対応表を引かなくてよい。
//
// 0xFF（ブロードキャスト）だけは電磁弁基板の「基板番号 7 × スロット 7」と重なるので、
// makeDeviceId() がこの 1 個を未設定へ倒す（仕様書 §2.2）。
enum class BoardKind : uint8_t {
    Servo = 1,
    Dc = 2,
    Solenoid = 3,
};

constexpr uint8_t kBoardKindShift = 6;
constexpr uint8_t kBoardNumberShift = 3;
constexpr uint8_t kMaxBoardNumber = 7;
constexpr uint8_t kMaxSlotNumber = 7;

// スロットの役割（INFO で自己申告する。仕様書 §3.4）。
enum class SlotKind : uint8_t {
    Actuator = 0,
    Sensor = 1,
};

// 仕様書 §2.2。0x00 は「DIP 設定忘れ」とみなして駆動を拒否する。
constexpr uint8_t kDeviceIdUnconfigured = 0x00;
constexpr uint8_t kDeviceIdBroadcast = 0xFF;
constexpr uint16_t kBroadcastEStopCanId = 0x0FF;

// デバイス ID を組み立てる。範囲外は未設定（駆動拒否）へ倒す。
// 黙って丸めると、DIP を回しすぎた基板が別の基板の ID を名乗る。
uint8_t makeDeviceId(BoardKind board, uint8_t boardNumber, uint8_t slot);

// ---------------------------------------------------------------------------
// 固定小数点（仕様書 §4）
// ---------------------------------------------------------------------------

// **CAN 上を流れる数値はすべて int16 の固定小数点で、float は 1 バイトも流れない。**
// float32 をやめたのは、NaN の防御をプロトコル全体から消すため。NaN は比較がすべて
// false になるので、クランプも範囲チェックも素通りし、一度内部へ入ると「無言で
// 止まったモータ」になる（診断ビットも到達フラグも立たない）。整数なら
// その失敗クラスごと存在しない。8bit MCU でソフトウェア浮動小数点を踏まない利点もある。
constexpr int32_t kAngleScale = 10;     // 0.1deg 単位
constexpr int32_t kDutyScale = 10000;   // 1/10000 単位（duty -1.0～+1.0）
constexpr int32_t kRateScale = 10;      // 0.1deg/s 単位

// float を固定小数点の int16 へ。**NaN と範囲外はここで飽和させる。**
// CAN 経路は int16 しか運ばないので NaN は入らない。float が入るのはシリアル
// デバッグだけなので、その唯一の入口をここに集約する。
int16_t toRaw(float value, int32_t scale);
float fromRaw(int16_t raw, int32_t scale);

// int16 に収まらない値は折り返さず飽和させる。
// キャストで折り返すと +4000deg が負値に化け、PC 側が逆方向へ位置制御しかねない。
int16_t saturateToInt16(int32_t value);

// ---------------------------------------------------------------------------
// CAN ID
// ---------------------------------------------------------------------------

// 仕様書 §2.1 の CAN ID レイアウト。上位 3bit がコマンド種別、下位 8bit がデバイス ID。
constexpr uint8_t kCommandTypeShift = 8;
constexpr uint16_t kCommandTypeMask = static_cast<uint16_t>(0x7u << kCommandTypeShift);

constexpr uint16_t commandIdBase(CommandType command) {
    return static_cast<uint16_t>(static_cast<uint16_t>(command) << kCommandTypeShift);
}

uint16_t buildCanId(CommandType command, uint8_t deviceId);

// ---------------------------------------------------------------------------
// 受信フィルタ（PC → モタドラ方向だけを通す）
// ---------------------------------------------------------------------------

// **CommandType のビット配置を基板側に再宣言させないためにある。**
// 電磁弁基板の bxCAN フィルタは `0x000 << 5` / `0x600 << 5` / `0x200 << 5` /
// `0x700 << 5` というリテラルで「上位 3bit が種別」「E_STOP=0b000 /
// SET_TARGET=0b001 / SET_PARAM=0b010」を再宣言しており、CommandType への参照が
// 1 つも無かった。**この enum は実際に一度動いている**（かつて E_STOP は 0b111 で、
// ブロードキャスト停止が最も優先度の低い ID だった。§2.1）。次に動かすと DC 用と
// サーボ用は parseCanId 経由で自動追従するが、電磁弁だけがフィルタで E_STOP を
// 落とし始める。症状は「電磁弁だけ緊急停止が効かない」で、FEEDBACK は流れ続けるので
// **PC からは正常に見える**。
//
// マスクをハードウェアのレジスタへどう載せるか（bxCAN の 32bit スケールでは
// STID が FilterIdHigh の bit15..5）は MCU 固有なので、そこは呼び出し側に残す。
struct CanIdFilter {
    uint16_t id;
    uint16_t mask;  // このビットが id と一致するフレームだけを通す
};

constexpr bool canIdPassesFilter(const CanIdFilter &filter, uint16_t canId) {
    return (canId & filter.mask) == filter.id;
}

// E_STOP と SET_TARGET は値が隣接するので 1 バンクで通す。**「隣接している」ことを
// 式で表す** —— 2 つの ID の差分ビットをマスクから落とせば、値が動いても追随する。
constexpr CanIdFilter kEStopAndSetTargetFilter{
    commandIdBase(CommandType::EStop),
    static_cast<uint16_t>(kCommandTypeMask & ~(commandIdBase(CommandType::EStop) ^
                                               commandIdBase(CommandType::SetTarget)))};

constexpr CanIdFilter kSetParamFilter{commandIdBase(CommandType::SetParam), kCommandTypeMask};

constexpr bool passesPcToBoardFilters(uint16_t canId) {
    return canIdPassesFilter(kEStopAndSetTargetFilter, canId) ||
           canIdPassesFilter(kSetParamFilter, canId);
}

// **CommandType の値を動かしたらここでビルドが落ちる。** 落ちない形にすると、
// 電磁弁基板だけが緊急停止を取りこぼす状態が実機まで見えない。
static_assert(passesPcToBoardFilters(commandIdBase(CommandType::EStop)),
              "E_STOP が受信フィルタを通らない（電磁弁基板だけ緊急停止が効かなくなる）");
static_assert(passesPcToBoardFilters(commandIdBase(CommandType::SetTarget)),
              "SET_TARGET が受信フィルタを通らない");
static_assert(passesPcToBoardFilters(commandIdBase(CommandType::SetParam)),
              "SET_PARAM が受信フィルタを通らない");
// モタドラ → PC 方向は落とす。通すと共有バス上の FEEDBACK（14 台 × 100Hz）が
// 受信 FIFO（深さ 3）へ流れ込み、loop() が一瞬伸びた隙に E_STOP を取りこぼす。
static_assert(!passesPcToBoardFilters(commandIdBase(CommandType::Feedback)),
              "FEEDBACK が受信フィルタを通ってしまう");
static_assert(!passesPcToBoardFilters(commandIdBase(CommandType::Info)),
              "INFO が受信フィルタを通ってしまう");
static_assert(!passesPcToBoardFilters(static_cast<uint16_t>(0x5u << kCommandTypeShift)) &&
                  !passesPcToBoardFilters(static_cast<uint16_t>(0x6u << kCommandTypeShift)) &&
                  !passesPcToBoardFilters(static_cast<uint16_t>(0x7u << kCommandTypeShift)),
              "予約コマンド種別が受信フィルタを通ってしまう");

struct CanIdInfo {
    CommandType command;
    uint8_t deviceId;
    bool valid;  // 予約コマンド種別・11bit 超過の ID では false
};

CanIdInfo parseCanId(uint16_t canId);

// ---------------------------------------------------------------------------
// スカラのバイト列変換（リトルエンディアン）
// ---------------------------------------------------------------------------

void packInt16Le(uint8_t *dst, int16_t value);
int16_t unpackInt16Le(const uint8_t *src);

// ---------------------------------------------------------------------------
// 受信フレームの復号（PC → モタドラ）
// ---------------------------------------------------------------------------

// 目標値は生の int16 のまま返す。単位（角度か duty か）は制御タイプで決まるので、
// 意味を与えるのは受け取った基板の役目（仕様書 §4）。
struct SetTargetCommand {
    ControlType type;
    int16_t raw;
    bool valid;
};
SetTargetCommand decodeSetTarget(const uint8_t *data, uint8_t length);

struct SetParamCommand {
    ParamId id;
    int16_t raw;
    bool valid;  // 未知のパラメータ ID は false（仕様書 §3.3: 無視する）
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

// 戻り値は DLC。**全基板が必ず持つ状態フラグを先頭に置き、位置は持つ基板だけが足す。**
// 逆順（フラグを末尾）にすると、位置を持たない基板も 8 バイト送ることになる。
constexpr uint8_t kFeedbackFlagsOnlyLength = 1;
constexpr uint8_t kFeedbackWithPositionLength = 3;
uint8_t encodeFeedback(uint8_t *out, uint8_t flags);
uint8_t encodeFeedback(uint8_t *out, uint8_t flags, int32_t position_0p1deg);

// FEEDBACK Byte0 を組み立てる**唯一の場所**（仕様書 §3.2）。
//
// 3 枚の main.cpp / app.cpp が各自でフラグを OR していた頃、ここの規則は
// Arduino / HAL の翻訳単位にあって native テストが 1 件も届かなかった。
// **DC 基板の buildStatusFlags() に `flags |= kReached;` を 1 行足しても
// 131 件すべて緑**という状態で、仕様書 §9.3 が「断線したソレノイドも到達と
// 報告される」と警告している不変条件を誰も検査していなかった。
//
// 規則は 2 つ。どちらも「観測手段を持たない基板に測ったように見える値を
// 運ばせない」ことが理由で、板ごとの例外を作ってはならない。
//
//   - **到達フラグを立てられるのはサーボスロットだけ**（仕様書 §7.3 の推定値）。
//     DC 基板はエンコーダを持たず（§3.2 / §8）、電磁弁基板は弁が開いたかを
//     観測できない（§9.3）ので、reached に何を渡しても立たない
//   - **センサスロットは緊急停止・ウォッチドッグ・到達を立てない**（仕様書 §5.2）。
//     駆動されないので意味を持たず、立てると PC 側 check_safety_error() が
//     「駆動できない状態」と読んで動作確認を打ち切る
//
// safetyFlags は MotorSafety::statusFlags() の戻り値をそのまま渡すこと
// （緊急停止 / ウォッチドッグ / 起動後未受信の判定はあちらが単独で持つ）。
uint8_t composeFeedbackFlags(BoardKind board, SlotKind slot, uint8_t safetyFlags,
                             bool configured, bool reached, bool sensorActive);

// 仕様書 §3.4。焼き忘れた基板をセッティングタイムに見つけるための自己申告。
//
// **DLC 可変。** サーボスロットだけが可動レンジ（仕様書 §3.4 の Byte3-4）を足す。
// FEEDBACK で位置を持つ基板だけが位置を足すのと同じ形で、**測る対象を持たない基板に
// 0 を運ばせない**（常に 0 の値は PC 側に「測ったように見える 0」として届く）。
//
// レンジを申告するのは、**180 度サーボと 270 度サーボの取り違えが CAN 越しに
// 一切見えない**ため（仕様書 §7.7）。角度 → パルス幅の変換は config.h の
// ServoPulseSpec にしかなく、270 度用の設定で 180 度サーボを回すと指令の 1.5 倍
// 動くのに、FEEDBACK が返すのはクランプ後の指令角なので PC には正常に見える。
constexpr uint8_t kInfoBaseLength = 3;
constexpr uint8_t kInfoWithServoRangeLength = 5;
uint8_t encodeInfo(uint8_t *out, uint8_t firmwareVersion, BoardKind board, SlotKind slot);
uint8_t encodeInfo(uint8_t *out, uint8_t firmwareVersion, BoardKind board, SlotKind slot,
                   float angleRangeDeg);

// ---------------------------------------------------------------------------
// SET_TARGET / SET_PARAM の値域（仕様書 §3.3 / §5.3）
// ---------------------------------------------------------------------------

// 仕様書 §3.3 の既定値のうち、PC 側との契約になっているもの。
// command_timeout_ms は PC 側の目標値再送周期の根拠であり、feedback_interval_ms は
// PC 側の STALE 判定が前提にしている送信周期。基板ごとに変えてよい値ではないので、
// 基板の config.h ではなくここが単一定義を持つ。
constexpr uint16_t kDefaultCommandTimeoutMs = 500;
constexpr uint16_t kDefaultFeedbackIntervalMs = 10;  // 100Hz

// command_timeout_ms に上限が無いと、CAN の 1 フレームでウォッチドッグを実質無効に
// できてしまう。仕様書 §5.1 が「WATCHDOG_ENABLED に SET_PARAM の ID は無い」と書いて
// 最後の砦を守っているのに、猶予そのものを伸ばせば同じ結果になる。上限は既定の 4 倍で、
// これを超えると「PC が落ちてもコンベアが数秒回り続ける」ことになり砦として機能しない。
// 下限は PC 側の再送周期の目安（既定 500ms に対して 50ms）。それより短い猶予は、
// 契約どおり再送している健全な機体を止めるだけで安全性を上げない。
constexpr uint16_t kMinCommandTimeoutMs = 50;
constexpr uint16_t kMaxCommandTimeoutMs = 2000;

// 0 は送信が詰まってバスを埋める。上限側は、極端に長い周期にすると PC からは
// 「基板が死んだ（STALE）」ようにしか見えず、原因の切り分けができなくなる。
constexpr uint16_t kMinFeedbackIntervalMs = 1;
constexpr uint16_t kMaxFeedbackIntervalMs = 1000;

// 範囲外は境界値へ丸める（書いた値に近い側で動かす方が現場で挙動を推測しやすい）。
// 負値は下限へ。int16 なので float32 のような未定義動作の心配は無い。
uint16_t clampCommandTimeoutMs(int16_t raw);
uint16_t clampFeedbackIntervalMs(int16_t raw);

// 3 枚に共通する SET_PARAM 2 件（仕様書 §3.3 の `0x01` / `0x02`）を処理する。
// 処理したら true を返すので、各基板の applyParam は先頭でこれを呼び、
// false のときだけ自分固有の ID を見る。
//
// 引数の Channel は DcChannel / ServoChannel / SolenoidChannel のどれか。
// テンプレートにしてあるのは、この 3 つに共通の基底型が無いのと、ファームごとに
// 1 種類しか実体化されないので Nano でもコード量が増えないため。
//
// **feedback_interval_ms は基板全体で 1 つ**（チャンネルごとに変えると
// PeriodicTimer::stagger が割り当てた送信位相の分散が崩れる）。
// **command_timeout_ms はチャンネル単位**（宛先がデバイス ID ＝ チャンネルなので、
// 1 チャンネルへの指令が途絶えても他は動き続ける）。この非対称を 3 箇所に書き写すと、
// いつか片方だけが逆になる。
template <typename Channel>
bool applyCommonParam(const SetParamCommand &cmd, Channel &channel,
                      uint16_t &feedbackIntervalMs) {
    switch (cmd.id) {
        case ParamId::CommandTimeoutMs:
            // 猶予に上限が無いと、仕様書 §5.1 が守っている最後の砦が SET_PARAM
            // 1 フレームで実質外れる（49.7 日の猶予 = 無効化）。範囲の根拠は上の定数。
            channel.setCommandTimeoutMs(clampCommandTimeoutMs(cmd.raw));
            return true;
        case ParamId::FeedbackIntervalMs:
            feedbackIntervalMs = clampFeedbackIntervalMs(cmd.raw);
            return true;
        default:
            return false;
    }
}

// ---------------------------------------------------------------------------
// duty
// ---------------------------------------------------------------------------

// duty を [-maxDuty, +maxDuty] に収める（仕様書 §5.3）。maxDuty も 0.0-1.0 に丸める。
float clampDuty(float duty, float maxDuty);

// duty を「PWM に出す大きさ」と「方向ピンを倒す向き」に分ける。
// 実基板の出力段は PWM 1 本 + 方向 1 本で、符号の扱いを main.cpp に置くと
// ペリフェラルに埋まって native テストが掛からない。duty 0 で reverse が
// true になると、停止指令のたびに方向ピンが反転して機構に衝撃が入る。
struct DutyOutput {
    float magnitude;  // 0.0–1.0（clampDuty 済みの絶対値）
    bool reverse;     // duty < 0 のときだけ true
};
DutyOutput splitDuty(float duty, float maxDuty);

}  // namespace motorcan
