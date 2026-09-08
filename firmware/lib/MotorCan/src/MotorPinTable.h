#pragma once

#include <stdint.h>

namespace motorcan {

// ポートは自前の uint8_t 索引で持つ。HAL の GPIOA / GPIOB は
// `((GPIO_TypeDef *) GPIOA_BASE)` へ展開されるポインタキャストなので constexpr 文脈で
// 比較できない。
constexpr uint8_t kPortIndexUnknown = 0xFF;

struct PortPin {
    uint8_t port;
    uint16_t pin;
};

constexpr bool portPinEqual(const PortPin &a, const PortPin &b) {
    if (a.port == kPortIndexUnknown || b.port == kPortIndexUnknown) {
        return false;
    }
    return a.port == b.port && a.pin == b.pin;
}

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

}
