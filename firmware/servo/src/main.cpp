// サーボ用自作モタドラのファームウェア本体（Arduino UNO R4 / RA4M1）。
//
// プロトコルの単一情報源は docs/motor_driver_can_protocol.md（特に §7）。
// 機体依存の定数はすべて include/config.h にある。
//
// 責務の分割:
//   MotorCan（Arduino 非依存）… フレームの符号化・復号、緊急停止ラッチ、ウォッチドッグ、角度補間
//   このファイル              … ペリフェラル初期化、チャンネル管理、CAN 送受信の配線
//
// DC 用（firmware/dc_motor）との違いは、サーボが位置フィードバックを持たないことに由来する。
//   - 1 枚の基板が複数チャンネルを持ち、チャンネルごとに独立したデバイス ID を持つ（§7.1）
//   - position モードのみ受理する（§7.2）
//   - 到達フラグは実測ではなくファームの推定値（§7.3）
//   - 緊急停止・ウォッチドッグでは脱力させず現在角を保持する（§7.5）

#include <Arduino.h>
#include <Arduino_CAN.h>

#include "MotorCanProtocol.h"
#include "MotorSafety.h"
#include "ServoMotion.h"
#include "config.h"
#include "pwm.h"

using namespace motorcan;

// ===========================================================================
// 配線の静的検証
// ===========================================================================

// サーボ出力ピンが CAN ペリフェラルのピンと重なると、CAN が上がらず PC から止められない
// 基板になる（もしくはサーボが動かない）。どちらも現場では原因が分かりにくいので、
// config.h を書き換えた時点でビルドを失敗させる。
static constexpr bool servoPinsAreSane() {
    for (uint8_t i = 0; i < kServoChannelCount; ++i) {
        if (kServoChannels[i].pin == PIN_CAN0_TX || kServoChannels[i].pin == PIN_CAN0_RX) {
            return false;
        }
        for (uint8_t j = static_cast<uint8_t>(i + 1); j < kServoChannelCount; ++j) {
            if (kServoChannels[i].pin == kServoChannels[j].pin) {
                return false;
            }
        }
    }
    return true;
}
static_assert(servoPinsAreSane(),
              "config.h のサーボ出力ピンが CAN のピンと衝突しているか、重複している");

// デバイス ID がチャンネル間で重複すると、1 つの SET_TARGET が複数のサーボを動かす。
static constexpr bool servoDeviceIdsAreUnique() {
    for (uint8_t i = 0; i < kServoChannelCount; ++i) {
        for (uint8_t j = static_cast<uint8_t>(i + 1); j < kServoChannelCount; ++j) {
            if (kServoChannels[i].deviceId == kServoChannels[j].deviceId) {
                return false;
            }
        }
    }
    return true;
}
static_assert(servoDeviceIdsAreUnique(), "config.h のチャンネル表でデバイス ID が重複している");

// 下の g_pwm / g_motion / g_safety は各チャンネルを明示的に初期化している。
// チャンネルを増やすときはそれらの初期化子も一緒に足すこと。
static_assert(kServoChannelCount == 3,
              "チャンネル数を変えたら g_pwm / g_motion / g_safety の初期化子も更新すること");

// ===========================================================================
// ペリフェラルと状態（すべてチャンネル単位）
// ===========================================================================

static PwmOut g_pwm[kServoChannelCount] = {
    PwmOut(kServoChannels[0].pin),
    PwmOut(kServoChannels[1].pin),
    PwmOut(kServoChannels[2].pin),
};

static ServoMotion g_motion[kServoChannelCount] = {
    ServoMotion(kServoChannels[0].initialAngleDeg, kServoChannels[0].limits),
    ServoMotion(kServoChannels[1].initialAngleDeg, kServoChannels[1].limits),
    ServoMotion(kServoChannels[2].initialAngleDeg, kServoChannels[2].limits),
};

