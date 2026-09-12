#include <Arduino.h>

#include "config.h"
#include "pwm.h"

#if HAS_RGB_LED
#include <Adafruit_NeoPixel.h>
#endif

// ===========================================================================
// 配線の静的検証
// ===========================================================================

// CAN ピン D4/D5 はこの版では未使用。

static constexpr uint8_t kAllPins[] = {
    kPinPwm[0], kPinPwm[1], kPinPwm[2], kPinDir[0], kPinDir[1],
    kPinDir[2], kPinRef,    kPinLed,    kPinRgb,
};
static constexpr uint8_t kAllPinCount = sizeof(kAllPins) / sizeof(kAllPins[0]);

static constexpr bool pinsAreUnique() {
    for (uint8_t i = 0; i < kAllPinCount; ++i) {
        for (uint8_t j = static_cast<uint8_t>(i + 1); j < kAllPinCount; ++j) {
            if (kAllPins[i] == kAllPins[j]) {
                return false;
            }
        }
    }
    return true;
}
static_assert(pinsAreUnique(), "config.h のピンが重複している");

static_assert(kDcChannelCount == 3,
              "チャンネル数を変えたら g_pwm の初期化子も更新すること");

// ===========================================================================
// ペリフェラルと状態（すべてチャンネル単位）
// ===========================================================================

static PwmOut g_pwm[kDcChannelCount] = {
    PwmOut(kDcChannels[0].pwmPin),
    PwmOut(kDcChannels[1].pwmPin),
    PwmOut(kDcChannels[2].pwmPin),
};

static bool g_pwmStarted[kDcChannelCount] = {false, false, false};

// 出力は状態が変わったときだけ書き換える。PwmOut::pulse_perc() はタイマのデューティ
// レジスタを直接書くので、ループ周期（数 us）で叩くと波形が乱れる。
static bool g_outputApplied = false;
static bool g_appliedStopped = false;

static uint32_t g_blinkAtMs = 0;
static bool g_ledOn = false;

// 停止側は即時、解除側だけこの時間の連続解放を待つ（チャタリングで再始動しないため）。
static constexpr uint32_t kStopReleaseDebounceMs = 50;

// 起動直後は停止側から始める。
static bool g_stopped = true;
static bool g_releaseTiming = false;
static uint32_t g_releasedAtMs = 0;

#if HAS_RGB_LED
static Adafruit_NeoPixel g_strip(1, kPinRgb, NEO_GRB + NEO_KHZ800);
#endif

// ===========================================================================
// 入力
// ===========================================================================

static bool isPhysicalStopPressed() {
    const int level = digitalRead(kPinRef);
    return kRefActiveLow ? (level == LOW) : (level == HIGH);
}

static bool updateStopState(uint32_t nowMs) {
    if (isPhysicalStopPressed()) {
        g_releaseTiming = false;
        g_stopped = true;
        return true;
    }
    if (g_stopped) {
        if (!g_releaseTiming) {
            g_releaseTiming = true;
            g_releasedAtMs = nowMs;
        } else if (nowMs - g_releasedAtMs >= kStopReleaseDebounceMs) {
            g_releaseTiming = false;
            g_stopped = false;
        }
    }
    return g_stopped;
}

// ===========================================================================
// 出力
// ===========================================================================

struct DutyOutput {
    float magnitude;
    bool reverse;
};

static DutyOutput splitDuty(float duty, float maxDuty) {
    const bool reverse = duty < 0.0f;
    float magnitude = reverse ? -duty : duty;
    if (magnitude > maxDuty) {
        magnitude = maxDuty;
    }
    return DutyOutput{magnitude, reverse};
}

static void applyChannelOutput(uint8_t ch, bool stopped) {
    if (!g_pwmStarted[ch]) {
        return;
    }

    const DutyOutput out =
        stopped ? DutyOutput{0.0f, false} : splitDuty(kFixedDuty[ch], kDcChannels[ch].maxDuty);
    digitalWrite(kDcChannels[ch].dirPin, (out.reverse != kDirForwardIsLow) ? LOW : HIGH);
    g_pwm[ch].pulse_perc(out.magnitude * 100.0f);
}

// ===========================================================================
// LED
// ===========================================================================

static void updateLed(uint32_t nowMs, bool stopped, bool forceNow) {
    const bool blinkDue = nowMs - g_blinkAtMs >= kHeartbeatIntervalMs;
    if (!forceNow && !blinkDue) {
        return;
    }
    if (blinkDue) {
        g_blinkAtMs = nowMs;
        g_ledOn = !g_ledOn;
    }

    digitalWrite(kPinLed, (!stopped && g_ledOn) ? HIGH : LOW);

#if HAS_RGB_LED
    uint8_t r = 0;
    uint8_t g = 0;
    uint8_t b = 0;
    if (stopped) {
        r = 255;
    } else {
        b = g_ledOn ? 255 : 32;
    }
    g_strip.setPixelColor(0, g_strip.Color(r, g, b));
    g_strip.show();
#endif
}

// ===========================================================================
// setup / loop
// ===========================================================================

void setup() {
    for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
        pinMode(kDcChannels[ch].pwmPin, OUTPUT);
        digitalWrite(kDcChannels[ch].pwmPin, LOW);
        pinMode(kDcChannels[ch].dirPin, OUTPUT);
        digitalWrite(kDcChannels[ch].dirPin, kDirForwardIsLow ? LOW : HIGH);
    }

    pinMode(kPinRef, INPUT_PULLUP);
    pinMode(kPinLed, OUTPUT);
    digitalWrite(kPinLed, LOW);

#if HAS_RGB_LED
    g_strip.begin();
    g_strip.setBrightness(kRgbBrightness);
#endif

    for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
        g_pwmStarted[ch] = g_pwm[ch].begin(kPwmFrequencyHz, 0.0f);
    }

    g_blinkAtMs = millis();
}

void loop() {
    const uint32_t nowMs = millis();

    const bool stopped = updateStopState(nowMs);

    const bool changed = !g_outputApplied || (g_appliedStopped != stopped);
    if (changed) {
        g_outputApplied = true;
        g_appliedStopped = stopped;
        for (uint8_t ch = 0; ch < kDcChannelCount; ++ch) {
            applyChannelOutput(ch, stopped);
        }
    }

    updateLed(nowMs, stopped, changed);
}
