// Regression tests for two bugs that actually happened on this bench,
// plus the firmware-build detection. Both were invisible in review and
// showed up only as vague symptoms — exactly what comes back during a
// refactor.

import { renderHook, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSerialData } from "./useSerialData";
import { setAuthToken } from "../api";

class MockWebSocket {
  static instances = [];
  static OPEN = 1;
  static CLOSED = 3;

  constructor(url) {
    this.url = url;
    this.readyState = MockWebSocket.OPEN;
    this.sent = [];
    this.closed = false;
    MockWebSocket.instances.push(this);
  }

  send(data) { this.sent.push(data); }
  close() { this.closed = true; this.readyState = MockWebSocket.CLOSED; }

  // Test-side helpers
  open() { this.onopen?.(); }
  receive(payload) { this.onmessage?.({ data: JSON.stringify(payload) }); }
  receiveRaw(text) { this.onmessage?.({ data: text }); }

  static get latest() { return MockWebSocket.instances.at(-1); }
  static reset() { MockWebSocket.instances = []; }
}

/** Render the hook and let its first /status poll settle — otherwise
 *  that promise resolves after the test body starts and React reports
 *  an update outside act(). */
async function renderSerialHook(options = { enabled: true }) {
  const rendered = renderHook(() => useSerialData(options));
  await act(async () => {});
  return rendered;
}

function telemetryFrame(overrides = {}) {
  return {
    torque: 1.2, thrust: 45.6, s1: 1.0, s2: -0.4, s3: 0.9, s4: 0.03,
    stable: true, dir: "CW", rpm: 12450, rpm_raw: 12470,
    current_a: 1.85, voltage_v: 22.1, current_a2: 0, voltage_v2: 0,
    throttle_us: 1300, ts: Date.now() / 1000,
    ...overrides,
  };
}

beforeEach(() => {
  MockWebSocket.reset();
  vi.stubGlobal("WebSocket", MockWebSocket);
  // /status polling is not what these tests are about; keep it quiet.
  vi.stubGlobal("fetch", vi.fn(() =>
    Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) })));
  // Through the setter, not localStorage directly: api.js reads the
  // stored token once at module load, which has already happened by
  // the time this runs.
  setAuthToken("test-token");
});

afterEach(() => {
  vi.unstubAllGlobals();
  setAuthToken(null);
  localStorage.clear();
});


