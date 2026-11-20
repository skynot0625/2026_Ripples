// Nano ESP32: USB reception on core0, double-buffered FastLED output on core1.
// Build with build_optimized.py and the project-local vendor/FastLED patch.
// Core: esp32 3.3.12; FastLED 3.10.5; Pin Numbering: By GPIO number (legacy).
#include <Arduino.h>
#include <esp_idf_version.h>
#if !defined(CONFIG_IDF_TARGET_ESP32S3)
#error "Select Arduino Nano ESP32 (ESP32-S3)"
#endif
#if defined(BOARD_HAS_PIN_REMAP) && !defined(BOARD_USES_HW_GPIO_NUMBERS)
#error "Tools > Pin Numbering > By GPIO number (legacy)"
#endif
#if !ARDUINO_USB_CDC_ON_BOOT
#error "Enable USB CDC on boot: Serial must use native USB, not UART0"
#endif
#include <FastLED.h>
#include <driver/gpio.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/semphr.h>
#include <stdarg.h>
#include <esp_random.h>
#include <esp_wifi.h>
#include <esp_bt.h>
// Fixed 12-line I2S/LCD_CAM backend. The pin-mask version is a separate sketch.
#include "fl/channels/channel_typed.h"
#include "platforms/esp/32/drivers/i2s/bus_traits.h"
#include "platforms/esp/32/drivers/i2s/i2s_lcd_cam_peripheral_esp.h"
#if FASTLED_VERSION != 3010005
#error "This example is checked with FastLED 3.10.5"
#endif
#ifndef LED_PATCH_PROFILE
#define LED_PATCH_PROFILE 0
#endif
#if LED_PATCH_PROFILE
extern "C" uint64_t lighting_encode_us();
extern "C" uint64_t lighting_encode_calls();
extern "C" uint32_t lighting_pixel_checks();
#endif

constexpr uint8_t UNIT_COUNT = 12;
constexpr uint8_t CHANNEL_COUNT = 16;
constexpr uint16_t MAX_LEDS = 66;
constexpr uint16_t MAX_VISIBLE = 65;
constexpr uint8_t BRIGHTNESS = 32;
constexpr uint32_t RX_TIMEOUT_MS = 2000;
constexpr uint32_t LATCH_US = 300;
constexpr uint8_t DATA_PINS[] = {2, 4, 13, 14, 5, 6, 18, 17, 10, 9, 8, 7};
constexpr uint8_t MUX_PINS[] = {21, 38, 47, 1}; // S0..S3, least significant first.

// BEGIN TRANSPORT: portable byte parser, also exercised by host tests.
class FrameReceiver {
public:
    static constexpr uint32_t CAPACITY = 12UL * 16 * MAX_VISIBLE * 2;
    uint8_t* pixels;
    explicit FrameReceiver(uint8_t* destination) : pixels(destination) {}
    uint32_t sequence = 0;
    uint32_t payloadCrc = 0;
    uint16_t command = 0;
    uint8_t components = 2;
    uint16_t leds = 0;
    enum Result { NONE, COMPLETE, BAD_HEADER, BAD_CRC, INTERNAL_BENCH };

    bool active() const { return matched != 0 || headerUsed != 0 || receiving; }
    void reset() { matched = headerUsed = used = 0; receiving = false; }

    Result feed(uint8_t byte) {
        if (receiving) {
            pixels[used++] = byte;
            crc ^= byte;
            for (uint8_t bit = 0; bit < 8; ++bit)
                crc = (crc >> 1) ^ ((crc & 1) ? 0xEDB88320UL : 0);
            if (used == length) {
                payloadCrc = crc ^ 0xFFFFFFFFUL;
                const bool valid = payloadCrc == expectedCrc;
                reset();
                return valid ? COMPLETE : BAD_CRC;
            }
            return NONE;
        }
        if (matched < 4) {
            const uint8_t magic[] = {'L', 'E', 'D', '2'};
            matched = byte == magic[matched] ? matched + 1 : (byte == 'L' ? 1 : 0);
            return NONE;
        }
        header[headerUsed++] = byte;
        if (headerUsed < sizeof(header)) return NONE;
        sequence = le32(header);
        leds = static_cast<uint16_t>(header[4] | (uint16_t(header[5]) << 8));
        const uint16_t flags = header[6] | (uint16_t(header[7]) << 8);
        length = le32(header + 8);
        expectedCrc = le32(header + 12);
        command = (flags == 1 || flags == 2) ? flags : (flags == 7 ? 4 : 0);
        // Control-only benchmark request. N is frame count here; no pixel payload.
        if ((flags == 1 || flags == 2) && leds >= 1 && leds <= 1000 && length == 0 && expectedCrc == 0) {
            reset();
            return INTERNAL_BENCH;
        }
        if (leds < 1 || leds > MAX_VISIBLE || (flags != 6 && flags != 7) ||
            length != 12UL * 16 * leds * components || length > CAPACITY) {
            reset();
            return BAD_HEADER;
        }
        crc = 0xFFFFFFFFUL;
        used = 0;
        receiving = true;
        return NONE;
    }
private:
    static uint32_t le32(const uint8_t* b) {
        return uint32_t(b[0]) | (uint32_t(b[1]) << 8) |
               (uint32_t(b[2]) << 16) | (uint32_t(b[3]) << 24);
    }
    uint8_t header[16] = {};
    uint32_t matched = 0, headerUsed = 0, used = 0, length = 0;
    uint32_t expectedCrc = 0, crc = 0;
    bool receiving = false;
};
// END TRANSPORT

