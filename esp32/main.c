#include <stdio.h>
#include <stdbool.h>
#include <stdlib.h>
#include <stdint.h>
#include <math.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "driver/gpio.h"
#include "driver/i2c_master.h"           // IDF v6 bus+device handle API; driver/i2c.h is gone
#include "driver/ledc.h"
#include "driver/uart.h"                 // NOT usb_serial_jtag: the native USB port is physically
                                          //   damaged, so the console lives on UART0, which is what
                                          //   the external adapter is wired to.
                                          //   usb_serial_jtag_read_bytes() only ever received bytes
                                          //   over the native-USB D+/D- pins, which is why commands
                                          //   stopped arriving and telemetry appeared to freeze.
#include "driver/uart_vfs.h"             // uart_vfs_dev_use_driver(). On IDF v5.3+/v6.x this replaced
                                          //   esp_vfs_dev.h / esp_vfs_dev_uart_use_driver(); the legacy
                                          //   name is an implicit-declaration error at build time.
#include "esp_timer.h"
#include "rom/ets_sys.h"                 // ets_delay_us — microsecond bit-bang timing
#include "esp_rom_sys.h"
#include "esp_log.h"
#include "esp_system.h"                  // esp_reset_reason() — boot-time brownout diagnostic
#include "nvs_flash.h"                   // Stores dashboard-edited calibration across reboots
#include "nvs.h"
#include "hx711.h"                       // Struct-based driver for the thrust cell

static const char *TAG = "NWA-BENCH-CONSOLE";

#define TELEMETRY_PERIOD_MS     50       // 20 Hz target via vTaskDelayUntil — a pace to aim for, not a guarantee
#define LOOP_TIMING_LOG_EVERY   40       // One timing breakdown every N loops (~2 s), not every cycle
#define CMD_READ_TIMEOUT_MS     20       // Bounds command latency and lets command_task sleep rather than busy-poll

// There is no HX711 bus mutex any more: since the tare moved into
// measurement_task (see LOAD CELL TARE below), that task is the only
// one that ever clocks the cells.
//
// Keeps a command_task event line and a measurement_task telemetry line
// from interleaving into one corrupted line.
static SemaphoreHandle_t s_io_mutex;
// An ADS1115 read is THREE bus transactions (config write, wait, pointer
// write, read) and is not atomic. Without this, a zero-cal running in
// command_task and measurement_task's normal read interleaved
// mid-conversion and one came back for the wrong channel — which is
// what made a zero-cal land on garbage (reading -5 A at a true 0 A).
// The runtime zero-cal now rides on measurement_task's own reads, but
// the mutex stays so a future second I2C caller can't bring that back.
static SemaphoreHandle_t s_i2c_mutex;

#define CALIBRATION_MODE   0             // 1 prints raw counts for a calibration run

#define GRAVITY         9.81f            // m/s²
#define TORQUE_ARM_M    0.300f           // Carbon fibre torque arm, metres

// ── RPM sensor (IR optical slot sensor), interrupt-driven ────────
// GPIO1, not GPIO39: GPIO39 is in the ESP32-S3 JTAG pin group (39-42)
// and may not behave as plain GPIO depending on JTAG/USB routing state.
// GPIO1 has no such conflict on the N8R8 WROOM-1 variant.
#define RPM_PIN               1          // Via a CD4050BE level shifter
#define SLOTS_PER_REV         1          // Slot interrupts per revolution — raise if the disc has more
// RPM comes from the period between consecutive pulses, not a fixed
// window — see RPM_DEBOUNCE_US / RPM_STALL_TIMEOUT_MS by rpm_read().

// ── ESC / throttle (U15II KV100 via a standard PWM ESC) ──────────
// No UI arm/disarm gate: the ESC arming handshake runs automatically on
// every boot, matching the working Arduino reference firmware.
#define ESC_PIN              21
#define THR_MIN            1000          // 0% throttle, µs — below the motor start threshold
#define THR_MAX            2000          // 100% throttle, µs
#define ESC_PWM_FREQ_HZ      50          // Standard analog ESC frame rate (20 ms period)
#define ESC_PWM_RES   LEDC_TIMER_14_BIT  // Fine µs resolution at 50 Hz
#define ESC_LEDC_TIMER     LEDC_TIMER_0
#define ESC_LEDC_CHANNEL LEDC_CHANNEL_0
#define ESC_ARM_DELAY_MS   8000          // Hold at minimum signal during arming (matches the Arduino ref)

static volatile int current_throttle_us = THR_MIN;

// ================================================================
// ADS1115 — CURRENT + VOLTAGE  (HAS-600S current, LV25-P voltage)
//   A0 = current, A1 = voltage, both via a resistor network.
//
//   Analog front end (bipolar ±15V supply, star ground):
//     - Two Mean Well RS-15-15 units make the ±15V rails, Unit A -V
//       tied to Unit B +V as the 0V common. That common MUST be tied
//       to ESP32 GND.
//     - HAS-600S: ± to the rails, 0 to common, OUT through a 100k into
//       a 3-resistor divider joint (100k from sensor, 68k to 3.3V,
//       220k to common) landing on A0.
//     - LV25-P: same rails, VOUT through its own 100k into an
//       identical 68k/220k joint, landing on A1.
//     - ADS1115 VDD=3.3V, ADDR=GND (locks the address to 0x48).
//
//   The firmware only needs NETWORK_GAIN/NETWORK_OFFSET below, which
//   already reverse the network back to the sensor's true output — but
//   the wiring is recorded here because CURRENT_SCALE/VOLTAGE_SCALE
//   stay valid only for THIS exact resistor/rail setup. Don't change
//   them without re-deriving from fresh reference readings.
// ================================================================
#define I2C_MASTER_SCL_IO          41
#define I2C_MASTER_SDA_IO          40
#define I2C_MASTER_NUM    I2C_NUM_0
#define I2C_MASTER_FREQ_HZ    100000
#define ADS1115_ADDR             0x48    // ADDR pin tied to GND

#define ADS_LSB              0.000125f   // LSB size for the ±4.096V PGA range

// Resistor network reverse calculation — verified correct, do not change
#define NETWORK_GAIN            2.925f
#define NETWORK_OFFSET          4.853f

// HAS-600S, 2-point fit from reference data. The offset is only the
// BOOT-TIME SEED for s_current_zero_offset_v; the live zero point is
// re-measured on every tare, so per-board and thermal drift can't leave
// a permanent bias the way the old fixed constant did.
#define CURRENT_ZERO_OFFSET_V 0.104076f
#define CURRENT_SCALE          160.02 // 1-input
#define CURRENT_ZERO_CAL_SAMPLES     20  // Samples averaged when re-zeroing on tare
#define CURRENT_DEADBAND_A        0.05f  // Clamps residual post-tare creep to exactly 0.00 A

// LV25-P
#define VOLTAGE_ZERO_OFFSET_V     0.0f
#define VOLTAGE_SCALE            62.51f   // 2 - input
                                          // [FLAG] KNOWN WRONG. This was a single-point rescale
                                          // (46.19 * 48.2 actual / 35.62 measured), and the bench now
                                          // reports ~5-6V against a real 48V bus — roughly 8.7x out,
                                          // far too large for "one more multiply". That is the
                                          // signature of the measurement chain itself changing during
                                          // the panel rebuild (most likely the LV25-P's primary-side
                                          // series resistor or the burden network), which would
                                          // invalidate NETWORK_GAIN/NETWORK_OFFSET too.
                                          // DO NOT rescale from one data point again: use the periodic
                                          // [ADS DEBUG] log line in measurement_task to capture
                                          // sensor_out_a1 at two multimeter-verified bus voltages, and
                                          // re-derive the divider constants and this scale properly.

#define EMA_ALPHA_CURRENT        0.15f
#define EMA_ALPHA_VOLTAGE        0.08f   // Heavier than current: the bus voltage is slow-moving DC

// ================================================================
// SECOND CURRENT + VOLTAGE PAIR  (ADS1115 A2 / A3)
//
//   The hardware is not installed yet, so SENSOR2_ENABLED is 0 and the
//   firmware publishes a hard 0.00 for current_a2 / voltage_v2. The
//   fields are always in the telemetry frame regardless, so the
//   dashboard and CSV columns stay aligned from day one — flipping
//   this flag is the only change needed once the sensors are wired.
//
//   A COMPILE FLAG, NOT AUTO-DETECTION: an ADS1115 input with nothing
//   attached is a floating pin. It reads drifting garbage (railing
//   toward VDD through the leakage path), NOT zero, so "is a sensor
//   connected?" can't be answered from the reading. Skipping the reads
//   entirely is both honest and free.
//
//   COST WHEN ENABLED: each read costs ~10 ms of single-shot
//   conversion wait plus I2C overhead. Two extra channels is ~20-25 ms
//   of the 50 ms budget; four is essentially all of it, dragging the
//   rate below the 20 Hz target. Switch the ADS1115 to round-robin
//   (one channel per loop, each refreshing at 5 Hz — current and bus
//   voltage are slow signals) before turning this on for real testing.
//
//   CALIBRATION: the constants below mirror the sensor-1 values as a
//   starting point and are NOT valid until re-derived against the real
//   second pair and its own resistor network.
// ================================================================
#define SENSOR2_ENABLED             0    // 1 once sensor pair #2 is wired to A2/A3

#define CURRENT2_ZERO_OFFSET_V  0.104076f  // Boot-time seed; re-measured on every tare, like sensor 1
#define CURRENT2_SCALE            160.02f  // 3 - input // PLACEHOLDER — re-derive against the real sensor #2
#define VOLTAGE2_ZERO_OFFSET_V      0.0f
#define VOLTAGE2_SCALE             62.51f  // 4 - input // PLACEHOLDER — re-derive against the real sensor #2
#define NETWORK2_GAIN              2.925f  // PLACEHOLDER — only valid if pair #2 uses an identical network
#define NETWORK2_OFFSET            4.853f  // PLACEHOLDER — as above

// ================================================================
// DESPIKE FILTER — rejects single-sample transients (ESC switching
// noise coupling into the analog front end during a ramp) before they
// reach the EMA. A MAD test over a small rolling history per channel:
// a sample too many MADs from the window's median is rejected and the
// EMA holds its last value instead of being pulled by the glitch.
// Rejected samples still enter the history window, so a genuine fast
// ramp costs only ~1 loop cycle of lag while the median catches up —
// it suppresses only movement that goes against the recent trend.
// ================================================================
#define DESPIKE_WINDOW        5          // ~250 ms at the 50 ms loop period
#define DESPIKE_MAD_MULT      5.0f       // Reject if |sample - median| exceeds this many MADs
#define DESPIKE_MAD_FLOOR_A   0.05f      // Avoids over-rejecting when the signal is genuinely flat
#define DESPIKE_MAD_FLOOR_V   0.05f

static float ema_current_amps = 0.0f;
static float ema_bus_voltage  = 0.0f;
static bool  ema_initialized   = false;           // Seeds the EMA rather than ramping it up from zero
// LIVE zero point (sensor_out_a0 volts at a true 0 A), re-measured on
// every tare — see tare_finish() and, at boot, ads1115_zero_calibrate_current().
static float s_current_zero_offset_v = CURRENT_ZERO_OFFSET_V;

// Sensor pair #2. These stay at exactly 0.0f while SENSOR2_ENABLED is
// 0 — nothing writes them, so the frame reports a clean 0.00 rather
// than a floating-pin reading.
static float ema_current2_amps = 0.0f;
static float ema_bus_voltage2  = 0.0f;
static bool  ema2_initialized  = false;
static float s_current2_zero_offset_v = CURRENT2_ZERO_OFFSET_V;

