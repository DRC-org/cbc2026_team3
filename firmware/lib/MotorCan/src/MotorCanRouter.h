#pragma once

#include <stdint.h>

#include "MotorCanProtocol.h"

namespace motorcan {

constexpr uint8_t kMaxChannels = 8;

struct FrameRoute {
    bool accepted;
    CommandType command;
    uint8_t channelMask;
};

FrameRoute routeFrame(uint16_t canId, bool isStandardId, const uint8_t *deviceIds,
                      uint8_t channelCount);

uint8_t readDipSwitch(const uint8_t *pins, uint8_t count, int (*readPin)(uint8_t pin),
                      int activeLevel);

void resolveDeviceIds(uint8_t *out, uint8_t count, BoardKind board, uint8_t boardNumber,
                      bool (*isDevice)(uint8_t slot));

}
