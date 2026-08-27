// 受信フレームの宛先判定と、自分のデバイス ID の解決（仕様書 §2.2 / §3.5 / §7.1）。
//
// DC 用（1 チャンネル）とサーボ用（複数チャンネル）で同じ規則を使う。以前は両方の
// main.cpp が同じ判定を各自で書いており、ペリフェラルに埋まっているせいで
// native テストが 1 件も無かった。取り違えると「他のアクチュエータ宛のフレームで
// 自分が動く」「ブロードキャスト緊急停止が届かない」のどちらも起こりうる。
//
// Arduino.h を include しないのは意図的で、native 環境でそのままテストできるようにするため。

#pragma once

#include <stdint.h>

#include "MotorCanProtocol.h"

namespace motorcan {

// channelMask が uint8_t なので 1 枚で扱えるチャンネルは 8 まで。
// 増やすときは mask の型と config.h の static_assert を一緒に広げること。
constexpr uint8_t kMaxChannels = 8;

struct FrameRoute {
    bool accepted;
    CommandType command;
    uint8_t channelMask;  // bit n = チャンネル n が処理する
};

// canId / isStandardId のフレームを、deviceIds[0..channelCount) のどのチャンネルが
// 処理すべきかに解決する。受理条件は以下だけで、それ以外はすべて捨てる。
//
//   - Standard Frame であること（仕様書 §1。EDULITE の Extended Frame を拾わない）
//   - コマンド種別が予約値でないこと（仕様書 §2.1）
//   - 宛先が 0xFF なら E_STOP のときだけ受理し、全チャンネルへ配る（§3.5）。
//     デバイス ID 未設定のチャンネルにも配るのは、「未設定だから止められない」
//     経路を作らないため
//   - それ以外は、チャンネル表に載っている ID と一致するものだけ。
//     0x00（未設定）に「自分宛」は存在しない（§2.2）
FrameRoute routeFrame(uint16_t canId, bool isStandardId, const uint8_t *deviceIds,
                      uint8_t channelCount);

// DIP は「スロット表全体に加えるブロックオフセット」として働く（仕様書 §2.2 / §7.1）。
// 1 枚が複数スロットを持つ基板を複数枚使うときは、
// **オフセットの刻み幅をスロット数にしないとブロックが重なる。**
// 例: 基準 {0x11,0x12,0x13} に stride=1 で +1 すると {0x12,0x13,0x14} となり、
// 隣の DIP 設定の基板と 2 チャンネルが同じ ID を名乗る。同じ ID の基板が 2 枚
// 居ると、PC 側の受信ループは最初にマッチした 1 台で打ち切るため、もう一方は
// 永久にフィードバックを得られないうえ、1 通の SET_TARGET で 2 台が同時に動く。
//
// 刻み幅は「実際に使うチャンネル数」ではなく **スロット数** を渡すこと。
// サーボ基板はスロットの役割（サーボ / センサ）を配線で選べるので、センサに
// 使ったスロットも ID を 1 つ予約したままにしないと、役割を変えた瞬間に
// ブロックの幅が縮んで隣の基板と重なる。
//
// bandEndId はこの基板種別に割り当てられた ID 帯の上端（この値を含む）。
// 帯を越えたブロックは未設定へ倒す。倒さずに割り当てると、サーボ基板の DIP を
// 上げただけで DC 基板の帯を踏み、同じ ID の基板が 2 枚並ぶ。未設定にしておけば
// LED が赤く速く点滅し、DIP の設定ミスがその場で目に見える。
//
// offset * stride は 8bit を容易に超えるので 16bit で計算し、はみ出したものは
// 回り込ませずに未設定へ倒す（回り込んだ ID は他基板のブロックを踏む）。
uint8_t applyDeviceIdBlockOffset(uint8_t baseDeviceId, uint8_t offset, uint8_t stride,
                                 uint8_t bandEndId);

// DIP スイッチを 4bit などの整数として読む。pins[i] がビット i に対応し、
// readPin(pin) が activeLevel を返したビットを 1 とする（INPUT_PULLUP なら LOW = ON）。
//
// 論理と添字の対応をここに 1 つだけ置くのは、両ファームが各自で同じループを書くと
// 片方だけビット順や論理が反転しても誰も気付けないため。反転すると別のアクチュエータが動く。
uint8_t readDipSwitch(const uint8_t *pins, uint8_t count, int (*readPin)(uint8_t pin),
                      int activeLevel);

}  // namespace motorcan