// 仕様書 §5.4: 起動時の緊急停止ラッチは解除済み。§7.1 のとおり宛先がチャンネルなので
// ウォッチドッグもチャンネルごとに独立して動く。
static MotorSafety g_safety[kServoChannelCount] = {
    MotorSafety(kDefaultCommandTimeoutMs),
    MotorSafety(kDefaultCommandTimeoutMs),
    MotorSafety(kDefaultCommandTimeoutMs),
};

// DIP オフセット適用後の実効デバイス ID。0x00 なら駆動しない（§2.2 / §7.1）。
static uint8_t g_deviceId[kServoChannelCount] = {0, 0, 0};

// PwmOut::begin() を通したかどうか。ID 未設定チャンネルは begin() すらしない。
static bool g_pwmStarted[kServoChannelCount] = {false, false, false};

static uint32_t g_feedbackIntervalMs = kDefaultFeedbackIntervalMs;
static uint32_t g_lastFeedbackMs[kServoChannelCount] = {0, 0, 0};

static uint32_t g_lastMotionMs = 0;
static uint32_t g_lastBlinkMs = 0;
static bool g_ledOn = false;

#if ENABLE_SERIAL_DEBUG
// シリアルから角度を入力している間だけ true。
// CAN の SET_TARGET を受けたら解除して、PC の指令とシリアルが競合しないようにする。
static bool g_serialOverride = false;
static String g_serialLine;
#endif

// ===========================================================================
// 出力
// ===========================================================================

// 仕様書 §2.2 / §7.1: オフセット適用後のデバイス ID が 0x00 のチャンネルは駆動しない。
static bool isChannelConfigured(uint8_t ch) {
    return g_deviceId[ch] != kDeviceIdUnconfigured;
}

// WATCHDOG_ENABLED=0 のときは満了しないものとして扱う（FEEDBACK の bit4 も立てない）。
// PC 側の目標値定期再送が未実装のあいだの暫定運用のための逃げ道（仕様書 §5.1 / §8）。
static bool isWatchdogTripped(uint8_t ch, uint32_t nowMs) {
#if WATCHDOG_ENABLED
    return g_safety[ch].isExpired(nowMs);
#else
    (void)ch;
    (void)nowMs;
    return false;
#endif
}

static bool isDriveAllowed(uint8_t ch, uint32_t nowMs) {
    return !g_safety[ch].isLatched() && !isWatchdogTripped(ch, nowMs);
}

// サーボへのパルス出力はすべてこの関数を通す。
// ラッチ・ウォッチドッグ・ID 未設定・可動範囲クランプのいずれかを迂回する経路を作らないため
// （dc_motor の applyOutput() と同じ方針）。
static void applyChannelOutput(uint8_t ch, uint32_t nowMs) {
    if (!isChannelConfigured(ch) || !g_pwmStarted[ch]) {
        // 設定ミスで意図しないアクチュエータが動くより、動かない方が安全。
        // ID 未設定チャンネルは PwmOut::begin() を通していないのでパルスは 1 発も出ない。
        // begin() が失敗したチャンネル（PWM チャンネルの取り合い等）も同様に触らない。
        return;
    }

    if (!isDriveAllowed(ch, nowMs)) {
        // 仕様書 §7.5: 新しい角度指令の受け付けを止め、その時点の角度を保持し続ける。
        // 目標角を現在角へ落としておくことで、緊急停止を解除した瞬間に動き出すこともない
        // （DC 用が §3.5 で目標値を 0 に戻すのと同じ意図。サーボで目標 0 にすると
        //  angle_min まで振れてしまうので、代わりに現在角で凍結する）。
        g_motion[ch].holdHere(nowMs);

        if (kEStopDetach) {
            // 脱力させたい機構のための切り替え。既定は false。
            // パルス幅 0 でサーボへのフレームが消え、出力軸が back-drivable になる。
            g_pwm[ch].pulseWidth_us(0);
            return;
        }
    }

    // ここに来る角度は ServoMotion が angle_min / angle_max でクランプ済み（仕様書 §7.2）。
    // angleToPulseUs 側でもパルス幅を [minUs, maxUs] に収める二重の防壁になっている。
    const uint16_t pulseUs =
        angleToPulseUs(g_motion[ch].currentAngleDeg(), kServoChannels[ch].pulse);
    g_pwm[ch].pulseWidth_us(static_cast<int>(pulseUs));
}

