// 「基板の config.h が書いているピン割当」と「ビルド系が実際に初期化したピン割当」を
// 突き合わせるための比較規則。
//
// **どちらの表を作るかは呼び出し側の仕事で、ここは比べるだけ。** 電磁弁基板では
// CubeMX 生成の main.h（`PUMP1_SW_GPIO_Port` 等）が正だが、その `GPIOA` は
// `((GPIO_TypeDef *) GPIOA_BASE)` へ展開されるポインタキャストなので、比較の
// 中に持ち込むと HAL の翻訳単位にしか置けなくなり native テストが 1 件も掛からない
// （サーボの「安全機構 × 角度補間」を ServoChannel に閉じ込めたのと同じ理由）。
// ポインタ → ポート番号の逆引きだけを呼び出し側に残し、規則はここに置く。
//
// Arduino.h も stm32f3xx_hal.h も include しない。

#pragma once

#include <stdint.h>

namespace motorcan {

// 逆引きが知らないポートを指していたときの値。
//
// **「A でなければ B」と丸めてはならない**ので、丸めない側の受け皿が要る。
// CubeMX がピンを 3 つ目のポート（GPIOF 等）へ動かしたとき、B と読み替えると
// config.h が B と書いてあるだけで「一致」になり、検査そのものが素通りする。
constexpr uint8_t kPortIndexUnknown = 0xFF;

// ピン 1 本を「ポート番号 + ピンビット」で表す。ポート番号は基板の config.h が
// 持つ enum の値そのもの（電磁弁基板なら Port::A = 0 / Port::B = 1）。
struct PortPin {
    uint8_t port;
    uint16_t pin;  // 1u << n のビットマスク（HAL の GPIO_PIN_n と同じ形）
};

// **ポートとピンの両方を見る。** 片方だけの比較は「並べてあるのに見ていない」欄を作り、
// 電磁弁基板ではそれがまさに塞ぎたかった穴だった（ピン番号しか突き合わせていないと
// ch4 を `{Port::A, 1 << 3}` と書き間違えても通る）。
constexpr bool portPinEqual(const PortPin &a, const PortPin &b) {
    // 逆引きに失敗した側は、相手も同じ 0xFF だったときに一致してしまう。
    // 「どちらのポートも分からない」は照合できていないので不一致へ倒す。
    if (a.port == kPortIndexUnknown || b.port == kPortIndexUnknown) {
        return false;
    }
    return a.port == b.port && a.pin == b.pin;
}

// actual[0..count) と expected[0..count) が 1 対 1 で一致しているか。
//
// **count == 0 は「一致」ではなく「照合できていない」。** 表の組み立てを間違えて
// 空の表を渡したときに、検査が黙って通ってしまう経路を残さない。
constexpr bool pinTablesMatch(const PortPin *actual, const PortPin *expected, uint8_t count) {
    if (actual == nullptr || expected == nullptr || count == 0) {
        return false;
    }
    for (uint8_t i = 0; i < count; ++i) {
        if (!portPinEqual(actual[i], expected[i])) {
            return false;
        }
    }
    return true;
}

}  // namespace motorcan