// Rolling despike history, one per channel
static float s_current_hist[DESPIKE_WINDOW];
static int   s_current_hist_n   = 0;
static int   s_current_hist_idx = 0;
static float s_voltage_hist[DESPIKE_WINDOW];
static int   s_voltage_hist_n   = 0;
static int   s_voltage_hist_idx = 0;
static float s_current2_hist[DESPIKE_WINDOW];
static int   s_current2_hist_n   = 0;
static int   s_current2_hist_idx = 0;
static float s_voltage2_hist[DESPIKE_WINDOW];
static int   s_voltage2_hist_n   = 0;
static int   s_voltage2_hist_idx = 0;

// The window is tiny (≤5), not worth qsort's overhead
static void despike_isort(float *a, int n) {
    for (int i = 1; i < n; i++) {
        float key = a[i]; int j = i - 1;
        while (j >= 0 && a[j] > key) { a[j + 1] = a[j]; j--; }
        a[j + 1] = key;
    }
}

// True if `sample` should be KEPT. Always pushes it into the history
// window either way, so a genuine step change is delayed by about one
// loop cycle before the median catches up to it.
static bool despike_check(float *hist, int *hist_n, int *hist_idx, float sample, float mad_floor) {
    bool keep = true;
    if (*hist_n >= 3) {                                    // Too few samples for a meaningful median
        float sorted[DESPIKE_WINDOW];
        for (int i = 0; i < *hist_n; i++) sorted[i] = hist[i];
        despike_isort(sorted, *hist_n);
        float median = sorted[*hist_n / 2];

        float abs_dev[DESPIKE_WINDOW];
        for (int i = 0; i < *hist_n; i++) abs_dev[i] = fabsf(hist[i] - median);
        despike_isort(abs_dev, *hist_n);
        float mad = abs_dev[*hist_n / 2];
        if (mad < mad_floor) mad = mad_floor;

        if (fabsf(sample - median) > DESPIKE_MAD_MULT * mad) keep = false;
    }
    hist[*hist_idx] = sample;
    *hist_idx = (*hist_idx + 1) % DESPIKE_WINDOW;
    if (*hist_n < DESPIKE_WINDOW) (*hist_n)++;
    return keep;
}

// Raw ADS1115 chain, captured every read and printed periodically (the
// [ADS DEBUG] log in measurement_task) so a proper 2-point voltage
// recalibration can be done from real numbers.
static int16_t s_dbg_raw_a0 = 0, s_dbg_raw_a1 = 0;
static float   s_dbg_node_v_a0 = 0, s_dbg_sensor_out_a0 = 0;
static float   s_dbg_node_v_a1 = 0, s_dbg_sensor_out_a1 = 0;

// Set up once in i2c_master_init(), used by every read afterwards
static i2c_master_bus_handle_t s_i2c_bus   = NULL;
static i2c_master_dev_handle_t s_ads1115_dev = NULL;  // Bound to ADS1115_ADDR on that bus

// ── Beam cell pin map (torque) ───────────────────────────────────
#define S1_DT   4                        // S1 top
#define S1_SCK  5
#define S2_DT   6                        // S2 bottom
#define S2_SCK  7
#define S3_DT  15                        // S3 right
#define S3_SCK 16
#define S4_DT  8                        // S4 left
#define S4_SCK 3

// ── S-type load cell (thrust) ────────────────────────────────────
// Uses the struct-based hx711_t driver — both hx711.h AND hx711.c must
// be in the build (the component's CMakeLists SRCS), not just the header.
#define THRUST_SCALE_DEFAULT 18178.5f    // Seeds the runtime table; the live value is thrust_cell.scale

static hx711_t thrust_cell = {
    .dout   = GPIO_NUM_36,
    .sck    = GPIO_NUM_35,
    .offset = 0,                         // Tare offset, populated at startup
    .scale  = THRUST_SCALE_DEFAULT       //5 - input (runtime-editable) — counts/kg. Was 17492.8f;
                                          // 24"/26"/28" V804 validation against the T-Motor reference
                                          // showed thrust reading ~3.9% high (CW k=1.0228, CCW
                                          // k=1.0556), so 17492.8 * 1.0392 = 18178.5
};

// ── Individual beam cell scales (counts/kg) ──────────────────────
#define CAL_KG_S1   73420.5f             // S1 top    //6 - input
#define CAL_KG_S2   74984.0f             // S2 bottom //7 - input
#define CAL_KG_S3   78894.8f             // S3 right  //8 - input
#define CAL_KG_S4   77297.9f             // S4 left   //9 - input

// ================================================================
// TORQUE COEFFICIENTS  (directional, from ttotal.py)
//   torque_kg = TQ_K1*S1 + TQ_K2*S2 + TQ_K3*S3 + TQ_K4*S4
//   torque_Nm = torque_kg * 9.81 * 0.300
//
//   Split into a CW table and a CCW table, selected at runtime from the
//   sign of S3 with hysteresis (TQ_HYST_KG) so noise near zero can't
//   flicker between them.
//
//   Corrected 2026-07-26 against the T-Motor V804 reference (24"/26"/28"):
//   tested torque read LOW, k_CW=0.9248, k_CCW=0.9334 on a
//   through-origin fit of in-RPM-range points. Each coefficient is the
//   previous-pass value divided by its direction's k.
// ================================================================
#define TQ_K1_CW    0.49213672f   // prev 0.45512804f   //9 - input
#define TQ_K2_CW    0.33566028f   // prev 0.31041863f   //10 - input
#define TQ_K3_CW    0.38270391f   // prev 0.35392458f   //11 - input
#define TQ_K4_CW    0.33088148f   // prev 0.30599919f   //12 - input

#define TQ_K1_CCW   0.22886047f   // prev 0.21361836f   //13 - input
#define TQ_K2_CCW  -0.87877041f   // prev -0.82024430f  //14 - input
#define TQ_K3_CCW  -0.42894325f   // prev -0.40037563f  //15 - input
#define TQ_K4_CCW  -0.28771460f   // prev -0.26855281f  //16 - input

#define TQ_HYST_KG   0.050f               // Band (kg on S3) around zero for CW/CCW switching

// ================================================================
// RUNTIME CALIBRATION TABLE
//
//   The 17 constants marked "//N - input" are re-derived every time the
//   rig is recalibrated against a reference motor. They are RUNTIME
//   values in s_cal, seeded from the #defines, editable from the
//   dashboard, and persisted in NVS — so a recalibration no longer
//   needs a toolchain, the adapter, and the bench offline.
//
//   The #defines stay the single source of truth for the DEFAULTS:
//   "reset to firmware defaults" restores exactly those numbers, so a
//   bench edit can be folded back into this file without the two
//   drifting.
//
//   DELIBERATELY NOT INCLUDED: zero offsets (already re-measured on
//   every "T"); NETWORK_GAIN/OFFSET and the per-cell polynomial
//   linearity corrections, which describe fixed hardware rather than a
//   per-session fit and where a typo is far harder to spot than a wrong
//   scale factor; pin maps, filter constants and throttle limits.
//
//   THREADING: measurement_task reads these floats while command_task
//   writes them. A single aligned 32-bit float store is atomic on the
//   ESP32, so no mutex — the worst case is one frame computed from a
//   mix of old and new coefficients, indistinguishable from the edit
//   having landed one frame later. A mutex would buy nothing and would
//   let a settings write stall the sensor loop.
// ================================================================
#define CAL_NVS_NAMESPACE  "nwcal"

typedef struct {
    float current_scale;                 // "1 - input"  — HAS-600S #1, amps per volt
    float voltage_scale;                 // "2 - input"  — LV25-P #1, volts per volt
    float current2_scale;                // "3 - input"  — pair #2, placeholder until wired
    float voltage2_scale;                // "4 - input"  — pair #2, placeholder until wired
    float cal_kg[4];                     // "6..9 - input"   — counts/kg, S1..S4
    float tq_cw[4];                      // "9..12 - input"  — torque weights, CW, K1..K4
    float tq_ccw[4];                     // "13..16 - input" — torque weights, CCW, K1..K4
} cal_t;

static cal_t s_cal = {
    .current_scale  = (float)CURRENT_SCALE,
    .voltage_scale  = VOLTAGE_SCALE,
    .current2_scale = CURRENT2_SCALE,
    .voltage2_scale = VOLTAGE2_SCALE,
    .cal_kg = { CAL_KG_S1, CAL_KG_S2, CAL_KG_S3, CAL_KG_S4 },
    .tq_cw  = { TQ_K1_CW,  TQ_K2_CW,  TQ_K3_CW,  TQ_K4_CW  },
    .tq_ccw = { TQ_K1_CCW, TQ_K2_CCW, TQ_K3_CCW, TQ_K4_CCW },
};

// ── Field registry ───────────────────────────────────────────────
//   One row per editable value.
//
//   `ptr` for thrust_scale points straight at thrust_cell.scale rather
//   than a copy inside s_cal: read_thrust_kg() and hx711_get_units()
//   both divide by thrust_cell.scale directly, and one storage location
//   means no copy can go stale.
//
//   NVS caps keys at 15 characters — keep new ones short.
//
//   RANGES exist to stop a typo bricking the rig, not to second-guess a
//   real calibration. The counts-per-kg and thrust scales are DIVISORS,
//   so a zero would produce inf/NaN torque and thrust on every frame
//   until the next reflash — hence a hard positive lower bound rather
//   than just "is finite".
typedef struct {
    const char *key;                     // Wire + NVS key, e.g. "cal_kg_s1"
    float      *ptr;                     // Live storage
    float       def;                     // Compiled-in default
    float       min;                     // Inclusive
    float       max;                     // Inclusive
} cal_field_t;

static const cal_field_t CAL_FIELDS[] = {
    { "thrust_scale",   &thrust_cell.scale,   THRUST_SCALE_DEFAULT,  1.0f,      1.0e7f  },  // counts/kg — divisor
    { "cal_kg_s1",      &s_cal.cal_kg[0],     CAL_KG_S1,             1.0f,      1.0e7f  },  // counts/kg — divisor
    { "cal_kg_s2",      &s_cal.cal_kg[1],     CAL_KG_S2,             1.0f,      1.0e7f  },
    { "cal_kg_s3",      &s_cal.cal_kg[2],     CAL_KG_S3,             1.0f,      1.0e7f  },
    { "cal_kg_s4",      &s_cal.cal_kg[3],     CAL_KG_S4,             1.0f,      1.0e7f  },
    { "tq_k1_cw",       &s_cal.tq_cw[0],      TQ_K1_CW,             -100.0f,    100.0f  },  // weighting — may legitimately be negative
    { "tq_k2_cw",       &s_cal.tq_cw[1],      TQ_K2_CW,             -100.0f,    100.0f  },
    { "tq_k3_cw",       &s_cal.tq_cw[2],      TQ_K3_CW,             -100.0f,    100.0f  },
    { "tq_k4_cw",       &s_cal.tq_cw[3],      TQ_K4_CW,             -100.0f,    100.0f  },
    { "tq_k1_ccw",      &s_cal.tq_ccw[0],     TQ_K1_CCW,            -100.0f,    100.0f  },
    { "tq_k2_ccw",      &s_cal.tq_ccw[1],     TQ_K2_CCW,            -100.0f,    100.0f  },
    { "tq_k3_ccw",      &s_cal.tq_ccw[2],     TQ_K3_CCW,            -100.0f,    100.0f  },
    { "tq_k4_ccw",      &s_cal.tq_ccw[3],     TQ_K4_CCW,            -100.0f,    100.0f  },
    { "current_scale",  &s_cal.current_scale, (float)CURRENT_SCALE,  0.001f,    1.0e5f  },  // multiplier — a zero only mutes the channel, but is never useful
    { "voltage_scale",  &s_cal.voltage_scale, VOLTAGE_SCALE,         0.001f,    1.0e5f  },
    { "current2_scale", &s_cal.current2_scale,CURRENT2_SCALE,        0.001f,    1.0e5f  },
    { "voltage2_scale", &s_cal.voltage2_scale,VOLTAGE2_SCALE,        0.001f,    1.0e5f  },
};
#define CAL_FIELD_COUNT (sizeof(CAL_FIELDS) / sizeof(CAL_FIELDS[0]))

