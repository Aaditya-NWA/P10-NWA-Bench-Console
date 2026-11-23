import React from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// The Live Bench page with a signed-in operator and one telemetry frame.
// Heavy children (chart, link-health card) are stubbed: this pins which
// power channel lands under which label, nothing else.
const FRAME = {
  torque: 1.2, thrust: 40, s1: 0.1, s2: 0.1, s3: 0.1, s4: 0.1,
  stable: true, dir: "CW", rpm: 9000, rpm_raw: 9000, throttle_us: 1500,
  current_a: 25.5, voltage_v: 24.0,
  current_a2: 11.11, current_a3: 22.22, current_a4: 33.33, voltage_v2: 44.44,
};

vi.mock("./hooks/useAuth", () => ({
  useAuth: () => ({ checking: false, authenticated: true, username: "tester", logout: vi.fn() }),
}));
vi.mock("./hooks/useSerialData", () => ({
  useSerialData: () => ({
    connected: true, serialConnected: true, latest: FRAME, history: [FRAME],
    ports: [], health: null, profile: null, calibration: null,
    sendThrottle: vi.fn(), sendTare: vi.fn(), refreshPorts: vi.fn(),
  }),
}));
vi.mock("./components/LiveChart", () => ({ default: () => null }));
vi.mock("./components/StatusBar", () => ({ default: () => null }));

import App from "./App";

function cardValue(label) {
  // MetricCard: <span label/> then a row whose first span is the value
  return screen.getByText(label).nextSibling.firstChild.textContent;
}

describe("Live Bench power readouts", () => {
  it("labels the HAS-600S/LV25-P pair as DC", () => {
    render(<App />);
    expect(cardValue("DC CURRENT")).toBe("25.50");
    expect(cardValue("DC VOLTAGE")).toBe("24.00");
    expect(cardValue("DC POWER")).toBe("612.0");
  });

  it("maps current_a2..a4 to AC CURRENT 1..3 and voltage_v2 to AC VOLTAGE", () => {
    render(<App />);
    expect(cardValue("AC CURRENT 1")).toBe("11.11");
    expect(cardValue("AC CURRENT 2")).toBe("22.22");
    expect(cardValue("AC CURRENT 3")).toBe("33.33");
    expect(cardValue("AC VOLTAGE")).toBe("44.44");
  });

  it("no longer shows the old unqualified labels", () => {
    render(<App />);
    for (const old of ["CURRENT", "VOLTAGE", "POWER", "CURRENT 2", "VOLTAGE 2"]) {
      expect(screen.queryByText(old)).toBeNull();
    }
  });
});
