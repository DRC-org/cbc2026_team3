#pragma once

#include <stdint.h>

namespace servo_can {

using FrameHandler = void (*)(uint16_t canId, bool standard, const uint8_t *data, uint8_t len);

bool begin();

bool send(uint16_t canId, uint8_t len, const uint8_t *data);

void poll(FrameHandler handler);

}
