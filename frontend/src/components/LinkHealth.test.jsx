import React from "react";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import LinkHealth from "./LinkHealth";

/** 10 s of 20 Hz frames, the newest `lagMs` ago. */
function liveHistory(lagMs = 30) {
  const last = (Date.now() - lagMs) / 1000;
  return Array.from({ length: 200 }, (_, i) => ({ ts: last - (199 - i) / 20, seq: i }));
}

function health(overrides = {}) {
  return {
    frames_total: 125_438, last_frame_at: Date.now() / 1000 + 10, // nothing newer than this in history
    malformed_total: 0, stalled: false, verdict: "ok", expected_rate_hz: 20,
    dropped_total: 0, seq_supported: true, device_resets: 0,
    command_link: { state: "ok", rtt_avg_ms: 12, last_pong_age_s: 0.2 },
    device: { up_ms: 3_861_000, boot_reason: "POWERON" },
    port: { name: "COM7", baud: 115200, description: "Silicon Labs CP210x" },
    timeline: [], issues: [], events: [],
    received_at: Date.now(),
    ...overrides,
  };
}

function renderCard({ lagMs = 30, h = health(), serialConnected = true, wsConnected = true } = {}) {
  const history = liveHistory(lagMs);
  return render(
    <LinkHealth health={h} serialConnected={serialConnected} serialPort="COM7"
                wsConnected={wsConnected} latest={history.at(-1)} history={history} />
  );
}

async function openCard() {
  await userEvent.click(screen.getByRole("button", { name: /esp32 communication status/i }));
}

afterEach(() => { vi.useRealTimers(); });

describe("ESP32 communication card", () => {
  it("is closed by default — just the square", () => {
    renderCard();
    expect(screen.getByRole("button", { name: /esp32 communication status/i })).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens to the status and the link's numbers", async () => {
    renderCard();
    await openCard();

    expect(screen.getByText("ESP32 COMMUNICATION")).toBeTruthy();
    expect(screen.getByText("CONNECTED")).toBeTruthy();
    expect(screen.getByText("COM7")).toBeTruthy();
    expect(screen.getByText("115200")).toBeTruthy();
    expect(screen.getByText(/^\d+ ms ago$/)).toBeTruthy();
    expect(screen.getByText("125,438")).toBeTruthy();
    expect(screen.getByText(/^1h 04m 2\ds$/)).toBeTruthy();
    expect(screen.getByText("Connection Quality")).toBeTruthy();
  });

  it("goes yellow between 250 ms and 1 s without a packet", async () => {
    renderCard({ lagMs: 600 });
    await openCard();
    expect(screen.getByText("WARNING")).toBeTruthy();
  });

  it("goes red beyond 1 s, even though the COM port is still open", async () => {
    renderCard({ lagMs: 3000 });
    await openCard();
    expect(screen.getByText("DISCONNECTED")).toBeTruthy();
    expect(screen.getByText(/No packet for 3\.\d s/)).toBeTruthy();
  });

  it("turns yellow on its own when frames stop arriving", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderCard({ lagMs: 30 });
    await openCard();
    expect(screen.getByText("CONNECTED")).toBeTruthy();

    // No new frames, no new props — only the card's own tick runs
    act(() => { vi.advanceTimersByTime(400); });
    expect(screen.getByText("WARNING")).toBeTruthy();
  });

  it("is red with no COM port open", async () => {
    renderCard({ serialConnected: false });
    await openCard();
    expect(screen.getByText("DISCONNECTED")).toBeTruthy();
    expect(screen.getByText("No COM port open")).toBeTruthy();
  });

  it("surfaces a dead command wire even while packets look fine", async () => {
    renderCard({
      h: health({
        command_link: { state: "lost" },
        issues: [{ level: "error",
                   text: "Telemetry is arriving, but the ESP32 isn't answering — tare, throttle and CUT may not reach it",
                   hint: "Check the adapter TX → ESP32 RX (GPIO44) wire." }],
      }),
    });

    // Flagged on the closed square too
    expect(screen.getByRole("button", { name: /esp32 communication status/i })
      .getAttribute("title")).toMatch(/CUT/);

    await openCard();
    expect(screen.getByText("CONNECTED")).toBeTruthy();
    expect(screen.getByText("No reply")).toBeTruthy();
    expect(screen.getByText(/GPIO44/)).toBeTruthy();
  });

  it("keeps the deeper diagnostics folded under Details", async () => {
    renderCard({ h: health({ events: [{ ts: Date.now() / 1000, level: "ok", text: "Command path confirmed" }] }) });
    await openCard();
    expect(screen.queryByText("Command path confirmed")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: /details/i }));
    expect(screen.getByText("Command path confirmed")).toBeTruthy();
  });

  it("closes on Escape", async () => {
    renderCard();
    await openCard();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
