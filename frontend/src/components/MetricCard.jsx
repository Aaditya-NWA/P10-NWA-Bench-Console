import React, { memo } from "react";
import { NO_VALUE } from "../constants";

/**
 * MetricCard — one primary measurement as a large numeric readout.
 *
 * Memoised: several of these re-render every frame at ~20 Hz, and most
 * frames change only one card's value.
 *
 * Props: label, value, unit, accent (CSS colour), decimals,
 *   sub / subColour / subTitle — optional sub-label below the value
 *   subBadge — render the sub-label as an outlined pill rather than
 *              plain text; used for spin direction, where the colour IS
 *              the information and has to read at a glance
 */
function MetricCard({
  label,
  value,
  unit,
  accent,
  decimals = 3,
  sub,
  subColour,
  subBadge = false,
  subTitle,
}) {
  const display =
    value === null || value === undefined
      ? NO_VALUE
      : value.toFixed(decimals);

  return (
    <div style={styles.card}>
      <div style={{ ...styles.accent, background: accent }} />

      <div style={styles.body}>
        <span style={styles.label}>{label}</span>

        <div style={styles.valueRow}>
          <span style={{ ...styles.value, color: accent }}>{display}</span>
          <span style={styles.unit}>{unit}</span>
        </div>

        {sub && (
          <span
            title={subTitle}
            style={{
              ...styles.sub,
              color: subColour || accent,
              ...(subBadge ? {
                ...styles.subBadge,
                borderColor: subColour || accent,
              } : {}),
            }}
          >
            {sub}
          </span>
        )}
      </div>
    </div>
  );
}

export default memo(MetricCard);

const styles = {
  card: {
    display:         "flex",
    flexDirection:   "row",
    background:      "var(--bg-surface)",
    borderRadius:    "var(--radius-md)",
    boxShadow:       "var(--shadow-card)",
    overflow:        "hidden",
    flex:            "1 1 150px",                 // Equal-width siblings, wraps below ~150px each
    minWidth:        "150px",
    border:          "1px solid var(--bg-border)",
  },
  accent: {
    width:           "4px",
    flexShrink:      0,
  },
  body: {
    padding:         "0.65rem 0.85rem",
    display:         "flex",
    flexDirection:   "column",
    gap:             "0.2rem",
    flex:            1,
    minWidth:        0,
  },
  label: {
    fontFamily:      "var(--font-ui)",
    fontSize:        "0.62rem",
    fontWeight:      600,
    letterSpacing:   "0.1em",
    textTransform:   "uppercase",
    color:           "var(--text-secondary)",
  },
  valueRow: {
    display:         "flex",
    alignItems:      "baseline",
    gap:             "0.35rem",
  },
  value: {
    fontFamily:      "var(--font-mono)",
    fontSize:        "1.85rem",                   // Compact, but still dominant in its card
    fontWeight:      600,
    lineHeight:      1,
    letterSpacing:   "-0.01em",
  },
  unit: {
    fontFamily:      "var(--font-mono)",
    fontSize:        "0.85rem",
    fontWeight:      400,
    color:           "var(--text-secondary)",
  },
  sub: {
    fontFamily:      "var(--font-ui)",
    fontSize:        "0.72rem",
    fontWeight:      600,
    letterSpacing:   "0.06em",
    textTransform:   "uppercase",
    marginTop:       "0.05rem",
    whiteSpace:      "nowrap",
    overflow:        "hidden",
    textOverflow:    "ellipsis",
  },
  subBadge: {
    alignSelf:       "flex-start",                // Pill hugs its text instead of spanning the card
    border:          "1px solid",
    borderRadius:    "999px",
    padding:         "0.05rem 0.45rem",
    marginTop:       "0.15rem",
    fontWeight:      700,
  },
};
