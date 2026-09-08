#include "SerialLineBuffer.h"

#include <stdlib.h>

namespace motorcan {

SerialLineBuffer::SerialLineBuffer(char *storage, uint8_t capacity)
    : storage_(storage), capacity_(capacity), length_(0), pendingReset_(false) {
    if (storage_ != nullptr && capacity_ > 0) {
        storage_[0] = '\0';
    }
}

bool SerialLineBuffer::push(char c) {
    if (storage_ == nullptr || capacity_ < 2) {
        return false;
    }
    if (pendingReset_) {
        length_ = 0;
        storage_[0] = '\0';
        pendingReset_ = false;
    }

    if (c != '\n' && c != '\r') {
        if (length_ + 1 < capacity_) {
            storage_[length_++] = c;
            storage_[length_] = '\0';
        }
        return false;
    }

    if (length_ == 0) {
        return false;
    }
    pendingReset_ = true;
    return true;
}

SerialCommand parseSerialCommand(const char *line, uint8_t channelCount) {
    const SerialCommand none{SerialCommand::Kind::None, 0, nullptr};
    if (line == nullptr) {
        return none;
    }
    if (line[0] == 's' || line[0] == 'S') {
        return SerialCommand{SerialCommand::Kind::StopAll, 0, nullptr};
    }

    char *sep = nullptr;
    const long channel = strtol(line, &sep, 10);
    if (sep == line || *sep != ' ' || channel < 0 ||
        channel >= static_cast<long>(channelCount)) {
        return none;
    }
    return SerialCommand{SerialCommand::Kind::Channel, static_cast<uint8_t>(channel), sep + 1};
}

}
