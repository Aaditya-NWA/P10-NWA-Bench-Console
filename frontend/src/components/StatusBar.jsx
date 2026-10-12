import React, { useEffect, useState } from "react";
import { TARE_STATUS, THEME, UNIT_SI } from "../constants";
import LinkHealth from "./LinkHealth";

/**
 * StatusBar — top header: logo, serial port picker, link health,
 * stability badge, tare, recording control (with a test name), unit
 * toggle, calibration settings, theme toggle, and the operator.
 *
 * Props:
 *   wsConnected     — browser ↔ backend WebSocket state
 *   stable          — stability flag from the latest frame, or undefined
 *                     when the firmware build doesn't emit it (a neutral
 *                     "N/A" badge, not a "SETTLING" that never clears)
 *   health          — the backend's link health block, rendered by LinkHealth
 *   latest / history — live telemetry; LinkHealth judges packet age and
 *                     rate from these, since /status is only polled every 2 s
 *   commandsMayBeIgnored — the ESP32 build looks like it ignores
 *                     tare/throttle commands (RPM test pass)
 *   tareStatus      — null | sending | sent | taring | done | error
 *   tareResult      — summary of the last TARE_DONE (tare.js), or null
 *   onTare
 *   ports           — [{device, description}] from /ports
 *   refreshPorts
 *   serialConnected — backend ↔ ESP32 serial link state
 *   serialPort      — connected COM port, or ""
 *   onConnectSerial / onDisconnectSerial
 *   recording / frameCount
 *   recorderInfo    — where the run is being written (csv_path etc.)
 *   onStartRecording(testName) / onStopRecording
 *   theme / onToggleTheme
 *   unitSystem / onToggleUnits
 *   onOpenSettings  — opens the calibration panel
 *   operator / onSignOut
 */
