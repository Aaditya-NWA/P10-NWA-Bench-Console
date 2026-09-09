import React, { useMemo, useState, useEffect, useRef, memo } from "react";
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ReferenceLine,
} from "recharts";
import { convert } from "../units";
import { CHANNEL_TORQUE, CHANNEL_THRUST, PHASE, UNIT_SI } from "../constants";

const CHART_REFRESH_MS = 200;   // 5 Hz redraw — the numeric readouts stay at 20 Hz
const MARKER_LIMIT = 24;        // Plenty for a 30 s window; stops the array growing over a long run

// Hoisted so it's a stable reference rather than a new function every frame
function CustomTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null;
  return (
    <div style={tooltipStyle}>
      <span style={tooltipTime}>{label}s</span>
      {payload.map((p) => (
        <div key={p.name} style={tooltipRow}>
          <span style={{ color: p.color }}>{p.name.toUpperCase()}</span>
          <span style={tooltipVal}>{p.value}</span>
        </div>
      ))}
    </div>
  );
}

/**
 * LiveChart — rolling 30-second time-series for TORQUE and THRUST.
 *
 * Redraw is throttled to 5 Hz: at 20 Hz, Recharts rebuilding its SVG
 * tree every frame is the main source of the "whole UI feels sluggish"
 * symptom, since it's far heavier than a text update.
 *
 * Profile markers: a vertical line is dropped at each phase change.
 * Without them a sweep reads as one continuous curve with no way to
 * tell a ramp from the plateau that follows — exactly the distinction
 * the run was made to capture. Transitions are recorded as they arrive
 * because the backend reports only the CURRENT phase.
 *
 * Props: history (≤600 frames), profile (or null), unitSystem
 */
