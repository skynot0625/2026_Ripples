// 심 LED 테스트 패턴 4종 v2 (2026-11-02) — 기울어진 윗판 반영, 20초마다 자동 로테이션
// Arduino Nano ESP32 / WS2812 / 분산 유닛 12개 x MUX 16채널.
//
// 하드웨어 초기화(핀, FastLED LCD_CAM 12레인 병렬 출력, MUX 전환, 래치 시간)는 받은 원본 스케치와 같다.
// v2: 스트립마다 실제 공간 높이로 계산한다. 윗판이 23.5° 기울어 A쪽 구멍은 바닥 위 3.72 m, Q쪽은 2.97 m이고,
// 행마다 LED 수가 달라(A 65 … Q 50) 아래 끝은 모두 바닥 위 0.50–0.54 m로 거의 수평이다(/sim 시뮬레이터와 같은 값).
//
//   1  구역별 켜고 끄기 (흰색)    구역 1 → 12를 하나씩 부드럽게 켰다 끔
//   2  아래에서 위로 스캔 (흰색)  바닥과 평행한 빛 면이 0.5 m에서 3.8 m까지 올라감(5초에 한 번). 낮은 Q쪽 꼭대기를 먼저 지남
//   3  모래시계 (흰색)            알갱이가 각 스트립 꼭대기(구멍)에서 떨어지고, 바닥부터 수평으로 모래가 차오름.
//                                 낮은 Q쪽이 먼저 다 차고 A쪽이 마지막. 다 차면 잠깐 멈췄다 빠짐
//   4  물결 (두 색: 파랑 + 따뜻한 흰색)  바닥 위 약 1.6 m의 수면이 가운데에서 바깥으로 물결침. 수면 아래는 파랑(깊을수록
//                                 어두움), 수면은 따뜻한 흰색, 위는 꺼짐
//
// 1 → 2 → 3 → 4가 20초마다 자동으로 바뀜. 시리얼 모니터(115200)에서 1–4 = 그 패턴으로 바로, a = 자동 로테이션 켜기/끄기.
//
// 가정(실물과 다르면 아래 값만 고치면 됨):
//   - 스트립 위치표 kStrips는 도면 "LED Layout & Port Map"(sim_mapping.csv, 구역/포트)대로 연결됐다고 본 값.
//     지금 실물은 구역만 도면대로라서, 같은 구역 안에서는 스트립 높이가 최대 약 0.3 m 어긋날 수 있음
//   - 데이터선 DD0..DD11 = 구역 1..12 (kUnitZone), MUX 채널 0..15 = 포트 1..16
//   - LED 0번(데이터가 들어가는 첫 LED)이 위, 그 0번은 구멍 안 LED라 끔(시뮬레이터와 같음) (kIndex0IsTop)
//   - 전류 한도 kMaxMilliamps: 전원 장치 용량에 맞춰 바꿀 것
#include <Arduino.h>
#include <esp_idf_version.h>

#if !defined(CONFIG_IDF_TARGET_ESP32S3)
#error "Select Arduino Nano ESP32 (ESP32-S3)"
#endif
#if ESP_IDF_VERSION_MAJOR < 5
#error "Use esp32 by Espressif Systems 3.x (ESP-IDF 5.x or later)"
#endif
#if defined(BOARD_HAS_PIN_REMAP) && !defined(BOARD_USES_HW_GPIO_NUMBERS)
#error "Set Tools > Pin Numbering > By GPIO number (legacy) for FastLED compatibility"
#endif

#include <FastLED.h>

#if FASTLED_VERSION != 3010005
#error "This skeleton targets FastLED 3.10.5; review the channel API before changing versions"
#endif

#include "fl/channels/channel_typed.h"
#include "platforms/esp/32/drivers/i2s/bus_traits.h"
#include <driver/gpio.h>
#include <esp_random.h>