// REGRESSION #32 — the WebSocket must survive a recording toggle
describe("WebSocket stability across state changes", () => {
  it("does not tear down the socket when a recording starts or stops", async () => {
    // BUG: `recording` used to be a dependency of connect(), so the
    // mount effect's cleanup rebuilt the socket on every start/stop —
    // dropping frames at exactly the wrong moment.
    const { result } = await renderSerialHook();
    act(() => MockWebSocket.latest.open());

    const socketBefore = MockWebSocket.latest;
    expect(MockWebSocket.instances).toHaveLength(1);

    await act(async () => { await result.current.startRecording("Run 1"); });
    await act(async () => { await result.current.stopRecording(); });
    await act(async () => { await result.current.startRecording("Run 2"); });

    expect(MockWebSocket.instances).toHaveLength(1);
    expect(MockWebSocket.latest).toBe(socketBefore);
    expect(socketBefore.closed).toBe(false);
  });

  it("keeps counting frames after a recording toggle", async () => {
    const { result } = await renderSerialHook();
    act(() => MockWebSocket.latest.open());
    const socket = MockWebSocket.latest;

    await act(async () => { await result.current.startRecording("Run 1"); });
    act(() => socket.receive(telemetryFrame({ torque: 9.99 })));

    // The frame landed on the SAME socket that was open before.
    expect(result.current.latest.torque).toBe(9.99);
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it("opens no socket at all until the operator is signed in", async () => {
    // Hooks run before the caller's auth early-return, so without the
    // `enabled` gate this would connect (and 401) on the login screen.
    await renderSerialHook({ enabled: false });
    expect(MockWebSocket.instances).toHaveLength(0);
  });

  it("carries the session token on the WebSocket URL", async () => {
    await renderSerialHook();
    expect(MockWebSocket.latest.url).toContain("token=test-token");
  });
});


// REGRESSION #33 — tare status must ignore throttle acks
describe("acknowledgement routing", () => {
  it("ignores throttle acks when tracking tare status", async () => {
    // BUG: this fired for ANY ack. Throttle acks arrive every ~80 ms
    // while dragging, so each overwrote tareStatus and the Tare button
    // flickered through "Command Sent" during an unrelated drag.
    const { result } = await renderSerialHook();
    act(() => MockWebSocket.latest.open());
    const socket = MockWebSocket.latest;

    act(() => {
      for (let i = 0; i < 20; i++) {
        socket.receive({ type: "ack", action: "throttle", status: "sent", us: 1100 + i });
      }
    });

    expect(result.current.tareStatus).toBeNull();
  });

  it("still reacts to a genuine tare ack", async () => {
    const { result } = await renderSerialHook();
    act(() => MockWebSocket.latest.open());

    act(() => MockWebSocket.latest.receive({ type: "ack", action: "tare", status: "sent" }));
    expect(result.current.tareStatus).toBe("sent");
  });

  it("shows the firmware's TARE_DONE event", async () => {
    const { result } = await renderSerialHook();
    act(() => MockWebSocket.latest.open());

    act(() => MockWebSocket.latest.receive({ event: "TARE_DONE" }));
    expect(result.current.tareStatus).toBe("done");
  });

  it("does not let a throttle ack clear an in-flight tare status", async () => {
    const { result } = await renderSerialHook();
    act(() => MockWebSocket.latest.open());
    const socket = MockWebSocket.latest;

    act(() => socket.receive({ type: "ack", action: "tare", status: "sent" }));
    act(() => socket.receive({ type: "ack", action: "throttle", status: "sent", us: 1200 }));

    expect(result.current.tareStatus).toBe("sent");
  });
});


// #34 — old-firmware detection from the absence of `stable`
describe("firmware build detection", () => {
  it("treats a frame without `stable` as the RPM-test-pass build", async () => {
    // Detection is by ABSENCE of the field, not a version number —
    // so a `stable`-less frame is an old build, not a parsing bug.
    const { result } = await renderSerialHook();
    act(() => MockWebSocket.latest.open());

    const { stable, ...withoutStable } = telemetryFrame();
    act(() => MockWebSocket.latest.receive(withoutStable));

    expect(result.current.latest).toBeTruthy();
    expect(result.current.latest.stable).toBeUndefined();
    // App.jsx derives commandsMayBeIgnored from exactly this condition.
    expect(!!result.current.latest && result.current.latest.stable === undefined).toBe(true);
  });

  it("does not flag a current build that reports stable:false", async () => {
    const { result } = await renderSerialHook();
    act(() => MockWebSocket.latest.open());

    act(() => MockWebSocket.latest.receive(telemetryFrame({ stable: false })));

    expect(result.current.latest.stable).toBe(false);
    expect(result.current.latest.stable === undefined).toBe(false);
  });
});


describe("telemetry handling", () => {
  it("hydrates history and seeds the confirmed throttle on connect", async () => {
    const { result } = await renderSerialHook();
    act(() => MockWebSocket.latest.open());

    act(() => MockWebSocket.latest.receive({
      type: "history",
      data: [telemetryFrame({ throttle_us: 1150 }), telemetryFrame({ throttle_us: 1275 })],
    }));

    expect(result.current.history).toHaveLength(2);
    expect(result.current.confirmedThrottleUs).toBe(1275);
  });

  it("caps history at 600 frames to match the backend's window", async () => {
    const { result } = await renderSerialHook();
    act(() => MockWebSocket.latest.open());

    act(() => {
      const many = Array.from({ length: 700 }, (_, i) => telemetryFrame({ torque: i }));
      MockWebSocket.latest.receive({ type: "history", data: many });
    });
    act(() => MockWebSocket.latest.receive(telemetryFrame({ torque: 999 })));

    expect(result.current.history.length).toBeLessThanOrEqual(600);
    expect(result.current.history.at(-1).torque).toBe(999);
  });

  it("applies THROTTLE_SET immediately, without waiting for a telemetry frame", async () => {
    const { result } = await renderSerialHook();
    act(() => MockWebSocket.latest.open());

    act(() => MockWebSocket.latest.receive({ event: "THROTTLE_SET", us: 1425 }));
    expect(result.current.confirmedThrottleUs).toBe(1425);
  });

  it("survives an unparseable message without losing the connection", async () => {
    const { result } = await renderSerialHook();
    act(() => MockWebSocket.latest.open());

    act(() => MockWebSocket.latest.receiveRaw("<<not json>>"));
    act(() => MockWebSocket.latest.receive(telemetryFrame({ torque: 3.5 })));

    expect(result.current.latest.torque).toBe(3.5);
  });

  it("keeps profile pushes out of the telemetry history", async () => {
    const { result } = await renderSerialHook();
    act(() => MockWebSocket.latest.open());

    act(() => MockWebSocket.latest.receive({
      type: "profile", profile: { running: true, phase: "ramping", target_us: 1500 },
    }));

    expect(result.current.profile.phase).toBe("ramping");
    expect(result.current.history).toHaveLength(0);
  });
});
