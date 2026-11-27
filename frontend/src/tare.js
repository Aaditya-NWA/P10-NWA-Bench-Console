import { TARE_ZERO_LIMIT_KG } from "./constants";

// Firmware keys -> display names. "current" is the DC sensor; the firmware
// names the AC sensors current2..current4 (after their telemetry fields
// current_a2..a4), which the dashboard shows as AC Current 1..3.
const CELL_LABEL = {
  s1: "S1", s2: "S2", s3: "S3", s4: "S4", thrust: "Thrust",
  current: "DC Current",
  current2: "AC Current 1", current3: "AC Current 2", current4: "AC Current 3",
};

/**
 * Reduce a firmware TARE_DONE line to what the Tare button shows.
 *
 * Current firmware reports, per cell, how many samples went into the
 * zero and their spread. A cell with too few samples kept its OLD zero
 * ("failed"); a cell whose zero is uncertain by more than the display
 * dead band was probably being touched or vibrating ("noisy"). Older
 * firmware sends a bare {"event":"TARE_DONE"} — nothing to judge, so
 * it is simply reported as done.
 */
export function summarizeTare(msg) {
  const cells = msg?.cells;
  if (!cells || typeof cells !== "object") {
    return { ok: true, legacy: true, ms: null, failed: [], noisy: [], unsettled: [], detail: "" };
  }

  const label = (k) => CELL_LABEL[k] ?? k;
  const failed = (Array.isArray(msg.failed) ? msg.failed : []).map(label);
  // Current sensors whose output was still moving during the tare
  // (firmware compares the two halves of its 3 s window). Their zero was
  // applied, from the later half, but will likely need another tare.
  // Absent from firmware before 2026-11.
  const unsettled = (Array.isArray(msg.unsettled) ? msg.unsettled : []).map(label);
  const noisy = [];
  const parts = [];
  for (const [key, c] of Object.entries(cells)) {
    const name = label(key);
    if (!c || !(c.n > 0)) continue;
    const zeroUncertainty = (c.sd_kg ?? 0) / Math.sqrt(c.n);
    const limit = key === "thrust" ? TARE_ZERO_LIMIT_KG.thrust : TARE_ZERO_LIMIT_KG.beam;
    if (zeroUncertainty > limit) noisy.push(name);
    parts.push(`${name}: ${c.n} samples, σ ${(c.sd_kg * 1000).toFixed(2)} g`);
  }

  const currents = msg.currents && typeof msg.currents === "object" ? msg.currents : {};
  for (const [key, c] of Object.entries(currents)) {
    if (!c || !(c.n > 0)) continue;
    const drift = Number(c.drift_a ?? 0);
    parts.push(
      `${label(key)}: ${c.n} samples, σ ${Number(c.sd_a ?? 0).toFixed(2)} A, ` +
      `drift ${drift >= 0 ? "+" : ""}${drift.toFixed(2)} A`,
    );
  }

  return {
    ok: msg.ok !== false && failed.length === 0,
    legacy: false,
    ms: typeof msg.ms === "number" ? msg.ms : null,
    failed,
    noisy,
    unsettled,
    detail: parts.join("\n"),
  };
}
