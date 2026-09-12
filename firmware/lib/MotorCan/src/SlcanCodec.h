#pragma once

#include <stddef.h>
#include <stdint.h>

namespace motorcan {

// 受理する最長の行は 't' + ID 3 桁 + DLC 1 桁 + データ 16 桁。
constexpr uint8_t kSlcanMaxLineLength = 21;
// 上に CR と NUL を足した長さ。encodeFrame の出力先はこれ以上を確保する。
constexpr uint8_t kSlcanFrameTextCapacity = 23;

constexpr char kSlcanOk = '\r';
constexpr char kSlcanError = '\a';

constexpr uint16_t kSlcanMaxStandardId = 0x7FF;
constexpr uint8_t kSlcanMaxDataLength = 8;

constexpr char kSlcanVersionReply[] = "V1013\r";
constexpr char kSlcanSerialReply[] = "N0001\r";
// 物理 CAN を持たないので報告できるバスエラーが存在しない。
constexpr char kSlcanStatusReply[] = "F00\r";

enum class SlcanResult : uint8_t {
    None,
    Ok,
    Error,
    Reply,
    Frame,
};

struct SlcanFrame {
    uint16_t canId;
    uint8_t len;
    uint8_t data[kSlcanMaxDataLength];
};

struct SlcanRequest {
    SlcanResult result;
    const char *reply;
    SlcanFrame frame;
};

class SlcanCodec {
   public:
    SlcanRequest push(char c);

    bool isOpen() const { return open_; }

   private:
    SlcanRequest parseLine();
    SlcanRequest parseTransmit();

    char line_[kSlcanMaxLineLength + 1] = {0};
    uint8_t length_ = 0;
    bool overflowed_ = false;
    bool open_ = false;
};

size_t encodeFrame(char *out, size_t cap, uint16_t canId, uint8_t len, const uint8_t *data);

}
