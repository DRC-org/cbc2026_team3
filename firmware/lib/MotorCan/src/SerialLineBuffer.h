#pragma once

#include <stdint.h>

namespace motorcan {

constexpr uint8_t kSerialLineCapacity = 24;

class SerialLineBuffer {
   public:
    SerialLineBuffer(char *storage, uint8_t capacity);

    bool push(char c);

    const char *line() const { return storage_; }

   private:
    char *storage_;
    uint8_t capacity_;
    uint8_t length_;
    bool pendingReset_;
};

struct SerialCommand {
    enum class Kind : uint8_t {
        None,
        StopAll,
        Channel,
    };

    Kind kind;
    uint8_t channel;
    const char *value;
};

SerialCommand parseSerialCommand(const char *line, uint8_t channelCount);

}
