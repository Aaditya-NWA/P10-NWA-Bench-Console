#include "hx711.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_log.h"
#include "esp_rom_sys.h"                 // esp_rom_delay_us — microsecond bit-bang timing

static const char *TAG = "HX711";

// ================================================================
// INIT
// ================================================================
void hx711_init(hx711_t *hx)
{
    gpio_set_direction(hx->sck, GPIO_MODE_OUTPUT);

    gpio_set_direction(hx->dout, GPIO_MODE_INPUT);

    gpio_set_level(hx->sck, 0);                       // Clock idles low between reads

    ESP_LOGI(TAG, "HX711 INIT DONE");
}

// ================================================================
// IS READY — the HX711 pulls DOUT low when a conversion is ready
// ================================================================
bool hx711_is_ready(hx711_t *hx)
{
    return gpio_get_level(hx->dout) == 0;
}

// ================================================================
// WAIT READY — polls with a timeout, so a dead cell can't hang a task
// ================================================================
bool hx711_wait_ready(hx711_t *hx, int timeout_ms)
{
    int elapsed = 0;

    while (!hx711_is_ready(hx))
    {
        vTaskDelay(pdMS_TO_TICKS(1));

        elapsed++;

        if (elapsed >= timeout_ms)
        {
            ESP_LOGE(TAG, "HX711 TIMEOUT");

            return false;
        }
    }

    return true;
}

// ================================================================
// DEBUG — pulses SCK and logs DOUT, for wiring verification
// ================================================================
void hx711_debug(hx711_t *hx)
{
    int dt = gpio_get_level(hx->dout);

    ESP_LOGI(TAG, "DT PIN = %d", dt);

    ESP_LOGI(TAG, "SCK TEST START");

    for (int i = 0; i < 5; i++)
    {
        gpio_set_level(hx->sck, 1);

        esp_rom_delay_us(10);

        gpio_set_level(hx->sck, 0);

        esp_rom_delay_us(10);
    }

    ESP_LOGI(TAG, "SCK TEST DONE");
}

// ================================================================
// READ — one raw 24-bit conversion, sign-extended
// ================================================================
long hx711_read(hx711_t *hx)
{
    long count = 0;

    if (!hx711_wait_ready(hx, 1000))
    {
        return -1;
    }

    for (int i = 0; i < 24; i++)
    {
        gpio_set_level(hx->sck, 1);                    // Rising edge shifts the next bit out

        esp_rom_delay_us(2);

        count = count << 1;

        // READ DURING CLOCK HIGH
        if (gpio_get_level(hx->dout))
        {
            count++;
        }

        gpio_set_level(hx->sck, 0);

        esp_rom_delay_us(2);
    }

    // 25th CLOCK
    // GAIN 128
    gpio_set_level(hx->sck, 1);                        // 25th pulse selects 128x gain, channel A

    esp_rom_delay_us(2);

    gpio_set_level(hx->sck, 0);

    // SIGN EXTENSION
    if (count & 0x800000)
    {
        count |= ~0xffffff;
    }

    return count;
}

// ================================================================
// READ AVERAGE
// ================================================================
long hx711_read_average(hx711_t *hx, int times)
{
    long sum = 0;

    for (int i = 0; i < times; i++)
    {
        sum += hx711_read(hx);

        // The HX711 runs at 10 SPS, so samples are spaced — but only
        // BETWEEN them, not after the last one. This used to fire
        // unconditionally, adding a needless fixed 100 ms to every
        // single-sample call. Multi-sample callers like hx711_tare()
        // still get the same spacing as before.
        if (i < times - 1)
        {
            vTaskDelay(pdMS_TO_TICKS(100));
        }
    }

    return sum / times;
}

// ================================================================
// TARE — the current reading becomes the new zero-load offset
// ================================================================
void hx711_tare(hx711_t *hx, int times)
{
    ESP_LOGI(TAG, "TARING...");

    hx->offset = hx711_read_average(hx, times);

    ESP_LOGI(TAG, "OFFSET = %ld", hx->offset);
}

// ================================================================
// GET UNITS — tared and scaled into physical units
// ================================================================
float hx711_get_units(hx711_t *hx, int times)
{
    long value = hx711_read_average(hx, times);

    return (value - hx->offset) / hx->scale;
}