struct FrameSlot {
    uint8_t pixels[FrameReceiver::CAPACITY];
    uint32_t sequence, receiveUs, receiveEndUs, crc;
    uint16_t leds;
    uint16_t command;
    uint8_t components;
};
FrameSlot slots[2]; // Exclusive ownership: FREE -> RX -> READY -> DISPLAY -> FREE.
// Current agreed Zone1->DD0 ... Zone12->DD11 physical lengths.
constexpr uint8_t PHYSICAL_LENGTHS[12][16] = {
    {65, 66, 65, 66, 64, 64, 65, 64, 64, 63, 64, 63, 66, 65, 66, 65},
    {61, 61, 60, 59, 60, 59, 59, 59, 60, 61, 62, 63, 62, 62, 63, 63},
    {54, 54, 53, 53, 56, 55, 54, 53, 56, 57, 57, 57, 55, 55, 56, 57},
    {50, 51, 50, 51, 52, 53, 52, 53, 52, 52, 51, 52, 51, 50, 51, 50},
    {51, 50, 51, 50, 52, 52, 51, 52, 52, 53, 52, 53, 50, 51, 50, 51},
    {55, 55, 56, 57, 56, 57, 57, 57, 56, 55, 54, 53, 54, 54, 53, 53},
    {62, 62, 63, 63, 60, 61, 62, 63, 60, 59, 59, 59, 61, 61, 60, 59},
    {66, 65, 66, 65, 64, 63, 64, 63, 64, 64, 65, 64, 65, 66, 65, 66},
    {61, 61, 60, 0, 60, 60, 59, 59, 61, 61, 62, 0, 62, 62, 63, 0},
    {54, 54, 53, 0, 55, 55, 54, 0, 56, 56, 57, 57, 55, 55, 56, 0},
    {55, 55, 56, 0, 56, 56, 57, 57, 55, 55, 54, 0, 54, 54, 53, 0},
    {62, 62, 63, 0, 61, 61, 62, 0, 60, 60, 59, 59, 61, 61, 60, 0},
};
QueueHandle_t freeSlots, readySlots;
SemaphoreHandle_t serialLock;
TaskHandle_t rxTaskHandle = nullptr;
CRGB lane[UNIT_COUNT][MAX_LEDS];
struct PhaseTimes { uint64_t mux=0, fill=0, show=0, wait=0, latch=0; };
PhaseTimes phases;
bool collectPhases = false;
bool wifiDisabled = false, bleDisabled = false;

void disableWireless() {
    wifi_mode_t mode;
    esp_err_t status = esp_wifi_get_mode(&mode);
    if (status == ESP_OK) {
        esp_err_t stop = esp_wifi_stop();
        if (stop != ESP_OK && stop != ESP_ERR_WIFI_NOT_STARTED && stop != ESP_ERR_WIFI_NOT_INIT)
            ESP_ERROR_CHECK(stop);
        ESP_ERROR_CHECK(esp_wifi_deinit());
    } else if (status != ESP_ERR_WIFI_NOT_INIT) {
        ESP_ERROR_CHECK(status);
    }
    wifiDisabled = true;
#if CONFIG_BT_ENABLED
    if (esp_bt_controller_get_status() == ESP_BT_CONTROLLER_STATUS_ENABLED)
        ESP_ERROR_CHECK(esp_bt_controller_disable());
    if (esp_bt_controller_get_status() == ESP_BT_CONTROLLER_STATUS_INITED)
        ESP_ERROR_CHECK(esp_bt_controller_deinit());
    bleDisabled = esp_bt_controller_get_status() == ESP_BT_CONTROLLER_STATUS_IDLE;
#else
    bleDisabled = true;
#endif
}

