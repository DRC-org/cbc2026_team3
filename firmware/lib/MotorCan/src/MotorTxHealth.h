#pragma once

#include <stdint.h>

namespace motorcan {

class TxFailCounter {
   public:
    TxFailCounter() : streak_(0) {}

    void onSuccess() { streak_ = 0; }

    void onFailure() {
        if (streak_ < 0xFFFF) {
            ++streak_;
        }
    }

    bool isAlarming(uint16_t threshold) const { return streak_ >= threshold; }

    uint16_t streak() const { return streak_; }

   private:
    uint16_t streak_;
};

}
