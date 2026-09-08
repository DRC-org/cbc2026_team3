#pragma once

#include <stdint.h>

namespace motorcan {

class PeriodicTimer {
   public:
    PeriodicTimer() : lastMs_(0) {}

    void reset(uint32_t nowMs) { lastMs_ = nowMs; }

    void stagger(uint32_t startMs, uint32_t intervalMs, uint8_t index, uint8_t count) {
        if (count == 0) {
            reset(startMs);
            return;
        }
        lastMs_ = startMs - intervalMs + (intervalMs * index) / count;
    }

    bool due(uint32_t nowMs, uint32_t intervalMs) {
        if (nowMs - lastMs_ < intervalMs) {
            return false;
        }
        lastMs_ = nowMs;
        return true;
    }

   private:
    uint32_t lastMs_;
};

struct BoardIndication {
    explicit BoardIndication(bool canFailed)
        : canFailed_(canFailed), unconfigured_(false), stopped_(false), devices_(0) {}

    void observe(bool configured, bool latched) {
        ++devices_;
        if (!configured) {
            unconfigured_ = true;
        }
        if (latched) {
            stopped_ = true;
        }
    }

    bool urgent() const { return canFailed_ || unconfigured_ || devices_ == 0; }

    bool stopped() const { return stopped_; }

   private:
    bool canFailed_;
    bool unconfigured_;
    bool stopped_;
    uint8_t devices_;
};

inline uint32_t blinkIntervalFor(const BoardIndication &indication, uint32_t urgentMs,
                                 uint32_t stoppedMs, uint32_t heartbeatMs) {
    if (indication.urgent()) {
        return urgentMs;
    }
    if (indication.stopped()) {
        return stoppedMs;
    }
    return heartbeatMs;
}

}