void message(const char* format, ...) {
    char line[192];
    va_list args;
    va_start(args, format);
    vsnprintf(line, sizeof(line), format, args);
    va_end(args);
    xSemaphoreTake(serialLock, portMAX_DELAY);
    Serial.print(line); // One complete line under lock, even with two producers.
    xSemaphoreGive(serialLock);
}

void initPins() {
    for (uint8_t pin : DATA_PINS) {
        gpio_reset_pin(static_cast<gpio_num_t>(pin));
        gpio_set_level(static_cast<gpio_num_t>(pin), 0);
        gpio_set_direction(static_cast<gpio_num_t>(pin), GPIO_MODE_OUTPUT);
    }
    for (uint8_t pin : MUX_PINS) {
        gpio_reset_pin(static_cast<gpio_num_t>(pin));
        gpio_set_level(static_cast<gpio_num_t>(pin), 0);
        gpio_set_direction(static_cast<gpio_num_t>(pin), GPIO_MODE_OUTPUT);
    }
    delayMicroseconds(LATCH_US);
}

void initFastLED() {
    // Only this core may call FastLED or change MUX address/data buffers.
    FastLED.setExclusiveDriver<fl::Bus::I2S>();
    const auto timing = fl::makeTimingConfig<fl::TIMING_WS2812_800KHZ>();
    for (uint8_t unit = 0; unit < UNIT_COUNT; ++unit) {
        fl::ChannelConfigOf<fl::ClocklessChipset> config{
            fl::ClocklessChipset(DATA_PINS[unit], timing),
            fl::span<CRGB>(lane[unit], MAX_LEDS), GRB};
        config.options.mBus = fl::Bus::I2S;
        auto channel = fl::TypedChannel<fl::Bus::I2S, fl::ClocklessChipset>::create(config);
        if (!channel) abort();
        FastLED.add(channel);
    }
    FastLED.setBrightness(BRIGHTNESS);
    FastLED.setDither(0);
    FastLED.setMaxRefreshRate(0);
}

void selectMux(uint8_t channel) {
    // Previous show AND latch have already completed; DATA is LOW here.
    for (uint8_t bit = 0; bit < 4; ++bit)
        gpio_set_level(static_cast<gpio_num_t>(MUX_PINS[bit]), (channel >> bit) & 1);
    delayMicroseconds(10);
}

void renderFrame(const FrameSlot* frame) {
    for (uint8_t channel = 0; channel < CHANNEL_COUNT; ++channel) {
        uint32_t phaseStart = collectPhases ? micros() : 0;
        selectMux(channel);
        if (collectPhases) { phases.mux += uint32_t(micros()-phaseStart); phaseStart=micros(); }
        for (uint8_t unit = 0; unit < UNIT_COUNT; ++unit) {
            for (uint16_t k = 0; k < MAX_LEDS; ++k) {
                if (!frame || k == 0 || k-1 >= frame->leds) {
                    // Hidden first physical LED is always OFF. Pad remainder with black.
                    lane[unit][k] = CRGB::Black;
                } else {
                    const uint32_t offset = ((unit * 16UL + channel) * frame->leds + (k-1)) * 2;
                    const uint16_t v = frame->pixels[offset] | (uint16_t(frame->pixels[offset+1]) << 8);
                    const uint8_t r = v >> 11, g = (v >> 5) & 63, b = v & 31;
                    lane[unit][k] = CRGB((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2));
                }
            }
        }
        if (collectPhases) { phases.fill += uint32_t(micros()-phaseStart); phaseStart=micros(); }
        FastLED.show();
        if (collectPhases) { phases.show += uint32_t(micros()-phaseStart); phaseStart=micros(); }
        if (!FastLED.wait(1000)) {
            message("ERR LED_TIMEOUT\n");
            while (true) delay(1000); // Never switch MUX while transfer is incomplete.
        }
        if (collectPhases) { phases.wait += uint32_t(micros()-phaseStart); phaseStart=micros(); }
        delayMicroseconds(LATCH_US);
        if (collectPhases) phases.latch += uint32_t(micros()-phaseStart);
    }
}

