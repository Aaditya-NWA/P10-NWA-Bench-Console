// Shared string constants for the frontend. Anything that has to match
// a value produced elsewhere (the backend, the firmware, localStorage,
// a previous release) lives here so there is exactly one spelling of it.

// ── Backend address ───────────────────────────────────────────────
// Absolute, not relative: under the Tauri desktop shell the dashboard is
// loaded from the app's own bundled assets, so it is NOT same-origin
// with the backend and a relative path would resolve to the app itself.
//
// 127.0.0.1 rather than "localhost" on purpose. The sidecar binds
// 127.0.0.1 explicitly, and on Windows "localhost" often resolves to
// ::1 (IPv6) first — which nothing is listening on, so the request fails
// with a connection error that looks exactly like "the backend is down".
//
// Overridable at build time (VITE_API_HOST / VITE_API_PORT) so a dev
// server, a LAN build, or a second instance on another port needs no
// code edit. The port must match backend/constants.py's DEFAULT_PORT and
// the BACKEND_PORT constant in src-tauri/src/main.rs.
const API_HOST = import.meta.env?.VITE_API_HOST || "127.0.0.1";
const API_PORT = import.meta.env?.VITE_API_PORT || "8000";

export const API_BASE = `http://${API_HOST}:${API_PORT}`;
export const WS_BASE  = `ws://${API_HOST}:${API_PORT}/ws`;

// ── Browser storage keys ──────────────────────────────────────────
export const STORAGE_TOKEN = "nwa-bench-token";
export const STORAGE_THEME = "nw-theme";
export const STORAGE_UNITS = "nwa-units";

// ── Window events ─────────────────────────────────────────────────
export const EVT_AUTH_EXPIRED = "nwa-auth-expired"; // Any 401 anywhere ends the session

// ── REST endpoints ────────────────────────────────────────────────
export const EP = {
  AUTH_STATUS:      "/auth/status",
  AUTH_LOGIN:       "/auth/login",
  AUTH_SETUP:       "/auth/setup",
  AUTH_LOGOUT:      "/auth/logout",
  STATUS:           "/status",
  PORTS:            "/ports",
  CONNECT:          "/connect",
  DISCONNECT:       "/disconnect",
  RECORDING_START:  "/recording/start",
  RECORDING_STOP:   "/recording/stop",
  PROFILE_START:    "/profile/start",
  PROFILE_STOP:     "/profile/stop",
  PROFILE_STATUS:   "/profile/status",
  CALIBRATION:      "/calibration",
  CALIBRATION_RESET:"/calibration/reset",
  ANALYSIS_RUN:     "/analysis/run",
  ANALYSIS_EXPORT:  "/analysis/export-excel",
};

export const METHOD_GET  = "GET";
export const METHOD_POST = "POST";
export const HTTP_UNAUTHORIZED = 401;
export const WS_UNAUTHORIZED   = 4401; // Close code the backend uses for "no valid session"

// ── WebSocket message types (backend → browser) ───────────────────
export const MSG = {
  HISTORY: "history",
  ACK:     "ack",
  PROFILE: "profile",
};

// ── Command actions (browser → backend) ───────────────────────────
export const ACTION = {
  TARE:     "tare",
  THROTTLE: "throttle",
};

// ── Firmware event names, forwarded verbatim by the backend ───────
export const FW_EVENT = {
  TARE_DONE:    "TARE_DONE",
  THROTTLE_SET: "THROTTLE_SET",
  CAL:          "CAL",
  CAL_DEFAULTS: "CAL_DEFAULTS",
  CAL_SET:      "CAL_SET",
  CAL_RESET:    "CAL_RESET",
};

// ── UI state enums ────────────────────────────────────────────────
// SENDING is set locally the moment the command leaves the browser;
// SENT / ERROR come from the backend's ack, DONE from the firmware.
export const TARE_STATUS = {
  SENDING: "sending",
  SENT:    "sent",
  DONE:    "done",
  ERROR:   "error",
};

export const CAL_STATUS = {
  IDLE:   "idle",
  LOADING:"loading",
  SAVING: "saving",
  SAVED:  "saved",
  ERROR:  "error",
};

// ── Settings modal tabs ───────────────────────────────────────────
export const SETTINGS_TAB = {
  CALIBRATION: "calibration",
  UPDATES:     "updates",
};

// ── Updates ───────────────────────────────────────────────────────
// Mirrors the `state` field of the shell's updates_check command. The
// last three are deliberately distinct: "none" means the release
// channel was read and we are current, while "unreachable" means it
// could not be read at all — a private repository between releases, or
// no network. Showing "No updates available" for both would make a
// closed release window look like a working one.
export const UPDATE_STATE = {
  AVAILABLE:   "available",
  NONE:        "none",
  UNREACHABLE: "unreachable",
  UNSUPPORTED: "unsupported",   // Running in a browser, not the desktop app
};

// Emitted by the Rust shell while an update downloads.
export const EVT_UPDATE_PROGRESS = "update://progress";

export const MSG_NO_UPDATES = "No Updates Available.";

export const THEME = { DARK: "dark", LIGHT: "light" };
export const THEME_ATTR = "data-theme";

export const UNIT_SI  = "si";
export const UNIT_KGF = "kgf";

export const TAB = { LIVE: "live", ANALYSIS: "analysis" };

export const DIR_CW  = "CW";
export const DIR_CCW = "CCW";

export const CHANNEL_TORQUE = "torque";
export const CHANNEL_THRUST = "thrust";

// Profile phases reported by the backend sequencer
export const PHASE = {
  IDLE:         "idle",
  RAMPING:      "ramping",
  HOLDING:      "holding",
  RAMPING_DOWN: "ramping_down",
  DONE:         "done",
  ABORTED:      "aborted",
  ERROR:        "error",
};

// ── ESC throttle range, in microseconds ───────────────────────────
// Hand-duplicated in backend/main.py and esp32/main.c; the firmware
// clamps independently, so a mismatch shows up as the dashboard
// offering targets the firmware quietly refuses.
export const THR_MIN = 1000;
export const THR_MAX = 2000;

// ── Beam cell keys, in physical order ─────────────────────────────
export const CELL_KEYS = ["s1", "s2", "s3", "s4"];

// ── Placeholder shown wherever a reading is missing ───────────────
export const NO_VALUE = "—";

// ── Operator-facing messages reused in more than one place ────────
export const MSG_BACKEND_UNREACHABLE = "Could not reach backend";
export const MSG_SESSION_EXPIRED     = "Session expired — sign in again";
export const MSG_SIGN_IN_FAILED      = "Sign-in failed";