static const cal_field_t *cal_find(const char *key) {
    for (size_t i = 0; i < CAL_FIELD_COUNT; i++) {
        if (strcmp(CAL_FIELDS[i].key, key) == 0) return &CAL_FIELDS[i];
    }
    return NULL;
}

// isfinite() catches the "nan"/"inf" strtof() happily parses out of a
// typo'd text field; the range check catches the plausible-looking but
// wrong — a thrust scale of 0.5 instead of 18178.5, say.
static bool cal_value_ok(const cal_field_t *f, float v) {
    return isfinite(v) && v >= f->min && v <= f->max;
}

// ── NVS persistence ──────────────────────────────────────────────
//   One entry per field keyed by name, NOT one packed blob: adding or
//   removing a field then costs nothing (an absent key falls back to
//   its compiled default) instead of invalidating every stored value
//   at once the way a struct-layout change would.
static void cal_load_from_nvs(void) {
    nvs_handle_t h;
    esp_err_t err = nvs_open(CAL_NVS_NAMESPACE, NVS_READONLY, &h);
    if (err != ESP_OK) {
        // NOT_FOUND just means nothing was ever saved — a first boot on
        // a fresh chip. Defaults stand.
        ESP_LOGI(TAG, "No stored calibration (%s) — using compiled-in defaults", esp_err_to_name(err));
        return;
    }
    int restored = 0;
    for (size_t i = 0; i < CAL_FIELD_COUNT; i++) {
        const cal_field_t *f = &CAL_FIELDS[i];
        float    v;
        size_t   len = sizeof(v);
        if (nvs_get_blob(h, f->key, &v, &len) == ESP_OK && len == sizeof(v)) {
            // Re-validated on the way in: a stored value came through
            // the same check on the way out, but NVS survives firmware
            // updates, and a build that tightens a range must not let
            // an old out-of-range value slip past.
            if (cal_value_ok(f, v)) {
                *f->ptr = v;
                restored++;
            } else {
                ESP_LOGW(TAG, "Stored calibration %s = %g is out of range [%g, %g] — ignoring, using default %g",
                         f->key, (double)v, (double)f->min, (double)f->max, (double)f->def);
            }
        }
    }
    nvs_close(h);
    ESP_LOGI(TAG, "Calibration: restored %d/%d value(s) from NVS", restored, (int)CAL_FIELD_COUNT);
}