function LiveChart({ history, profile, unitSystem = UNIT_SI }) {
  const [throttledHistory, setThrottledHistory] = useState(history);
  const lastUpdateRef = useRef(0);

  const [markers, setMarkers] = useState([]);
  const lastPhaseRef = useRef(null);

  useEffect(() => {
    if (!profile) return;
    const phase = profile.phase;
    const step = profile.step_index;
    const signature = `${phase}:${step}`;
    if (signature === lastPhaseRef.current) return;
    lastPhaseRef.current = signature;

    // Only transitions that shape the curve are worth a line; idle and
    // terminal states would just clutter it.
    const label =
      phase === PHASE.RAMPING      ? `→ ${profile.target_us}µs`
    : phase === PHASE.HOLDING      ? `hold ${profile.target_us}µs`
    : phase === PHASE.RAMPING_DOWN ? "ramp down"
    : null;
    if (!label) return;

    setMarkers((prev) => [...prev, { ts: Date.now() / 1000, label, phase }].slice(-MARKER_LIMIT));
  }, [profile?.phase, profile?.step_index, profile?.target_us]);

  useEffect(() => {
    const now = performance.now();
    const since = now - lastUpdateRef.current;
    if (since >= CHART_REFRESH_MS) {
      lastUpdateRef.current = now;
      setThrottledHistory(history);
      return;
    }
    // BUG FIX: an update arriving inside the throttle window used to be
    // dropped with no timer scheduled, so it rendered only if ANOTHER
    // frame happened to follow. When telemetry stops — motor cut, serial
    // unplugged, end of a run — the last up-to-200 ms was silently never
    // drawn. The trailing timer guarantees the final frame lands.
    const timer = setTimeout(() => {
      lastUpdateRef.current = performance.now();
      setThrottledHistory(history);
    }, CHART_REFRESH_MS - since);
    return () => clearTimeout(timer);
  }, [history]);

  const { chartData, t0, tMax } = useMemo(() => {
    if (!throttledHistory.length) return { chartData: [], t0: 0, tMax: 0 };
    const first = throttledHistory[0].ts;                     // Anchor to the first sample in the window
    const data = throttledHistory.map((frame) => {
      const tq = convert(unitSystem, CHANNEL_TORQUE, frame.torque ?? 0);
      const th = convert(unitSystem, CHANNEL_THRUST, frame.thrust ?? 0);
      return {
        t:      parseFloat((frame.ts - first).toFixed(1)),
        torque: parseFloat(tq.value.toFixed(tq.decimals)),
        thrust: parseFloat(th.value.toFixed(th.decimals)),
      };
    });
    return { chartData: data, t0: first, tMax: data.length ? data[data.length - 1].t : 0 };
  }, [throttledHistory, unitSystem]);

  // Markers are absolute times but the axis is relative to the start of
  // the visible window, so they're re-anchored as that window slides.
  const visibleMarkers = useMemo(() => {
    if (!t0) return [];
    return markers
      .map((m) => ({ ...m, x: parseFloat((m.ts - t0).toFixed(1)) }))
      .filter((m) => m.x >= 0 && m.x <= tMax);
  }, [markers, t0, tMax]);

  const torqueUnit = convert(unitSystem, CHANNEL_TORQUE, 0).unit;
  const thrustUnit = convert(unitSystem, CHANNEL_THRUST, 0).unit;

  return (
    <div style={styles.wrapper}>
      <div style={styles.headingRow}>
        <span style={styles.heading}>LIVE CHART — 30 SECOND WINDOW</span>
        <span style={styles.unitNote}>
          torque {torqueUnit} · thrust {thrustUnit}
        </span>
      </div>

      <div style={styles.chartArea}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={chartData} margin={{ top: 6, right: 16, left: 0, bottom: 0 }}>

            <CartesianGrid
              strokeDasharray="3 3"
              stroke="var(--bg-border)"
              vertical={false}             // Vertical grid lines are too noisy here
            />

            {/* type="number" (not the default category) so a
                ReferenceLine can sit at an arbitrary time rather than
                snapping to the nearest sample */}
            <XAxis
              dataKey="t"
              type="number"
              domain={["dataMin", "dataMax"]}
              tick={{ fill: "var(--text-tertiary)", fontSize: 11, fontFamily: "var(--font-mono)" }}
              tickLine={false}
              axisLine={{ stroke: "var(--bg-border)" }}
              tickFormatter={(v) => `${v}s`}
              interval="preserveStartEnd"
            />

            <YAxis
              tick={{ fill: "var(--text-tertiary)", fontSize: 11, fontFamily: "var(--font-mono)" }}
              tickLine={false}
              axisLine={false}
              width={52}                                 // Fixed, to prevent layout shift
            />

            <Tooltip content={<CustomTooltip />} />

            <Legend
              wrapperStyle={{
                fontSize: "0.78rem",
                fontFamily: "var(--font-ui)",
                letterSpacing: "0.06em",
                textTransform: "uppercase",
                paddingTop: "6px",
              }}
            />

            {/* Dashed, so they never read as a data series; colours
                match the progress strip */}
            {visibleMarkers.map((m, i) => (
              <ReferenceLine
                key={`${m.ts}-${i}`}
                x={m.x}
                stroke={m.phase === PHASE.HOLDING ? "var(--status-stable)" : "var(--cw-colour)"}
                strokeDasharray="4 3"
                strokeOpacity={0.75}
                label={{
                  value: m.label,
                  position: "insideTopLeft",
                  fill: "var(--text-tertiary)",
                  fontSize: 10,
                  fontFamily: "var(--font-mono)",
                }}
              />
            ))}

            <Line
              type="monotone"
              dataKey="torque"
              stroke="var(--accent-torque)"
              strokeWidth={2}
              dot={false}                                // Too many points at 20 Hz
              isAnimationActive={false}                  // Animation fights real-time updates
            />

            <Line
              type="monotone"
              dataKey="thrust"
              stroke="var(--accent-thrust)"
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
            />

          </LineChart>
        </ResponsiveContainer>
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
    padding:       "0.85rem 1rem 0.7rem",
    display:       "flex",
    flexDirection: "column",
    gap:           "0.4rem",
    flex:          1,
    minHeight:     0,
  },
  headingRow: {
    display:        "flex",
    alignItems:     "baseline",
    justifyContent: "space-between",
    gap:            "0.5rem",
  },
  heading: {
    fontFamily:    "var(--font-ui)",
    fontSize:      "0.72rem",
    fontWeight:    600,
    letterSpacing: "0.1em",
    textTransform: "uppercase",
    color:         "var(--text-tertiary)",
  },
  unitNote: {
    fontFamily:    "var(--font-mono)",
    fontSize:      "0.68rem",
    color:         "var(--text-tertiary)",
    whiteSpace:    "nowrap",
  },
  chartArea: {
    flex:          1,
    minHeight:     0,
  },
};

const tooltipStyle = {
  background:    "var(--bg-elevated)",
  border:        "1px solid var(--bg-border)",
  borderRadius:  "var(--radius-sm)",
  padding:       "0.6rem 0.8rem",
  display:       "flex",
  flexDirection: "column",
  gap:           "0.3rem",
};
const tooltipTime = {
  fontFamily:    "var(--font-mono)",
  fontSize:      "0.7rem",
  color:         "var(--text-tertiary)",
  marginBottom:  "0.2rem",
};
const tooltipRow = {
  display:       "flex",
  justifyContent:"space-between",
  gap:           "1.2rem",
  fontFamily:    "var(--font-mono)",
  fontSize:      "0.78rem",
};
const tooltipVal = {
  color:         "var(--text-primary)",
  fontWeight:    600,
};

// `history` is a new array reference every frame, so default memo()
// would never skip a re-render. Length plus the newest timestamp are
// cheap checks that catch the only two ways it meaningfully changes.
function areEqual(prevProps, nextProps) {
  if (prevProps.unitSystem !== nextProps.unitSystem) return false;
  if (prevProps.profile?.phase !== nextProps.profile?.phase) return false;
  if (prevProps.profile?.step_index !== nextProps.profile?.step_index) return false;

  const a = prevProps.history;
  const b = nextProps.history;
  if (a === b) return true;
  if (a.length !== b.length) return false;
  if (a.length === 0) return true;
  return a[a.length - 1].ts === b[b.length - 1].ts;
}

export default memo(LiveChart, areEqual);
