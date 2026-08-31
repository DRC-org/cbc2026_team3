#include "SerialLineBuffer.h"

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

}  // namespace motorcan
