#include <Arduino.h>
#include <Arduino_CAN.h>

#include "can_backend.h"

namespace servo_can {

bool begin() {
    return CAN.begin(CanBitRate::BR_1000k);
}

bool send(uint16_t canId, uint8_t len, const uint8_t *data) {
    const CanMsg msg(CanStandardId(canId), len, data);
    return CAN.write(msg) > 0;
}

void poll(FrameHandler handler) {
    while (CAN.available()) {
        const CanMsg msg = CAN.read();
        handler(static_cast<uint16_t>(msg.getStandardId()), msg.isStandardId(), msg.data,
                msg.data_length);
    }
}

}