static bool cal_store_to_nvs(const cal_field_t *f, float v) {
    nvs_handle_t h;
    if (nvs_open(CAL_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) return false;
    esp_err_t err = nvs_set_blob(h, f->key, &v, sizeof(v));
    if (err == ESP_OK) err = nvs_commit(h);       // Without this the write is lost on the next reset
    nvs_close(h);
    return err == ESP_OK;
}

// Wipes every stored override, so the next boot comes from the
// compiled-in defaults again.
static bool cal_erase_nvs(void) {
    nvs_handle_t h;
    if (nvs_open(CAL_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) return false;
    esp_err_t err = nvs_erase_all(h);
    if (err == ESP_OK) err = nvs_commit(h);
    nvs_close(h);
    return err == ESP_OK;
}

// ── Smoothing / stability ────────────────────────────────────────
#define SMOOTH_N      5                  // Moving average window, samples
#define STABLE_WIN    6                  // Stability detection window, samples
#define STABLE_THR    0.100f             // Max peak-to-peak (kg) to declare stable

// ── Per-channel beam cell state ──────────────────────────────────
typedef struct {
    float sm_buf[SMOOTH_N];              // Circular smoothing buffer
    int   sm_idx, sm_filled;
    float st_buf[STABLE_WIN];            // Circular stability window
    int   st_idx, st_filled;
    long  offset;                        // Tare offset, raw ADC counts
} Chan;

static Chan CH[4];   // 0=S1  1=S2  2=S3  3=S4

// Which direction's torque table is active. S3 has to cross clear past
// TQ_HYST_KG on the OTHER side before the tables switch, so noise near
// zero torque can't flip it back and forth.
static bool s_is_ccw = false;

// ================================================================
// BEAM CELL DSP HELPERS
// ================================================================
static float ch_smooth(Chan *c, float v) {
    c->sm_buf[c->sm_idx] = v;
    c->sm_idx = (c->sm_idx + 1) % SMOOTH_N;
    if (c->sm_idx == 0) c->sm_filled = 1;
    int n = c->sm_filled ? SMOOTH_N : c->sm_idx;      // Actual sample count until the buffer fills
    float s = 0.0f;
    for (int i = 0; i < n; i++) s += c->sm_buf[i];
    return s / n;
}

static int ch_stable(Chan *c, float v, float *out) {
    c->st_buf[c->st_idx] = v;
    c->st_idx = (c->st_idx + 1) % STABLE_WIN;
    if (c->st_idx == 0) c->st_filled = 1;
    if (!c->st_filled) return 0;                       // Too few samples to call it stable
    float mn = c->st_buf[0], mx = c->st_buf[0], s = 0.0f;
    for (int i = 0; i < STABLE_WIN; i++) {
        if (c->st_buf[i] < mn) mn = c->st_buf[i];
        if (c->st_buf[i] > mx) mx = c->st_buf[i];
        s += c->st_buf[i];
    }
    if ((mx - mn) <= STABLE_THR) { *out = s / STABLE_WIN; return 1; }
    return 0;                                          // Peak-to-peak too large; still settling
}

// ================================================================
// BEAM CELL HX711 DRIVER  (raw GPIO bit-bang)
// ================================================================
static void beam_hx711_init(int dt, int sck) {
    gpio_set_direction(sck, GPIO_MODE_OUTPUT);
    gpio_set_direction(dt,  GPIO_MODE_INPUT);
    gpio_set_level(sck, 0);                            // Clock idles low
}

// True on success, with the result in *out_count. The 200 ms cap is
// generous headroom over the HX711's ~100 ms (10 SPS) conversion cycle.
static bool beam_hx711_read(int dt, int sck, int32_t *out_count) {
    uint32_t count = 0;
    int waited_ms = 0;
    while (gpio_get_level(dt) == 1) {                  // DOUT low means ready
        vTaskDelay(1);
        if (++waited_ms > 200) {
            return false;                               // Give up rather than block the whole task
        }
    }
    for (int i = 0; i < 24; i++) {
        gpio_set_level(sck, 1); ets_delay_us(1);      // Rising edge shifts one bit out
        count <<= 1;
        gpio_set_level(sck, 0); ets_delay_us(1);
        if (gpio_get_level(dt)) count++;
    }
    gpio_set_level(sck, 1); ets_delay_us(1);          // 25th pulse selects 128x gain, channel A
    gpio_set_level(sck, 0); ets_delay_us(1);
    if (count & 0x800000) count |= 0xFF000000;        // Sign-extend 24-bit two's complement
    *out_count = -(int32_t)count;                      // Negated to match the physical load direction
    return true;
}

// Averages up to n samples, silently skipping any that time out.
static int32_t beam_hx711_avg(int dt, int sck, int n) {
    int64_t s = 0;
    int ok = 0;
    for (int i = 0; i < n; i++) {
        int32_t v;
        if (beam_hx711_read(dt, sck, &v)) {
            s += v;
            ok++;
        }
    }
    if (ok == 0) return 0;
    return (int32_t)(s / ok);
}

static const int DT[4]  = { S1_DT,  S2_DT,  S3_DT,  S4_DT  };
static const int SCK[4] = { S1_SCK, S2_SCK, S3_SCK, S4_SCK };

// ================================================================
// THRUST CELL — S-TYPE POLYNOMIAL CORRECTION
// ================================================================
static float thrust_linearity_correction(float weight) {
    return (-0.04812f)
         + ( 1.00415f * weight)
         + (-0.000015f * weight * weight);             // Corrects high-load droop
}

// ================================================================
// PER-CELL LINEARITY CORRECTIONS  (from beam_cell_calibration.c)
// ================================================================
static float correct_S1(float w) {                     // Cubic
    return ( 0.97514385f   * w)
         + (-0.00003028f   * w * w)
         + (-0.0000014414f * w * w * w);
}
static float correct_S2(float w) {                     // Fifth-order — the highest nonlinearity of the four
    double d = (double)w;                              // Double, for precision in the high-order terms
    return (float)(
        ( 1.019185643          * d)
      + (-0.002276966          * d*d)
      + ( 0.000095672802       * d*d*d)
      + (-0.000001583323626    * d*d*d*d)
      + ( 0.000000008892712111 * d*d*d*d*d));
}
static float correct_S3(float w) {                     // Cascaded two-stage cubic
    float p1 = (1.03621623f    * w)
             + (-0.00208530f   * w*w)
             + ( 0.0000048550f * w*w*w);                // Stage 1: raw to intermediate
    return     (0.98436309f    * p1)
             + (0.00020858f    * p1*p1)
             + (-0.0000041221f * p1*p1*p1);             // Stage 2: intermediate to final
}
static float correct_S4(float w) {                     // Quadratic
    return w * (0.99098643f + w * (-0.0001398717f));
}

// ================================================================
// TARE ALL 4 BEAM CELLS
// ================================================================
static void beam_tare_all(int samples) {
    for (int i = 0; i < 4; i++)
        CH[i].offset = beam_hx711_avg(DT[i], SCK[i], samples);
    ESP_LOGI(TAG, "Beam tare: S1=%ld S2=%ld S3=%ld S4=%ld",
             CH[0].offset, CH[1].offset, CH[2].offset, CH[3].offset);
}

// ================================================================
// LOAD CELL TARE — all five cells sampled in PARALLEL
//
//   The HX711s convert independently at 10 SPS, so five cells can
//   deliver five samples every ~100 ms. The old tare read them one
//   cell at a time — 30 samples from S1, then 30 from S2, … — which
//   took ~13 s and held the sensor bus for all of it, freezing every
//   readout on the dashboard. Here each raw conversion the loop
//   already takes is also folded into a per-cell accumulator, so:
//     - a tare takes TARE_SAMPLES / 10 SPS ≈ 2 s, whatever the cell count
//     - telemetry keeps flowing throughout (published against the old
//       offsets until the new ones are applied in one step)
//     - all five zeros come from the SAME 2 s window, rather than S4's
//       being taken ~10 s after S1's
//
//   Accuracy: the offset's own uncertainty is sigma/sqrt(N). At N=20
//   that is ~22% wider than the old 30-sample beam tare, but beam noise
//   at 10 SPS is a few hundred counts against ~75,000 counts/kg, so it
//   stays an order of magnitude under the 0.005 kg display dead band.
//   The thrust cell gets 20 samples where it used to get 10. The
//   per-cell sigma is reported in TARE_DONE so this is checkable on the
//   real rig rather than assumed.
//
//   Owned entirely by measurement_task (plus the one-off blocking run
//   at boot, before that task exists): command_task only raises
//   s_tare_requested. No accumulator is ever touched by two tasks.
// ================================================================
#define TARE_SAMPLES        20           // Per load cell, ~2 s at 10 SPS
#define TARE_MIN_SAMPLES     5           // Below this a cell keeps its previous zero
#define TARE_TIMEOUT_MS   4000           // 2x the expected duration
#define TARE_CELLS           5           // S1-S4, then the thrust cell
#define TARE_THRUST          4

typedef struct {
    long   ref;                          // First sample; the rest accumulate as deviations from it,
                                         //   which keeps the variance sum free of float cancellation
    double sum, sumsq;
    int    n;
} tare_acc_t;

static const char *const TARE_CELL_KEYS[TARE_CELLS] = { "s1", "s2", "s3", "s4", "thrust" };

static tare_acc_t    s_tare_acc[TARE_CELLS];
static double        s_tare_cur_sum  = 0.0;   // sensor_out_a0 volts, for the current zero
static int           s_tare_cur_n    = 0;
static double        s_tare_cur2_sum = 0.0;   // Pair #2; only fed while SENSOR2_ENABLED
static int           s_tare_cur2_n   = 0;
static bool          s_tare_active   = false;
static bool          s_tare_with_current = false;
static int64_t       s_tare_started_us = 0;
static volatile bool s_tare_requested = false; // Raised by command_task, consumed by measurement_task

static void tare_begin(bool with_current) {
    memset(s_tare_acc, 0, sizeof(s_tare_acc));
    s_tare_cur_sum  = 0.0; s_tare_cur_n  = 0;
    s_tare_cur2_sum = 0.0; s_tare_cur2_n = 0;
    s_tare_with_current = with_current;
    s_tare_started_us   = esp_timer_get_time();
    s_tare_active       = true;
}

// Capped at TARE_SAMPLES so every cell's zero is averaged over the same
// count, even though a cell that converts slightly faster gets there first.
static void tare_add(int cell, long raw) {
    if (!s_tare_active) return;
    tare_acc_t *a = &s_tare_acc[cell];
    if (a->n >= TARE_SAMPLES) return;
    if (a->n == 0) a->ref = raw;
    double d = (double)(raw - a->ref);
    a->sum   += d;
    a->sumsq += d * d;
    a->n++;
}

static void tare_add_current(int pair, float sensor_out_v) {
    if (!s_tare_active || !s_tare_with_current) return;
    if (pair == 1 && s_tare_cur_n < CURRENT_ZERO_CAL_SAMPLES) {
        s_tare_cur_sum += sensor_out_v; s_tare_cur_n++;
    } else if (pair == 2 && s_tare_cur2_n < CURRENT_ZERO_CAL_SAMPLES) {
        s_tare_cur2_sum += sensor_out_v; s_tare_cur2_n++;
    }
}

static bool tare_complete(void) {
    for (int i = 0; i < TARE_CELLS; i++)
        if (s_tare_acc[i].n < TARE_SAMPLES) return false;
    if (s_tare_with_current) {
        if (s_tare_cur_n < CURRENT_ZERO_CAL_SAMPLES) return false;
#if SENSOR2_ENABLED
        if (s_tare_cur2_n < CURRENT_ZERO_CAL_SAMPLES) return false;
#endif
    }
    return true;
}

static bool tare_timed_out(void) {
    return (esp_timer_get_time() - s_tare_started_us) > (int64_t)TARE_TIMEOUT_MS * 1000;
}

// Old samples in the smoothing and stability windows were measured
// against the old zero. Left in place they make a fresh tare ease toward
// 0 over ~0.5 s instead of landing on it.
static void ch_reset_filters(Chan *c) {
    c->sm_idx = 0; c->sm_filled = 0;
    c->st_idx = 0; c->st_filled = 0;
}

// Applies whatever was gathered and, when emit_event is set, reports it
// as one TARE_DONE line. A cell short of TARE_MIN_SAMPLES keeps its
// previous offset and is named in "failed" — the old blocking tare
// silently set the offset to 0 when every read of a cell timed out,
// which turned a disconnected cell into a large, believable reading.
static void tare_finish(bool emit_event) {
    s_tare_active = false;
    int64_t elapsed_ms = (esp_timer_get_time() - s_tare_started_us) / 1000;

    bool  cell_ok[TARE_CELLS];
    float sd_kg[TARE_CELLS];
    bool  all_ok = true;
    for (int i = 0; i < TARE_CELLS; i++) {
        const tare_acc_t *a = &s_tare_acc[i];
        cell_ok[i] = a->n >= TARE_MIN_SAMPLES;
        sd_kg[i]   = 0.0f;
        if (!cell_ok[i]) { all_ok = false; continue; }

        double mean = a->sum / a->n;
        double var  = a->sumsq / a->n - mean * mean;
        if (var < 0.0) var = 0.0;
        long  offset = a->ref + lround(mean);
        float scale  = (i == TARE_THRUST) ? thrust_cell.scale : s_cal.cal_kg[i];
        sd_kg[i] = (float)(sqrt(var) / fabs((double)scale));

        if (i == TARE_THRUST) {
            thrust_cell.offset = offset;
        } else {
            CH[i].offset = offset;
            ch_reset_filters(&CH[i]);
        }
    }

    bool current_ok = true;
    if (s_tare_with_current) {
        if (s_tare_cur_n > 0) {
            s_current_zero_offset_v = (float)(s_tare_cur_sum / s_tare_cur_n);
            ema_current_amps   = 0.0f;              // Snapped, not decayed toward zero over several EMA cycles
            s_current_hist_n   = 0;                 // Its history is in amps against the OLD zero; left in,
            s_current_hist_idx = 0;                 //   the despike filter would reject the first few new samples
        } else {
            current_ok = false;
        }
#if SENSOR2_ENABLED
        if (s_tare_cur2_n > 0) {
            s_current2_zero_offset_v = (float)(s_tare_cur2_sum / s_tare_cur2_n);
            ema_current2_amps   = 0.0f;
            s_current2_hist_n   = 0;
            s_current2_hist_idx = 0;
        } else {
            current_ok = false;
        }
#endif
    }

    ESP_LOGI(TAG, "Tare %s in %lld ms: S1=%ld S2=%ld S3=%ld S4=%ld thrust=%ld (n=%d/%d/%d/%d/%d) current_zero=%.5f V",
             all_ok && current_ok ? "done" : "PARTIAL", (long long)elapsed_ms,
             CH[0].offset, CH[1].offset, CH[2].offset, CH[3].offset, thrust_cell.offset,
             s_tare_acc[0].n, s_tare_acc[1].n, s_tare_acc[2].n, s_tare_acc[3].n, s_tare_acc[4].n,
             (double)s_current_zero_offset_v);

    if (!emit_event) return;

    // ~230 bytes at most. Built first, printed under the mutex in one go.
    char body[320];
    int  n = 0;
    for (int i = 0; i < TARE_CELLS && n < (int)sizeof(body); i++) {
        n += snprintf(body + n, sizeof(body) - n, "%s\"%s\":{\"n\":%d,\"sd_kg\":%.5f}",
                      i ? "," : "", TARE_CELL_KEYS[i], s_tare_acc[i].n, (double)sd_kg[i]);
    }
    char failed[64];
    int  f = 0;
    failed[0] = '\0';
    for (int i = 0; i < TARE_CELLS && f < (int)sizeof(failed); i++) {
        if (!cell_ok[i])
            f += snprintf(failed + f, sizeof(failed) - f, "%s\"%s\"", f ? "," : "", TARE_CELL_KEYS[i]);
    }
    if (s_tare_with_current && !current_ok && f < (int)sizeof(failed))
        f += snprintf(failed + f, sizeof(failed) - f, "%s\"current\"", f ? "," : "");

    if (xSemaphoreTake(s_io_mutex, portMAX_DELAY) == pdTRUE) {
        printf("{\"event\":\"TARE_DONE\",\"ok\":%s,\"ms\":%lld,\"samples\":%d,\"cells\":{%s},\"failed\":[%s]}\n",
               all_ok && current_ok ? "true" : "false", (long long)elapsed_ms, TARE_SAMPLES, body, failed);
        xSemaphoreGive(s_io_mutex);
    }
}

// Boot only: nothing is publishing yet, so there is no loop to ride on.
// Same accumulator and the same parallel sampling — ~2 s, where the old
// one-cell-at-a-time boot tare took ~13 s. The current zero is taken
// separately at boot by ads1115_zero_calibrate_current().
static void tare_run_blocking(void) {
    tare_begin(false);
    while (!tare_complete() && !tare_timed_out()) {
        for (int i = 0; i < 4; i++) {
            int32_t raw;
            if (gpio_get_level(DT[i]) == 0 && beam_hx711_read(DT[i], SCK[i], &raw))
                tare_add(i, raw);
        }
        if (hx711_is_ready(&thrust_cell))
            tare_add(TARE_THRUST, hx711_read(&thrust_cell));
        vTaskDelay(1);                                       // At least one tick, so the idle task (and its watchdog) still runs
    }
    tare_finish(false);
}

// ================================================================
// READ ALL 4 BEAM CELLS → kg[]
// ================================================================
// Carried forward on cycles where that cell's HX711 hasn't finished its
// ~100 ms (10 SPS) conversion yet.
static float last_kg[4] = { 0.0f, 0.0f, 0.0f, 0.0f };

static void read_all_kg(float kg[4]) {
    const float *CAL = s_cal.cal_kg;                        // Runtime values, seeded from CAL_KG_S1-4
    for (int i = 0; i < 4; i++) {
        // Readiness is pre-checked here so the blocking
        // beam_hx711_read() is only ever called when it will return
        // immediately.
        if (gpio_get_level(DT[i]) == 0) {
            int32_t raw;
            if (beam_hx711_read(DT[i], SCK[i], &raw)) {
                tare_add(i, raw);                                // No-op unless a tare is collecting
                float w = (float)(raw - CH[i].offset) / CAL[i];  // Tare-corrected counts to kg
                switch (i) {
                    case 0: w = correct_S1(w); break;
                    case 1: w = correct_S2(w); break;
                    case 2: w = correct_S3(w); break;
                    case 3: w = correct_S4(w); break;
                }
                w = ch_smooth(&CH[i], w);
                if (fabsf(w) < 0.005f) w = 0.0f;               // Dead band
                last_kg[i] = w;
            }
        }
        kg[i] = last_kg[i];                                  // Fresh or cached, whichever is latest
    }
}

// ================================================================
// READ THRUST FROM S-TYPE CELL
// ================================================================
static float last_thrust_kg = 0.0f;

static float read_thrust_kg(void) {
    if (hx711_is_ready(&thrust_cell)) {
        long raw  = hx711_read(&thrust_cell);
        tare_add(TARE_THRUST, raw);                           // No-op unless a tare is collecting
        long diff = raw - thrust_cell.offset;
        float w   = (float)diff / thrust_cell.scale;
        w         = thrust_linearity_correction(w);
        if (fabsf(w) < 0.05f) w = 0.0f;                      // Dead band
        last_thrust_kg = w;
    }
    return last_thrust_kg;                                    // Fresh or cached, whichever is latest
}

// ================================================================
// RPM SENSOR — IR optical slot sensor on GPIO1, interrupt-driven.
//   RPM comes from the PERIOD between consecutive pulses.
// ================================================================
static volatile uint32_t rpm_pulse_total = 0;
static volatile int64_t  rpm_last_pulse_us = 0;
static volatile int64_t  rpm_last_period_us = 0;
static volatile int64_t  rpm_min_gap_us = -1;
static float               latest_rpm = 0.0f;

#define RPM_DEBOUNCE_US   15000
#define RPM_STALL_TIMEOUT_MS   1500
#define RPM_EMA_ALPHA   0.2f

static float ema_rpm = 0.0f;
static bool  rpm_ema_initialized = false;

static void IRAM_ATTR rpm_isr_handler(void *arg) {
    int64_t now = esp_timer_get_time();
    int64_t gap = now - rpm_last_pulse_us;
    if (rpm_last_pulse_us != 0 && gap < RPM_DEBOUNCE_US) {
        return;
    }
    if (rpm_last_pulse_us != 0) {
        rpm_last_period_us = gap;
        if (rpm_min_gap_us < 0 || gap < rpm_min_gap_us) {
            rpm_min_gap_us = gap;
        }
    }
    rpm_last_pulse_us = now;
    rpm_pulse_total++;
}

static void rpm_sensor_init(void) {
    gpio_config_t cfg = {
        .pin_bit_mask = (1ULL << RPM_PIN),
        .mode         = GPIO_MODE_INPUT,
        .pull_up_en   = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_NEGEDGE,
    };
    gpio_config(&cfg);
    gpio_install_isr_service(0);
    gpio_isr_handler_add(RPM_PIN, rpm_isr_handler, NULL);
    ESP_LOGI(TAG, "RPM sensor init done on GPIO%d, idle level = %d (expect 1 with nothing blocking the slot, if the sensor pulls LOW on trigger)",
             RPM_PIN, gpio_get_level(RPM_PIN));
}

static float rpm_read(float *out_raw) {
    int64_t now   = esp_timer_get_time();
    int64_t period = rpm_last_period_us;
    int64_t last   = rpm_last_pulse_us;

    if (period <= 0 || last == 0) {
        if (out_raw) *out_raw = 0.0f;
        latest_rpm = 0.0f;
        ema_rpm = 0.0f;
        rpm_ema_initialized = false;
        return latest_rpm;
    }

    if ((now - last) > (int64_t)RPM_STALL_TIMEOUT_MS * 1000) {
        if (out_raw) *out_raw = 0.0f;
        latest_rpm = 0.0f;
        ema_rpm = 0.0f;
        rpm_ema_initialized = false;
        return latest_rpm;
    }

    float raw_rpm = 60000000.0f / ((float)period * SLOTS_PER_REV);
    if (out_raw) *out_raw = raw_rpm;

    ema_rpm = rpm_ema_initialized
        ? (RPM_EMA_ALPHA * raw_rpm) + ((1.0f - RPM_EMA_ALPHA) * ema_rpm)
        : raw_rpm;
    rpm_ema_initialized = true;

    latest_rpm = ema_rpm;
    return latest_rpm;
}

// ================================================================
// ADS1115 — I2C INIT AND CHANNEL READ
// ================================================================

// One-time bus + device setup, using the IDF v6 handle API. The legacy
// i2c_param_config() + i2c_driver_install() sequence no longer exists.
static esp_err_t i2c_master_init(void) {
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port          = I2C_MASTER_NUM,
        .sda_io_num        = I2C_MASTER_SDA_IO,
        .scl_io_num        = I2C_MASTER_SCL_IO,
        .clk_source        = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,                       // The recommended default
        .flags.enable_internal_pullup = true,         // External pull-ups are still recommended
    };

    esp_err_t err = i2c_new_master_bus(&bus_cfg, &s_i2c_bus);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2c_new_master_bus failed: %s", esp_err_to_name(err));
        return err;
    }

    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address  = ADS1115_ADDR,
        .scl_speed_hz    = I2C_MASTER_FREQ_HZ,
    };

    err = i2c_master_bus_add_device(s_i2c_bus, &dev_cfg, &s_ads1115_dev);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2c_master_bus_add_device failed: %s", esp_err_to_name(err));
        return err;
    }

    return ESP_OK;
}