namespace Lighting {

constexpr uint8_t kUnitCount = 12;
constexpr uint8_t kMuxChannelCount = 16;
constexpr uint16_t kLedsPerStrip = 65;   // 가장 긴 행(A) 65개. 짧은 스트립은 끝을 넘는 데이터가 무시됨
constexpr uint8_t kBrightness = 32;
constexpr uint32_t kMuxSettleUs = 10;
constexpr uint32_t kLatchUs = 300;
constexpr uint32_t kTransmitTimeoutMs = 1000;

// Physical ESP32-S3 GPIO numbers, NOT Nano Arduino pin numbers.
// Index = distributed unit number = central schematic net Dn / DDn.
constexpr uint8_t kDataGpios[] = {
    2,  // DD0  -> A1 -> J13
    4,  // DD1  -> A3 -> J14
    13, // DD2  -> A6 -> J15
    14, // DD3  -> A7 -> J16
    5,  // DD4  -> D2 -> J17
    6,  // DD5  -> D3 -> J18
    18, // DD6  -> D9 -> J19
    17, // DD7  -> D8 -> J20
    10, // DD8  -> D7 -> J21
    9,  // DD9  -> D6 -> J22
    8,  // DD10 -> D5 -> J23
    7,  // DD11 -> D4 -> J24
};

// S0 (LSB), S1, S2, S3 (MSB): Nano D10, D11, D12, A0.
constexpr uint8_t kMuxGpios[] = {21, 38, 47, 1};

static_assert(sizeof(kDataGpios) / sizeof(kDataGpios[0]) == kUnitCount,
              "One data GPIO is required per distributed unit");
static_assert(kUnitCount <= 16, "LCD_CAM supports at most 16 parallel lanes");
static_assert(kMuxChannelCount == 16, "This board uses a 16-channel MUX");
static_assert(kLedsPerStrip > 0, "A strip must contain at least one pixel");

} // namespace Lighting

// ---- 패턴 설정 -----------------------------------------------------------------------------------------
namespace Lighting {

constexpr uint8_t kStartPattern = 1;
constexpr bool kAutoRotate = true;
constexpr uint32_t kPatternMs = 20000;   // 패턴 하나 시간

// 데이터선 DDn(인덱스)이 도면의 몇 번 구역인지
constexpr uint8_t kUnitZone[kUnitCount] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12};
// true = LED 0번이 맨 위(구멍), false = 맨 아래
constexpr bool kIndex0IsTop = true;
// 전체 전류 한도(mA), 0 = 한도 없음. WS2812 한 색 최대 20 mA 어림, 192개 스트립 전체 기준.
constexpr uint32_t kMaxMilliamps = 40000;
constexpr int kLedPitchMm = 50;