// ===========================================================================
// 補間
// ===========================================================================

static void updateMotion(uint32_t nowMs) {
    for (uint8_t ch = 0; ch < kServoChannelCount; ++ch) {
        g_motion[ch].update(nowMs);
        applyChannelOutput(ch, nowMs);
    }
}

// ===========================================================================
// CAN
// ===========================================================================

static int8_t findChannel(uint8_t deviceId) {
    if (deviceId == kDeviceIdUnconfigured) {
        return -1;
    }
    for (uint8_t ch = 0; ch < kServoChannelCount; ++ch) {
        if (g_deviceId[ch] == deviceId) {
            return static_cast<int8_t>(ch);
        }
    }
    return -1;
}

static uint8_t buildStatusFlags(uint8_t ch, uint32_t nowMs) {
    uint8_t flags = 0;
    if (g_safety[ch].isLatched()) {
        flags |= status_flag::kEStop;
    }
    if (isWatchdogTripped(ch, nowMs)) {
        flags |= status_flag::kWatchdog;
    }
    if (g_motion[ch].isReached()) {
        // 仕様書 §7.3: これは実測ではなく補間の完了を示す推定値。
        // 脱調・過負荷・メカ干渉で実際には動いていなくても立つ。
        flags |= status_flag::kReached;
    }
    if (!isChannelConfigured(ch)) {
        flags |= status_flag::kDeviceIdUnconfigured;
    }
    // 仕様書 §7.4: 電流センスも温度センサも無いので bit1（過電流）/ bit2（過熱）は常に 0。
    return flags;
}

static void sendFeedback(uint8_t ch, uint32_t nowMs) {
    uint8_t data[kFrameLength];

    // 仕様書 §7.4: 位置は補間中の「指令角」であって実測ではない。単位は 0.1deg。
    const int32_t position = static_cast<int32_t>(lroundf(g_motion[ch].currentAngleDeg() * 10.0f));
    // スルーレート [deg/s] を出力軸 rpm へ。deg/s ÷ 360 × 60 = deg/s ÷ 6。
    const int32_t rpm = static_cast<int32_t>(lroundf(g_motion[ch].currentSlewDegPerSec() / 6.0f));

    encodeFeedback(data, position, rpm, 0, 0, buildStatusFlags(ch, nowMs));

    // 緊急停止中・ウォッチドッグ作動中も送り続ける。
    // 止めると PC 側が STALE になり、なぜ動かないのかを操縦者が判別できなくなる。
    // ID 未設定チャンネルは CAN ID 0x100 で送ることになり、複数チャンネルが未設定だと
    // 同じ ID のフレームが重複するが、PC 側に bit5（設定忘れ）を届ける方を優先する。
    const CanMsg msg(CanStandardId(buildCanId(CommandType::Feedback, g_deviceId[ch])),
                     kFrameLength, data);
    CAN.write(msg);
}

static void applyServoParam(uint8_t ch, const ServoParamCommand &cmd) {
    switch (cmd.id) {
        case ServoParamId::CommandTimeoutMs:
            g_safety[ch].setTimeoutMs(static_cast<uint32_t>(cmd.value));
            break;
        case ServoParamId::FeedbackIntervalMs:
            // 0 にすると送信が詰まってバスを埋めるので下限を置く。
            // 周期は基板全体で 1 つ（チャンネルごとに変えると位相の分散が崩れる）。
            g_feedbackIntervalMs = cmd.value < 1.0f ? 1u : static_cast<uint32_t>(cmd.value);
            break;
        case ServoParamId::ReachedTolerance:
            g_motion[ch].setReachedToleranceDeg(cmd.value);
            break;
        case ServoParamId::SlewRate: {
            ServoLimits limits = g_motion[ch].limits();
            limits.slewRateDegPerSec = cmd.value;
            g_motion[ch].setLimits(limits);
            break;
        }
        case ServoParamId::AngleMin: {
            ServoLimits limits = g_motion[ch].limits();
            limits.angleMinDeg = cmd.value;
            g_motion[ch].setLimits(limits);
            break;
        }
        case ServoParamId::AngleMax: {
            ServoLimits limits = g_motion[ch].limits();
            limits.angleMaxDeg = cmd.value;
            g_motion[ch].setLimits(limits);
            break;
        }
    }
}