uint32_t randomState = 1;
uint32_t nextRandom() {
    // Seed once from hardware RNG. Pixel generation uses a fast deterministic PRNG.
    randomState ^= randomState << 13;
    randomState ^= randomState >> 17;
    randomState ^= randomState << 5;
    return randomState;
}

void randomize(FrameSlot& frame, bool solid) {
    frame.leds = MAX_VISIBLE;
    frame.components = 2;
    for (uint8_t u = 0; u < 12; ++u) {
        for (uint8_t p = 0; p < 16; ++p) {
            uint32_t color = nextRandom();
            for (uint16_t k = 0; k < MAX_VISIBLE; ++k) {
                if (!solid) color = nextRandom();
                const uint32_t offset = ((u * 16UL + p) * MAX_VISIBLE + k) * 2;
                bool on = k+1 < PHYSICAL_LENGTHS[u][p];
                frame.pixels[offset] = on ? uint8_t(color) : 0;
                frame.pixels[offset+1] = on ? uint8_t(color >> 8) : 0;
            }
        }
    }
}

void internalBenchmark(FrameSlot& frame) {
    const uint16_t count = frame.leds;
    const bool solid = frame.command == 2;
    for (uint8_t warmup = 0; warmup < 3; ++warmup) {
        randomize(frame, solid);
        renderFrame(&frame);
    }
    uint64_t randomUs = 0, scanUs = 0;
    phases = PhaseTimes{};
    collectPhases = true;
#if LED_PATCH_PROFILE
    uint64_t encodeBefore=lighting_encode_us(), callsBefore=lighting_encode_calls();
#endif
    uint32_t minUs = UINT32_MAX, maxUs = 0;
    uint32_t start = micros();
    for (uint16_t i = 0; i < count; ++i) {
        uint32_t t = micros();
        randomize(frame, solid);
        uint32_t before = micros();
        randomUs += static_cast<uint32_t>(before-t);
        renderFrame(&frame);
        uint32_t scan = micros()-before;
        scanUs += scan;
        minUs = min(minUs, scan); maxUs = max(maxUs, scan);
        vTaskDelay(1); // Let the core's idle/watchdog task run even in MCU-only loops.
    }
    uint32_t elapsed = micros()-start;
    collectPhases = false;
    uint32_t pixelChecks = 0;
#if LED_PATCH_PROFILE
    pixelChecks = lighting_pixel_checks();
#endif
    message("ENV %lu %lu %u %u %lu\n", (unsigned long)getCpuFrequencyMhz(),
            (unsigned long)getApbFrequency(), wifiDisabled, bleDisabled, (unsigned long)pixelChecks);
    uint64_t encodeUs=0, encodeCalls=0;
#if LED_PATCH_PROFILE
    encodeUs=lighting_encode_us()-encodeBefore;
    encodeCalls=lighting_encode_calls()-callsBefore;
#endif
    message("PHASE %llu %llu %llu %llu %llu %llu %llu\n",
            (unsigned long long)phases.mux, (unsigned long long)phases.fill,
            (unsigned long long)phases.show, (unsigned long long)phases.wait,
            (unsigned long long)phases.latch, (unsigned long long)encodeUs,
            (unsigned long long)encodeCalls);
    // No PC pixel traffic and no per-frame logging in the timed interval.
    message("BENCH %lu %u %lu %llu %llu %lu %lu %s\n", (unsigned long)frame.sequence,
            count, (unsigned long)elapsed, (unsigned long long)randomUs,
            (unsigned long long)scanUs, (unsigned long)minUs, (unsigned long)maxUs,
            solid ? "solid_strip" : "random_pixel");
    renderFrame(nullptr);
}

