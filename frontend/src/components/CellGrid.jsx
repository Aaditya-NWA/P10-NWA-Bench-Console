import React, { memo } from "react";
import { CELL_KEYS, NO_VALUE } from "../constants";

// A beam cell with a loose mount, a damaged wire or a half-seated HX711
// connector doesn't read nothing — it reads a plausible number that
// disagrees with its neighbours, invisible in the aggregate torque
// figure. A median/MAD test rather than a fixed limit, because the
// absolute values span orders of magnitude between idle and full throttle.
const FAULT_MAD_MULTIPLE = 5;    // Deviations beyond this many MADs are suspect
const FAULT_MIN_ABS_KG   = 0.4;  // …but never flag less than this, or idle noise trips it

const NEAR_ZERO_KG = 0.01;       // Below this a cell is "unloaded", not signed

function median(nums) {
  const sorted = [...nums].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

/** Which of s1–s4 disagree with the rest, and by how much.
 *  Returns a map of key → deviation in kg. */
export function detectCellFaults(data) {
  if (!data) return {};
  const entries = CELL_KEYS
    .map((key) => [key, data[key]])
    .filter(([, v]) => typeof v === "number" && Number.isFinite(v));
  if (entries.length < 3) return {};              // Too few readings to pick the odd one out

  const values = entries.map(([, v]) => v);
  const mid = median(values);
  const mad = median(values.map((v) => Math.abs(v - mid)));
  const limit = Math.max(FAULT_MIN_ABS_KG, FAULT_MAD_MULTIPLE * mad);

  const faults = {};
  for (const [key, v] of entries) {
    const deviation = Math.abs(v - mid);
    if (deviation > limit) faults[key] = deviation;
  }
  // If every cell is "an outlier" the test has told us nothing — that's
  // a rig under genuine asymmetric load, not four broken cells.
  return Object.keys(faults).length >= entries.length - 1 ? {} : faults;
}

// Positions match the physical layout on the test stand
const CELLS = [
  { key: "s1", pos: "TOP",    col: 1, row: 1 },
  { key: "s2", pos: "BOTTOM", col: 1, row: 2 },
  { key: "s3", pos: "RIGHT",  col: 2, row: 1 },
  { key: "s4", pos: "LEFT",   col: 2, row: 2 },
];

/** 2×2 grid of the individual beam cell readings (S1–S4), flagging any
 *  cell that disagrees sharply with the other three. */
function CellGrid({ data }) {
  const faults = detectCellFaults(data);
  const faultKeys = Object.keys(faults);

  return (
    <div style={styles.wrapper}>
      <div style={styles.headingRow}>
        <span style={styles.heading}>BEAM CELLS — DIAGNOSTIC</span>
        {faultKeys.length > 0 && (
          <span
            style={styles.faultTag}
            title="This cell's reading is far from the other three — check its mounting, wiring and HX711 connector"
          >
            ⚠ {faultKeys.map((k) => k.toUpperCase()).join(", ")}
          </span>
        )}
      </div>

      <div style={styles.grid}>
        {CELLS.map(({ key, pos }) => {
          const val = data?.[key];
          const display = val === undefined || val === null
            ? NO_VALUE
            : (val >= 0 ? "+" : "") + val.toFixed(3);   // Always signed, for diagnostic clarity

          const faulted = Object.prototype.hasOwnProperty.call(faults, key);

          // Sign highlights imbalance — unless flagged, where the fault
          // outranks the sign.
          const colour = faulted
            ? "var(--status-offline)"
            : val > NEAR_ZERO_KG
            ? "var(--accent-thrust)"
            : val < -NEAR_ZERO_KG
            ? "var(--accent-torque)"
            : "var(--text-secondary)";

          return (
            <div
              key={key}
              style={{
                ...styles.cell,
                ...(faulted ? styles.cellFaulted : {}),
              }}
              title={faulted
                ? `${key.toUpperCase()} is ${faults[key].toFixed(2)} kg away from the other cells — check its mounting and wiring`
                : undefined}
            >
              <span style={styles.cellLabel}>
                {key.toUpperCase()} — {pos}{faulted ? " ⚠" : ""}
              </span>
              <span style={{ ...styles.cellValue, color: colour }}>{display}</span>
              <span style={styles.cellUnit}>kg</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

const styles = {
  wrapper: {
    background:    "var(--bg-surface)",
    borderRadius:  "var(--radius-md)",
    border:        "1px solid var(--bg-border)",
    boxShadow:     "var(--shadow-card)",
    padding:       "0.75rem 0.9rem",
    display:       "flex",
    flexDirection: "column",
    gap:           "0.5rem",
    flexShrink:    0,
  },
  headingRow: {
    display:        "flex",
    alignItems:     "baseline",
    justifyContent: "space-between",
    gap:            "0.4rem",
  },
  heading: {
    fontFamily:    "var(--font-ui)",
    fontSize:      "0.72rem",
    fontWeight:    600,
    letterSpacing: "0.1em",
    textTransform: "uppercase",
    color:         "var(--text-tertiary)",
  },
  faultTag: {
    fontFamily:    "var(--font-ui)",
    fontSize:      "0.68rem",
    fontWeight:    700,
    letterSpacing: "0.04em",
    color:         "var(--status-offline)",
    whiteSpace:    "nowrap",
  },
  grid: {
    display:               "grid",
    gridTemplateColumns:   "1fr 1fr",            // Matches the S1/S3 and S2/S4 pairings
    gap:                   "0.45rem",
  },
  cell: {
    background:    "var(--bg-elevated)",
    borderRadius:  "var(--radius-sm)",
    padding:       "0.5rem 0.65rem",
    display:       "flex",
    flexDirection: "column",
    gap:           "0.1rem",
    border:        "1px solid transparent",      // Reserved so a faulted border doesn't shift layout
  },
  cellFaulted: {
    borderColor:   "var(--status-offline)",
    background:    "rgba(232,80,80,0.08)",
  },
  cellLabel: {
    fontFamily:    "var(--font-ui)",
    fontSize:      "0.68rem",
    fontWeight:    600,
    letterSpacing: "0.08em",
    textTransform: "uppercase",
    color:         "var(--text-tertiary)",
  },
  cellValue: {
    fontFamily:    "var(--font-mono)",
    fontSize:      "1.1rem",
    fontWeight:    600,
    lineHeight:    1,
  },
  cellUnit: {
    fontFamily:    "var(--font-mono)",
    fontSize:      "0.65rem",
    color:         "var(--text-tertiary)",
  },
};

// `data` is a new object reference every frame, so default memo() would
// never skip a re-render. Compare only the four fields this component
// actually reads.
function areEqual(prevProps, nextProps) {
  const a = prevProps.data;
  const b = nextProps.data;
  if (a === b) return true;
  if (!a || !b) return false;
  return (
    a.s1 === b.s1 &&
    a.s2 === b.s2 &&
    a.s3 === b.s3 &&
    a.s4 === b.s4
  );
}

export default memo(CellGrid, areEqual);