// 스트립 위치표 [구역 1–12][포트 1–16] = {LED 수(구멍 LED 포함, 0 = 빈 포트), 가로 x, 앞뒤 z(+ = Q쪽), 구멍 높이} mm.
// 바닥 = 0, 판 가운데 = 0,0. /sim(sim.html)의 치수·기울기와 같은 값으로 만든 표.
struct StripGeo { uint8_t n; int16_t x, z, top; };
constexpr StripGeo kStrips[12][16] = {
    {{64,  312, -756,3675}, {65,  374, -864,3722}, {64,  436, -756,3675}, {65,  499, -864,3722}, {63,  374, -648,3628}, {63,  499, -648,3628}, {64,  561, -756,3675}, {63,  624, -648,3628}, {63,  249, -648,3628}, {62,  187, -540,3581}, {63,  125, -648,3628}, {62,   62, -540,3581}, {65,  249, -864,3722}, {64,  187, -756,3675}, {65,  125, -864,3722}, {64,   62, -756,3675}},  // 구역 1: B8 A8 B9 A9 C9 C10 B10 C11 C8 D8 C7 D7 A7 B7 A6 B6
    {{60,  686, -324,3488}, {60,  811, -324,3488}, {59,  873, -216,3441}, {58,  935, -108,3394}, {59,  748, -216,3441}, {58,  561, -108,3394}, {58,  686, -108,3394}, {58,  811, -108,3394}, {59,  624, -216,3441}, {60,  561, -324,3488}, {61,  499, -432,3534}, {62,  436, -540,3581}, {61,  748, -432,3534}, {61,  624, -432,3534}, {62,  686, -540,3581}, {62,  561, -540,3581}},  // 구역 2: F13 F14 G15 H16 G14 H13 H14 H15 G13 F12 E11 D10 E13 E12 D12 D11
    {{54,  748,  432,3159}, {54,  624,  432,3159}, {53,  686,  540,3113}, {53,  561,  540,3113}, {56,  624,  216,3253}, {55,  561,  324,3206}, {54,  499,  432,3159}, {53,  436,  540,3113}, {56,  748,  216,3253}, {57,  561,  108,3300}, {57,  686,  108,3300}, {57,  811,  108,3300}, {55,  686,  324,3206}, {55,  811,  324,3206}, {56,  873,  216,3253}, {57,  935,  108,3300}},  // 구역 3: M13 M12 N12 N11 K13 L12 M11 N10 K14 J13 J14 J15 L13 L14 K15 J16
    {{50,  249,  864,2972}, {51,  187,  756,3019}, {50,  125,  864,2972}, {51,   62,  756,3019}, {52,  249,  648,3066}, {53,  187,  540,3113}, {52,  125,  648,3066}, {53,   62,  540,3113}, {52,  374,  648,3066}, {52,  499,  648,3066}, {51,  561,  756,3019}, {52,  624,  648,3066}, {51,  312,  756,3019}, {50,  374,  864,2972}, {51,  436,  756,3019}, {50,  499,  864,2972}},  // 구역 4: Q7 P7 Q6 P6 O8 N8 O7 N7 O9 O10 P10 O11 P8 Q8 P9 Q9
    {{51, -312,  756,3019}, {50, -374,  864,2972}, {51, -436,  756,3019}, {50, -499,  864,2972}, {52, -374,  648,3066}, {52, -499,  648,3066}, {51, -561,  756,3019}, {52, -624,  648,3066}, {52, -249,  648,3066}, {53, -187,  540,3113}, {52, -125,  648,3066}, {53,  -62,  540,3113}, {50, -249,  864,2972}, {51, -187,  756,3019}, {50, -125,  864,2972}, {51,  -62,  756,3019}},  // 구역 5: P3 Q2 P2 Q1 O3 O2 P1 O1 O4 N5 O5 N6 Q3 P4 Q4 P5
    {{55, -686,  324,3206}, {55, -811,  324,3206}, {56, -873,  216,3253}, {57, -935,  108,3300}, {56, -748,  216,3253}, {57, -561,  108,3300}, {57, -686,  108,3300}, {57, -811,  108,3300}, {56, -624,  216,3253}, {55, -561,  324,3206}, {54, -499,  432,3159}, {53, -436,  540,3113}, {54, -748,  432,3159}, {54, -624,  432,3159}, {53, -686,  540,3113}, {53, -561,  540,3113}},  // 구역 6: L2 L1 K1 J1 K2 J4 J3 J2 K3 L3 M3 N3 M1 M2 N1 N2
    {{61, -748, -432,3534}, {61, -624, -432,3534}, {62, -686, -540,3581}, {62, -561, -540,3581}, {59, -624, -216,3441}, {60, -561, -324,3488}, {61, -499, -432,3534}, {62, -436, -540,3581}, {59, -748, -216,3441}, {58, -561, -108,3394}, {58, -686, -108,3394}, {58, -811, -108,3394}, {60, -686, -324,3488}, {60, -811, -324,3488}, {59, -873, -216,3441}, {58, -935, -108,3394}},  // 구역 7: E1 E2 D1 D2 G3 F3 E3 D3 G2 H4 H3 H2 F2 F1 G1 H1
    {{65, -249, -864,3722}, {64, -187, -756,3675}, {65, -125, -864,3722}, {64,  -62, -756,3675}, {63, -249, -648,3628}, {62, -187, -540,3581}, {63, -125, -648,3628}, {62,  -62, -540,3581}, {63, -374, -648,3628}, {63, -499, -648,3628}, {64, -561, -756,3675}, {63, -624, -648,3628}, {64, -312, -756,3675}, {65, -374, -864,3722}, {64, -436, -756,3675}, {65, -499, -864,3722}},  // 구역 8: A3 B4 A4 B5 C4 D5 C5 D6 C3 C2 B1 C1 B3 A2 B2 A1
    {{60,  312, -324,3488}, {60,  436, -324,3488}, {59,  499, -216,3441}, { 0,    0,    0,   0}, {59,  249, -216,3441}, {59,  374, -216,3441}, {58,  312, -108,3394}, {58,  436, -108,3394}, {60,  187, -324,3488}, {60,   62, -324,3488}, {61,  125, -432,3534}, { 0,    0,    0,   0}, {61,  374, -432,3534}, {61,  249, -432,3534}, {62,  312, -540,3581}, { 0,    0,    0,   0}},  // 구역 9: F10 F11 G12 - G10 G11 H11 H12 F9 F8 E8 - E10 E9 D9 -
    {{54,  374,  432,3159}, {54,  249,  432,3159}, {53,  312,  540,3113}, { 0,    0,    0,   0}, {55,  187,  324,3206}, {55,   62,  324,3206}, {54,  125,  432,3159}, { 0,    0,    0,   0}, {56,  249,  216,3253}, {56,  374,  216,3253}, {57,  312,  108,3300}, {57,  436,  108,3300}, {55,  312,  324,3206}, {55,  436,  324,3206}, {56,  499,  216,3253}, { 0,    0,    0,   0}},  // 구역 10: M10 M9 N9 - L9 L8 M8 - K10 K11 J11 J12 L10 L11 K12 -
    {{55, -312,  324,3206}, {55, -436,  324,3206}, {56, -499,  216,3253}, { 0,    0,    0,   0}, {56, -249,  216,3253}, {56, -374,  216,3253}, {57, -312,  108,3300}, {57, -436,  108,3300}, {55, -187,  324,3206}, {55,  -62,  324,3206}, {54, -125,  432,3159}, { 0,    0,    0,   0}, {54, -374,  432,3159}, {54, -249,  432,3159}, {53, -312,  540,3113}, { 0,    0,    0,   0}},  // 구역 11: L5 L4 K4 - K6 K5 J6 J5 L6 L7 M6 - M4 M5 N4 -
    {{61, -374, -432,3534}, {61, -249, -432,3534}, {62, -312, -540,3581}, { 0,    0,    0,   0}, {60, -187, -324,3488}, {60,  -62, -324,3488}, {61, -125, -432,3534}, { 0,    0,    0,   0}, {59, -249, -216,3441}, {59, -374, -216,3441}, {58, -312, -108,3394}, {58, -436, -108,3394}, {60, -312, -324,3488}, {60, -436, -324,3488}, {59, -499, -216,3441}, { 0,    0,    0,   0}},  // 구역 12: E4 E5 D4 - F6 F7 E6 - G6 G5 H6 H5 F5 F4 G4 -
};

