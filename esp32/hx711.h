#ifndef HX711_H
#define HX711_H

#include "driver/gpio.h"
#include <stdbool.h>

// Struct-based HX711 driver, one instance per cell
typedef struct
{
    gpio_num_t dout;                     // HX711 → ESP32
    gpio_num_t sck;                      // ESP32 → HX711

    long offset;                         // Tare offset, raw ADC counts

    float scale;                         // Counts per unit

} hx711_t;

void hx711_init(hx711_t *hx);

bool hx711_is_ready(hx711_t *hx);        // DOUT low means a conversion is ready

bool hx711_wait_ready(hx711_t *hx, int timeout_ms);

long hx711_read(hx711_t *hx);            // One raw 24-bit read, sign-extended

long hx711_read_average(hx711_t *hx, int times);

void hx711_tare(hx711_t *hx, int times); // Current reading becomes the new zero

float hx711_get_units(hx711_t *hx, int times);        // Tared and scaled

void hx711_debug(hx711_t *hx);           // Toggles SCK and logs DOUT, for wiring diagnostics

#endif
