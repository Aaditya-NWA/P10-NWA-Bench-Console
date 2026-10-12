import { describe, expect, it } from "vitest";

import {
  commStatus, connectionQuality, esp32UptimeMs, lastPacketAgeMs,
  liveRateHz, packetsReceived, signalBars,
} from "./linkStatus";

const NOW = 1_800_000_000_000;                          // ms

/** Frames at `hz` for `seconds`, the newest `lagMs` before NOW. */
function frames(hz, seconds, lagMs = 0) {
  const n = Math.round(hz * seconds);
  const last = (NOW - lagMs) / 1000;
  return Array.from({ length: n }, (_, i) => ({ ts: last - (n - 1 - i) / hz, seq: i }));
}

describe("commStatus — the agreed thresholds", () => {
  it.each([
    [0,    "connected"],
    [249,  "connected"],
    [250,  "warning"],
    [1000, "warning"],
    [1001, "disconnected"],
    [null, "disconnected"],
  ])("%s ms since the last packet is %s", (age, expected) => {
    expect(commStatus(age)).toBe(expected);
  });
});

describe("live values", () => {
  it("measures packet age from the backend's receive stamp", () => {
    expect(lastPacketAgeMs({ ts: (NOW - 48) / 1000 }, NOW)).toBeCloseTo(48, 0);
    expect(lastPacketAgeMs(null, NOW)).toBeNull();
  });

  it("reads 20 Hz from a healthy stream", () => {
    expect(liveRateHz(frames(20, 10), NOW)).toBeCloseTo(20, 0);
  });

  it("lets the rate fall away once frames stop, instead of freezing", () => {
    // 10 s of data, then 2 s of silence
    expect(liveRateHz(frames(20, 10, 2000), NOW)).toBeLessThan(8);
    expect(liveRateHz(frames(20, 10, 5000), NOW)).toBe(0);
  });
});

describe("connectionQuality", () => {
  it("is 100% when every expected packet arrived", () => {
    expect(connectionQuality(frames(20, 12), NOW, 20)).toBeGreaterThanOrEqual(98);
  });

  it("drops in proportion to missing packets", () => {
    const h = frames(20, 12).filter((_, i) => i % 4 !== 0);   // a quarter lost
    const q = connectionQuality(h, NOW, 20);
    expect(q).toBeGreaterThan(70);
    expect(q).toBeLessThan(80);
  });

  it("falls when the stream stalls, not just when packets are lost", () => {
    expect(connectionQuality(frames(20, 12, 5000), NOW, 20)).toBeLessThan(55);
  });

  it("doesn't flash red in the first second after connecting", () => {
    expect(connectionQuality(frames(20, 0.5), NOW, 20)).toBe(100);
  });
});

describe("packetsReceived", () => {
  it("adds the frames that arrived after the backend's last snapshot", () => {
    const h = frames(20, 3);
    const snapshotAt = h[h.length - 11].ts;                     // 10 frames since
    expect(packetsReceived({ frames_total: 5000, last_frame_at: snapshotAt }, h)).toBe(5010);
  });

  it("is null when the backend hasn't reported yet", () => {
    expect(packetsReceived(null, frames(20, 1))).toBeNull();
  });
});

describe("esp32UptimeMs", () => {
  it("advances from the last PONG instead of jumping every poll", () => {
    const health = {
      device: { up_ms: 60_000 },
      command_link: { last_pong_age_s: 0.5 },
      received_at: NOW - 1500,
    };
    expect(esp32UptimeMs(health, NOW)).toBe(62_000);
  });

  it("is unknown for firmware that doesn't report it", () => {
    expect(esp32UptimeMs({ device: {} }, NOW)).toBeNull();
  });
});

describe("signalBars", () => {
  it("lights none when disconnected, whatever the history says", () => {
    expect(signalBars(100, "disconnected")).toBe(0);
  });
  it("scales with quality", () => {
    expect(signalBars(98, "connected")).toBe(4);
    expect(signalBars(75, "connected")).toBe(3);
    expect(signalBars(50, "warning")).toBe(2);
    expect(signalBars(10, "warning")).toBe(1);
  });
});