// 4번 두 색
const CRGB kWater = CRGB(10, 45, 255);      // 파랑
const CRGB kCrest = CRGB(255, 205, 140);    // 따뜻한 흰색

} // namespace Lighting

using namespace Lighting;

// One staging buffer per parallel lane; reused after the transfer completes.
CRGB laneLeds[kUnitCount][kLedsPerStrip];
// 이번 프레임의 전체 색: [유닛][MUX 채널][LED 인덱스]
CRGB pixels[kUnitCount][kMuxChannelCount][kLedsPerStrip];
uint8_t pattern = kStartPattern;
bool autoRotate = kAutoRotate;
uint32_t patternStartMs = 0;

void haltOnError(const char* message) {
    Serial.println(message);
    // Keep the MUX address unchanged if a transfer has stalled.
    while (true) {
        delay(1000);
    }
}

void initLowOutput(uint8_t gpio) {
    const auto pin = static_cast<gpio_num_t>(gpio);
    gpio_set_level(pin, 0);
    gpio_set_direction(pin, GPIO_MODE_OUTPUT);
}

void initPins() {
    for (uint8_t gpio : kDataGpios) {
        initLowOutput(gpio);
    }
    for (uint8_t gpio : kMuxGpios) {
        initLowOutput(gpio);
    }
    delayMicroseconds(kLatchUs);
}

void initFastLed() {
    // Explicitly register/select the 16-lane S3 LCD_CAM driver.
    // Do not rely on RMT's default driver selection for 12 simultaneous lanes.
    FastLED.setExclusiveDriver<fl::Bus::I2S>();
    const auto timing = fl::makeTimingConfig<fl::TIMING_WS2812_800KHZ>();
    for (uint8_t unit = 0; unit < kUnitCount; ++unit) {
        fl::ChannelConfigOf<fl::ClocklessChipset> config{
            fl::ClocklessChipset(kDataGpios[unit], timing),
            fl::span<CRGB>(laneLeds[unit], kLedsPerStrip), GRB};
        config.options.mBus = fl::Bus::I2S;
        auto channel =
            fl::TypedChannel<fl::Bus::I2S, fl::ClocklessChipset>::create(config);
        if (!channel) {
            haltOnError("ERROR: FastLED channel creation failed");
        }
        FastLED.add(channel);
    }
    FastLED.setBrightness(kBrightness);
    FastLED.setDither(0);
    FastLED.setMaxRefreshRate(0);
}