export default function StatusBar({
  wsConnected,
  stable,
  health,
  latest,
  history,
  commandsMayBeIgnored,
  tareStatus,
  tareResult,
  onTare,
  ports,
  refreshPorts,
  serialConnected,
  serialPort,
  onConnectSerial,
  onDisconnectSerial,
  recording,
  frameCount,
  recorderInfo,
  onStartRecording,
  onStopRecording,
  theme,
  onToggleTheme,
  unitSystem,
  onToggleUnits,
  onOpenSettings,
  operator,
  onSignOut,
}) {
  // Held here rather than in the hook because it's only meaningful at
  // the moment Start is pressed — the backend owns it from then on and
  // reports it back in recorderInfo.
  const [testName, setTestName] = useState("");
  const [showLogo, setShowLogo] = useState(true);

  useEffect(() => {
    refreshPorts();
  }, [refreshPorts]);

  // A tare that finished isn't automatically a good one: a cell can
  // keep its old zero (no samples) or have been disturbed while it was
  // sampled. Both are said on the button rather than buried in a log.
  const tarePartial = tareStatus === TARE_STATUS.DONE && tareResult && !tareResult.ok;
  const tareNoisy   = tareStatus === TARE_STATUS.DONE && tareResult?.ok && tareResult.noisy.length > 0;
  const tareSecs    = tareResult?.ms != null ? ` ${(tareResult.ms / 1000).toFixed(1)} s` : "";

  const tareLabel =
    tareStatus === TARE_STATUS.SENDING ? "Sending…"     :
    tareStatus === TARE_STATUS.SENT    ? "Command Sent" :
    tareStatus === TARE_STATUS.TARING  ? "Taring… hold still" :
    tarePartial                        ? `Tare partial — ${tareResult.failed.join(", ")} unchanged` :
    tareNoisy                          ? `Tare done — ${tareResult.noisy.join(", ")} noisy` :
    tareStatus === TARE_STATUS.DONE    ? `Tare Done ✓${tareSecs}` :
    tareStatus === TARE_STATUS.ERROR   ? "Serial Error" :
    "TARE ALL CELLS";

  const tareColour =
    tarePartial                        ? "var(--status-offline)"  :
    tareNoisy                          ? "var(--status-settling)" :
    tareStatus === TARE_STATUS.DONE    ? "var(--status-stable)"   :
    tareStatus === TARE_STATUS.ERROR   ? "var(--status-offline)"  :
    "var(--accent-thrust)";

  // Locked only while a tare is in flight. A finished one (even a
  // partial or noisy one, which stays on screen for a few seconds) can be
  // retried straight away — that is usually exactly what's needed.
  const tareInFlight = tareStatus === TARE_STATUS.SENDING || tareStatus === TARE_STATUS.SENT ||
                       tareStatus === TARE_STATUS.TARING;
  const tareLocked = !wsConnected || tareInFlight;

  const tareTitle =
    commandsMayBeIgnored
      ? "The connected firmware build doesn't appear to process tare/throttle commands (RPM test pass) — this may have no effect on the device."
    : tarePartial
      ? `These kept their previous zero because they returned too few samples: ${tareResult.failed.join(", ")}. Check their wiring, then tare again.`
    : tareNoisy
      ? `The zero for ${tareResult.noisy.join(", ")} is less certain than the display resolution — something was touching or shaking the rig. Tare again with it still.

${tareResult.detail}`
    : tareResult?.detail
      ? `Re-zero all four beam cells, the thrust cell and the current sensor (~2 s).

Last tare:
${tareResult.detail}`
    : "Re-zero all four beam cells, the thrust cell and the current sensor";

  const handlePortChange = (e) => {
    const port = e.target.value;
    if (port) onConnectSerial(port);              // Connect right away on selection
  };

  const handleRecordClick = () => {
    if (recording) onStopRecording();
    else onStartRecording(testName);
  };

  // A bench display is usually a dedicated screen; dropping the browser
  // chrome gives the chart back ~80px of height. Tracked from the
  // fullscreenchange EVENT rather than from what this button last did,
  // because the browser can leave fullscreen without going through it
  // at all (Esc and F11 both do).
  // Deriving it from our own click would leave the icon lying about
  // the current state.
  const [isFullscreen, setIsFullscreen] = useState(
    () => typeof document !== "undefined" && !!document.fullscreenElement
  );

  useEffect(() => {
    const sync = () => setIsFullscreen(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", sync);
    return () => document.removeEventListener("fullscreenchange", sync);
  }, []);

  const toggleFullscreen = async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.documentElement.requestFullscreen();
    } catch {
      // Chrome rejects the request outside a user gesture, and a kiosk
      // policy can block it outright. Swallowing it keeps an unhandled
      // rejection out of the console — fullscreenchange never fires, so
      // the icon correctly stays as it was.
    }
  };

  return (
    <header style={styles.bar}>
      <div style={styles.brand}>
        {showLogo ? (
          <img
            src="/logo.png"
            alt=""
            style={styles.logoImg}
            onError={() => setShowLogo(false)}   // Degrades to the text badge if public/logo.png is absent
          />
        ) : (
          <div style={styles.logo}>NWA</div>
        )}
        <div style={styles.brandText}>
          <span style={styles.productName}>NWA Bench Console</span>
          <span style={styles.productSub}>Motor Torque &amp; Thrust Test Stand</span>
        </div>
      </div>

      <div style={styles.controls}>

        {!serialConnected ? (
          <select
            style={styles.portSelect}
            onChange={handlePortChange}
            defaultValue=""
            title="Select a COM port to connect to the ESP32"
          >
            <option value="" disabled>Select COM port…</option>
            {ports.map((p) => (
              <option key={p.device} value={p.device}>
                {p.device} — {p.description}
              </option>
            ))}
          </select>
        ) : (
          <div style={styles.portConnected}>
            <span style={styles.portConnectedText}>{serialPort}</span>
            <button style={styles.disconnectBtn} onClick={onDisconnectSerial}>
              Disconnect
            </button>
          </div>
        )}

        {!serialConnected && (
          <button style={styles.refreshBtn} onClick={refreshPorts} title="Refresh port list">
            ↻
          </button>
        )}

        {/* The difference between "the port is open" and "data is
            actually arriving, intact, and commands are getting back".
            A chip here; click it for the waveform and diagnostics. */}
        <LinkHealth
          health={health}
          serialConnected={serialConnected}
          serialPort={serialPort}
          wsConnected={wsConnected}
          latest={latest}
          history={history}
        />

        {/* Neutral "N/A" when the firmware build doesn't emit "stable"
            at all, rather than a "SETTLING" that never clears */}
        <div style={{
          ...styles.badge,
          background: stable === undefined ? "rgba(150,150,150,0.10)"
                     : stable ? "rgba(76,186,106,0.12)" : "rgba(232,160,32,0.12)",
          border:     `1px solid ${stable === undefined ? "var(--text-tertiary)"
                     : stable ? "var(--status-stable)" : "var(--status-settling)"}`,
          color:      stable === undefined ? "var(--text-tertiary)"
                     : stable ? "var(--status-stable)" : "var(--status-settling)",
        }}>
          <span style={{
            ...styles.dot,
            background: stable === undefined ? "var(--text-tertiary)"
                       : stable ? "var(--status-stable)" : "var(--status-settling)",
            animation:  stable === false ? "pulse 1.2s ease-in-out infinite" : "none",
          }} />
          {stable === undefined ? "N/A" : stable ? "STABLE" : "SETTLING"}
        </div>

        {/* Browser ↔ backend link */}
        <div style={{
          ...styles.badge,
          background: wsConnected ? "rgba(76,186,106,0.10)" : "rgba(232,80,80,0.10)",
          border:     `1px solid ${wsConnected ? "var(--status-stable)" : "var(--status-offline)"}`,
          color:      wsConnected ? "var(--status-stable)" : "var(--status-offline)",
        }}>
          <span style={{
            ...styles.dot,
            background: wsConnected ? "var(--status-stable)" : "var(--status-offline)",
            animation:  wsConnected ? "none" : "pulse 1.2s ease-in-out infinite",
          }} />
          {wsConnected ? "CONNECTED" : "OFFLINE"}
        </div>

        <button
          style={{
            ...styles.tare,
            borderColor: tareColour,
            color:       tareColour,
            opacity:     tareLocked ? 0.6 : 1,
            cursor:      tareLocked ? "not-allowed" : "pointer",
          }}
          onClick={onTare}
          disabled={tareLocked}
          title={tareTitle}
        >
          {tareLabel}
        </button>
        {commandsMayBeIgnored && (
          <span style={styles.firmwareNote} title="This firmware build's USB command listener is disabled">
            ⚠ commands may be ignored
          </span>
        )}

        {/* Names the run's CSV and its metadata. Locked while
            recording: the backend already opened the file under it. */}
        <input
          style={{
            ...styles.testName,
            opacity: recording ? 0.65 : 1,
          }}
          value={recording ? (recorderInfo?.test_name || testName) : testName}
          onChange={(e) => setTestName(e.target.value)}
          placeholder="Test name (optional)"
          disabled={recording}
          spellCheck={false}
          title={recording && recorderInfo?.csv_path
            ? `Writing to ${recorderInfo.csv_path}`
            : "Names this run's CSV and Excel export — e.g. 'Prop 18x6 CW'"}
        />

        <button
          style={{
            ...styles.record,
            borderColor: recording ? "var(--status-offline)" : "var(--accent-load)",
            color:       recording ? "var(--status-offline)" : "var(--accent-load)",
          }}
          onClick={handleRecordClick}
          title={recording
            ? `Stop recording${recorderInfo?.csv_path ? ` — writing to ${recorderInfo.csv_path}` : ""}`
            : "Start logging — writes a CSV to Documents/NWA Bench Console/CSV as it records"}
        >
          <span style={{
            ...styles.recDot,
            background: recording ? "var(--status-offline)" : "var(--accent-load)",
            animation:  recording ? "pulse 1s ease-in-out infinite" : "none",
          }} />
          {recording ? `RECORDING (${frameCount})` : "START LOGGING"}
        </button>

        {/* Display only — recorded and exported data is always SI */}
        <button
          style={styles.unitBtn}
          onClick={onToggleUnits}
          title={unitSystem === UNIT_SI
            ? "Showing newtons — switch to kilogram-force (matches most propeller datasheets). Logged and exported data stays in SI either way."
            : "Showing kilogram-force — switch back to newtons. Logged and exported data is always SI."}
        >
          {unitSystem === UNIT_SI ? "N" : "kgf"}
        </button>

        {/* Next to the theme toggle rather than near Tare/Logging: it
            isn't part of a run, it's the "what is this rig calibrated
            to" control, used between runs */}
        <button
          style={styles.settingsBtn}
          onClick={onOpenSettings}
          title="Calibration settings — edit the ESP32's load cell, torque and sensor constants without reflashing"
        >
          ⚙
        </button>

        <button
          style={styles.themeToggle}
          onClick={onToggleTheme}
          title={theme === THEME.DARK ? "Switch to light mode" : "Switch to dark mode"}
        >
          {theme === THEME.DARK ? "☀" : "☾"}
        </button>

        {/* Shown because every throttle command and calibration write
            is logged against this name */}
        <div style={styles.operatorBox} title={`Signed in as ${operator} — throttle and calibration changes are logged against this name`}>
          <span style={styles.operatorName}>{operator}</span>
          <button style={styles.signOutBtn} onClick={onSignOut} title="Sign out">
            ⏻
          </button>
        </div>

        <button
          style={styles.fullscreenBtn}
          onClick={toggleFullscreen}
          title={isFullscreen
            ? "Leave fullscreen (Esc also works)"
            : "Fullscreen — hides the browser chrome and gives the chart more height"}
          aria-label={isFullscreen ? "Leave fullscreen" : "Enter fullscreen"}
          aria-pressed={isFullscreen}
        >
          {isFullscreen ? "⤡" : "⛶"}
        </button>

      </div>
    </header>
  );
}

