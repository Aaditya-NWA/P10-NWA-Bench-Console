# See the build system documentation in IDF programming guide
# for more information about component CMakeLists.txt files.
#
# Reference copy only — the actual build tree lives outside this repo (see
# idfcmd.txt / CLAUDE.md). main.c, hx711.c and hx711.h are copied into that
# tree's main/ directory before building; this file is NOT copied alongside
# them, so if you add a new PRIV_REQUIRES here, you must also add it to the
# build tree's main/CMakeLists.txt by hand or the build will fail with a
# "fatal error: ...h: No such file or directory" that looks unrelated.

idf_component_register(
    SRCS main.c hx711.c    # list the source files of this component
    INCLUDE_DIRS           # optional, add here public include directories
    PRIV_INCLUDE_DIRS       # optional, add here private include directories
    REQUIRES                # optional, list the public requirements (component names)
    PRIV_REQUIRES esp_driver_usb_serial_jtag esp_timer driver esp_driver_gpio esp_driver_ledc esp_driver_i2c esp_driver_uart nvs_flash    # esp_driver_usb_serial_jtag: tare/throttle commands (renamed from usb_serial_jtag in IDF 5.3+) | esp_timer: RPM windowing | driver: legacy catch-all, no longer re-exports GPIO under IDF v6 | esp_driver_gpio: driver/gpio.h — HX711 bit-bang and RPM pulse ISR | esp_driver_ledc: PWM signal generation for ESC (renamed from ledc in IDF 5.3+) | esp_driver_i2c: ADS1115 current/voltage sensor (renamed from i2c in IDF 5.3+) | esp_driver_uart: UART0 command/telemetry transport | nvs_flash: persists dashboard-edited calibration
)