void receiveTask(void*) {
    uint8_t index;
    uint8_t bytes[256];
    size_t cursor = 0, count = 0; // Preserve the next header across buffer handoff.
    for (;;) {
        // With no free buffer, USB applies backpressure. Never overwrite DISPLAY/READY.
        xQueueReceive(freeSlots, &index, portMAX_DELAY);
        FrameReceiver receiver(slots[index].pixels);
        uint32_t firstByteUs = 0, lastByteAt = millis();
        bool complete = false;
        while (!complete) {
            if (receiver.active() && static_cast<uint32_t>(millis() - lastByteAt) > RX_TIMEOUT_MS) {
                receiver.reset();
                message("ERR RX_TIMEOUT\n");
            }
            // Limit to available bytes and keep a bounded stack buffer.
            if (cursor == count) {
                int available = Serial.available();
                if (available <= 0) {
                    // Event wake-up, not a 1ms delay for every short USB packet.
                    ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(20));
                    continue;
                }
                count = Serial.read(bytes, min(static_cast<size_t>(available), sizeof(bytes)));
                cursor = 0;
            }
            lastByteAt = millis();
            while (cursor < count) {
                if (!receiver.active()) firstByteUs = micros();
                auto result = receiver.feed(bytes[cursor++]);
                if (result == FrameReceiver::BAD_HEADER) message("ERR HEADER\n");
                if (result == FrameReceiver::BAD_CRC)
                    message("ERR CRC %lu\n", (unsigned long)receiver.sequence);
                if (result == FrameReceiver::COMPLETE || result == FrameReceiver::INTERNAL_BENCH) {
                    auto& frame = slots[index];
                    frame.command = receiver.command;
                    frame.components = receiver.components;
                    frame.sequence = receiver.sequence;
                    frame.leds = receiver.leds;
                    frame.crc = receiver.payloadCrc;
                    frame.receiveEndUs = micros();
                    frame.receiveUs = frame.receiveEndUs - firstByteUs;
                    message("RX %lu %lu %lu %08lx\n", (unsigned long)frame.sequence,
                            (unsigned long)frame.receiveUs, (unsigned long)frame.receiveEndUs,
                            (unsigned long)frame.crc);
                    xQueueSend(readySlots, &index, portMAX_DELAY);
                    vTaskDelay(1); // Once per full frame, never once per USB packet.
                    complete = true;
                    break;
                }
            }
        }
    }
}

void setup() {
    if (!setCpuFrequencyMhz(240)) abort();
    disableWireless();
    Serial.setRxBufferSize(8192);
    Serial.onEvent(
#if ARDUINO_USB_MODE
        ARDUINO_HW_CDC_RX_EVENT,
#else
        ARDUINO_USB_CDC_RX_EVENT,
#endif
        [](void*, esp_event_base_t, int32_t, void*) {
        if (rxTaskHandle) xTaskNotifyGive(rxTaskHandle);
    });
    Serial.begin(115200);
#if !ARDUINO_USB_MODE
    Serial.enableReboot(false); // Reopening a data session must not enter bootloader.
#endif
    serialLock = xSemaphoreCreateMutex();
    freeSlots = xQueueCreate(2, sizeof(uint8_t));
    readySlots = xQueueCreate(2, sizeof(uint8_t));
    if (!serialLock || !freeSlots || !readySlots) abort();
    initPins();
    randomState = esp_random();
    if (!randomState) randomState = 1;
    initFastLED();
    renderFrame(nullptr);
    const auto& peripheral = fl::detail::I2sLcdCamPeripheralEsp::instance();
    const auto& config = peripheral.getConfig();
    if (!peripheral.isInitialized() || config.num_lanes != UNIT_COUNT) abort();
    for (uint8_t i = 0; i < UNIT_COUNT; ++i)
        if (config.data_gpios[i] != DATA_PINS[i]) abort();
    for (uint8_t i = 0; i < 2; ++i) xQueueSend(freeSlots, &i, portMAX_DELAY);
    message("READY LED2 pipeline=2 backend=%s units=12 channels=16 max_physical=66 max_visible=65 hidden_prefix=BLACK\n",
            "I2S_LCD_CAM");
    if (xTaskCreatePinnedToCore(receiveTask, "USB_RX", 6144, nullptr, 2, &rxTaskHandle, 0) != pdPASS)
        abort();
}

void loop() {
    uint8_t index;
    if (xQueueReceive(readySlots, &index, portMAX_DELAY) != pdTRUE) return;
    const auto& frame = slots[index];
    if (frame.command == 1 || frame.command == 2) {
        internalBenchmark(slots[index]);
        xQueueSend(freeSlots, &index, portMAX_DELAY);
        return;
    }
    uint32_t start = micros();
    if (frame.command != 4) renderFrame(&frame); // RX-only mode skips the LED output.
    uint32_t end = micros();
    message("OK %lu %lu %lu %lu %lu %lu %08lx\n", (unsigned long)frame.sequence,
            (unsigned long)frame.receiveUs, (unsigned long)(end - start),
            (unsigned long)start, (unsigned long)end, (unsigned long)frame.receiveEndUs,
            (unsigned long)frame.crc);
    xQueueSend(freeSlots, &index, portMAX_DELAY);
    vTaskDelay(1);
}
