import React, { useEffect, useReducer, useRef, useState } from "react";
import { CMD_LINK, COMM_STATUS, COMM_LOST_MS, COMM_WARN_MS, NO_VALUE } from "../constants";
import {
  commStatus, connectionQuality, esp32UptimeMs, lastPacketAgeMs,
  liveRateHz, packetsReceived, signalBars,
} from "../linkStatus";

/**
 * LinkHealth — the ESP32 Communication card.
 *
 * Closed by default: a small square in the header showing signal bars in
 * the status colour, so it costs no room until someone wants it. Click
 * it for the card — one status word, the link's vital numbers, and a
 * connection-quality bar — with the deeper diagnostics (waveform,
 * messages, event log) folded under "Details".
 *
 * The status word comes from the age of the newest VALID packet, not
 * from whether the COM port is open: a port can be open on a board that
 * has hung. Green under 250 ms, yellow to 1000 ms, red beyond. Those and
 * the rate, packet count and quality are computed live from the
 * WebSocket frames (see linkStatus.js); /status, polled every 2 s,
 * supplies the rest.
 *
 * Props: health, serialConnected, serialPort, wsConnected, latest, history
 */

const OPEN_TICK_MS   = 100;          // "48 ms ago" needs to move
const CLOSED_TICK_MS = 250;          // Enough for the square to turn yellow at 250 ms
const WAVE_W = 340;
const WAVE_H = 70;
const RTT_H  = 26;

const STATUS_STYLE = {
  [COMM_STATUS.CONNECTED]:    { colour: "var(--status-stable)",   label: "CONNECTED"    },
  [COMM_STATUS.WARNING]:      { colour: "var(--status-settling)", label: "WARNING"      },
  [COMM_STATUS.DISCONNECTED]: { colour: "var(--status-offline)",  label: "DISCONNECTED" },
};

const LEVEL_COLOUR = {
  ok:    "var(--status-stable)",
  info:  "var(--accent-thrust)",
  warn:  "var(--status-settling)",
  error: "var(--status-offline)",
};

const COMMAND_PATH = {
  [CMD_LINK.OK]:         { text: "Confirmed",                 colour: "var(--status-stable)"  },
  [CMD_LINK.LOST]:       { text: "No reply",                  colour: "var(--status-offline)" },
  [CMD_LINK.WAITING]:    { text: "Checking…",                 colour: "var(--text-secondary)" },
  [CMD_LINK.UNVERIFIED]: { text: "Not supported by firmware", colour: "var(--text-secondary)" },
  [CMD_LINK.OFFLINE]:    { text: NO_VALUE,                    colour: "var(--text-secondary)" },
};

function fmtDuration(ms) {
  if (ms === null || ms === undefined) return NO_VALUE;
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m ${String(sec).padStart(2, "0")}s`;
  if (m) return `${m}m ${String(sec).padStart(2, "0")}s`;
  return `${sec}s`;
}

function fmtAge(ms) {
  if (ms === null) return NO_VALUE;
  if (ms < 1000) return `${Math.round(ms)} ms ago`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s ago`;
  return `${fmtDuration(ms)} ago`;
}

function fmtClock(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
}

function qualityColour(q) {
  return q >= 90 ? "var(--status-stable)" : q >= 70 ? "var(--status-settling)" : "var(--status-offline)";
}

/** SVG polyline/area path for a series in a box, y from 0..yMax. */
function seriesPath(values, w, h, yMax) {
  if (values.length < 2) return { line: "", area: "" };
  const dx = w / (values.length - 1);
  const pts = values.map((v, i) => [i * dx, h - (Math.min(v ?? 0, yMax) / yMax) * h]);
  const line = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  return { line, area: `${line} L${w},${h} L0,${h} Z` };
}

function SignalBars({ lit, colour }) {
  return (
    <svg width="18" height="16" viewBox="0 0 18 16" aria-hidden="true">
      {[0, 1, 2, 3].map((i) => (
        <rect key={i} x={i * 4.7} y={12 - i * 4} width="3.4" height={4 + i * 4} rx="0.8"
              fill={i < lit ? colour : "var(--bg-border)"} />
      ))}
    </svg>
  );
}

