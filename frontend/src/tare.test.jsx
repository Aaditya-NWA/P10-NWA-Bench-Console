import { describe, expect, it } from "vitest";

import { summarizeTare } from "./tare";

const quiet = { n: 20, sd_kg: 0.0004 };

describe("summarizeTare", () => {
  it("treats a bare TARE_DONE from older firmware as plain success", () => {
    const r = summarizeTare({ event: "TARE_DONE" });
    expect(r.ok).toBe(true);
    expect(r.legacy).toBe(true);
    expect(r.failed).toEqual([]);
  });

  it("reports a clean tare with its duration", () => {
    const r = summarizeTare({
      event: "TARE_DONE", ok: true, ms: 2013, samples: 20, failed: [],
      cells: { s1: quiet, s2: quiet, s3: quiet, s4: quiet, thrust: { n: 20, sd_kg: 0.005 } },
    });
    expect(r.ok).toBe(true);
    expect(r.ms).toBe(2013);
    expect(r.noisy).toEqual([]);
  });

  it("names the cells that kept their old zero", () => {
    // The old blocking tare silently set a dead cell's offset to 0 — a
    // large, believable reading. Now the cell is named instead.
    const r = summarizeTare({
      event: "TARE_DONE", ok: false, ms: 4001, failed: ["s4"],
      cells: { s1: quiet, s2: quiet, s3: quiet, s4: { n: 0, sd_kg: 0 }, thrust: quiet },
    });
    expect(r.ok).toBe(false);
    expect(r.failed).toEqual(["S4"]);
  });

  it("flags a cell whose zero is less certain than the display dead band", () => {
    // sd 0.05 kg over 20 samples → ±0.011 kg on the zero, past the
    // 0.005 kg beam dead band: someone was leaning on the rig.
    const r = summarizeTare({
      event: "TARE_DONE", ok: true, ms: 2000, failed: [],
      cells: { s1: quiet, s2: { n: 20, sd_kg: 0.05 }, s3: quiet, s4: quiet, thrust: quiet },
    });
    expect(r.ok).toBe(true);
    expect(r.noisy).toEqual(["S2"]);
  });

  it("judges the thrust cell against its own, wider dead band", () => {
    const r = summarizeTare({
      event: "TARE_DONE", ok: true, ms: 2000, failed: [],
      cells: { s1: quiet, s2: quiet, s3: quiet, s4: quiet, thrust: { n: 20, sd_kg: 0.05 } },
    });
    expect(r.noisy).toEqual([]);
  });
});
