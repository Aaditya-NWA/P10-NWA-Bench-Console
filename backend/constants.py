# Shared constants for the backend. Anything that has to match a value
# produced elsewhere — the firmware's wire protocol, the frontend, a
# folder name on the operator's disk — lives here so there is exactly
# one spelling of it.

from __future__ import annotations

# ── Version ───────────────────────────────────────────────────────
# The single source of truth for the backend's version. Hand-duplicated
# in frontend/package.json, src-tauri/tauri.conf.json and Cargo.toml —
# tests/test_deployment.py fails the build if they drift apart, because
# the updater compares versions and a stale one means installed copies
# either miss a release or reinstall one they already have.
APP_VERSION = "1.0.0"

# ── Desktop shell (Tauri sidecar) ─────────────────────────────────
# The backend runs as a child process of the Rust shell, which needs a
# way to (a) know when it is actually accepting connections and (b) ask
# it to stop *gracefully*. Killing it outright skips the shutdown
# handler, and that handler is what aborts a running profile and writes
# THR_MIN to the ESC — see DEPLOYMENT.md §9. Everything below exists to
# keep that write reachable.
DEFAULT_HOST = "127.0.0.1"              # Loopback only; there is no LAN story yet
DEFAULT_PORT = 8000                     # Also in frontend/src/constants.js

# The shell generates a fresh random token per launch and passes it in
# the environment rather than argv — argv is readable by any process on
# the machine, and this token stops the motor.
SHUTDOWN_TOKEN_ENV = "NWA_SHUTDOWN_TOKEN"
SHUTDOWN_TOKEN_HEADER = "X-Shutdown-Token"

# Absent token means "not launched by the shell" (dev, tests). The
# endpoint then refuses everyone rather than defaulting to open: in dev
# the operator has Ctrl+C, so an open shutdown route would be all risk
# and no benefit.
ERR_SHUTDOWN_DISABLED = "Shutdown endpoint is not enabled"
ERR_SHUTDOWN_BAD_TOKEN = "Invalid shutdown token"

# ── Serial link ───────────────────────────────────────────────────
DEFAULT_BAUD = 115200                 # Must match the ESP32 firmware
SERIAL_READ_TIMEOUT_S = 1.0
SERIAL_ENCODING = "utf-8"

# Description keywords that identify an ESP32-like USB port. "jtag"
# matches the ESP32-S3's own "USB JTAG/serial debug unit" descriptor,
# which the substring list without it silently missed.
ESP32_PORT_KEYWORDS = (
    "cp210", "ch340", "ftdi", "uart", "usb serial", "usb-serial", "jtag",
)

# ── Throttle range, in microseconds ───────────────────────────────
# Hand-duplicated in esp32/main.c and frontend/src/constants.js. The
# firmware clamps independently, so a mismatch can't drive the ESC out
# of range — it just means the dashboard offers targets it refuses.
THR_MIN = 1000
THR_MAX = 2000

# ── Commands written to the ESP32 ─────────────────────────────────
CMD_TARE = "T"                        # Re-zero all cells and the current sensor
CMD_THROTTLE = "S"                    # S<us>
CMD_PING = "P"                        # P<n>; answered with a PONG carrying n — see link_health.py
CMD_CAL_PREFIX = "C"                  # Every calibration command starts with this
CMD_CAL_DUMP = CMD_CAL_PREFIX         # Bare "C" asks for the runtime table
CMD_CAL_RESET = "C!"                  # Restore every value to its firmware default
CMD_CAL_SET = "C{key}={value}"        # Set one value; see CAL_VALUE_FORMAT

# 9 significant digits is the documented round-trip width for a 32-bit
# float, so the number strtof() produces on the device is bit-identical
# to what was validated here. It also keeps the line well inside the
# firmware's 48-byte command buffer, which repr() would not.
CAL_VALUE_FORMAT = "{:.9g}"