void selectMuxChannel(uint8_t channel) {
    // Caller guarantees previous transfer AND latch time are complete.
    // COM is LOW while S bits change; /E is hard-wired LOW on the PCB.
    for (uint8_t bit = 0; bit < 4; ++bit) {
        gpio_set_level(static_cast<gpio_num_t>(kMuxGpios[bit]),
                       (channel >> bit) & 1U);
    }
    delayMicroseconds(kMuxSettleUs);
}

void sendSelectedStrips() {
    // A SINGLE show queues all twelve lanes into the same parallel transfer.
    FastLED.show();
    if (!FastLED.wait(kTransmitTimeoutMs)) {
        haltOnError("ERROR: LED transfer timed out; MUX address held");
    }
    // Keep the old channel selected for an additional LOW reset/latch interval before switching the MUX.
    delayMicroseconds(kLatchUs);
}

// 전체 프레임의 어림 전류로 이번 프레임 밝기를 정함 (한도 안이면 kBrightness 그대로)
void applyPowerLimit() {
    if (kMaxMilliamps == 0) {
        FastLED.setBrightness(kBrightness);
        return;
    }
    uint64_t sum = 0;
    const CRGB* p = &pixels[0][0][0];
    for (uint32_t i = 0; i < uint32_t(kUnitCount) * kMuxChannelCount * kLedsPerStrip; ++i) {
        sum += p[i].r + p[i].g + p[i].b;
    }
    const double milliamps = sum / 255.0 * 20.0 * kBrightness / 255.0;
    uint8_t b = kBrightness;
    if (milliamps > kMaxMilliamps) b = static_cast<uint8_t>(kBrightness * (kMaxMilliamps / milliamps));
    FastLED.setBrightness(b);
}

// 16개 MUX 슬롯을 차례로: 슬롯마다 12개 유닛의 그 채널 스트립을 한 번에 보냄
void showAllStrips() {
    applyPowerLimit();
    for (uint8_t channel = 0; channel < kMuxChannelCount; ++channel) {
        selectMuxChannel(channel);
        for (uint8_t unit = 0; unit < kUnitCount; ++unit) {
            ::memcpy(static_cast<void*>(laneLeds[unit]), static_cast<const void*>(pixels[unit][channel]), sizeof(laneLeds[unit]));
        }
        sendSelectedStrips();
    }
}

void clearPixels() {
    ::memset(static_cast<void*>(pixels), 0, sizeof(pixels));   // void*: FastLED's own fl::memset is not picked
}

const StripGeo& geoOf(uint8_t unit, uint8_t ch) { return kStrips[kUnitZone[unit] - 1][ch]; }

// 위에서 j번째 LED(0 = 구멍 안 LED)
CRGB& ledFromTop(uint8_t unit, uint8_t ch, int j) {
    const int n = geoOf(unit, ch).n;
    return pixels[unit][ch][kIndex0IsTop ? j : n - 1 - j];
}

// 켜는 LED(구멍 LED 제외) 하나하나에: f(unit, ch, j, 높이 mm, 스트립)
template <typename F>
void forEachLed(F f) {
    for (uint8_t u = 0; u < kUnitCount; ++u) {
        for (uint8_t ch = 0; ch < kMuxChannelCount; ++ch) {
            const StripGeo& g = geoOf(u, ch);
            for (int j = 1; j < g.n; ++j) f(u, ch, j, g.top - j * kLedPitchMm, g);
        }
    }
}

uint32_t rand32(uint32_t lo, uint32_t hi) { return lo + esp_random() % (hi - lo + 1); }
CRGB scaled(const CRGB& c, float v) { return CRGB(uint8_t(c.r * v), uint8_t(c.g * v), uint8_t(c.b * v)); }
CRGB white(float v) { const uint8_t c = uint8_t(255 * fminf(1, fmaxf(0, v))); return CRGB(c, c, c); }

