// サーボ用自作モタドラの CAN バックエンド。**MCU 依存をここから先へ漏らさないための境界。**
//
// サーボ基板は 3 枚あって MCU が 2 種類ある（config.h 冒頭）。
//   - 基板 #0 / #1（Arduino Nano）      … MCP2515 を SPI で外付け → can_mcp2515.cpp
//   - 基板 #2     （UNO R4 Minima）     … 内蔵 CAN ペリフェラル   → can_r4.cpp
// どちらを組み込むかは platformio.ini の build_src_filter が env ごとに決める。
//
// **main.cpp は CAN の実体を知らない。** ここを挟まないと、mcp_can 固有の値
// （拡張フレームを表す bit31・RTR の bit30・CAN_OK・MCP2515_OK）や Arduino_CAN 固有の型
// （CanMsg / CanStandardId）が main.cpp の宛先判定や送信の周りへ染み出し、MCU が増える
// たびに #if が安全機構の中まで入り込む。
//
// **motorcan:: 名前空間は使わない。** あちらは Arduino 非依存で native テスト圏内の
// 共有ライブラリ（3 枚の基板が共有する規則）であり、ここは逆に Arduino/MCU にべったりの層。
// 同じ名前空間に混ぜると、native テストから見える層とそうでない層の境界が読めなくなる。

#pragma once

#include <stdint.h>

namespace servo_can {

// **PC → モタドラ方向のフレームだけを渡す。** 拡張フレーム / RTR の解釈はバックエンド側の
// 責務で、ここへ来る canId は必ず標準 ID の 11bit（standard=false ならそれ以外の種別）。
using FrameHandler = void (*)(uint16_t canId, bool standard, const uint8_t *data, uint8_t len);

// 仕様書 §1: 1 Mbps。受信フィルタの設定まで含めて済ませる。
// **失敗したら false を返す。** 呼び出し側（setup）は緊急停止ラッチへ落とす —— CAN が
// 上がらない基板は PC から止められないので、駆動させてはならない。
bool begin();

// 1 通送る。**空きを待たない**（詰まったバスの上で loop() が止まると、ウォッチドッグ満了の
// 反映も出力の更新も止まる）。**戻り値を捨ててはならない** —— 呼び出し側の sendFrame() が
// 連続失敗を数えて LED を赤へ倒す唯一の口になっている。
bool send(uint16_t canId, uint8_t len, const uint8_t *data);

// 溜まっている受信フレームを handler へ配る。1 回の呼び出しで戻ってくること
// （loop() が止まると補間もフィードバックも凍り、PC からは STALE にしか見えない）。
void poll(FrameHandler handler);

}  // namespace servo_can