static void handleChannelFrame(uint8_t ch, const CanIdInfo &info, const CanMsg &msg,
                               uint32_t nowMs) {
    switch (info.command) {
        case CommandType::SetTarget: {
            // 仕様書 §6: 緊急停止ラッチ中でもウォッチドッグは養う。
            // 養わないと解除した直後に満了済みで動かない。
            // 制御タイプが position でなくても養うのは、通信自体は生きているため。
            g_safety[ch].feed(nowMs);

            const SetTargetCommand cmd = decodeSetTarget(msg.data, msg.data_length);
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
            // setTarget が angle_min / angle_max でクランプする（§7.2）。
            g_motion[ch].setTarget(cmd.value, nowMs);
            break;
        }
        case CommandType::SetMode:
            // 仕様書 §7.2: モードは position 固定。SET_MODE は無視する。
            break;
        case CommandType::SetParam: {
            // 仕様書 §7.6: 0x04 / 0x05 / 0x07 / 0x10 / 0x11 / 0x12 のみ処理し、
            // kp / ki / kd / max_duty / overcurrent は制御則を持たないので無視する。
            const ServoParamCommand cmd = decodeServoSetParam(msg.data, msg.data_length);
            if (cmd.valid) {
                applyServoParam(ch, cmd);
            }
            break;
        }
        case CommandType::EStop: {
            const EStopAction action = g_safety[ch].handleEStopFrame(msg.data, msg.data_length);
            if (action != EStopAction::None) {
                // 停止でも解除でも、その場で現在角の保持へ倒す（§7.5）。
                applyChannelOutput(ch, nowMs);
#if ENABLE_SERIAL_DEBUG
                g_serialOverride = false;
#endif
            }
            break;
        }
        case CommandType::Feedback:
            // 他チャンネル・他基板の FEEDBACK。ID は衝突しないが念のため無視する。
            break;
    }
}

static void handleFrame(const CanMsg &msg) {
    // 本プロトコルは Standard Frame のみ（仕様書 §1）。
    if (!msg.isStandardId()) {
        return;
    }

    const CanIdInfo info = parseCanId(static_cast<uint16_t>(msg.getStandardId()));
    if (!info.valid) {
        // 予約コマンド種別（仕様書 §2.1）や他プロトコルの相乗り。無視する。
        return;
    }

    const uint32_t nowMs = millis();

    // ブロードキャスト E_STOP（0x7FF）は全チャンネルに効く（仕様書 §3.5）。
    if (info.command == CommandType::EStop && info.deviceId == kDeviceIdBroadcast) {
        for (uint8_t ch = 0; ch < kServoChannelCount; ++ch) {
            handleChannelFrame(ch, info, msg, nowMs);
        }
        return;
    }

    // 宛先指定のフレームは、チャンネル表に載っている ID のものだけを処理する（仕様書 §7.1）。
    const int8_t ch = findChannel(info.deviceId);
    if (ch < 0) {
        return;
    }
    handleChannelFrame(static_cast<uint8_t>(ch), info, msg, nowMs);
}

static void pollCan() {
    while (CAN.available()) {
        const CanMsg msg = CAN.read();
        handleFrame(msg);
    }
}

// ===========================================================================
// デバイス ID（DIP スイッチ = チャンネル表全体へのオフセット）
// ===========================================================================