# ── Event names the firmware sends back ───────────────────────────
EVENT_KEY = "event"
EV_TARE_STARTED = "TARE_STARTED"     # Sent at once on "T"; TARE_DONE follows ~2 s later
EV_TARE_DONE = "TARE_DONE"
EV_THROTTLE_SET = "THROTTLE_SET"
EV_PONG = "PONG"                      # Reply to a ping, with the device's link counters
EV_BOOT = "BOOT"                      # First line after any reset, with the reset reason
EV_CAL = "CAL"
EV_CAL_DEFAULTS = "CAL_DEFAULTS"
EV_CAL_SET = "CAL_SET"
EV_CAL_RESET = "CAL_RESET"

# ── WebSocket message types (backend → browser) ───────────────────
MSG_HISTORY = "history"
MSG_ACK = "ack"
MSG_PROFILE = "profile"

# ── Command actions (browser → backend) ───────────────────────────
ACTION_TARE = "tare"
ACTION_THROTTLE = "throttle"

ACK_SENT = "sent"
ACK_ERROR = "error"

WS_UNAUTHORIZED = 4401                # Application-level "no valid session"

# ── Profile sequencer phases ──────────────────────────────────────
PHASE_IDLE = "idle"
PHASE_RAMPING = "ramping"
PHASE_HOLDING = "holding"
PHASE_RAMPING_DOWN = "ramping_down"
PHASE_DONE = "done"
PHASE_ABORTED = "aborted"
PHASE_ERROR = "error"

# ── Audit trail categories, events and sources ────────────────────
AUDIT_THROTTLE = "throttle"
AUDIT_CALIBRATION = "calibration"

AUDIT_EV_SET = "set"
AUDIT_EV_CUT = "cut"
AUDIT_EV_RESET = "reset"

THROTTLE_SOURCE_MANUAL = "manual"
THROTTLE_SOURCE_CUT = "cut"
CAL_SOURCE_DASHBOARD = "dashboard"

# ── Operator data folders, under <Documents>/ ─────────────────────
APP_FOLDER_NAME = "NWA Bench Console"
DATA_DIR_ENV = "NWA_BENCH_DATA_DIR"   # Set to relocate everything; the test suite does

DIR_CSV = "CSV"
DIR_LOGIN = "Login"
DIR_LOGS = "Logs"
DIR_RECOVERY = "Recovery"
LOGIN_FILENAME = "login.json"

FILE_ENCODING = "utf-8"
# Excel opens a plain utf-8 CSV as mojibake on a Windows machine with a
# non-UTF8 locale, and these files carry "N·m" and "µs" in the header.
CSV_ENCODING = "utf-8-sig"

UNKNOWN_OPERATOR = "unknown"

# ── Spin direction ────────────────────────────────────────────────
DIR_CW = "CW"
DIR_CCW = "CCW"

# ── Log line prefixes ─────────────────────────────────────────────
LOG_SERIAL = "[Serial]"
LOG_SERVER = "[Server]"
LOG_WS = "[WS]"
LOG_RECORDING = "[Recording]"
LOG_PROFILE = "[Profile]"
LOG_AUDIT = "[Audit]"
LOG_AUTH = "[Auth]"
LOG_REPLAY = "[Replay]"

# ── Repeated operator-facing messages ─────────────────────────────
ERR_SERIAL_DOWN = "Serial not connected"
ERR_SERIAL_CONNECT_FIRST = "Serial not connected — connect to the ESP32 first"
ERR_SERIAL_WRITE = "Serial write failed — check the connection"
ERR_ALREADY_CONNECTED = "Already connected — disconnect first"
ERR_PROFILE_RUNNING_CAL = (
    "A throttle profile is running — stop it before changing calibration"
)

# ── Accounts & sessions ───────────────────────────────────────────
ROLE_OPERATOR = "operator"
ROLE_ADMIN = "admin"
# Returned in place of a username while no account exists — setup mode
# is open by design, or there would be no way to create the first one.
SETUP_USER = "setup"

PBKDF2_HASH_NAME = "sha256"
MIN_PASSWORD_LEN = 6
MAX_USERNAME_LEN = 40

HTTP_UNAUTHORIZED = 401
ERR_NOT_SIGNED_IN = "Not signed in"
# Same message whether the username or the password was wrong — telling
# an unauthenticated caller that a username exists is free
# reconnaissance for no operator benefit.
ERR_BAD_CREDENTIALS = "Incorrect username or password"
