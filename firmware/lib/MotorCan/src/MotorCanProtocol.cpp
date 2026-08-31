#include "MotorCanProtocol.h"

namespace motorcan {

namespace {

// SET_TARGET / SET_PARAM とも Byte0 が種別、Byte1-2 が値（仕様書 §3.1 / §3.3）。
// 途中に予約バイトを挟まないので、DLC 3 で足りる。
constexpr uint8_t kCommandLength = 3;
constexpr uint8_t kValueOffset = 1;

bool isKnownControlType(uint8_t raw) {
    return raw == static_cast<uint8_t>(ControlType::Position) ||
           raw == static_cast<uint8_t>(ControlType::Velocity) ||
           raw == static_cast<uint8_t>(ControlType::Duty) ||
           raw == static_cast<uint8_t>(ControlType::OnOff);
}

bool isKnownParamId(uint8_t raw) {
    return raw <= static_cast<uint8_t>(ParamId::AngleMax);
}

uint16_t clampRawTo(int16_t raw, uint16_t lo, uint16_t hi) {
    if (raw < 0) {
        return lo;
    }
    const uint16_t value = static_cast<uint16_t>(raw);
    if (value < lo) {
        return lo;
    }
    if (value > hi) {
        return hi;
    }
    return value;
}

}  // namespace

uint8_t makeDeviceId(BoardKind board, uint8_t boardNumber, uint8_t slot) {
    if (boardNumber > kMaxBoardNumber || slot > kMaxSlotNumber) {
        // 黙って丸めると、DIP を回しすぎた基板が別の基板の ID を名乗る。
        // 未設定にしておけば LED が赤く速く点滅し、設定ミスがその場で目に見える。
        return kDeviceIdUnconfigured;
    }
    const uint8_t deviceId =
        static_cast<uint8_t>((static_cast<uint8_t>(board) << kBoardKindShift) |
                             (boardNumber << kBoardNumberShift) | slot);
    if (deviceId == kDeviceIdBroadcast) {
        // 電磁弁基板（種別 3）の「基板番号 7 × スロット 7」だけがここへ落ちる。
        // ブロードキャストと同じデバイス ID を名乗る基板が居ると、そのスロット宛の
        // SET_TARGET と全基板向けの E_STOP がデバイス ID の上で区別できなくなる。
        // 潰れるのは 512 個中 1 個で、8 枚目の基板の 8ch 目という最も使われない場所。
        return kDeviceIdUnconfigured;
    }
    return deviceId;
}

// ---------------------------------------------------------------------------
// 固定小数点
// ---------------------------------------------------------------------------

int16_t saturateToInt16(int32_t value) {
    if (value > 32767) {
        return 32767;
    }
    if (value < -32768) {
        return -32768;
    }
    return static_cast<int16_t>(value);
}

int16_t toRaw(float value, int32_t scale) {
    // NaN は比較がすべて false になるので、真っ先に落とす。**プロトコル全体で
    // NaN を気にするのはここだけ。** CAN は int16 しか運ばず、float が入るのは
    // シリアルデバッグの唯一の経路なので、その入口をここに集約している。
    if (!(value == value)) {
        return 0;
    }
    const float scaled = value * static_cast<float>(scale);
    if (scaled >= 32767.0f) {
        return 32767;
    }
    if (scaled <= -32768.0f) {
        return -32768;
    }
    return static_cast<int16_t>(scaled >= 0.0f ? scaled + 0.5f : scaled - 0.5f);
}

float fromRaw(int16_t raw, int32_t scale) {
    return static_cast<float>(raw) / static_cast<float>(scale);
}

// ---------------------------------------------------------------------------
// CAN ID
// ---------------------------------------------------------------------------

uint16_t buildCanId(CommandType command, uint8_t deviceId) {
    return static_cast<uint16_t>(commandIdBase(command) | deviceId);
}

CanIdInfo parseCanId(uint16_t canId) {
    CanIdInfo info{CommandType::EStop, 0, false};

    // CAN 2.0A の Standard Frame は 11bit（仕様書 §1）。それを超える ID は他プロトコルの
    // 相乗りとみなして無視する。
    if (canId > 0x7FF) {
        return info;
    }

    const uint8_t raw = static_cast<uint8_t>((canId >> kCommandTypeShift) & 0x07);
    switch (raw) {
        case static_cast<uint8_t>(CommandType::EStop):
        case static_cast<uint8_t>(CommandType::SetTarget):
        case static_cast<uint8_t>(CommandType::SetParam):
        case static_cast<uint8_t>(CommandType::Feedback):
        case static_cast<uint8_t>(CommandType::Info):
            info.command = static_cast<CommandType>(raw);
            info.deviceId = static_cast<uint8_t>(canId & 0xFF);
            info.valid = true;
            break;
        default:
            // 仕様書 §2.1 の予約値。使用禁止なので無効として返す。
            break;
    }
    return info;
}

// ---------------------------------------------------------------------------
// スカラのバイト列変換
// ---------------------------------------------------------------------------

void packInt16Le(uint8_t *dst, int16_t value) {
    const uint16_t bits = static_cast<uint16_t>(value);
    dst[0] = static_cast<uint8_t>(bits & 0xFF);
    dst[1] = static_cast<uint8_t>((bits >> 8) & 0xFF);
}

int16_t unpackInt16Le(const uint8_t *src) {
    return static_cast<int16_t>(static_cast<uint16_t>(src[0]) |
                                (static_cast<uint16_t>(src[1]) << 8));
}

// ---------------------------------------------------------------------------
// 受信フレームの復号
// ---------------------------------------------------------------------------

SetTargetCommand decodeSetTarget(const uint8_t *data, uint8_t length) {
    SetTargetCommand cmd{ControlType::Duty, 0, false};
    if (data == nullptr || length < kCommandLength) {
        return cmd;
    }
    if (!isKnownControlType(data[0])) {
        return cmd;
    }
    cmd.type = static_cast<ControlType>(data[0]);
    cmd.raw = unpackInt16Le(&data[kValueOffset]);
    cmd.valid = true;
    return cmd;
}

SetParamCommand decodeSetParam(const uint8_t *data, uint8_t length) {
    SetParamCommand cmd{ParamId::MaxDuty, 0, false};
    if (data == nullptr || length < kCommandLength) {
        return cmd;
    }
    if (!isKnownParamId(data[0])) {
        // 未知のパラメータ ID は無視する（仕様書 §3.3）。
        return cmd;
    }
    cmd.id = static_cast<ParamId>(data[0]);
    cmd.raw = unpackInt16Le(&data[kValueOffset]);
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
    if (data[0] == 0x01 && data[1] == 0x5A && data[2] == 0xA5) {
        return EStopAction::Clear;
    }
    // 解除意図に見えてもマジックバイトが揃わなければ何もしない（仕様書 §3.5）。
    // 「解除に失敗したら停止のまま」に倒すのが安全側。
    return EStopAction::None;
}

// ---------------------------------------------------------------------------
// 送信フレームの構築
// ---------------------------------------------------------------------------

uint8_t encodeFeedback(uint8_t *out, uint8_t flags) {
    out[0] = flags;
    return kFeedbackFlagsOnlyLength;
}

uint8_t encodeFeedback(uint8_t *out, uint8_t flags, int32_t position_0p1deg) {
    out[0] = flags;
    packInt16Le(&out[1], saturateToInt16(position_0p1deg));
    return kFeedbackWithPositionLength;
}

uint8_t composeFeedbackFlags(BoardKind board, SlotKind slot, uint8_t safetyFlags,
                             bool configured, bool reached, bool sensorActive) {
    uint8_t flags = 0;

    if (slot == SlotKind::Sensor) {
        // 仕様書 §5.2: センサは駆動されないので、緊急停止もウォッチドッグも到達も
        // 意味を持たない。safetyFlags を素通しにすると PC 側 check_safety_error() が
        // 「駆動できない状態」と読んで動作確認を打ち切る（センサに駆動できない状態は無い）。
        if (!configured) {
            flags |= status_flag::kDeviceIdUnconfigured;
        }
        if (sensorActive) {
            flags |= status_flag::kSensor;
        }
        return flags;
    }

    flags = safetyFlags;
    if (!configured) {
        // 仕様書 §2.2: 設定ミスは運用前に必ず気付くべきなので、PC 側 is_fault() へ倒す。
        flags |= status_flag::kDeviceIdUnconfigured;
    }
    if (board == BoardKind::Servo && reached) {
        // 仕様書 §7.3: サーボの到達は補間完了の**推定値**であって実測ではない。
        // DC 基板（§3.2 / §8）と電磁弁基板（§9.3）は観測手段を 1 つも持たないので、
        // ここで board を見て弾く。**「指令したから到達した」は実測でも推定でもない嘘**で、
        // 断線したソレノイドも抜けたコネクタも「到達」と報告されることになる。
        flags |= status_flag::kReached;
    }
    return flags;
}

uint8_t encodeInfo(uint8_t *out, uint8_t firmwareVersion, BoardKind board, SlotKind slot) {
    out[0] = firmwareVersion;
    out[1] = static_cast<uint8_t>(board);
    out[2] = static_cast<uint8_t>(slot);
    return kInfoBaseLength;
}

uint8_t encodeInfo(uint8_t *out, uint8_t firmwareVersion, BoardKind board, SlotKind slot,
                   float angleRangeDeg) {
    encodeInfo(out, firmwareVersion, board, slot);
    packInt16Le(&out[3], toRaw(angleRangeDeg, kAngleScale));
    return kInfoWithServoRangeLength;
}

// ---------------------------------------------------------------------------
// 値域
// ---------------------------------------------------------------------------

uint16_t clampCommandTimeoutMs(int16_t raw) {
    return clampRawTo(raw, kMinCommandTimeoutMs, kMaxCommandTimeoutMs);
}

uint16_t clampFeedbackIntervalMs(int16_t raw) {
    return clampRawTo(raw, kMinFeedbackIntervalMs, kMaxFeedbackIntervalMs);
}

// ---------------------------------------------------------------------------
// duty
// ---------------------------------------------------------------------------

float clampDuty(float duty, float maxDuty) {
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