// ---- 1. 구역별 켜고 끄기 (흰색): 20초 동안 구역 1→12, 구역마다 0.3초에 켜지고 0.3초에 꺼짐 -------------
int lastZone = -1;
void patternZones(uint32_t t) {
    clearPixels();
    const uint32_t slot = kPatternMs / 12, in = t % slot;
    const uint8_t zone = (t / slot) % 12 + 1;
    const float on = slot * 0.82f;
    float v = 0;
    if (in < 300) v = in / 300.0f;
    else if (in < on - 300) v = 1;
    else if (in < on) v = (on - in) / 300.0f;
    for (uint8_t u = 0; u < kUnitCount; ++u) {
        if (kUnitZone[u] != zone) continue;
        for (uint8_t ch = 0; ch < kMuxChannelCount; ++ch) {
            const StripGeo& g = geoOf(u, ch);
            for (int j = 1; j < g.n; ++j) ledFromTop(u, ch, j) = white(v);
        }
    }
    if (zone != lastZone) { Serial.printf("구역 %u\n", zone); lastZone = zone; }
}

// ---- 2. 아래에서 위로 스캔 (흰색): 바닥과 평행한 빛 면이 올라감 -----------------------------------------
void patternScan(uint32_t t) {
    clearPixels();
    const float level = 400 + 3500.0f * float(t % 5000) / 5000;   // 0.4 m → 3.9 m
    forEachLed([&](uint8_t u, uint8_t ch, int j, int y, const StripGeo&) {
        const float d = y - level;
        // 면 바로 위는 밝고 짧게, 아래(지나온 쪽)는 길게 흐려짐
        const float v = d >= 0 ? 1 - d / 120.0f : 1 + d / 400.0f;
        if (v > 0) ledFromTop(u, ch, j) = white(v);
    });
}

// ---- 3. 모래시계 (흰색): 16초 동안 차오름 → 1.5초 멈춤 → 2.5초 빠짐 -----------------------------------
struct Grain { float y; float v; uint32_t wait; };
Grain grains[kUnitCount][kMuxChannelCount];
uint32_t sandLastMs = 0;
constexpr float kSandBottom = 450, kSandTop = 3760, kGravity = 2500;   // mm, mm/s² (실제보다 느리게)

void resetSand() {
    for (auto& row : grains) for (auto& g : row) g = {-1, 0, rand32(0, 1500)};
    sandLastMs = 0;
}

void patternSand(uint32_t t) {
    clearPixels();
    const float dt = sandLastMs ? (t - sandLastMs) / 1000.0f : 0;
    sandLastMs = t;
    const uint32_t c = t % kPatternMs;
    float level;
    const bool filling = c < 16000;
    if (filling) level = kSandBottom + (kSandTop - kSandBottom) * c / 16000.0f;
    else if (c < 17500) level = kSandTop;
    else level = kSandTop - (kSandTop - kSandBottom) * (c - 17500) / 2500.0f;
    // 쌓인 모래: 수평면 아래는 은은하게, 표면 한 칸은 조금 더 밝게
    forEachLed([&](uint8_t u, uint8_t ch, int j, int y, const StripGeo&) {
        if (y <= level - kLedPitchMm) ledFromTop(u, ch, j) = white(0.35f);
        else if (y <= level) ledFromTop(u, ch, j) = white(0.7f);
    });
    // 떨어지는 알갱이: 스트립마다 하나, 구멍에서 출발해 모래 면에 닿으면 다시 위에서
    for (uint8_t u = 0; u < kUnitCount; ++u) {
        for (uint8_t ch = 0; ch < kMuxChannelCount; ++ch) {
            const StripGeo& s = geoOf(u, ch);
            if (!s.n) continue;
            Grain& g = grains[u][ch];
            const float bottom = s.top - (s.n - 1) * kLedPitchMm;
            if (!filling || level >= s.top - kLedPitchMm) { g.y = -1; continue; }   // 다 찼거나 빠지는 중
            if (g.y < 0) {
                const uint32_t ms = uint32_t(dt * 1000);
                if (g.wait > ms) { g.wait -= ms; continue; }
                g = {float(s.top - kLedPitchMm), 0, 0};
            }
            g.v += kGravity * dt;
            g.y -= g.v * dt;
            if (g.y <= fmaxf(level, bottom)) { g = {-1, 0, rand32(150, 1500)}; continue; }
            const int j = int(lroundf((s.top - g.y) / kLedPitchMm));
            if (j >= 1 && j < s.n) ledFromTop(u, ch, j) = white(1);
        }
    }
}

