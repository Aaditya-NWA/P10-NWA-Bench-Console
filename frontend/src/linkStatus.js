import { COMM_LOST_MS, COMM_STATUS, COMM_WARN_MS } from "./constants";

// Everything the ESP32 Communication card shows that has to be LIVE.
// /status is only polled every 2 s, which is far too coarse to judge a
// 250 ms threshold or show "48 ms ago", so these are computed from the
// telemetry frames the WebSocket delivers, each stamped `ts` (seconds)
// by the backend the moment it read the line off the serial port.

const RATE_WINDOW_S    = 3;     // Short enough to react, long enough not to flicker
const QUALITY_WINDOW_S = 10;

/** Green / yellow / red from how long ago the newest valid packet arrived. */
export function commStatus(ageMs) {
  if (ageMs === null || ageMs === undefined || ageMs > COMM_LOST_MS) return COMM_STATUS.DISCONNECTED;
  if (ageMs >= COMM_WARN_MS) return COMM_STATUS.WARNING;
  return COMM_STATUS.CONNECTED;
}

/** Milliseconds since the newest frame, or null if there isn't one. */
export function lastPacketAgeMs(latest, nowMs) {
  const ts = latest?.ts;
  if (typeof ts !== "number") return null;
  return Math.max(0, nowMs - ts * 1000);
}

/** Index of the first frame at or after `sinceSec`, scanning from the end. */
function firstIndexSince(history, sinceSec) {
  let i = history.length;
  while (i > 0 && (history[i - 1]?.ts ?? -Infinity) >= sinceSec) i--;
  return i;
}

/**
 * Packets per second over the last few seconds, measured against NOW —
 * so the rate falls away when frames stop, rather than freezing at the
 * last good value the way a rate between the newest frames would.
 */
export function liveRateHz(history, nowMs) {
  if (!history?.length) return 0;
  const now = nowMs / 1000;
  const start = firstIndexSince(history, now - RATE_WINDOW_S);
  const n = history.length - start;
  if (n < 2) return 0;
  const first = history[start].ts;
  // Until the window has filled (just connected), divide by the span
  // actually covered rather than the full window.
  const span = Math.min(RATE_WINDOW_S, now - first);
  return span > 0 ? n / span : 0;
}

/**
 * Connection quality, 0–100: of the packets the ESP32 should have sent
 * in the last 10 s, the share that arrived intact. One number that
 * drops for a slow loop, for lost or corrupted lines and for a stall
 * alike — which is what an operator glancing at a bar wants to know.
 */
export function connectionQuality(history, nowMs, expectedHz) {
  if (!history?.length || !expectedHz) return 0;
  const now = nowMs / 1000;
  const start = firstIndexSince(history, now - QUALITY_WINDOW_S);
  const n = history.length - start;
  if (n === 0) return 0;
  const window = Math.min(QUALITY_WINDOW_S, now - history[start].ts);
  if (window < 1) return n > 0 ? 100 : 0;       // Too early to judge — don't flash red on connect
  return Math.max(0, Math.min(100, Math.round((100 * n) / (window * expectedHz))));
}

/**
 * Packets received since the port opened, kept live between polls: the
 * backend's count as of its last snapshot, plus every frame since then.
 */
export function packetsReceived(health, history) {
  const base = health?.frames_total;
  if (typeof base !== "number") return null;
  const since = health.last_frame_at;
  if (typeof since !== "number" || !history?.length) return base;
  let extra = 0;
  for (let i = history.length - 1; i >= 0 && history[i].ts > since; i--) extra++;
  return base + extra;
}

/** ESP32 uptime now, advanced locally from the last PONG the backend saw. */
export function esp32UptimeMs(health, nowMs) {
  const up = health?.device?.up_ms;
  if (typeof up !== "number") return null;
  const pongAge = (health.command_link?.last_pong_age_s ?? 0) * 1000;
  const sincePoll = health.received_at ? Math.max(0, nowMs - health.received_at) : 0;
  return up + pongAge + sincePoll;
}

/** 0–4 lit signal bars for the collapsed button. */
export function signalBars(quality, status) {
  if (status === COMM_STATUS.DISCONNECTED) return 0;
  if (quality >= 90) return 4;
  if (quality >= 70) return 3;
  if (quality >= 40) return 2;
  return 1;
}
