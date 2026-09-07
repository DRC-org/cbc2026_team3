// CAN バックエンド: UNO R4 Minima の内蔵ペリフェラル。**基板 #2 用。**
//
// ピンは D4(TX) / D5(RX) に固定で、Arduino_CAN の CAN インスタンスが variant の
// PIN_CAN0_TX / PIN_CAN0_RX を使う。config.h に写しは置かず、衝突検査は main.cpp の
// pinsAvoidCan() が variant のマクロを直接見る（DC 用 firmware/dc_motor と同じ扱い）。
//
// **受信フィルタは設定しない。Arduino_CAN に API が無い**（R7FA4M1_CAN は全 mailbox を
// 受け入れ設定で開く）。したがってこの基板には共有バス上の FEEDBACK / INFO も含めた
// 全フレームが届き、宛先でない分は routeFrame が弾く。**黙って放置しているのではなく、
// それで足りると判断している** —— 同じ DC 基板（UNO R4 Minima / Arduino_CAN）が
// 同じ共有バス上でフィルタ無しのまま動いている実績があり、Nano 版で filter が要ったのは
// MCP2515 の受信バッファが RXB0/RXB1 の 2 段しかないという固有の事情による
// （溢れるとブロードキャスト E_STOP が落ちうる）。R4 の受信は FIFO とリングバッファで
// 受けるので、読み捨てるコストしか掛からない。**フィルタが要る事態になったら
// （＝取りこぼしが実測されたら）ここへ書く**。
//
// **この翻訳単位は R4 ビルドにしか入らない**（platformio.ini の build_src_filter）。

#include <Arduino.h>
#include <Arduino_CAN.h>

#include "can_backend.h"

namespace servo_can {

bool begin() {
    // 仕様書 §1: 1 Mbps。
    return CAN.begin(CanBitRate::BR_1000k);
}

bool send(uint16_t canId, uint8_t len, const uint8_t *data) {
    // **空かなければ諦める。待ってはならない** —— 詰まったバスの上で loop() が止まると、
    // ウォッチドッグ満了の反映も出力の更新も止まる。FEEDBACK は次の周期でまた送られるので、
    // 1 通落ちても PC 側の STALE 判定（既定 500ms）には遠く届かない。
    //
    // **戻り値を捨ててはならない** —— R4 の Arduino_CAN は標準 ID の mailbox を 1 本しか
    // 使わない（R7FA4M1_CAN.cpp の write が CAN_MAILBOX_ID_0 固定）ので、同じ反復で
    // 連続送信すると 2 通目以降が必ず落ちる。DC 基板ではこれで INFO が 4 秒間 1 通も
    // 出ていなかった（LED にもログにも現れなかった）。
    const CanMsg msg(CanStandardId(canId), len, data);
    return CAN.write(msg) > 0;
}

void poll(FrameHandler handler) {
    // 受信リングバッファに溜まっている分を出し切る。上限を置かなくても available() が
    // バッファ長で頭打ちになるので loop() は必ず戻る（DC 用 main.cpp と同じ形）。
    while (CAN.available()) {
        const CanMsg msg = CAN.read();
        handler(static_cast<uint16_t>(msg.getStandardId()), msg.isStandardId(), msg.data,
                msg.data_length);
    }
}

}  // namespace servo_can
