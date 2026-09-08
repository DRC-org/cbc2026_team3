#include <Arduino.h>
#include <SPI.h>
#include <mcp_can.h>

#include "MotorCanProtocol.h"
#include "can_backend.h"
#include "config.h"

using namespace motorcan;

namespace {

MCP_CAN g_can(kPinMcpCs);

constexpr uint32_t kStdIdShiftInFilterReg = 16;

constexpr uint32_t toFilterReg(uint16_t stdId) {
    return static_cast<uint32_t>(stdId) << kStdIdShiftInFilterReg;
}

static_assert(toFilterReg(0x7FF) == 0x07FF0000UL,
              "標準 ID がマスク/フィルタレジスタの想定位置（bit16 以降）に載っていない");
static_assert((toFilterReg(kEStopAndSetTargetFilter.mask) & 0xFFFFUL) == 0,
              "拡張 ID 側のマスクが 0 でない（標準フレームがデータ 1-2 バイト目と比較される）");

bool configureCanFilters() {
    // RXB0 のフィルタ 2 本は同じ値で埋める。埋めないと初期化時の 0x000 が別のマスクで
    // 残り、予期しない ID を通す。
    if (g_can.init_Mask(0, 0, toFilterReg(kEStopAndSetTargetFilter.mask)) != MCP2515_OK) {
        return false;
    }
    for (uint8_t f = 0; f <= 1; ++f) {
        if (g_can.init_Filt(f, 0, toFilterReg(kEStopAndSetTargetFilter.id)) != MCP2515_OK) {
            return false;
        }
    }

    if (g_can.init_Mask(1, 0, toFilterReg(kSetParamFilter.mask)) != MCP2515_OK) {
        return false;
    }
    for (uint8_t f = 2; f <= 5; ++f) {
        if (g_can.init_Filt(f, 0, toFilterReg(kSetParamFilter.id)) != MCP2515_OK) {
            return false;
        }
    }
    return true;
}

}

namespace servo_can {

bool begin() {
    pinMode(kPinMcpInt, INPUT);

    if (g_can.begin(MCP_STDEXT, CAN_1000KBPS, MCP_16MHZ) != CAN_OK || !configureCanFilters()) {
        return false;
    }
    g_can.setMode(MCP_NORMAL);
    return true;
}

bool send(uint16_t canId, uint8_t len, const uint8_t *data) {
    // const_cast は mcp_can の宣言が `INT8U *buf` で const を取らないため。
    // ライブラリは送信バッファを読むだけで書き換えない。
    return g_can.sendMsgBuf(canId, 0, len, const_cast<uint8_t *>(data)) == CAN_OK;
}

void poll(FrameHandler handler) {
    for (uint8_t guard = 0; guard < 8; ++guard) {
        if (digitalRead(kPinMcpInt) != LOW) {
            return;
        }
        unsigned long rxId = 0;
        unsigned char len = 0;
        unsigned char buf[8];
        if (g_can.readMsgBuf(&rxId, &len, buf) != CAN_OK) {
            return;
        }

        const bool extended = (rxId & 0x80000000UL) != 0;
        const bool remote = (rxId & 0x40000000UL) != 0;
        handler(static_cast<uint16_t>(rxId & 0x7FFUL), !extended && !remote, buf,
                static_cast<uint8_t>(len));
    }
}

}