const styles = {
  bar: {
    display:        "flex",
    alignItems:     "center",
    justifyContent: "space-between",
    padding:        "0.4rem 1.4rem",
    minHeight:      "56px",
    background:     "var(--bg-surface)",
    borderBottom:   "1px solid var(--bg-border)",
    flexShrink:     0,
    gap:            "0.8rem",
    // Wraps rather than scrolls: a control that has scrolled out of
    // sight on a narrow window is a control the operator can't find.
    flexWrap:       "wrap",
    rowGap:         "0.5rem",
  },
  brand: {
    display:        "flex",
    alignItems:     "center",
    gap:            "0.7rem",
    flexShrink:     0,
  },
  logoImg: {
    width:          "34px",
    height:         "34px",
    flexShrink:     0,
  },
  logo: {
    fontFamily:     "var(--font-mono)",
    fontWeight:     700,
    fontSize:       "0.95rem",
    color:          "var(--accent-torque)",
    background:     "rgba(232,160,32,0.12)",
    border:         "1px solid rgba(232,160,32,0.3)",
    borderRadius:   "var(--radius-sm)",
    padding:        "0.25rem 0.5rem",
    letterSpacing:  "0.04em",
    flexShrink:     0,
  },
  brandText: {
    display:        "flex",
    flexDirection:  "column",
    gap:            "1px",
  },
  productName: {
    fontFamily:     "var(--font-ui)",
    fontWeight:     800,
    fontSize:       "1.2rem",
    letterSpacing:  "0.01em",
    color:          "var(--text-primary)",
    lineHeight:     1,
    whiteSpace:     "nowrap",
  },
  productSub: {
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.72rem",
    color:          "var(--text-tertiary)",
    letterSpacing:  "0.04em",
    whiteSpace:     "nowrap",
  },
  controls: {
    display:        "flex",
    alignItems:     "center",
    gap:            "0.5rem",
    flexWrap:       "wrap",
    rowGap:         "0.4rem",
  },
  portSelect: {
    background:     "var(--bg-elevated)",
    border:         "1px solid var(--bg-border)",
    borderRadius:   "var(--radius-sm)",
    color:          "var(--text-primary)",
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.8rem",
    padding:        "0.4rem 0.6rem",
    maxWidth:       "200px",
    cursor:         "pointer",
  },
  portConnected: {
    display:        "flex",
    alignItems:     "center",
    gap:            "0.5rem",
    background:     "rgba(76,186,106,0.10)",
    border:         "1px solid var(--status-stable)",
    borderRadius:   "var(--radius-sm)",
    padding:        "0.3rem 0.5rem 0.3rem 0.7rem",
  },
  portConnectedText: {
    fontFamily:     "var(--font-mono)",
    fontSize:       "0.76rem",
    fontWeight:     600,
    color:          "var(--status-stable)",
  },
  disconnectBtn: {
    background:     "transparent",
    border:         "1px solid var(--status-offline)",
    borderRadius:   "var(--radius-sm)",
    color:          "var(--status-offline)",
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.72rem",
    fontWeight:     600,
    padding:        "0.2rem 0.5rem",
    cursor:         "pointer",
  },
  refreshBtn: {
    background:     "var(--bg-elevated)",
    border:         "1px solid var(--bg-border)",
    borderRadius:   "var(--radius-sm)",
    color:          "var(--text-secondary)",
    width:          "32px",
    height:         "32px",
    cursor:         "pointer",
    fontSize:       "0.9rem",
  },
  badge: {
    display:        "flex",
    alignItems:     "center",
    gap:            "0.4rem",
    padding:        "0.3rem 0.75rem",
    borderRadius:   "999px",
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.74rem",
    fontWeight:     700,
    letterSpacing:  "0.1em",
    whiteSpace:     "nowrap",
  },
  dot: {
    width:          "7px",
    height:         "7px",
    borderRadius:   "50%",
    flexShrink:     0,
  },
  firmwareNote: {
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.7rem",
    color:          "var(--status-settling)",
    whiteSpace:     "nowrap",
  },
  tare: {
    background:     "transparent",
    border:         "1px solid",
    borderRadius:   "var(--radius-sm)",
    padding:        "0.4rem 1rem",
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.78rem",
    fontWeight:     700,
    letterSpacing:  "0.08em",
    textTransform:  "uppercase",
    transition:     "var(--transition)",
    whiteSpace:     "nowrap",
  },
  testName: {
    background:     "var(--bg-elevated)",
    border:         "1px solid var(--bg-border)",
    borderRadius:   "var(--radius-sm)",
    color:          "var(--text-primary)",
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.8rem",
    padding:        "0.4rem 0.6rem",
    width:          "150px",
    transition:     "var(--transition)",
  },
  record: {
    background:     "transparent",
    border:         "1px solid",
    borderRadius:   "var(--radius-sm)",
    padding:        "0.4rem 0.9rem",
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.78rem",
    fontWeight:     700,
    letterSpacing:  "0.06em",
    textTransform:  "uppercase",
    display:        "flex",
    alignItems:     "center",
    gap:            "0.4rem",
    cursor:         "pointer",
    whiteSpace:     "nowrap",
  },
  recDot: {
    width:          "7px",
    height:         "7px",
    borderRadius:   "50%",
    flexShrink:     0,
  },
  // Same shape as the toggles beside it, so the utility controls at the
  // end of the bar read as one group.
  unitBtn: {
    background:     "var(--bg-elevated)",
    border:         "1px solid var(--bg-border)",
    borderRadius:   "var(--radius-sm)",
    color:          "var(--text-secondary)",
    minWidth:       "36px",
    height:         "36px",
    padding:        "0 0.4rem",
    display:        "flex",
    alignItems:     "center",
    justifyContent: "center",
    fontFamily:     "var(--font-mono)",
    fontSize:       "0.78rem",
    fontWeight:     700,
    cursor:         "pointer",
    transition:     "var(--transition)",
    flexShrink:     0,
  },
  settingsBtn: {
    background:     "var(--bg-elevated)",
    border:         "1px solid var(--bg-border)",
    borderRadius:   "var(--radius-sm)",
    color:          "var(--text-secondary)",
    width:          "36px",
    height:         "36px",
    display:        "flex",
    alignItems:     "center",
    justifyContent: "center",
    fontSize:       "1rem",
    cursor:         "pointer",
    transition:     "var(--transition)",
    flexShrink:     0,
  },
  themeToggle: {
    background:     "var(--bg-elevated)",
    border:         "1px solid var(--bg-border)",
    borderRadius:   "var(--radius-sm)",
    color:          "var(--text-secondary)",
    width:          "36px",
    height:         "36px",
    display:        "flex",
    alignItems:     "center",
    justifyContent: "center",
    fontSize:       "1rem",
    cursor:         "pointer",
    transition:     "var(--transition)",
    flexShrink:     0,
  },
  operatorBox: {
    display:        "flex",
    alignItems:     "center",
    gap:            "0.35rem",
    background:     "var(--bg-elevated)",
    border:         "1px solid var(--bg-border)",
    borderRadius:   "var(--radius-sm)",
    padding:        "0.2rem 0.25rem 0.2rem 0.6rem",
    flexShrink:     0,
  },
  operatorName: {
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.8rem",
    fontWeight:     600,
    color:          "var(--text-secondary)",
    maxWidth:       "110px",
    overflow:       "hidden",
    textOverflow:   "ellipsis",
    whiteSpace:     "nowrap",
  },
  signOutBtn: {
    background:     "transparent",
    border:         "none",
    color:          "var(--text-tertiary)",
    fontSize:       "0.9rem",
    lineHeight:     1,
    padding:        "0.25rem 0.35rem",
    cursor:         "pointer",
    borderRadius:   "var(--radius-sm)",
  },
  fullscreenBtn: {
    background:     "var(--bg-elevated)",
    border:         "1px solid var(--bg-border)",
    borderRadius:   "var(--radius-sm)",
    color:          "var(--text-secondary)",
    width:          "36px",
    height:         "36px",
    display:        "flex",
    alignItems:     "center",
    justifyContent: "center",
    fontSize:       "1rem",
    lineHeight:     1,
    cursor:         "pointer",
    transition:     "var(--transition)",
    flexShrink:     0,
  },
};
