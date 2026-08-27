#include "MotorCanProtocol.h"

#include <string.h>

namespace motorcan {

namespace {

// float32 の位置は Byte2-5、パラメータ値も Byte2-5（仕様書 §3.1 / §3.4）
constexpr uint8_t kFloatPayloadOffset = 2;

bool isKnownControlType(uint8_t raw) {
    return raw <= static_cast<uint8_t>(ControlType::Duty);
}

// NaN は比較がすべて false になるため、以降のクランプ・範囲判定をすべて素通りする。
// DC 側では PID の積分項に入った時点で以後**正常な目標に対しても**出力が NaN になり、
// clampDuty が 0 へ落とすので「無言で死んだモータ」になる（診断ビットも立たない）。
// 解釈できないフレームとして復号層で捨てる（サーボ側 ServoMotion::setTarget と同じ判断）。
bool isNan(float value) { return value != value; }

bool isKnownParamId(uint8_t raw) {
    return raw <= static_cast<uint8_t>(ParamId::ReachedTolerance);
}

uint32_t sanitizeTimingMs(float value, uint32_t fallbackMs, uint32_t minMs, uint32_t maxMs) {
    // NaN は比較がすべて false になるので、範囲判定より先に弾く。
    if (!(value == value)) {
        return fallbackMs;
    }
    if (!(value > static_cast<float>(minMs))) {
        return minMs;
    }
    if (value >= static_cast<float>(maxMs)) {
        return maxMs;
    }
    return static_cast<uint32_t>(value);
}

}  // namespace

uint16_t buildCanId(CommandType command, uint8_t deviceId) {
    return static_cast<uint16_t>((static_cast<uint16_t>(command) << 8) | deviceId);
}

CanIdInfo parseCanId(uint16_t canId) {
    CanIdInfo info{CommandType::SetTarget, 0, false};

    // CAN 2.0A の Standard Frame は 11bit（仕様書 §1）。それを超える ID は他プロトコルの
    // 相乗りとみなして無視する。
    if (canId > 0x7FF) {
        return info;
    }

    const uint8_t raw = static_cast<uint8_t>((canId >> 8) & 0x07);
    switch (raw) {
        case static_cast<uint8_t>(CommandType::SetTarget):
        case static_cast<uint8_t>(CommandType::Feedback):
        case static_cast<uint8_t>(CommandType::SetMode):
        case static_cast<uint8_t>(CommandType::SetParam):
        case static_cast<uint8_t>(CommandType::EStop):
            info.command = static_cast<CommandType>(raw);
            info.deviceId = static_cast<uint8_t>(canId & 0xFF);
            info.valid = true;
            break;
        default:
            // 仕様書 §2.1 の予約値 0b100/0b101/0b110。使用禁止なので無効として返す。
            break;
    }
    return info;
}

void packFloatLe(uint8_t *dst, float value) {
    uint32_t bits = 0;
    memcpy(&bits, &value, sizeof(bits));
    dst[0] = static_cast<uint8_t>(bits & 0xFF);
    dst[1] = static_cast<uint8_t>((bits >> 8) & 0xFF);
    dst[2] = static_cast<uint8_t>((bits >> 16) & 0xFF);
    dst[3] = static_cast<uint8_t>((bits >> 24) & 0xFF);
}

float unpackFloatLe(const uint8_t *src) {
    const uint32_t bits = static_cast<uint32_t>(src[0]) |
                          (static_cast<uint32_t>(src[1]) << 8) |
                          (static_cast<uint32_t>(src[2]) << 16) |
                          (static_cast<uint32_t>(src[3]) << 24);
    float value = 0.0f;
    memcpy(&value, &bits, sizeof(value));
    return value;
}

void packInt16Le(uint8_t *dst, int16_t value) {
    const uint16_t bits = static_cast<uint16_t>(value);
    dst[0] = static_cast<uint8_t>(bits & 0xFF);
    dst[1] = static_cast<uint8_t>((bits >> 8) & 0xFF);
}

int16_t unpackInt16Le(const uint8_t *src) {
    const uint16_t bits =
        static_cast<uint16_t>(src[0]) | static_cast<uint16_t>(static_cast<uint16_t>(src[1]) << 8);
    return static_cast<int16_t>(bits);
}

int16_t saturateToInt16(int32_t value) {
    if (value > 32767) {
        return 32767;
    }
    if (value < -32768) {
        return -32768;
    }
    return static_cast<int16_t>(value);
}

SetTargetCommand decodeSetTarget(const uint8_t *data, uint8_t length) {
    SetTargetCommand cmd{ControlType::Duty, 0.0f, false};
    if (data == nullptr || length < kFrameLength) {
        return cmd;
    }
    if (!isKnownControlType(data[0])) {
        // 未知の制御タイプを duty などに読み替えると別次元の目標値で暴走する。
        // 解釈できないフレームは捨てる方が安全。
        return cmd;
    }
    cmd.type = static_cast<ControlType>(data[0]);
    const float value = unpackFloatLe(&data[kFloatPayloadOffset]);
    if (isNan(value)) {
        return cmd;
    }
    cmd.value = value;
    cmd.valid = true;
    return cmd;
}

SetParamCommand decodeSetParam(const uint8_t *data, uint8_t length) {
    SetParamCommand cmd{ParamId::Kp, 0.0f, false};
    if (data == nullptr || length < kFrameLength) {
        return cmd;
    }
    if (!isKnownParamId(data[0])) {
        // 仕様書 §3.4: 未知のパラメータ ID は無視する。
        // 新しい PC 側と古い基板が混在しても止まらないようにするため。
        return cmd;
    }
    cmd.id = static_cast<ParamId>(data[0]);
    const float value = unpackFloatLe(&data[kFloatPayloadOffset]);
    if (isNan(value)) {
        // NaN のゲインを受け付けると PID の出力が永久に NaN になる。
        return cmd;
    }
    cmd.value = value;
    cmd.valid = true;
    return cmd;
}

EStopAction decodeEStop(const uint8_t *data, uint8_t length) {
    if (data == nullptr || length < 3) {
        // 解除にはマジックバイト 2 つが必要なので、Byte0-2 が無いフレームは判定できない。
        return EStopAction::None;
    }
    if (data[0] == 0x00) {
        return EStopAction::Stop;
    }
    if (data[0] == 0x01 && data[1] == kEStopClearMagic1 && data[2] == kEStopClearMagic2) {
        return EStopAction::Clear;
    }
    // 解除意図に見えてもマジックバイトが揃わなければ何もしない（仕様書 §3.5）。
    // 「解除に失敗したら停止のまま」に倒すのが安全側。
    return EStopAction::None;
}

void encodeFeedback(uint8_t *out, int32_t position_0p1deg, int32_t rpm, int32_t current_ma,
                    uint8_t temperature_c, uint8_t flags) {
    packInt16Le(&out[0], saturateToInt16(position_0p1deg));
    packInt16Le(&out[2], saturateToInt16(rpm));
    packInt16Le(&out[4], saturateToInt16(current_ma));
    out[6] = temperature_c;
    out[7] = flags;
}

uint32_t sanitizeCommandTimeoutMs(float value, uint32_t fallbackMs) {
    return sanitizeTimingMs(value, fallbackMs, kMinCommandTimeoutMs, kMaxCommandTimeoutMs);
}

uint32_t sanitizeFeedbackIntervalMs(float value, uint32_t fallbackMs) {
    return sanitizeTimingMs(value, fallbackMs, kMinFeedbackIntervalMs, kMaxFeedbackIntervalMs);
}

float clampDuty(float duty, float maxDuty) {
    // NaN は比較がすべて false になり PWM 段まで素通りするため、明示的に 0 へ落とす。
    if (!(duty == duty) || !(maxDuty == maxDuty)) {
        return 0.0f;
    }
    if (maxDuty < 0.0f) {
        maxDuty = 0.0f;
    } else if (maxDuty > 1.0f) {
        maxDuty = 1.0f;
    }
    if (duty > maxDuty) {
        return maxDuty;
    }
    if (duty < -maxDuty) {
        return -maxDuty;
    }
    return duty;
}

DutyOutput splitDuty(float duty, float maxDuty) {
    const float clamped = clampDuty(duty, maxDuty);
    // 0 は reverse=false 側に倒す。0 を「負でない」ではなく「負」と扱うと、
    // 停止のたびに方向ピンが反転する。
    return DutyOutput{clamped < 0.0f ? -clamped : clamped, clamped < 0.0f};
}

}  // namespace motorcan