// One single-ended channel. MUX codes: 4=A0, 5=A1, 6=A2, 7=A3. True on
// success with the raw 16-bit result in *out_raw; false on any I2C
// failure WITHOUT touching *out_raw.
//
// Deliberately THREE separate transactions rather than one combined
// transmit_receive(): config write (own STOP), pointer write (own
// STOP), then a read with a fresh START. That matches the known-working
// legacy driver's pattern rather than using a repeated START between
// the last two, which some ADS1115 breakouts handle unreliably.
static bool ads1115_read_channel(uint8_t mux_channel, int16_t *out_raw) {
    if (xSemaphoreTake(s_i2c_mutex, pdMS_TO_TICKS(100)) != pdTRUE) {
        ESP_LOGW(TAG, "ADS1115 ch%d: I2C bus busy (other task mid-transaction), skipping this cycle", mux_channel);
        return false;
    }

    uint16_t config =
        (1 << 15) |                 // Start a single conversion
        (mux_channel << 12) |       // Input channel
        (1 << 9)  |                 // PGA ±4.096V
        (1 << 8)  |                 // Single-shot mode
        (4 << 5)  |                 // 128 SPS
        0x0003;                     // Comparator off — required in single-shot

    uint8_t write_buf[3] = {
        0x01,                                    // Config register pointer
        (uint8_t)(config >> 8),
        (uint8_t)(config & 0xFF)
    };

    esp_err_t err = i2c_master_transmit(s_ads1115_dev, write_buf, sizeof(write_buf), 100);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "ADS1115 ch%d config write failed: %s", mux_channel, esp_err_to_name(err));
        xSemaphoreGive(s_i2c_mutex);
        return false;
    }

    vTaskDelay(pdMS_TO_TICKS(10));                // Conversion time

    uint8_t pointer = 0x00;                       // Conversion register
    err = i2c_master_transmit(s_ads1115_dev, &pointer, 1, 100);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "ADS1115 ch%d pointer write failed: %s", mux_channel, esp_err_to_name(err));
        xSemaphoreGive(s_i2c_mutex);
        return false;
    }

    uint8_t read_buf[2];
    err = i2c_master_receive(s_ads1115_dev, read_buf, sizeof(read_buf), 100);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "ADS1115 ch%d result read failed: %s", mux_channel, esp_err_to_name(err));
        xSemaphoreGive(s_i2c_mutex);
        return false;
    }

    *out_raw = (int16_t)((read_buf[0] << 8) | read_buf[1]);
    xSemaphoreGive(s_i2c_mutex);
    return true;
}

// Reads current (A0) and voltage (A1), applies calibration and EMA
// smoothing. If a channel's I2C read fails, that channel's EMA is left
// holding its last good value rather than corrupted by a garbage sample.
static void ads1115_read_current_voltage(float *out_current_amps, float *out_voltage) {
    // ── A0 — current (HAS-600S) ───────────────────────────────────
    int16_t raw_a0;
    if (ads1115_read_channel(4, &raw_a0)) {
        float node_volts_a0   = raw_a0 * ADS_LSB;                      // Counts to volts at the ADC pin
        float sensor_out_a0   = (node_volts_a0 * NETWORK_GAIN) - NETWORK_OFFSET;  // Undo the resistor network
        tare_add_current(1, sensor_out_a0);                                    // Collected for the new zero during a tare
        float current_amps_raw = (sensor_out_a0 - s_current_zero_offset_v) * s_cal.current_scale;  // LIVE, tare-updated zero point

        s_dbg_raw_a0 = raw_a0; s_dbg_node_v_a0 = node_volts_a0; s_dbg_sensor_out_a0 = sensor_out_a0;  // For the [ADS DEBUG] log

        if (despike_check(s_current_hist, &s_current_hist_n, &s_current_hist_idx, current_amps_raw, DESPIKE_MAD_FLOOR_A)) {
            ema_current_amps = ema_initialized
                ? (EMA_ALPHA_CURRENT * current_amps_raw) + ((1.0f - EMA_ALPHA_CURRENT) * ema_current_amps)
                : current_amps_raw;
        }                                                                  // Rejected: hold the last EMA, don't let a glitch pull it
        if (fabsf(ema_current_amps) < CURRENT_DEADBAND_A) ema_current_amps = 0.0f;
    }

    // ── A1 — voltage (LV25-P) ─────────────────────────────────────
    int16_t raw_a1;
    if (ads1115_read_channel(5, &raw_a1)) {
        float node_volts_a1  = raw_a1 * ADS_LSB;
        float sensor_out_a1  = (node_volts_a1 * NETWORK_GAIN) - NETWORK_OFFSET;
        float bus_voltage_raw = (sensor_out_a1 - VOLTAGE_ZERO_OFFSET_V) * s_cal.voltage_scale;

        s_dbg_raw_a1 = raw_a1; s_dbg_node_v_a1 = node_volts_a1; s_dbg_sensor_out_a1 = sensor_out_a1;

        if (despike_check(s_voltage_hist, &s_voltage_hist_n, &s_voltage_hist_idx, bus_voltage_raw, DESPIKE_MAD_FLOOR_V)) {
            ema_bus_voltage = ema_initialized
                ? (EMA_ALPHA_VOLTAGE * bus_voltage_raw) + ((1.0f - EMA_ALPHA_VOLTAGE) * ema_bus_voltage)
                : bus_voltage_raw;                                         // Seeded, not ramped up from zero on boot
        }
    }
    ema_initialized = true;

    *out_current_amps = ema_current_amps;
    *out_voltage       = ema_bus_voltage;
}

// ================================================================
// SECOND SENSOR PAIR — A2 (current #2) / A3 (voltage #2)
//
//   Same chain as ads1115_read_current_voltage(): network reversal →
//   calibration → MAD despike → EMA. A separate function rather than a
//   parameterised one because the two pairs have independent
//   calibration constants and independent filter state, and folding
//   them together would make the load-bearing sensor-1 path harder to
//   read for no real gain.
//
//   With SENSOR2_ENABLED at 0 this compiles down to "write 0.0 and
//   return" — no I2C traffic, so it costs nothing while the hardware
//   is absent.
// ================================================================
static void ads1115_read_current_voltage_2(float *out_current2_amps, float *out_voltage2) {
#if SENSOR2_ENABLED
    int16_t raw_a2;
    if (ads1115_read_channel(6, &raw_a2)) {
        float node_volts_a2    = raw_a2 * ADS_LSB;
        float sensor_out_a2    = (node_volts_a2 * NETWORK2_GAIN) - NETWORK2_OFFSET;
        tare_add_current(2, sensor_out_a2);
        float current2_amps_raw = (sensor_out_a2 - s_current2_zero_offset_v) * s_cal.current2_scale;

        if (despike_check(s_current2_hist, &s_current2_hist_n, &s_current2_hist_idx, current2_amps_raw, DESPIKE_MAD_FLOOR_A)) {
            ema_current2_amps = ema2_initialized
                ? (EMA_ALPHA_CURRENT * current2_amps_raw) + ((1.0f - EMA_ALPHA_CURRENT) * ema_current2_amps)
                : current2_amps_raw;
        }
        if (fabsf(ema_current2_amps) < CURRENT_DEADBAND_A) ema_current2_amps = 0.0f;
    }

    int16_t raw_a3;
    if (ads1115_read_channel(7, &raw_a3)) {
        float node_volts_a3   = raw_a3 * ADS_LSB;
        float sensor_out_a3   = (node_volts_a3 * NETWORK2_GAIN) - NETWORK2_OFFSET;
        float bus_voltage2_raw = (sensor_out_a3 - VOLTAGE2_ZERO_OFFSET_V) * s_cal.voltage2_scale;

        if (despike_check(s_voltage2_hist, &s_voltage2_hist_n, &s_voltage2_hist_idx, bus_voltage2_raw, DESPIKE_MAD_FLOOR_V)) {
            ema_bus_voltage2 = ema2_initialized
                ? (EMA_ALPHA_VOLTAGE * bus_voltage2_raw) + ((1.0f - EMA_ALPHA_VOLTAGE) * ema_bus_voltage2)
                : bus_voltage2_raw;
        }
    }
    ema2_initialized = true;

    *out_current2_amps = ema_current2_amps;
    *out_voltage2      = ema_bus_voltage2;
#else
    // Hardware absent — a clean zero rather than whatever a floating
    // A2/A3 pin happens to be sitting at.
    (void)s_current2_zero_offset_v;                                 // Silence "set but unused" while disabled
    *out_current2_amps = 0.0f;
    *out_voltage2      = 0.0f;
#endif
}