export default function LinkHealth({
  health, serialConnected, serialPort, wsConnected = true, latest = null, history = [],
}) {
  const [open, setOpen] = useState(false);
  const [showDetails, setShowDetails] = useState(false);
  const rootRef = useRef(null);

  // Frames re-render this 20 times a second while they flow. The tick is
  // for when they STOP — otherwise "last packet" would freeze at its
  // last value and never turn yellow, which is the whole point.
  const [, tick] = useReducer((x) => x + 1, 0);
  useEffect(() => {
    const id = setInterval(tick, open ? OPEN_TICK_MS : CLOSED_TICK_MS);
    return () => clearInterval(id);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e) => { if (!rootRef.current?.contains(e.target)) setOpen(false); };
    const onKey = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const nowMs    = Date.now();
  const linkUp   = wsConnected && serialConnected;
  const ageMs    = linkUp ? lastPacketAgeMs(latest, nowMs) : null;
  const status   = commStatus(ageMs);
  const ss       = STATUS_STYLE[status];
  const expected = health?.expected_rate_hz ?? 20;
  const rate     = linkUp ? liveRateHz(history, nowMs) : 0;
  const quality  = linkUp ? connectionQuality(history, nowMs, expected) : 0;
  const bars     = signalBars(quality, status);

  // Things wrong beyond packet age — a dead command wire, lost frames, a
  // reboot. Flagged on the square too, so a green square can't hide them.
  const problems = (health?.issues ?? []).filter((i) => i.level === "warn" || i.level === "error");
  const worst    = problems.find((i) => i.level === "error") ?? problems[0];

  const subline =
    !wsConnected       ? "The dashboard has lost the backend"
  : !serialConnected   ? "No COM port open"
  : ageMs === null     ? "No packets received yet"
  : status === COMM_STATUS.DISCONNECTED ? `No packet for ${(ageMs / 1000).toFixed(1)} s`
  : null;

  return (
    <div ref={rootRef} style={styles.root}>
      <button
        type="button"
        style={{ ...styles.square, borderColor: linkUp ? ss.colour : "var(--bg-border)" }}
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-label="ESP32 communication status"
        title={`ESP32 communication: ${ss.label.toLowerCase()}` +
               (linkUp && rate ? ` · ${rate.toFixed(1)} Hz` : "") +
               (worst ? ` — ${worst.text}` : "") + ". Click for details."}
      >
        <span style={{ animation: status === COMM_STATUS.DISCONNECTED && linkUp ? "pulse 1.2s ease-in-out infinite" : "none", display: "flex" }}>
          <SignalBars lit={bars} colour={ss.colour} />
        </span>
        {worst && status !== COMM_STATUS.DISCONNECTED && (
          <span style={{ ...styles.badge, background: LEVEL_COLOUR[worst.level] }} />
        )}
      </button>

      {open && (
        <div style={styles.card} role="dialog" aria-label="ESP32 communication">
          <div style={styles.header}>
            <span style={styles.title}>ESP32 COMMUNICATION</span>
            <button type="button" style={styles.close} onClick={() => setOpen(false)} aria-label="Close">×</button>
          </div>

          <div style={styles.statusRow}>
            <span style={{ ...styles.bigDot, background: ss.colour,
                           animation: status === COMM_STATUS.DISCONNECTED ? "pulse 1.2s ease-in-out infinite" : "none" }} />
            <span style={{ ...styles.statusWord, color: ss.colour }}>{ss.label}</span>
          </div>
          {subline && <div style={styles.subline}>{subline}</div>}

          <dl style={styles.rows}>
            <Row label="Serial Port"  value={serialConnected ? (serialPort || health?.port?.name || NO_VALUE) : NO_VALUE} />
            <Row label="Baud Rate"    value={serialConnected && health?.port?.baud ? String(health.port.baud) : NO_VALUE} />
            <Row label="Data Rate"    value={linkUp ? `${rate.toFixed(1)} Hz` : NO_VALUE}
                 colour={linkUp && rate > 0 && rate < expected * 0.75 ? "var(--status-settling)" : undefined} />
            <Row label="Last Packet"  value={fmtAge(ageMs)} colour={linkUp && ageMs !== null ? ss.colour : undefined} />
            <Row label="Packets Recv" value={serialConnected ? (packetsReceived(health, history)?.toLocaleString() ?? NO_VALUE) : NO_VALUE} />
            <Row label="Lost Packets"
                 value={!serialConnected ? NO_VALUE
                      : health?.seq_supported === false ? "n/a — older firmware"
                      : (health?.dropped_total ?? NO_VALUE).toLocaleString()}
                 colour={(health?.dropped_total ?? 0) > 0 ? "var(--status-settling)" : undefined} />
            <Row label="ESP32 Uptime" value={serialConnected ? fmtDuration(esp32UptimeMs(health, nowMs)) : NO_VALUE} />
            <Row label="Command Path"
                 value={`${(COMMAND_PATH[health?.command_link?.state] ?? COMMAND_PATH[CMD_LINK.OFFLINE]).text}` +
                        (health?.command_link?.state === CMD_LINK.OK && health.command_link.rtt_avg_ms != null
                          ? ` (${Math.round(health.command_link.rtt_avg_ms)} ms)` : "")}
                 colour={(COMMAND_PATH[health?.command_link?.state] ?? COMMAND_PATH[CMD_LINK.OFFLINE]).colour} />
          </dl>

          <div>
            <div style={styles.qualityHead}>
              <span>Connection Quality</span>
              <span style={{ fontFamily: "var(--font-mono)", color: linkUp ? qualityColour(quality) : "var(--text-tertiary)" }}>
                {linkUp ? `${quality}%` : NO_VALUE}
              </span>
            </div>
            <div style={styles.track} title="Share of the packets the ESP32 should have sent in the last 10 s that arrived intact">
              <div style={{ ...styles.fill, width: `${linkUp ? quality : 0}%`, background: qualityColour(quality) }} />
            </div>
          </div>

          {linkUp && worst && (
            <div style={{ ...styles.problem, borderLeftColor: LEVEL_COLOUR[worst.level] }}>
              <div style={{ color: LEVEL_COLOUR[worst.level], fontWeight: 600 }}>{worst.text}</div>
              {worst.hint && <div style={styles.hint}>{worst.hint}</div>}
              {problems.length > 1 && <div style={styles.hint}>+{problems.length - 1} more under Details</div>}
            </div>
          )}

          <button type="button" style={styles.detailsToggle} onClick={() => setShowDetails((d) => !d)}
                  aria-expanded={showDetails}>
            {showDetails ? "▾" : "▸"} DETAILS
          </button>
          {showDetails && <Details health={health} serialConnected={serialConnected} ss={ss} />}

          <div style={styles.legendFoot}>
            Green &lt; {COMM_WARN_MS} ms · yellow {COMM_WARN_MS}–{COMM_LOST_MS} ms · red &gt; {COMM_LOST_MS} ms since the last valid packet
          </div>
        </div>
      )}
    </div>
  );
}

function Row({ label, value, colour }) {
  return (
    <>
      <dt style={styles.rowLabel}>{label}</dt>
      <dd style={styles.rowColon}>:</dd>
      <dd style={{ ...styles.rowValue, color: colour ?? "var(--text-primary)" }}>{value}</dd>
    </>
  );
}

/** The deeper diagnostics — folded away so the card reads at a glance. */
function Details({ health, serialConnected, ss }) {
  const timeline = health?.timeline ?? [];
  const expected = health?.expected_rate_hz ?? 20;
  const dev = health?.device ?? {};
  const port = health?.port ?? {};
  const issues = health?.issues ?? [];
  const events = [...(health?.events ?? [])].reverse();

  const yMax = Math.max(expected * 1.25, ...timeline.map((b) => b.frames));
  const wave = seriesPath(timeline.map((b) => b.frames), WAVE_W, WAVE_H, yMax);
  const targetY = WAVE_H - (expected / yMax) * WAVE_H;
  const dx = timeline.length > 1 ? WAVE_W / (timeline.length - 1) : 0;

  // Bars, not a line: a second with no reply has NO round trip, and a
  // line would have to draw it as 0 ms — the fastest-looking value.
  const rtts = timeline.map((b) => b.rtt_ms);
  const hasRtt = rtts.some((v) => v != null);
  const rttMax = Math.max(50, ...rtts.filter((v) => v != null));
  const barW = timeline.length ? Math.max(1, WAVE_W / timeline.length - 0.6) : 0;

  const adapter = [port.description, port.vid_pid].filter(Boolean).join(" · ");

  return (
    <div style={styles.details}>
      {serialConnected && (
        <>
          <div style={styles.sectionHead}>
            <span>Packets per second · last {timeline.length} s</span>
            <span style={styles.legend}>
              <i style={{ ...styles.swatch, background: "var(--status-offline)" }} /> lost
              <i style={{ ...styles.swatch, background: "var(--status-settling)", marginLeft: 8 }} /> corrupted
            </span>
          </div>
          {timeline.length > 1 ? (
            <svg viewBox={`0 0 ${WAVE_W} ${WAVE_H}`} preserveAspectRatio="none" style={styles.wave} aria-hidden="true">
              <line x1="0" x2={WAVE_W} y1={targetY} y2={targetY}
                    stroke="var(--text-tertiary)" strokeDasharray="3 3" strokeWidth="0.8" />
              <path d={wave.area} fill={ss.colour} opacity="0.14" />
              <path d={wave.line} fill="none" stroke={ss.colour} strokeWidth="1.4" />
              {timeline.map((b, i) => (b.dropped || b.malformed) ? (
                <rect key={b.t} x={i * dx - 1.5} y={WAVE_H - 6} width="3" height="6"
                      fill={b.dropped ? "var(--status-offline)" : "var(--status-settling)"} />
              ) : null)}
            </svg>
          ) : (
            <div style={styles.waveEmpty}>Collecting…</div>
          )}
          <div style={styles.axis}>
            <span>−{timeline.length} s</span><span>dashed = {expected} Hz target</span><span>now</span>
          </div>

          {hasRtt && (
            <>
              <div style={{ ...styles.sectionHead, marginTop: 6 }}>
                <span>Command round trip · peak {Math.round(rttMax)} ms</span>
              </div>
              <svg viewBox={`0 0 ${WAVE_W} ${RTT_H}`} preserveAspectRatio="none" style={styles.rtt} aria-hidden="true">
                {rtts.map((v, i) => v == null ? null : (
                  <rect key={timeline[i].t} x={(i * WAVE_W) / timeline.length} width={barW}
                        y={RTT_H - (v / rttMax) * RTT_H} height={(v / rttMax) * RTT_H}
                        fill="var(--accent-thrust)" opacity="0.8" />
                ))}
              </svg>
            </>
          )}

          <dl style={styles.rows}>
            {adapter && <Row label="Adapter" value={adapter} />}
            <Row label="Corrupted" value={`${health?.malformed_total ?? 0} lines`} />
            <Row label="Jitter p95" value={health?.gap_p95_ms != null ? `${Math.round(health.gap_p95_ms)} ms` : NO_VALUE} />
            <Row label="Resets" value={`${health?.device_resets ?? 0}` + (dev.boot_reason ? ` · last boot ${dev.boot_reason}` : "")} />
            <Row label="ESP32 RX errs" value={dev.rx_err === undefined ? NO_VALUE
                 : `${(dev.rx_err ?? 0) + (dev.rx_ovf ?? 0)}` + (dev.bad_cmd ? ` · ${dev.bad_cmd} bad cmd` : "")} />
          </dl>
        </>
      )}

      {issues.length > 0 && (
        <div style={styles.sectionBlock}>
          <div style={styles.sectionHead}><span>Messages</span></div>
          {issues.map((i, k) => (
            <div key={k} style={{ ...styles.problem, borderLeftColor: LEVEL_COLOUR[i.level] }}>
              <div style={{ color: LEVEL_COLOUR[i.level], fontWeight: 600 }}>{i.text}</div>
              {i.hint && <div style={styles.hint}>{i.hint}</div>}
            </div>
          ))}
        </div>
      )}

      {events.length > 0 && (
        <div style={styles.sectionBlock}>
          <div style={styles.sectionHead}><span>Recent link events</span></div>
          <div style={styles.events}>
            {events.map((e, k) => (
              <div key={k} style={styles.event}>
                <span style={styles.eventTime}>{fmtClock(e.ts)}</span>
                <span style={{ ...styles.eventDot, background: LEVEL_COLOUR[e.level] ?? "var(--text-tertiary)" }} />
                <span>{e.text}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

const styles = {
  root: {
    position:       "relative",
    flexShrink:     0,
  },
  // Same footprint as the unit / settings / theme buttons beside it
  square: {
    position:       "relative",
    width:          "36px",
    height:         "36px",
    display:        "flex",
    alignItems:     "center",
    justifyContent: "center",
    background:     "var(--bg-elevated)",
    border:         "1px solid",
    borderRadius:   "var(--radius-sm)",
    cursor:         "pointer",
    transition:     "var(--transition)",
    padding:        0,
  },
  badge: {
    position:       "absolute",
    top:            "4px",
    right:          "4px",
    width:          "7px",
    height:         "7px",
    borderRadius:   "50%",
    boxShadow:      "0 0 0 2px var(--bg-elevated)",
  },
  card: {
    position:       "absolute",
    top:            "calc(100% + 8px)",
    right:          0,
    zIndex:         50,
    width:          "330px",
    maxWidth:       "calc(100vw - 32px)",
    maxHeight:      "calc(100vh - 90px)",
    overflowY:      "auto",
    background:     "var(--bg-surface)",
    border:         "1px solid var(--bg-border)",
    borderRadius:   "var(--radius-md)",
    boxShadow:      "var(--shadow-card)",
    padding:        "0.85rem 1rem 0.75rem",
    display:        "flex",
    flexDirection:  "column",
    gap:            "0.65rem",
    animation:      "fadeIn 140ms ease",
    fontFamily:     "var(--font-ui)",
    color:          "var(--text-primary)",
  },
  header: {
    display:        "flex",
    alignItems:     "center",
  },
  title: {
    fontWeight:     800,
    fontSize:       "1rem",
    letterSpacing:  "0.06em",
  },
  close: {
    marginLeft:     "auto",
    background:     "transparent",
    border:         "none",
    color:          "var(--text-tertiary)",
    fontSize:       "1.2rem",
    lineHeight:     1,
    cursor:         "pointer",
    padding:        "0 0.2rem",
  },
  statusRow: {
    display:        "flex",
    alignItems:     "center",
    gap:            "0.6rem",
  },
  bigDot: {
    width:          "18px",
    height:         "18px",
    borderRadius:   "50%",
    flexShrink:     0,
  },
  statusWord: {
    fontWeight:     800,
    fontSize:       "1.45rem",
    letterSpacing:  "0.04em",
    lineHeight:     1,
  },
  subline: {
    fontSize:       "0.8rem",
    color:          "var(--text-secondary)",
    marginTop:      "-0.35rem",
  },
  rows: {
    display:        "grid",
    gridTemplateColumns: "auto auto 1fr",
    columnGap:      "0.5rem",
    rowGap:         "0.2rem",
    margin:         0,
    fontSize:       "0.92rem",
  },
  rowLabel: {
    color:          "var(--text-secondary)",
    whiteSpace:     "nowrap",
    margin:         0,
  },
  rowColon: {
    color:          "var(--text-tertiary)",
    margin:         0,
  },
  rowValue: {
    fontFamily:     "var(--font-mono)",
    fontSize:       "0.82rem",
    fontWeight:     600,
    margin:         0,
    alignSelf:      "center",
    minWidth:       0,
    overflowWrap:   "anywhere",
  },
  qualityHead: {
    display:        "flex",
    justifyContent: "space-between",
    fontSize:       "0.85rem",
    fontWeight:     600,
    marginBottom:   "0.3rem",
  },
  track: {
    height:         "10px",
    background:     "var(--bg-elevated)",
    border:         "1px solid var(--bg-border)",
    borderRadius:   "999px",
    overflow:       "hidden",
  },
  fill: {
    height:         "100%",
    borderRadius:   "999px",
    transition:     "width 300ms ease, background 300ms ease",
  },
  problem: {
    fontSize:       "0.8rem",
    lineHeight:     1.35,
    background:     "var(--bg-elevated)",
    borderLeft:     "3px solid var(--bg-border)",
    borderRadius:   "var(--radius-sm)",
    padding:        "0.4rem 0.6rem",
  },
  hint: {
    marginTop:      "0.2rem",
    color:          "var(--text-secondary)",
    fontSize:       "0.76rem",
  },
  detailsToggle: {
    alignSelf:      "flex-start",
    background:     "transparent",
    border:         "none",
    padding:        0,
    color:          "var(--text-secondary)",
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.74rem",
    fontWeight:     700,
    letterSpacing:  "0.08em",
    cursor:         "pointer",
  },
  details: {
    display:        "flex",
    flexDirection:  "column",
    gap:            "0.35rem",
    borderTop:      "1px solid var(--bg-border)",
    paddingTop:     "0.55rem",
    marginTop:      "-0.3rem",
  },
  sectionBlock: {
    display:        "flex",
    flexDirection:  "column",
    gap:            "0.3rem",
    marginTop:      "0.3rem",
  },
  sectionHead: {
    display:        "flex",
    justifyContent: "space-between",
    alignItems:     "center",
    fontSize:       "0.7rem",
    fontWeight:     600,
    letterSpacing:  "0.06em",
    textTransform:  "uppercase",
    color:          "var(--text-secondary)",
  },
  legend: {
    display:        "flex",
    alignItems:     "center",
    gap:            "0.25rem",
    textTransform:  "none",
    letterSpacing:  0,
    fontWeight:     400,
  },
  swatch: {
    display:        "inline-block",
    width:          "8px",
    height:         "8px",
    borderRadius:   "2px",
  },
  wave: {
    width:          "100%",
    height:         `${WAVE_H}px`,
    background:     "var(--bg-elevated)",
    borderRadius:   "var(--radius-sm)",
    display:        "block",
  },
  waveEmpty: {
    height:         `${WAVE_H}px`,
    background:     "var(--bg-elevated)",
    borderRadius:   "var(--radius-sm)",
    display:        "flex",
    alignItems:     "center",
    justifyContent: "center",
    fontSize:       "0.78rem",
    color:          "var(--text-tertiary)",
  },
  rtt: {
    width:          "100%",
    height:         `${RTT_H}px`,
    background:     "var(--bg-elevated)",
    borderRadius:   "var(--radius-sm)",
    display:        "block",
  },
  axis: {
    display:        "flex",
    justifyContent: "space-between",
    fontFamily:     "var(--font-mono)",
    fontSize:       "0.62rem",
    color:          "var(--text-tertiary)",
  },
  events: {
    display:        "flex",
    flexDirection:  "column",
    gap:            "0.2rem",
    maxHeight:      "130px",
    overflowY:      "auto",
  },
  event: {
    display:        "flex",
    alignItems:     "baseline",
    gap:            "0.45rem",
    fontSize:       "0.76rem",
    color:          "var(--text-secondary)",
  },
  eventTime: {
    fontFamily:     "var(--font-mono)",
    fontSize:       "0.68rem",
    color:          "var(--text-tertiary)",
    flexShrink:     0,
  },
  eventDot: {
    width:          "6px",
    height:         "6px",
    borderRadius:   "50%",
    flexShrink:     0,
    alignSelf:      "center",
  },
  legendFoot: {
    fontSize:       "0.66rem",
    color:          "var(--text-tertiary)",
    borderTop:      "1px solid var(--bg-border)",
    paddingTop:     "0.45rem",
  },
};