// ---- 4. 물결 (파랑 + 따뜻한 흰색) ----------------------------------------------------------------------
void patternWater(uint32_t t) {
    clearPixels();
    const float ts = t / 1000.0f;
    for (uint8_t u = 0; u < kUnitCount; ++u) {
        for (uint8_t ch = 0; ch < kMuxChannelCount; ++ch) {
            const StripGeo& s = geoOf(u, ch);
            if (!s.n) continue;
            // 판 가운데에서 바깥으로 퍼지는 물결: 반경 1.1 m마다 한 마루, 4초에 한 파장
            const float r = sqrtf(float(s.x) * s.x + float(s.z) * s.z);
            const float surface = 1600 + 160 * sinf(2 * PI * (r / 1100.0f - ts / 4.0f)) + 60 * sinf(2 * PI * ts / 9.0f);
            for (int j = 1; j < s.n; ++j) {
                const float d = surface - (s.top - j * kLedPitchMm);   // + = 수면 아래
                CRGB c = CRGB::Black;
                if (d >= -40 && d <= 40) c = kCrest;                                       // 수면
                else if (d > 40) {
                    const float k = fmaxf(0, 1 - (d - 40) / 140.0f);                       // 수면 바로 아래는 흰색이 남음
                    const float deep = 0.12f + 0.75f * expf(-(d - 40) / 650.0f);           // 깊을수록 어두운 파랑
                    c = scaled(kWater, deep);
                    c.r = uint8_t(c.r + (kCrest.r - c.r) * k * 0.6f);
                    c.g = uint8_t(c.g + (kCrest.g - c.g) * k * 0.6f);
                    c.b = uint8_t(c.b + (kCrest.b - c.b) * k * 0.6f);
                }
                ledFromTop(u, ch, j) = c;
            }
        }
    }
}

// ---- 공통 ----------------------------------------------------------------------------------------------
const char* patternName(uint8_t p) {
    switch (p) {
        case 1: return "구역별 켜고 끄기 (흰색)";
        case 2: return "아래에서 위로 스캔 (흰색)";
        case 3: return "모래시계 (흰색)";
        default: return "물결 (파랑 + 따뜻한 흰색)";
    }
}

void setPattern(uint8_t p) {
    pattern = p;
    patternStartMs = millis();
    lastZone = -1;
    if (p == 3) resetSand();
    Serial.printf("패턴 %u: %s%s\n", p, patternName(p), autoRotate ? " (20초 뒤 다음)" : "");
}

void readSerial() {
    while (Serial.available()) {
        const char c = Serial.read();
        if (c >= '1' && c <= '4') setPattern(c - '0');
        if (c == 'a' || c == 'A') {
            autoRotate = !autoRotate;
            patternStartMs = millis();
            Serial.printf("자동 로테이션 %s\n", autoRotate ? "켜짐" : "꺼짐");
        }
    }
}

uint32_t statsAt = 0, statsFrames = 0;

void setup() {
    Serial.begin(115200);
    initPins();
    initFastLed();
    clearPixels();
    showAllStrips();
    Serial.println("심 LED 테스트 패턴 v2: 1–4 = 패턴, a = 자동 로테이션 켜기/끄기 (20초)");
    setPattern(kStartPattern);
    statsAt = millis();
}

void loop() {
    readSerial();
    if (autoRotate && millis() - patternStartMs >= kPatternMs) setPattern(pattern % 4 + 1);
    const uint32_t t = millis() - patternStartMs;
    switch (pattern) {
        case 1: patternZones(t); break;
        case 2: patternScan(t); break;
        case 3: patternSand(t); break;
        default: patternWater(t); break;
    }
    showAllStrips();
    ++statsFrames;
    if (millis() - statsAt >= 5000) {
        Serial.printf("FPS=%.1f, 밝기=%u\n", statsFrames * 1000.0 / (millis() - statsAt), FastLED.getBrightness());
        statsAt = millis();
        statsFrames = 0;
        delay(1);
    }
}
