// CAN バックエンド: MCP2515（SPI 外付け）。**基板 #0 / #1（Arduino Nano）用。**
//
// Nano には CAN ペリフェラルが無いので、MCP2515 を SPI でぶら下げて INT ピンのレベルで
// 受信を知る。SPI が D11/D12/D13 を占有するのはこの基板固有の制約で、そこから
// 「D13 は SCK なのでステータス LED に使えない」が導かれる（config.h 参照）。
//
// **この翻訳単位は Nano ビルドにしか入らない**（platformio.ini の build_src_filter）。

#include <Arduino.h>
#include <SPI.h>
#include <mcp_can.h>

#include "MotorCanProtocol.h"
#include "can_backend.h"
#include "config.h"

using namespace motorcan;

namespace {

MCP_CAN g_can(kPinMcpCs);

// **PC → モタドラ方向のフレームだけを通す**（E_STOP / SET_TARGET / SET_PARAM）。
//
// 全通過（MCP_ANY）にすると、共有バス上の FEEDBACK（自作モタドラ 14 台 × 100Hz）と
// INFO まで MCP2515 の受信バッファ（**RXB0 / RXB1 の 2 段しかない**）へ流れ込み、
// SPI で読み出しては捨てるだけの仕事が loop() に乗る。この基板の loop() は
// sendMsgBuf が 1 通あたり最大 5ms（空き TX 待ち + TXREQ クリア待ち）まで伸びうるので、
// 2 段は 1.4ms 相当で溢れる。落ちるのがブロードキャスト E_STOP だと、症状は
// 「たまに緊急停止が効かないサーボ基板」という最も追いにくい形になる。
//
// **どの ID を通すかは CommandType から導く**（`kEStopAndSetTargetFilter` /
// `kSetParamFilter`）。ここに残しているのは MCP2515 固有の事情だけ ——
// mcp2515_write_mf は ext=0 のとき ulData の **bit16 以降**を SIDH/SIDL へ詰めるので、
// 標準 ID はそこへ載せる（低位 16bit は拡張 ID のマスクになるので 0 にする。
// 非 0 にすると標準フレームでもデータ 1-2 バイト目と比較され始める）。
//
// **1 本のマスクでは 3 値を表せない**ので 2 バンクに分ける。
//   RXB0（マスク 0）… E_STOP + SET_TARGET。時間に厳しい方を優先度の高い RXB0 に置く
//   RXB1（マスク 1）… SET_PARAM
// RXB0 は BUKT（ロールオーバー）付きなので、RXB0 が埋まっている間に来た E_STOP は
// RXB1 のフィルタに関係なく RXB1 へ落ちる。
constexpr uint32_t kStdIdShiftInFilterReg = 16;

constexpr uint32_t toFilterReg(uint16_t stdId) {
    return static_cast<uint32_t>(stdId) << kStdIdShiftInFilterReg;
}

// **ビット位置を取り違えるとフィルタが全通過にも全遮断にもなる。**
// 全遮断なら「CAN が生きているのに指令が 1 通も効かない」、全通過なら
// この修正そのものが無効になり、どちらも実機でしか気付けない。
static_assert(toFilterReg(0x7FF) == 0x07FF0000UL,
              "標準 ID がマスク/フィルタレジスタの想定位置（bit16 以降）に載っていない");
static_assert((toFilterReg(kEStopAndSetTargetFilter.mask) & 0xFFFFUL) == 0,
              "拡張 ID 側のマスクが 0 でない（標準フレームがデータ 1-2 バイト目と比較される）");

bool configureCanFilters() {
    // RXB0: E_STOP + SET_TARGET。フィルタ 2 本とも同じ値にしないと、
    // 初期化時の 0x000 が別のマスクで残って予期しない ID を通す。
    if (g_can.init_Mask(0, 0, toFilterReg(kEStopAndSetTargetFilter.mask)) != MCP2515_OK) {
        return false;
    }
    for (uint8_t f = 0; f <= 1; ++f) {
        if (g_can.init_Filt(f, 0, toFilterReg(kEStopAndSetTargetFilter.id)) != MCP2515_OK) {
            return false;
        }
    }

    // RXB1: SET_PARAM。フィルタは 4 本あるので全部同じ値で埋める。
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

}  // namespace

namespace servo_can {

bool begin() {
    // INT は受信バッファが埋まっている間 LOW のまま。**この 1 行が MCP2515 固有**なので
    // main.cpp の setup() ではなくここに置く（R4 版に INT ピンは存在しない）。
    pinMode(kPinMcpInt, INPUT);

    // 仕様書 §1: 1 Mbps。MCP2515 の水晶は 16MHz。
    //
    // **MCP_ANY ではなく MCP_STDEXT を渡す。** MCP_ANY はマスク／フィルタを丸ごと
    // 無効化するので configureCanFilters() が効かない。**MCP_STD は使えない** ——
    // mcp_can では「シリコンのバグ」としてコメントアウトされており、渡すと
    // begin() が MCP2515_FAIL を返して基板がまるごと止まる。MCP_STDEXT は
    // フィルタを有効にしたまま標準・拡張の両方を各フィルタの EXIDE で判別する設定で、
    // 全 6 本を標準 ID として書き直すので拡張フレームは 1 通も通らない。
    if (g_can.begin(MCP_STDEXT, CAN_1000KBPS, MCP_16MHZ) != CAN_OK || !configureCanFilters()) {
        return false;
    }
    g_can.setMode(MCP_NORMAL);
    return true;
}

bool send(uint16_t canId, uint8_t len, const uint8_t *data) {
    // **sendMsgBuf の戻り値を捨ててはならない** —— mcp_can の sendMsg() は空き TX バッファ
    // 待ちと TXREQ クリア待ちの二段で TIMEOUTVALUE(2500us) まで回るので、バス不通・bus-off・
    // 調停混雑では 1 通あたり最大 5ms を食う。捨てると「loop() だけが伸び続けて誰にも何も
    // 届かない基板」が平常時と同じ青のハートビートを出し続ける。
    //
    // const_cast は mcp_can の宣言が `INT8U *buf` で const を取らないため。
    // ライブラリは送信バッファを読むだけで書き換えない。
    return g_can.sendMsgBuf(canId, 0, len, const_cast<uint8_t *>(data)) == CAN_OK;
}

void poll(FrameHandler handler) {
    // MCP2515 の INT は受信バッファが空になるまで LOW のまま。mcp_can は RX 割り込みだけを
    // 有効にするので、LOW = 受信あり と見てよい。
    // 回数に上限を置くのは、万一 INT が別の理由で張り付いても loop() を止めないため
    // （止まると補間もフィードバックも凍り、PC からは STALE にしか見えない）。
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

        // mcp_can は拡張フレームを bit31、RTR を bit30 で返す。どちらも我々のプロトコルには
        // 存在しないので、ID の下位 11bit だけを渡して呼び出し側に判定させる。
        const bool extended = (rxId & 0x80000000UL) != 0;
        const bool remote = (rxId & 0x40000000UL) != 0;
        handler(static_cast<uint16_t>(rxId & 0x7FFUL), !extended && !remote, buf,
                static_cast<uint8_t>(len));
    }
}

}  // namespace servo_can
