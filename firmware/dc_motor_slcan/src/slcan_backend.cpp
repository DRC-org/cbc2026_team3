#include "slcan_backend.h"

#include <Arduino.h>
#include <string.h>

#include "SlcanCodec.h"
#include "config.h"

namespace dc_slcan {

namespace {

motorcan::SlcanCodec g_codec;

// USB CDC はフロー制御を持たず、空きより多く write すると送信が捌けるまで内部で回り続ける。
// 空きぶんしか書かない（書けなければ諦める）ことで loop() が詰まったホストに引きずられない。
bool writeIfRoom(const char *text, size_t length) {
    if (Serial.availableForWrite() < static_cast<int>(length)) {
        return false;
    }
    return Serial.write(reinterpret_cast<const uint8_t *>(text), length) == length;
}

void writeReply(const motorcan::SlcanRequest &request) {
    switch (request.result) {
        case motorcan::SlcanResult::Ok:
        case motorcan::SlcanResult::Frame:
            writeIfRoom(&motorcan::kSlcanOk, 1);
            break;
        case motorcan::SlcanResult::Error:
            writeIfRoom(&motorcan::kSlcanError, 1);
            break;
        case motorcan::SlcanResult::Reply:
            writeIfRoom(request.reply, strlen(request.reply));
            break;
        case motorcan::SlcanResult::None:
            break;
    }
}

}

bool begin() {
    Serial.begin(kSlcanBaud);
    return true;
}

bool send(uint16_t canId, uint8_t len, const uint8_t *data) {
    // slcand は 'O' を送ってから通信を始める。開くまでは 1 通も出してはならない。
    if (!g_codec.isOpen()) {
        return false;
    }

    char text[motorcan::kSlcanFrameTextCapacity];
    const size_t length = motorcan::encodeFrame(text, sizeof(text), canId, len, data);
    if (length == 0) {
        return false;
    }
    return writeIfRoom(text, length);
}

void poll(FrameHandler handler) {
    for (uint16_t i = 0; i < kSlcanRxBytesPerLoop && Serial.available() > 0; ++i) {
        // DTR が落ちている間 available() は残量を返すのに read() は -1 を返して消費しない。
        // 0xFF として codec へ流すと、毎周期この上限ぶん空回りしたうえ行が壊れる。
        const int c = Serial.read();
        if (c < 0) {
            break;
        }

        const motorcan::SlcanRequest request = g_codec.push(static_cast<char>(c));
        writeReply(request);
        if (request.result == motorcan::SlcanResult::Frame && handler != nullptr) {
            // SlcanCodec は標準 ID しか通さない。
            handler(request.frame.canId, true, request.frame.data, request.frame.len);
        }
    }
}

}
