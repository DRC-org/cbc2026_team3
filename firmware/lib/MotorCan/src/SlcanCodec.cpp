#include "SlcanCodec.h"

namespace motorcan {

namespace {

constexpr char kHexDigits[] = "0123456789ABCDEF";
constexpr uint8_t kTransmitHeaderLength = 5;

const SlcanRequest kNone{SlcanResult::None, nullptr, {0, 0, {0}}};
const SlcanRequest kOk{SlcanResult::Ok, nullptr, {0, 0, {0}}};
const SlcanRequest kError{SlcanResult::Error, nullptr, {0, 0, {0}}};

SlcanRequest reply(const char *text) {
    return SlcanRequest{SlcanResult::Reply, text, {0, 0, {0}}};
}

int hexValue(char c) {
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

}

SlcanRequest SlcanCodec::push(char c) {
    if (c != '\r' && c != '\n') {
        if (length_ < kSlcanMaxLineLength) {
            line_[length_++] = c;
            line_[length_] = '\0';
        } else {
            overflowed_ = true;
        }
        return kNone;
    }

    SlcanRequest request = kNone;
    if (overflowed_) {
        request = kError;
    } else if (length_ > 0) {
        request = parseLine();
    }

    length_ = 0;
    line_[0] = '\0';
    overflowed_ = false;
    return request;
}

SlcanRequest SlcanCodec::parseLine() {
    switch (line_[0]) {
        case 'S':
            // 物理 CAN を持たないのでビットレートは受理して捨てる。
            if (length_ != 2 || line_[1] < '0' || line_[1] > '8') {
                return kError;
            }
            return kOk;
        case 'O':
            if (length_ != 1) {
                return kError;
            }
            open_ = true;
            return kOk;
        case 'C':
            if (length_ != 1) {
                return kError;
            }
            open_ = false;
            return kOk;
        case 'V':
            return length_ == 1 ? reply(kSlcanVersionReply) : kError;
        case 'N':
            return length_ == 1 ? reply(kSlcanSerialReply) : kError;
        case 'F':
            return length_ == 1 ? reply(kSlcanStatusReply) : kError;
        case 't':
        case 'T':
            // 拡張 ID ('T') はこの基板が使わないので受理しない。
            return line_[0] == 't' ? parseTransmit() : kError;
        default:
            return kError;
    }
}

SlcanRequest SlcanCodec::parseTransmit() {
    // slcand は必ず 'O' を送ってから通信を始める。開いていない間は 1 フレームも通さない。
    if (!open_ || length_ < kTransmitHeaderLength) {
        return kError;
    }

    uint16_t canId = 0;
    for (uint8_t i = 1; i < 4; ++i) {
        const int digit = hexValue(line_[i]);
        if (digit < 0) {
            return kError;
        }
        canId = static_cast<uint16_t>((canId << 4) | static_cast<uint16_t>(digit));
    }
    if (canId > kSlcanMaxStandardId) {
        return kError;
    }

    if (line_[4] < '0' || line_[4] > '8') {
        return kError;
    }
    const uint8_t len = static_cast<uint8_t>(line_[4] - '0');
    if (length_ != kTransmitHeaderLength + static_cast<uint8_t>(len * 2)) {
        return kError;
    }

    SlcanRequest request{SlcanResult::Frame, nullptr, {canId, len, {0}}};
    for (uint8_t i = 0; i < len; ++i) {
        const int hi = hexValue(line_[kTransmitHeaderLength + i * 2]);
        const int lo = hexValue(line_[kTransmitHeaderLength + i * 2 + 1]);
        if (hi < 0 || lo < 0) {
            return kError;
        }
        request.frame.data[i] = static_cast<uint8_t>((hi << 4) | lo);
    }
    return request;
}

size_t encodeFrame(char *out, size_t cap, uint16_t canId, uint8_t len, const uint8_t *data) {
    if (out == nullptr || canId > kSlcanMaxStandardId || len > kSlcanMaxDataLength) {
        return 0;
    }
    if (len > 0 && data == nullptr) {
        return 0;
    }
    const size_t needed = static_cast<size_t>(kTransmitHeaderLength) + len * 2 + 1;
    if (cap < needed + 1) {
        return 0;
    }

    size_t pos = 0;
    out[pos++] = 't';
    out[pos++] = kHexDigits[(canId >> 8) & 0x0F];
    out[pos++] = kHexDigits[(canId >> 4) & 0x0F];
    out[pos++] = kHexDigits[canId & 0x0F];
    out[pos++] = static_cast<char>('0' + len);
    for (uint8_t i = 0; i < len; ++i) {
        out[pos++] = kHexDigits[(data[i] >> 4) & 0x0F];
        out[pos++] = kHexDigits[data[i] & 0x0F];
    }
    out[pos++] = kSlcanOk;
    out[pos] = '\0';
    return pos;
}

}