// DC 用は「DIP の値がそのままデバイス ID」だが、サーボ用では意味が違う。
// 1 枚がチャンネルごとに別のデバイス ID を持つため（§7.1）、DIP は
// **チャンネル表全体に加えるオフセット**として働く。同一ファームの基板を複数枚使うとき、
// 2 枚目の DIP を 1 段上げるだけで全チャンネルの ID がまとめてずれる。
static uint8_t readDipOffset() {
    uint8_t offset = 0;
    for (uint8_t bit = 0; bit < 4; ++bit) {
        // INPUT_PULLUP の負論理: LOW = ON = 1
        if (digitalRead(kPinDip[bit]) == LOW) {
            offset |= static_cast<uint8_t>(1u << bit);
        }
    }
    return offset;
}

static void resolveDeviceIds() {
    const uint8_t offset = readDipOffset();
    for (uint8_t ch = 0; ch < kServoChannelCount; ++ch) {
        const uint8_t id = static_cast<uint8_t>(kServoChannels[ch].deviceId + offset);
        // 0x00 は「未設定」、0xFF は E_STOP のブロードキャスト用に予約されている（§2.2）。
        // オフセットの足し算で 8bit を回り込んだ結果どちらかになったチャンネルは駆動しない。
        g_deviceId[ch] = (id == kDeviceIdUnconfigured || id == kDeviceIdBroadcast)
                             ? kDeviceIdUnconfigured
                             : id;
    }
}

// ===========================================================================
// LED
// ===========================================================================

static void updateLed(uint32_t nowMs) {
#if HAS_STATUS_LED
    // ID 未設定のチャンネルがあれば速い点滅で知らせる（仕様書 §2.2 / §7.1）。
    bool unconfigured = false;
    for (uint8_t ch = 0; ch < kServoChannelCount; ++ch) {
        if (!isChannelConfigured(ch)) {
            unconfigured = true;
        }
    }
    const uint32_t interval = unconfigured ? kUnconfiguredBlinkIntervalMs : kHeartbeatIntervalMs;

    if (nowMs - g_lastBlinkMs < interval) {
        return;
    }
    g_lastBlinkMs = nowMs;
    g_ledOn = !g_ledOn;
    digitalWrite(kPinLed, g_ledOn ? HIGH : LOW);

#if HAS_RGB_LED
    // TODO(実機で確認): RGB LED ライブラリを platformio.ini の lib_deps に追加し、
    // ここで unconfigured=赤 / 緊急停止=橙 / 通常=緑 を出す。
    // 依存が取れない環境でもビルドが通るよう既定では無効にしてある。
#endif
#else
    // WiFi 基板では LED_BUILTIN(D13) が CAN RX と兼用で、握ると CAN 受信が死ぬ（config.h 参照）。
    (void)nowMs;
#endif
}

// ===========================================================================
// デバッグ用シリアル
// ===========================================================================

#if ENABLE_SERIAL_DEBUG

// 「<ch> <角度[deg]>」で 1 チャンネルへ角度を指令する。's' で全チャンネルを現在角に凍結。
// 角度は setTarget が可動範囲でクランプし、緊急停止ラッチ中は applyChannelOutput が
// 一括で出力を禁止するため、ここから安全機構を迂回することはできない（仕様書 §5.2 の要求）。
static void pollSerial(uint32_t nowMs) {
    while (Serial.available() > 0) {
        const char c = static_cast<char>(Serial.read());
        if (c != '\n' && c != '\r') {
            if (g_serialLine.length() < 24) {
                g_serialLine += c;
            }
            continue;
        }
        if (g_serialLine.length() == 0) {
            continue;
        }

        if (g_serialLine[0] == 's' || g_serialLine[0] == 'S') {
            g_serialOverride = false;
            for (uint8_t ch = 0; ch < kServoChannelCount; ++ch) {
                g_motion[ch].holdHere(nowMs);
            }
        } else {
            const int sep = g_serialLine.indexOf(' ');
            const long ch = sep > 0 ? g_serialLine.substring(0, sep).toInt() : -1;
            if (ch >= 0 && ch < static_cast<long>(kServoChannelCount)) {
                g_motion[ch].setTarget(g_serialLine.substring(sep + 1).toFloat(), nowMs);
                g_serialOverride = true;
            }
        }
        g_serialLine = "";
    }

    // シリアル操作中はウォッチドッグを養い続ける。
    // 1 回だけ養う実装だと command_timeout_ms 後に必ず止まってデバッグにならない。
    // 'S' 入力・CAN の SET_TARGET・電源断のいずれでもこのモードは解除される。
    if (g_serialOverride) {
        for (uint8_t ch = 0; ch < kServoChannelCount; ++ch) {
            g_safety[ch].feed(nowMs);
        }
    }
}

