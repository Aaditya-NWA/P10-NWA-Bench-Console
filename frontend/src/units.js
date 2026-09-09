// Display-only unit conversion. The firmware always reports SI; most
// propeller/motor datasheets are published in kgf. Nothing sent to the
// ESP32, recorded, or exported is ever converted — a CSV is always SI,
// so a file can't be ambiguous about its own units.

import { UNIT_SI, UNIT_KGF } from "./constants";

export const G = 9.80665;          // 1 kgf = 9.80665 N

export const UNIT_SYSTEMS = {
  [UNIT_SI]: {
    id:     UNIT_SI,
    label:  "SI",
    hint:   "Newtons and newton-metres — what the firmware reports",
    thrust: { unit: "N",      convert: (n) => n,            decimals: 3 },
    torque: { unit: "N·m",    convert: (nm) => nm,          decimals: 4 },
  },
  [UNIT_KGF]: {
    id:     UNIT_KGF,
    label:  "kgf",
    hint:   "Kilogram-force — matches most propeller and motor datasheets",
    thrust: { unit: "kgf",    convert: (n) => n / G,        decimals: 3 },
    // kgf·cm not kgf·m: bench torques are order 1 N·m (~0.1 kgf·m), and
    // a reading that lives below 1 is harder to read than one in tens.
    torque: { unit: "kgf·cm", convert: (nm) => (nm / G) * 100, decimals: 2 },
  },
};

export const DEFAULT_UNIT_SYSTEM = UNIT_SI;

/** Convert a value for display. Returns { value, unit, decimals }. */
export function convert(system, channel, value) {
  const spec = (UNIT_SYSTEMS[system] ?? UNIT_SYSTEMS[DEFAULT_UNIT_SYSTEM])[channel];
  return {
    value: value === null || value === undefined ? value : spec.convert(value),
    unit: spec.unit,
    decimals: spec.decimals,
  };
}
