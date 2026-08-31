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
        // 完成した行は次の 1 文字が来るまで読めるようにしておく。
        length_ = 0;
        storage_[0] = '\0';
        pendingReset_ = false;
    }

    if (c != '\n' && c != '\r') {
        // あふれた分は捨てる。ノイズで長い行が来ても書き潰さない。
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

    // 番号と値が空白で区切られていない行は捨てる。
    // 番号を読み違えると別のアクチュエータが動くので、曖昧な入力は指令にしない。
    char *sep = nullptr;
    const long channel = strtol(line, &sep, 10);
    if (sep == line || *sep != ' ' || channel < 0 ||
        channel >= static_cast<long>(channelCount)) {
        return none;
    }
    return SerialCommand{SerialCommand::Kind::Channel, static_cast<uint8_t>(channel), sep + 1};
}

}  // namespace motorcan