#endif  // ENABLE_SERIAL_DEBUG

// ===========================================================================
// setup / loop
// ===========================================================================

void setup() {
#if HAS_STATUS_LED
    pinMode(kPinLed, OUTPUT);
    digitalWrite(kPinLed, LOW);
#endif

    for (uint8_t bit = 0; bit < 4; ++bit) {
        pinMode(kPinDip[bit], INPUT_PULLUP);
    }

#if ENABLE_SERIAL_DEBUG
    Serial.begin(kSerialBaud);
#endif

    // PWM を立ち上げる前に実効デバイス ID を確定させる。
    // ID 未設定のチャンネルには PwmOut::begin() すら通さず、パルスを 1 発も出さない
    // （仕様書 §2.2: 設定ミスで意図しないアクチュエータが動くより動かない方が安全）。
    resolveDeviceIds();

    const uint32_t startMs = millis();

    for (uint8_t ch = 0; ch < kServoChannelCount; ++ch) {
        // 全チャンネルが同じ周期で同時に送ると 3 フレームのバーストになり、他バスの
        // 周期送信と重なったときに調停待ちが伸びて FEEDBACK の間隔が波打つ。
        // 周期を等分した位相をチャンネルごとにずらして平準化する。
        // ID 未設定のチャンネルも bit5 を知らせるために送るので、ここは全チャンネル分やる。
        g_lastFeedbackMs[ch] = startMs - g_feedbackIntervalMs +
                               (g_feedbackIntervalMs * ch) / kServoChannelCount;

        if (!isChannelConfigured(ch)) {
            continue;
        }
        // 仕様書 §5.4 / §7: 起動時は config.h の初期角へ。
        // PwmOut::begin() の引数無し版は 490Hz・duty 50% で始まり、サーボにとっては
        // 意味不明な指令になるので、周期とパルス幅を明示して初期角から立ち上げる。
        const uint16_t initialPulseUs =
            angleToPulseUs(g_motion[ch].currentAngleDeg(), kServoChannels[ch].pulse);
        g_pwmStarted[ch] =
            g_pwm[ch].begin(kServoPwmPeriodUs, static_cast<uint32_t>(initialPulseUs));
    }

    // 仕様書 §1: 1 Mbps。
    // CAN が上がらない基板を駆動させると PC から止められないので、
    // begin 失敗時は緊急停止ラッチに落として新しい角度指令を受け付けなくする
    // （§7.5 のとおり出力は切らず、初期角を保持したままになる）。
    if (!CAN.begin(CanBitRate::BR_1000k)) {
        for (uint8_t ch = 0; ch < kServoChannelCount; ++ch) {
            g_safety[ch].stop();
        }
    }

    g_lastMotionMs = startMs;
    g_lastBlinkMs = startMs;
}

void loop() {
    const uint32_t nowMs = millis();

    pollCan();
#if ENABLE_SERIAL_DEBUG
    pollSerial(nowMs);
#endif

    if (nowMs - g_lastMotionMs >= kMotionIntervalMs) {
        g_lastMotionMs = nowMs;
        updateMotion(nowMs);
    }

    for (uint8_t ch = 0; ch < kServoChannelCount; ++ch) {
        if (nowMs - g_lastFeedbackMs[ch] >= g_feedbackIntervalMs) {
            g_lastFeedbackMs[ch] = nowMs;
            sendFeedback(ch, nowMs);
        }
    }

    updateLed(nowMs);
}