// ================================================================
// CURRENT SENSOR ZERO CALIBRATION  (boot only)
//   A runtime "T" takes the same zero from measurement_task's own A0
//   reads instead (tare_add_current), so it costs no extra I2C time and
//   can't contend with the loop for the bus. This blocking version is
//   for boot, before that loop exists.
//
//   Re-measures sensor_out_a0 — the sensor's output after undoing the
//   resistor network, before the gain — and stores it as the new zero
//   point. Only valid when the motor is genuinely stationary and
//   unloaded, the same assumption "T" already makes for the beam and
//   thrust cells. This is what makes a tare actually zero the current
//   reading rather than leaving it on a fixed constant that can't
//   track per-board or thermal drift.
// ================================================================
static void ads1115_zero_calibrate_current(int samples) {
    float sum = 0.0f;
    int   ok  = 0;
    for (int i = 0; i < samples; i++) {
        int16_t raw_a0;
        if (ads1115_read_channel(4, &raw_a0)) {
            float node_volts_a0 = raw_a0 * ADS_LSB;
            float sensor_out_a0 = (node_volts_a0 * NETWORK_GAIN) - NETWORK_OFFSET;
            sum += sensor_out_a0;
            ok++;
        }
    }
    if (ok > 0) {
        s_current_zero_offset_v = sum / ok;
        ema_current_amps = 0.0f;          // Snapped, not decayed toward zero over several EMA cycles
        ESP_LOGI(TAG, "Current zero-cal: sensor_out_a0 avg = %.5f V over %d/%d samples (was %.5f V)",
                 s_current_zero_offset_v, ok, samples, (double)CURRENT_ZERO_OFFSET_V);
    } else {
        ESP_LOGE(TAG, "Current zero-cal FAILED — no successful ADS1115 reads, keeping previous zero offset (%.5f V)",
                 s_current_zero_offset_v);
    }

#if SENSOR2_ENABLED
    // Compiled out entirely while pair #2 is unpopulated, so a tare
    // costs no extra I2C time today.
    float sum2 = 0.0f;
    int   ok2  = 0;
    for (int i = 0; i < samples; i++) {
        int16_t raw_a2;
        if (ads1115_read_channel(6, &raw_a2)) {
            float node_volts_a2 = raw_a2 * ADS_LSB;
            sum2 += (node_volts_a2 * NETWORK2_GAIN) - NETWORK2_OFFSET;
            ok2++;
        }
    }
    if (ok2 > 0) {
        s_current2_zero_offset_v = sum2 / ok2;
        ema_current2_amps = 0.0f;
        ESP_LOGI(TAG, "Current#2 zero-cal: sensor_out_a2 avg = %.5f V over %d/%d samples",
                 s_current2_zero_offset_v, ok2, samples);
    } else {
        ESP_LOGE(TAG, "Current#2 zero-cal FAILED — keeping previous zero offset (%.5f V)",
                 s_current2_zero_offset_v);
    }
#endif
}

// ================================================================
// ESC / THROTTLE — LEDC PWM SETUP AND CONTROL
// ================================================================
static uint32_t us_to_duty(int us) {
    uint32_t max_duty   = (1 << ESC_PWM_RES) - 1;
    float    period_us  = 1000000.0f / ESC_PWM_FREQ_HZ;
    float    duty_frac  = (float)us / period_us;
    return (uint32_t)(duty_frac * max_duty);
}

static void esc_pwm_init(void) {
    ledc_timer_config_t timer_cfg = {
        .speed_mode      = LEDC_LOW_SPEED_MODE,
        .duty_resolution = ESC_PWM_RES,
        .timer_num       = ESC_LEDC_TIMER,
        .freq_hz         = ESC_PWM_FREQ_HZ,
        .clk_cfg         = LEDC_AUTO_CLK,
    };
    ledc_timer_config(&timer_cfg);

    ledc_channel_config_t channel_cfg = {
        .gpio_num   = ESC_PIN,
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .channel    = ESC_LEDC_CHANNEL,
        .timer_sel  = ESC_LEDC_TIMER,
        .duty       = us_to_duty(THR_MIN),
        .hpoint     = 0,
    };
    ledc_channel_config(&channel_cfg);
}

static void esc_set_throttle_us(int us) {
    if (us < THR_MIN) us = THR_MIN;
    if (us > THR_MAX) us = THR_MAX;
    current_throttle_us = us;
    ledc_set_duty(LEDC_LOW_SPEED_MODE, ESC_LEDC_CHANNEL, us_to_duty(us));
    ledc_update_duty(LEDC_LOW_SPEED_MODE, ESC_LEDC_CHANNEL);
}

static void esc_arm(void) {
    ESP_LOGI(TAG, "Arming ESC...");
    esc_set_throttle_us(1000);
    vTaskDelay(pdMS_TO_TICKS(ESC_ARM_DELAY_MS));
    esc_set_throttle_us(THR_MIN);
    vTaskDelay(pdMS_TO_TICKS(500));
    ESP_LOGI(TAG, "ESC armed — ready for throttle commands");
}

// ================================================================
// COMMAND-READ INIT — receives tare/throttle/calibration commands
//   UART0, not the native USB-Serial/JTAG peripheral. UART0's default
//   ESP32-S3 pins are GPIO43 (TX) / GPIO44 (RX) — the same pins the ROM
//   bootloader's early banner goes out on, so if that banner reaches
//   the external adapter, these are the pins it's wired to. Change
//   UART_NUM_0 and the pins below if the adapter is elsewhere.
//   CMakeLists.txt needs `esp_driver_uart` in REQUIRES/PRIV_REQUIRES.
// ================================================================
// ── Link-health counters ─────────────────────────────────────────
// Reported in every PONG, so the dashboard can see the link from the
// ESP32's side too — the backend alone can only see what arrives at
// the PC, never what was lost on the way IN to the board. Each is
// written by one task and read by command_task; a 32-bit aligned load
// or store is atomic on the ESP32, so no lock is needed.
static QueueHandle_t     s_uart_evt_queue    = NULL;
static volatile uint32_t s_uart_rx_overflows = 0;  // FIFO overflow / RX ring full: command bytes were DROPPED
static volatile uint32_t s_uart_rx_errors    = 0;  // Framing/parity errors: noise on the adapter-TX → GPIO44 wire, or a baud mismatch
static volatile uint32_t s_bad_commands      = 0;  // Lines that were not a known command, or overran cmd_buf
static volatile uint32_t s_loop_overruns     = 0;  // measurement_task cycles longer than TELEMETRY_PERIOD_MS
static uint32_t          s_frame_seq         = 0;  // Telemetry sequence number; a gap at the PC is a dropped frame

