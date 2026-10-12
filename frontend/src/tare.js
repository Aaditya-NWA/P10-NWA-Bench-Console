import { TARE_ZERO_LIMIT_KG } from "./constants";

const CELL_LABEL = { s1: "S1", s2: "S2", s3: "S3", s4: "S4", thrust: "Thrust", current: "Current" };

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
    return { ok: true, legacy: true, ms: null, failed: [], noisy: [], detail: "" };
  }

  const failed = (Array.isArray(msg.failed) ? msg.failed : []).map((k) => CELL_LABEL[k] ?? k);
  const noisy = [];
  const parts = [];
  for (const [key, c] of Object.entries(cells)) {
    const label = CELL_LABEL[key] ?? key;
    if (!c || !(c.n > 0)) continue;
    const zeroUncertainty = (c.sd_kg ?? 0) / Math.sqrt(c.n);
    const limit = key === "thrust" ? TARE_ZERO_LIMIT_KG.thrust : TARE_ZERO_LIMIT_KG.beam;
    if (zeroUncertainty > limit) noisy.push(label);
    parts.push(`${label}: ${c.n} samples, σ ${(c.sd_kg * 1000).toFixed(2)} g`);
  }

  return {
    ok: msg.ok !== false && failed.length === 0,
    legacy: false,
    ms: typeof msg.ms === "number" ? msg.ms : null,
    failed,
    noisy,
    detail: parts.join("\n"),
  };
}
