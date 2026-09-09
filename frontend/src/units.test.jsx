// Unit conversion + beam-cell fault detection. Both are pure functions
// behind readouts an operator makes decisions from.

import { describe, expect, it } from "vitest";

import { convert, G, UNIT_SYSTEMS } from "./units";
import { detectCellFaults } from "./components/CellGrid";

describe("unit conversion", () => {
  it("leaves SI values untouched", () => {
    expect(convert("si", "thrust", 249.5).value).toBe(249.5);
    expect(convert("si", "torque", 3.3325).value).toBe(3.3325);
    expect(convert("si", "thrust", 0).unit).toBe("N");
    expect(convert("si", "torque", 0).unit).toBe("N·m");
  });

  it("converts newtons to kilogram-force", () => {
    // 1 kgf = 9.80665 N by definition
    expect(convert("kgf", "thrust", G).value).toBeCloseTo(1.0, 9);
    expect(convert("kgf", "thrust", 249.5).value).toBeCloseTo(249.5 / G, 9);
    expect(convert("kgf", "thrust", 0).unit).toBe("kgf");
  });

  it("converts newton-metres to kgf·cm", () => {
    // 1 N·m = 100/9.80665 kgf·cm ≈ 10.1972
    expect(convert("kgf", "torque", 1).value).toBeCloseTo(10.1972, 3);
    expect(convert("kgf", "torque", 0).unit).toBe("kgf·cm");
  });

  it("round-trips back to the original value", () => {
    const original = 187.34;
    const asKgf = convert("kgf", "thrust", original).value;
    expect(asKgf * G).toBeCloseTo(original, 9);
  });

  it("preserves the sign of a CCW torque", () => {
    expect(convert("kgf", "torque", -2.5).value).toBeLessThan(0);
  });

  it("passes null through rather than converting it to zero", () => {
    // A missing reading must stay "—", not become a real-looking 0.
    expect(convert("kgf", "thrust", null).value).toBeNull();
    expect(convert("si", "torque", undefined).value).toBeUndefined();
  });

  it("falls back to SI for an unknown system", () => {
    expect(convert("furlongs", "thrust", 100).value).toBe(100);
  });

  it("gives kgf fewer decimals than SI, since the numbers are larger", () => {
    expect(UNIT_SYSTEMS.kgf.torque.decimals)
      .toBeLessThan(UNIT_SYSTEMS.si.torque.decimals);
  });
});


describe("beam cell fault detection", () => {
  it("flags nothing when all four cells agree", () => {
    expect(detectCellFaults({ s1: 1.0, s2: 1.05, s3: 0.98, s4: 1.02 })).toEqual({});
  });

  it("flags nothing at idle, where everything is near zero noise", () => {
    // The absolute floor exists so sensor noise at rest doesn't trip it.
    expect(detectCellFaults({ s1: 0.004, s2: -0.003, s3: 0.006, s4: -0.002 })).toEqual({});
  });

  it("flags the single cell that disagrees with its three neighbours", () => {
    const faults = detectCellFaults({ s1: 1.0, s2: 1.05, s3: 0.98, s4: 14.5 });

    expect(Object.keys(faults)).toEqual(["s4"]);
    expect(faults.s4).toBeGreaterThan(10);
  });

  it("flags a cell reading zero while the others are loaded", () => {
    // The signature of a disconnected HX711 under real load.
    const faults = detectCellFaults({ s1: 8.0, s2: 8.2, s3: 7.9, s4: 0.0 });
    expect(Object.keys(faults)).toEqual(["s4"]);
  });

  it("does not flag anything under a genuine asymmetric load", () => {
    // Torque legitimately loads S3/S4 in opposition — if every cell
    // reads as an outlier, the test has told us nothing useful.
    expect(detectCellFaults({ s1: 5.0, s2: -5.0, s3: 5.0, s4: -5.0 })).toEqual({});
  });

  it("returns nothing when there is no data", () => {
    expect(detectCellFaults(null)).toEqual({});
    expect(detectCellFaults({})).toEqual({});
  });

  it("ignores non-numeric and missing cells rather than flagging them", () => {
    expect(detectCellFaults({ s1: 1.0, s2: 1.05, s3: null })).toEqual({});
    expect(detectCellFaults({ s1: 1.0, s2: NaN, s3: 1.02, s4: 0.99 })).toEqual({});
  });

  it("scales with the load rather than using a fixed limit", () => {
    // At full throttle the cells read in the tens of kg; a deviation
    // that matters at idle is noise up there.
    const atLoad = detectCellFaults({ s1: 40.0, s2: 40.5, s3: 39.8, s4: 41.0 });
    expect(atLoad).toEqual({});
  });
});