static void uart_cmd_init(void) {
    const uart_port_t port = UART_NUM_0;
    uart_config_t cfg = {
        .baud_rate = 115200,
        .data_bits = UART_DATA_8_BITS,
        .parity    = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    // The event queue exists only so command_task can COUNT overflow
    // and framing errors; the bytes themselves are still read with
    // uart_read_bytes(). 16 deep is plenty when it's drained every 20 ms.
    esp_err_t err = uart_driver_install(port, 256, 512, 16, &s_uart_evt_queue, 0);   // rx_buf, tx_buf, event queue
    if (err == ESP_OK) err = uart_param_config(port, &cfg);
    if (err == ESP_OK) err = uart_set_pin(port, GPIO_NUM_43, GPIO_NUM_44, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    if (err == ESP_OK) {
        uart_vfs_dev_use_driver(port);   // printf/ESP_LOGI route through this same driver instance
    } else {
        ESP_LOGE(TAG, "UART0 console init failed (%d) — tare/throttle commands will not be read", err);
    }
}

// ================================================================
// CALIBRATION COMMAND REPORTING
//   Emits the runtime table as an event line, in the same
//   telemetry-less {"event":...} shape as TARE_DONE/THROTTLE_SET, so
//   the backend's existing event branch routes it away from history,
//   latest_data and any recording without needing to know about it.
//
//   Values and defaults go out as TWO lines, not one object. Each is
//   ~450 bytes, and printf blocks until the TX buffer drains (~40 ms
//   per line at 115200) while holding s_io_mutex — splitting them lets
//   a telemetry frame slip out between the two instead of stalling
//   measurement_task for ~90 ms straight.
//
//   Numbers go out in their SHORTEST form that still reads back as the
//   identical float. No fixed precision does both jobs: %.6g rounds
//   0.49213672 to 0.492137, while %.8g prints 78894.8f as "78894.797"
//   — technically that float's true value, but it shows up in the
//   panel as a number nobody typed, and every reload looks like drift.
// ================================================================
// The shortest decimal string that parses back to exactly `v`. Starts
// at 6 significant digits and widens as needed; 9 is the documented
// ceiling for round-tripping a 32-bit float, so this always terminates
// with an exact result.
static void cal_format_float(char *out, size_t out_len, float v) {
    for (int prec = 6; prec <= 9; prec++) {
        snprintf(out, out_len, "%.*g", prec, (double)v);
        if (strtof(out, NULL) == v) return;
    }
}

static void cal_emit_line(const char *event_name, const char *obj_key, bool use_defaults) {
    char body[640];
    int  n = 0;
    for (size_t i = 0; i < CAL_FIELD_COUNT; i++) {
        const cal_field_t *f = &CAL_FIELDS[i];
        float v = use_defaults ? f->def : *f->ptr;
        char  num[24];
        cal_format_float(num, sizeof(num), v);
        int w = snprintf(body + n, sizeof(body) - n, "%s\"%s\":%s",
                         i ? "," : "", f->key, num);
        if (w < 0 || w >= (int)sizeof(body) - n) {         // Truncated: the backend rejects a malformed line rather than acting on half a table
            ESP_LOGE(TAG, "Calibration report truncated — body buffer too small");
            break;
        }
        n += w;
    }
    if (xSemaphoreTake(s_io_mutex, portMAX_DELAY) == pdTRUE) {
        printf("{\"event\":\"%s\",\"%s\":{%s}}\n", event_name, obj_key, body);
        xSemaphoreGive(s_io_mutex);
    }
}

static void cal_report_all(void) {
    cal_emit_line("CAL",          "values",   false);      // Live, including anything restored from NVS
    cal_emit_line("CAL_DEFAULTS", "defaults", true);        // So the panel can show "was X" and offer a revert
}

// The value WAS applied live but could not be written to flash.
// Compared by pointer at the call site, so that case is acked as a
// success-with-warning rather than a rejection — the operator's bench
// session is already using the new number.
static const char CAL_ERR_NVS[] = "saved_live_but_nvs_write_failed";

// Applies one "key=value" pair. Returns a short reason string for the
// ack line (NULL on success), so the operator sees *why* a value was
// refused instead of it silently not taking effect.
static const char *cal_apply(const char *key, const char *value_str, float *out_applied) {
    const cal_field_t *f = cal_find(key);
    if (!f) return "unknown_key";

    char *end = NULL;
    float v = strtof(value_str, &end);
    if (end == value_str || *end != '\0') return "not_a_number";   // Rejects "12abc" and "" alike
    if (!cal_value_ok(f, v))              return "out_of_range";

    *f->ptr = v;                                            // Immediate — the next measurement cycle uses it
    *out_applied = v;
    if (!cal_store_to_nvs(f, v)) return CAL_ERR_NVS;         // Still applied: a flash fault shouldn't block the bench session, it just won't survive a reboot
    return NULL;
}

// ================================================================
// COMMAND PROTOCOL — newline-terminated ASCII lines over UART0
//     "T"          → re-tare all load cells and the current sensor
//                    (TARE_STARTED now, TARE_DONE ~2 s later)
//     "P<n>"       → link check; answered at once with PONG n plus the
//                    counters above, so the PC can time the round trip
//                    and knows its commands are actually arriving
//     "S<us>"      → set ESC throttle, e.g. "S1300"
//     "C"          → dump the runtime calibration table (two event lines)
//     "C<key>=<v>" → set one value, e.g. "Cthrust_scale=18178.5"
//     "C!"         → restore every value to its firmware default
// ================================================================
#define CMD_BUF_SIZE 48                  // Longest is a calibration set, ~27 chars; 32 left almost no headroom
static char    cmd_buf[CMD_BUF_SIZE];
static int     cmd_buf_len = 0;
static int64_t s_cmd_recv_start_us = 0;

static void handle_command_line(const char *line) {
    if (line[0] == 'T' && line[1] == '\0') {
        // Only a request: measurement_task does the sampling with the
        // reads it already takes, and sends TARE_DONE when every cell
        // has its samples. A "T" while one is running restarts it.
        // Still assumes no load on the cells and the motor genuinely at
        // 0 A, same as ever.
        ESP_LOGI(TAG, "Tare command received — collecting %d samples per cell", TARE_SAMPLES);
        s_tare_requested = true;
        if (xSemaphoreTake(s_io_mutex, portMAX_DELAY) == pdTRUE) {
            printf("{\"event\":\"TARE_STARTED\",\"samples\":%d,\"timeout_ms\":%d}\n",
                   TARE_SAMPLES, TARE_TIMEOUT_MS);
            xSemaphoreGive(s_io_mutex);
        }
    }
    else if (line[0] == 'P') {
        // Answered here in command_task, at its higher priority, so the
        // round trip measures the link and the command path — not how
        // far through a sensor read measurement_task happened to be.
        unsigned long n = strtoul(line + 1, NULL, 10);
        if (xSemaphoreTake(s_io_mutex, portMAX_DELAY) == pdTRUE) {
            printf("{\"event\":\"PONG\",\"n\":%lu,\"up_ms\":%lld,"
                   "\"rx_ovf\":%lu,\"rx_err\":%lu,\"bad_cmd\":%lu,\"late\":%lu}\n",
                   n, (long long)(esp_timer_get_time() / 1000),
                   (unsigned long)s_uart_rx_overflows, (unsigned long)s_uart_rx_errors,
                   (unsigned long)s_bad_commands, (unsigned long)s_loop_overruns);
            xSemaphoreGive(s_io_mutex);
        }
    }
    else if (line[0] == 'S') {
        int us = atoi(line + 1);
        esc_set_throttle_us(us);
        int64_t latency_us = esp_timer_get_time() - s_cmd_recv_start_us;
        if (xSemaphoreTake(s_io_mutex, portMAX_DELAY) == pdTRUE) {
            printf("{\"event\":\"THROTTLE_SET\",\"us\":%d}\n", current_throttle_us);
            xSemaphoreGive(s_io_mutex);
        }
        ESP_LOGI(TAG, "[TIMING] throttle cmd -> applied in %lld us (target %d us, confirmed %d us)",
                 (long long)latency_us, us, current_throttle_us);
    }
    // Handled in command_task, not measurement_task: an NVS commit is
    // single-digit milliseconds of flash write and must not land
    // inside the paced sensor loop.
    else if (line[0] == 'C') {
        if (line[1] == '\0') {
            cal_report_all();                               // "C" — what is the device using?
        }
        else if (line[1] == '!' && line[2] == '\0') {
            // Live values revert first, then NVS is wiped: in that
            // order a failed erase still leaves the operator on the
            // defaults for this session rather than half-reverted.
            for (size_t i = 0; i < CAL_FIELD_COUNT; i++) *CAL_FIELDS[i].ptr = CAL_FIELDS[i].def;
            bool erased = cal_erase_nvs();
            ESP_LOGI(TAG, "Calibration reset to firmware defaults (NVS erase %s)", erased ? "ok" : "FAILED");
            if (xSemaphoreTake(s_io_mutex, portMAX_DELAY) == pdTRUE) {
                printf("{\"event\":\"CAL_RESET\",\"ok\":%s}\n", erased ? "true" : "false");
                xSemaphoreGive(s_io_mutex);
            }
            cal_report_all();                               // Echoed, so the dashboard needn't ask again
        }
        else {
            // "C<key>=<value>", split on the first '=' in place — the
            // command buffer is ours and is rebuilt for every line.
            const char *eq = strchr(line + 1, '=');
            const char *err = "malformed";
            float applied = 0.0f;
            char  key[32] = {0};
            if (eq) {
                size_t klen = (size_t)(eq - (line + 1));
                if (klen > 0 && klen < sizeof(key)) {
                    memcpy(key, line + 1, klen);
                    key[klen] = '\0';
                    err = cal_apply(key, eq + 1, &applied);
                }
            }
            if (err == NULL || err == CAL_ERR_NVS) {
                char num[24];
                cal_format_float(num, sizeof(num), applied);  // Same form as the full dump, so the two never disagree on a digit
                ESP_LOGI(TAG, "Calibration %s = %s (%s)", key, num,
                         err == CAL_ERR_NVS ? "LIVE ONLY — flash write failed, will not survive a reboot" : "saved");
                if (xSemaphoreTake(s_io_mutex, portMAX_DELAY) == pdTRUE) {
                    if (err == CAL_ERR_NVS) {
                        printf("{\"event\":\"CAL_SET\",\"key\":\"%s\",\"value\":%s,\"ok\":true,\"warning\":\"%s\"}\n",
                               key, num, err);
                    } else {
                        printf("{\"event\":\"CAL_SET\",\"key\":\"%s\",\"value\":%s,\"ok\":true}\n", key, num);
                    }
                    xSemaphoreGive(s_io_mutex);
                }
            } else {
                ESP_LOGW(TAG, "Calibration command rejected (%s): %s", err, line);
                if (xSemaphoreTake(s_io_mutex, portMAX_DELAY) == pdTRUE) {
                    printf("{\"event\":\"CAL_SET\",\"key\":\"%s\",\"ok\":false,\"error\":\"%s\"}\n", key, err);
                    xSemaphoreGive(s_io_mutex);
                }
            }
        }
    }
    else {
        // Counted rather than answered: a garbled line is a symptom of
        // the link, and the PONG counters are where that shows up.
        s_bad_commands++;
        ESP_LOGW(TAG, "Unknown command ignored: %s", line);
    }
}

static void uart_count_events(void) {
    if (s_uart_evt_queue == NULL) return;
    uart_event_t ev;
    while (xQueueReceive(s_uart_evt_queue, &ev, 0) == pdTRUE) {
        switch (ev.type) {
            case UART_FIFO_OVF:
            case UART_BUFFER_FULL:  s_uart_rx_overflows++; break;
            case UART_FRAME_ERR:
            case UART_PARITY_ERR:   s_uart_rx_errors++;    break;
            default:                                       break;   // UART_DATA etc. — nothing to count
        }
    }
}

static void process_command_byte(uint8_t byte) {
    if (cmd_buf_len == 0 && byte != '\n' && byte != '\r') {
        s_cmd_recv_start_us = esp_timer_get_time();
    }

    if (byte == '\n' || byte == '\r') {
        if (cmd_buf_len > 0) {
            cmd_buf[cmd_buf_len] = '\0';
            handle_command_line(cmd_buf);
            cmd_buf_len = 0;
        }
        return;
    }

    if (cmd_buf_len < CMD_BUF_SIZE - 1) {
        cmd_buf[cmd_buf_len++] = (char)byte;
    } else {
        cmd_buf_len = 0;
        s_bad_commands++;                                    // Overran the buffer — dropped
    }
}

static void command_task(void *arg) {
    uint8_t buf[64];
    while (1) {
        int len = uart_read_bytes(UART_NUM_0, buf, sizeof(buf), pdMS_TO_TICKS(CMD_READ_TIMEOUT_MS));
        for (int i = 0; i < len; i++) {
            process_command_byte(buf[i]);
        }
        uart_count_events();
    }
}

// ================================================================
// CALIBRATION MODE
// ================================================================
static void run_calibration(void) {
    printf("\n================================================\n");
    printf("  CALIBRATION MODE — beam cells raw counts\n");
    printf("================================================\n");
    printf("  Remove ALL loads. Taring in 5 s...\n\n");
    vTaskDelay(pdMS_TO_TICKS(5000));
    beam_tare_all(50);

    printf("  %-6s  %-12s %-12s %-12s %-12s\n",
           "#", "S1_cnt", "S2_cnt", "S3_cnt", "S4_cnt");
    printf("  %-6s  %-12s %-12s %-12s %-12s\n",
           "──────","────────────","────────────",
           "────────────","────────────");
    int n = 1;
    while (1) {
        long d[4];
        for (int i = 0; i < 4; i++)
            d[i] = beam_hx711_avg(DT[i], SCK[i], 20) - CH[i].offset;
        printf("  %-6d  %-12ld %-12ld %-12ld %-12ld\n",
               n++, d[0], d[1], d[2], d[3]);
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}

// ================================================================
// NORMAL MEASUREMENT MODE — one-time setup
// ================================================================
static void run_measurement_setup(void) {
    printf("\n================================================\n");
    printf("  NWA BENCH CONSOLE — MOTOR TORQUE + THRUST + RPM\n");
    printf("  Torque arm : 0.300 m\n");
    printf("  Output     : JSON @ target %dHz over USB (see LOOP_TIMING_LOG_EVERY logs for the measured actual rate)\n", 1000 / TELEMETRY_PERIOD_MS);
    printf("  Tare cmd   : send \"T\" to re-zero all cells (beam x4, thrust, current sensor)\n");
    printf("  Throttle   : send \"S<us>\" to set ESC, e.g. S1300\n");
    printf("================================================\n");
    ESP_LOGI(TAG, "TQ_K CW : %.4f %.4f %.4f %.4f", s_cal.tq_cw[0], s_cal.tq_cw[1], s_cal.tq_cw[2], s_cal.tq_cw[3]);
    ESP_LOGI(TAG, "TQ_K CCW: %.4f %.4f %.4f %.4f", s_cal.tq_ccw[0], s_cal.tq_ccw[1], s_cal.tq_ccw[2], s_cal.tq_ccw[3]);
    ESP_LOGI(TAG, "Thrust scale: %.1f  STABLE_THR: %.3f kg", thrust_cell.scale, (double)STABLE_THR);
    // These print the LIVE table, so a value edited from the dashboard
    // and restored from NVS shows up in the boot log — the quickest way
    // to tell "running an override" from "running what main.c says".
    ESP_LOGI(TAG, "Beam scales (counts/kg): S1=%.1f S2=%.1f S3=%.1f S4=%.1f",
             s_cal.cal_kg[0], s_cal.cal_kg[1], s_cal.cal_kg[2], s_cal.cal_kg[3]);
    ESP_LOGI(TAG, "Sensor scales: current=%.3f voltage=%.3f  current2=%.3f voltage2=%.3f",
             s_cal.current_scale, s_cal.voltage_scale, s_cal.current2_scale, s_cal.voltage2_scale);
    ESP_LOGI(TAG, "Sensor pair #2 (ADS1115 A2/A3): %s",
             SENSOR2_ENABLED ? "ENABLED — readings are UNVERIFIED until CURRENT2/VOLTAGE2 constants are re-derived"
                             : "disabled (hardware absent) — current_a2/voltage_v2 publish 0.00");
    printf("  Remove all loads. Taring in 5 s...\n\n");
    vTaskDelay(pdMS_TO_TICKS(5000));

    tare_run_blocking();                                         // All five cells in parallel, ~2 s

    ads1115_zero_calibrate_current(CURRENT_ZERO_CAL_SAMPLES);   // At boot too, not just on "T" — same "remove all loads" window
}

// ================================================================
// MEASUREMENT TASK
// ================================================================
static void measurement_task(void *arg) {
    int loop_n = 0;
    TickType_t last_wake = xTaskGetTickCount();

    while (1) {
        int64_t t_loop_start = esp_timer_get_time();

        // Finished at the TOP of a cycle, before this cycle's reads: the
        // previous frame went out on the old zero, so applying the new
        // offsets here means the first frame after TARE_DONE is already
        // on the new one, with no stale frame in between.
        if (s_tare_active && (tare_complete() || tare_timed_out())) {
            tare_finish(true);
        }
        // Picked up here too, so the accumulators are only ever touched
        // from this task.
        if (s_tare_requested) {
            s_tare_requested = false;
            tare_begin(true);
        }

        float kg[4];
        read_all_kg(kg);                                     // Also feeds a running tare
        int64_t t_beam_done = esp_timer_get_time();

        float thrust_kg = read_thrust_kg();
        float thrust_N  = thrust_kg * GRAVITY;
        int64_t t_thrust_done = esp_timer_get_time();

        // Direction from S3, with hysteresis
        if (kg[2] > TQ_HYST_KG) {
            s_is_ccw = false;
        } else if (kg[2] < -TQ_HYST_KG) {
            s_is_ccw = true;
        }

        // Read once per frame into locals, so a settings write landing
        // mid-computation can't mix the two tables inside one value.
        float TQ_K1 = s_is_ccw ? s_cal.tq_ccw[0] : s_cal.tq_cw[0];
        float TQ_K2 = s_is_ccw ? s_cal.tq_ccw[1] : s_cal.tq_cw[1];
        float TQ_K3 = s_is_ccw ? s_cal.tq_ccw[2] : s_cal.tq_cw[2];
        float TQ_K4 = s_is_ccw ? s_cal.tq_ccw[3] : s_cal.tq_cw[3];

        // F_arm (arm force, N) is no longer published, but it's still
        // the intermediate the torque figure is built from.
        float tq_kg  = TQ_K1*kg[0] + TQ_K2*kg[1]
                     + TQ_K3*kg[2] + TQ_K4*kg[3];
        float F_arm  = tq_kg  * GRAVITY;
        float tau_Nm = F_arm  * TORQUE_ARM_M;

        float sv_tq = 0.0f;
        int   stable = ch_stable(&CH[2], tq_kg, &sv_tq);
        float sv_tau = sv_tq * GRAVITY * TORQUE_ARM_M;

        float rpm_raw = 0.0f;
        float rpm = rpm_read(&rpm_raw);
        int64_t t_rpm_done = esp_timer_get_time();

        float current_amps = 0.0f, voltage_v = 0.0f;
        ads1115_read_current_voltage(&current_amps, &voltage_v);
        float current2_amps = 0.0f, voltage2_v = 0.0f;
        ads1115_read_current_voltage_2(&current2_amps, &voltage2_v);   // Returns 0.00 while SENSOR2_ENABLED is 0
        int64_t t_ads_done = esp_timer_get_time();

        if (xSemaphoreTake(s_io_mutex, portMAX_DELAY) == pdTRUE) {
            printf("{\"torque\":%.4f,\"thrust\":%.3f,"
                   "\"s1\":%.3f,\"s2\":%.3f,\"s3\":%.3f,\"s4\":%.3f,"
                   "\"stable\":%s,\"dir\":\"%s\","
                   "\"rpm\":%.1f,\"rpm_raw\":%.1f,"
                   "\"current_a\":%.2f,\"voltage_v\":%.2f,"
                   "\"current_a2\":%.2f,\"voltage_v2\":%.2f,"
                   "\"throttle_us\":%d,\"seq\":%lu}\n",
                   stable ? sv_tau : tau_Nm,
                   thrust_N,
                   kg[0], kg[1], kg[2], kg[3],
                   stable ? "true" : "false",
                   s_is_ccw ? "CCW" : "CW",
                   rpm,
                   rpm_raw,
                   current_amps,
                   voltage_v,
                   current2_amps,
                   voltage2_v,
                   current_throttle_us,
                   (unsigned long)s_frame_seq);
            xSemaphoreGive(s_io_mutex);
        }
        s_frame_seq++;                                       // Wraps at 2^32 (~6.8 years at 20 Hz); the backend handles it

        int64_t t_loop_end = esp_timer_get_time();
        if (t_loop_end - t_loop_start > (int64_t)TELEMETRY_PERIOD_MS * 1000) {
            s_loop_overruns++;                               // This cycle alone used up the whole 50 ms budget
        }
        loop_n++;
        if (loop_n >= LOOP_TIMING_LOG_EVERY) {
            loop_n = 0;
            ESP_LOGI(TAG, "[TIMING] loop=%lldus beam=%lldus thrust=%lldus rpm=%lldus ads1115=%lldus json+tail=%lldus",
                     (long long)(t_loop_end - t_loop_start),
                     (long long)(t_beam_done - t_loop_start),
                     (long long)(t_thrust_done - t_beam_done),
                     (long long)(t_rpm_done - t_thrust_done),
                     (long long)(t_ads_done - t_rpm_done),
                     (long long)(t_loop_end - t_ads_done));
            // To re-derive VOLTAGE_SCALE (and NETWORK_GAIN/OFFSET if
            // needed) after the panel rebuild: apply a known,
            // multimeter-verified bus voltage, read sensor_out_a1 from
            // this line, and true_scale = actual_volts / sensor_out_a1.
            // Do it at TWO known voltages, not one, before trusting a
            // new constant.
            ESP_LOGI(TAG, "[ADS DEBUG] A0(I) raw=%d node_v=%.4f sensor_out=%.4f amps=%.3f | A1(V) raw=%d node_v=%.4f sensor_out=%.4f volts=%.3f",
                     s_dbg_raw_a0, s_dbg_node_v_a0, s_dbg_sensor_out_a0, current_amps,
                     s_dbg_raw_a1, s_dbg_node_v_a1, s_dbg_sensor_out_a1, voltage_v);
        }

        vTaskDelayUntil(&last_wake, pdMS_TO_TICKS(TELEMETRY_PERIOD_MS));
    }
}

// ================================================================
// ENTRY POINT
// ================================================================
// Stable names for the BOOT event — the enum's numeric values are an
// IDF implementation detail and not something the dashboard should key on.
static const char *reset_reason_key(esp_reset_reason_t r) {
    switch (r) {
        case ESP_RST_POWERON:   return "POWERON";        // Also what an adapter's DTR/RTS auto-reset looks like
        case ESP_RST_EXT:       return "EXTERNAL";
        case ESP_RST_SW:        return "SOFTWARE";
        case ESP_RST_PANIC:     return "PANIC";
        case ESP_RST_INT_WDT:   return "INT_WDT";
        case ESP_RST_TASK_WDT:  return "TASK_WDT";
        case ESP_RST_WDT:       return "WDT";
        case ESP_RST_DEEPSLEEP: return "DEEPSLEEP";
        case ESP_RST_BROWNOUT:  return "BROWNOUT";
        case ESP_RST_SDIO:      return "SDIO";
        default:                return "OTHER";
    }
}

void app_main(void) {
    // Check this line first if the ESC or sensors misbehave. On a
    // current-limited UART adapter's power pin, a brownout under load
    // (ESC arming, or the extra draw during a tare) shows up here as
    // ESP_RST_BROWNOUT — a power delivery problem, not a wiring or code
    // one.
    esp_reset_reason_t reset_reason = esp_reset_reason();
    ESP_LOGI(TAG, "Reset reason: %d (%s)", reset_reason,
             reset_reason == ESP_RST_BROWNOUT ? "BROWNOUT — check board power supply/current capacity" :
             reset_reason == ESP_RST_POWERON  ? "power-on (normal cold boot)" :
             reset_reason == ESP_RST_SW       ? "software reset" :
             reset_reason == ESP_RST_PANIC    ? "PANIC — check for a crash log above this line" :
             "other");

    uart_cmd_init();

    // A machine-readable twin of the log line above. A brownout
    // mid-run otherwise looks, from the PC, like a ~15 s stall followed
    // by normal data — with the tare and throttle state silently lost.
    // No other task exists yet, so no s_io_mutex (it isn't created yet either).
    printf("{\"event\":\"BOOT\",\"reason\":\"%s\"}\n", reset_reason_key(reset_reason));

    // Must run before the first sensor read, so the very first frame
    // already uses the operator's stored values rather than briefly
    // publishing default-scaled numbers.
    //
    // The retry on NO_FREE_PAGES/NEW_VERSION_FOUND is the standard IDF
    // pattern, hit after a partition-table change or an OTA to a newer
    // NVS format. Erasing loses the stored calibration — falling back
    // to the defines, which is the safe direction — but leaves the
    // device usable rather than failing every save from then on.
    esp_err_t nvs_err = nvs_flash_init();
    if (nvs_err == ESP_ERR_NVS_NO_FREE_PAGES || nvs_err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "NVS partition needs erasing (%s) — stored calibration will be lost, reverting to firmware defaults",
                 esp_err_to_name(nvs_err));
        ESP_ERROR_CHECK(nvs_flash_erase());
        nvs_err = nvs_flash_init();
    }
    if (nvs_err == ESP_OK) {
        cal_load_from_nvs();
    } else {
        // Not fatal: the rig runs fine on the compiled-in constants, it
        // just can't remember dashboard edits.
        ESP_LOGE(TAG, "NVS init failed (%s) — calibration edits will apply live but won't persist",
                 esp_err_to_name(nvs_err));
    }

    rpm_sensor_init();

    esc_pwm_init();
    esc_arm();

    if (i2c_master_init() != ESP_OK) {
        ESP_LOGE(TAG, "I2C init failed — current/voltage will read 0");
        // Continue anyway — torque/thrust/RPM don't need it
    }

    for (int i = 0; i < 4; i++)
        beam_hx711_init(DT[i], SCK[i]);

    hx711_init(&thrust_cell);

    if (!hx711_wait_ready(&thrust_cell, 3000)) {
        ESP_LOGE(TAG, "Thrust HX711 not detected!");
    }

#if CALIBRATION_MODE
    run_calibration();
#else
    s_io_mutex  = xSemaphoreCreateMutex();
    s_i2c_mutex = xSemaphoreCreateMutex();

    run_measurement_setup();

    xTaskCreate(command_task,     "cmd_task",   4096, NULL, 10, NULL);
    xTaskCreate(measurement_task, "meas_task",  8192, NULL, 5,  NULL);
#endif
}