import React, { useState, useEffect, useCallback } from "react";
import { useSerialData }  from "./hooks/useSerialData";
import { useAuth }        from "./hooks/useAuth";
import { DEFAULT_UNIT_SYSTEM, convert } from "./units";
import {
  STORAGE_THEME, STORAGE_UNITS, THEME, THEME_ATTR,
  UNIT_SI, UNIT_KGF, TAB, DIR_CW, DIR_CCW,
  CHANNEL_TORQUE, CHANNEL_THRUST, THR_MIN, CMD_LINK, LINK_VERDICT,
} from "./constants";
import LoginScreen        from "./components/LoginScreen";
import StatusBar          from "./components/StatusBar";
import MetricCard         from "./components/MetricCard";
import CellGrid           from "./components/CellGrid";
import LiveChart          from "./components/LiveChart";
import ThrottleSlider     from "./components/ThrottleSlider";
import AnalysisTab        from "./components/AnalysisTab";
import SettingsModal      from "./components/SettingsModal";

// Beyond this the IR pickup is seeing something it shouldn't — a
// reflection, a stray blade edge, or ESC noise. Below it, raw and
// filtered differ only by the filter's own lag.
const RPM_DIVERGENCE_WARN = 300;

/** Root component. Gates on an operator session, owns theme and unit
 *  state, and distributes live data + controls from useSerialData. */
export default function App() {
  const [theme, setTheme] = useState(
    () => localStorage.getItem(STORAGE_THEME) || THEME.DARK
  );

  useEffect(() => {
    document.documentElement.setAttribute(THEME_ATTR, theme);   // Triggers the CSS variable swap
    localStorage.setItem(STORAGE_THEME, theme);
  }, [theme]);

  const toggleTheme = () =>
    setTheme((t) => (t === THEME.DARK ? THEME.LIGHT : THEME.DARK));

  // Display only — never affects logged data
  const [unitSystem, setUnitSystem] = useState(
    () => localStorage.getItem(STORAGE_UNITS) || DEFAULT_UNIT_SYSTEM
  );
  useEffect(() => { localStorage.setItem(STORAGE_UNITS, unitSystem); }, [unitSystem]);
  const toggleUnits = () =>
    setUnitSystem((u) => (u === UNIT_SI ? UNIT_KGF : UNIT_SI));

  const auth = useAuth();

  const [activeTab, setActiveTab] = useState(TAB.LIVE);

  // A modal rather than a third tab: it isn't a place you work from,
  // it's a thing you change between runs, and it must stay reachable
  // from either tab.
  const [settingsOpen, setSettingsOpen] = useState(false);

  const {
    latest, history, connected, tareStatus, tareResult, sendTare, sendThrottle,
    confirmedThrottleUs,
    ports, refreshPorts, serialConnected, serialPort, serialError,
    connectSerial, disconnectSerial,
    health,
    recording, frameCount, startRecording, stopRecording,
    recorderInfo, recordingError, recordingNotice,
    profile, profileError, startProfile, stopProfile,
    calibration, calibrationStatus, calibrationError,
    fetchCalibration, saveCalibration, resetCalibration,
    // Hooks can't sit behind the auth early-return below, so the hook is
    // told explicitly not to touch the network until there's a session.
  } = useSerialData({ enabled: auth.authenticated });

  // The CUT THROTTLE button lives in the right-hand column and can be
  // scrolled out of view on a short window; a key always works. Space is
  // ignored while typing or while a button has focus, where it means
  // something else entirely.
  const cutThrottle = useCallback(() => sendThrottle(THR_MIN), [sendThrottle]);

  useEffect(() => {
    const onKeyDown = (e) => {
      if (e.key === "Escape" && settingsOpen) { setSettingsOpen(false); return; }
      if (e.code !== "Space" || e.repeat) return;
      if (settingsOpen) return;
      const t = e.target;
      const tag = t?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" ||
          tag === "BUTTON" || tag === "A" || t?.isContentEditable) return;
      e.preventDefault();                            // Stop the page scrolling instead
      cutThrottle();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [cutThrottle, settingsOpen]);

  const torque  = latest?.torque ?? null;           // N·m, signed
  const thrust  = latest?.thrust ?? null;           // N from the S-type cell
  const rpm     = latest?.rpm    ?? null;
  const rpmRaw  = latest?.rpm_raw ?? null;          // Before the firmware's smoothing
  const current = latest?.current_a ?? null;        // ADS1115 A0 (HAS-600S #1)
  const voltage = latest?.voltage_v ?? null;        // ADS1115 A1 (LV25-P #1)
  // Sensor pair 2 on ADS1115 A2/A3. The firmware always sends these but
  // publishes a hard 0.00 until the hardware is fitted and
  // SENSOR2_ENABLED is on, so they read 0 rather than "—".
  const current2 = latest?.current_a2 ?? null;
  const voltage2 = latest?.voltage_v2 ?? null;
  // Left undefined (not defaulted to false) when the frame omits it —
  // the RPM-test-pass firmware doesn't send it, and StatusBar shows a
  // neutral "N/A" badge instead of a "SETTLING" that would never clear.
  const stable  = latest?.stable;

  // That build also disables the tare/throttle command listener. We
  // infer it from the absence of `stable` in a frame we've received, and
  // surface a note rather than letting a click silently do nothing.
  const commandsMayBeIgnored = !!latest && stable === undefined;

  // Prefer the firmware's own `dir`: it applies hysteresis (TQ_HYST_KG)
  // around the changeover. Deriving it from the sign of torque — which
  // this used to do — flickered near zero, and could disagree with the
  // direction the firmware used to pick its calibration coefficients.
  const dirRaw = latest?.dir ?? (torque === null ? null : torque >= 0 ? DIR_CW : DIR_CCW);
  const spinDir = dirRaw === null ? null : dirRaw === DIR_CCW ? "CCW ↺" : "CW ↻";
  const spinCol = dirRaw === null ? null
                : dirRaw === DIR_CCW ? "var(--ccw-colour)" : "var(--cw-colour)";

  // rpm_raw was in every frame but shown nowhere, so a noisy IR pickup
  // could only be diagnosed from a log afterwards.
  const rpmDelta = (rpm !== null && rpmRaw !== null) ? rpmRaw - rpm : null;
  const rpmNoisy = rpmDelta !== null && Math.abs(rpmDelta) > RPM_DIVERGENCE_WARN;
  const rpmSub = rpmDelta === null ? null
               : `raw ${Math.round(rpmRaw)} · Δ${rpmDelta >= 0 ? "+" : ""}${Math.round(rpmDelta)}`;

  const torqueDisp = convert(unitSystem, CHANNEL_TORQUE, torque);
  const thrustDisp = convert(unitSystem, CHANNEL_THRUST, thrust);

  // Genuinely different failures that all used to look like "the numbers
  // stopped" — or, for the last one, like nothing at all: browser lost
  // the backend, backend lost the ESP32, the port is open but no frames
  // arrive, or frames arrive but commands don't get back to the board.
  //
  // A stall while the ESP32 is booting (right after connecting, or
  // after a reset) is expected and shown as a warning, not a fault. An
  // older backend sends no `verdict`, so a stall there stays an error.
  const bootStall = health?.stalled && health.verdict && health.verdict !== LINK_VERDICT.ERROR;
  const linkProblem =
    !connected      ? { text: "Disconnected from the backend — retrying every 2 seconds. The bench is not being commanded.", severity: "error" }
  : !serialConnected ? { text: "Backend is running but no ESP32 is connected — select a COM port above.", severity: "warn" }
  : bootStall        ? { text: health.issues?.[0]?.text ?? "Waiting for the ESP32 to start sending telemetry.", severity: "warn" }
  : health?.stalled  ? { text: `Serial port is open but no telemetry has arrived for ${health.last_frame_age_s ?? "?"}s — check power to the board and the TX line.`, severity: "error" }
  : health?.command_link?.state === CMD_LINK.LOST
    ? { text: "Telemetry is arriving, but the ESP32 has stopped answering — tare, throttle and CUT may not be reaching it. Check the adapter TX → ESP32 RX (GPIO44) wire.", severity: "error" }
  : null;

  // Nothing below renders until an operator is signed in — every control
  // needs a session anyway, and showing it first would present a wall of
  // failing buttons.
  if (auth.checking) {
    return <div style={styles.booting}>Starting NWA Bench Console…</div>;
  }

  if (!auth.authenticated) {
    return (
      <LoginScreen
        setupRequired={auth.setupRequired}
        busy={auth.busy}
        error={auth.error}
        backendDown={auth.backendDown}
        loginFile={auth.loginFile}
        onLogin={auth.login}
        onCreate={auth.createFirstAccount}
        onRetry={auth.refresh}
      />
    );
  }

  return (
    <div style={styles.shell}>

      <StatusBar
        wsConnected={connected}
        stable={stable}
        health={health}
        latest={latest}
        history={history}
        commandsMayBeIgnored={commandsMayBeIgnored}
        tareStatus={tareStatus}
        tareResult={tareResult}
        onTare={sendTare}
        ports={ports}
        refreshPorts={refreshPorts}
        serialConnected={serialConnected}
        serialPort={serialPort}
        onConnectSerial={connectSerial}
        onDisconnectSerial={disconnectSerial}
        recording={recording}
        frameCount={frameCount}
        recorderInfo={recorderInfo}
        onStartRecording={startRecording}
        onStopRecording={stopRecording}
        theme={theme}
        onToggleTheme={toggleTheme}
        unitSystem={unitSystem}
        onToggleUnits={toggleUnits}
        onOpenSettings={() => setSettingsOpen(true)}
        operator={auth.username}
        onSignOut={auth.logout}
      />

      {/* Edits the ESP32's runtime constants over serial; nothing is
          stored in the browser */}
      <SettingsModal
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        calibration={calibration}
        status={calibrationStatus}
        error={calibrationError}
        serialConnected={serialConnected}
        onReload={fetchCalibration}
        onSave={saveCalibration}
        onReset={resetCalibration}
      />

      {/* Persistent and full width — a header dot was too easy to miss
          during a long profile run */}
      {linkProblem && (
        <div style={{
          ...styles.linkBanner,
          background:   linkProblem.severity === "error" ? "rgba(232,80,80,0.12)" : "rgba(232,160,32,0.12)",
          borderBottom: `1px solid ${linkProblem.severity === "error" ? "var(--status-offline)" : "var(--status-settling)"}`,
          color:        linkProblem.severity === "error" ? "var(--status-offline)" : "var(--status-settling)",
        }}>
          <span style={{
            ...styles.bannerDot,
            background: "currentColor",
            animation:  "pulse 1.2s ease-in-out infinite",
          }} />
          {linkProblem.text}
        </div>
      )}

      {serialError && (
        <div style={styles.errorBanner}>
          Serial error: {serialError} — select a port above to retry.
        </div>
      )}

      {recordingError && (
        <div style={styles.errorBanner}>{recordingError}</div>
      )}

      {/* There's no download to confirm a run any more, so the path is
          the confirmation — without it Stop would look like a no-op */}
      {recordingNotice && (
        <div style={styles.savedBanner}>{recordingNotice}</div>
      )}

      <nav style={styles.tabBar}>
        <button
          style={{ ...styles.tabBtn, ...(activeTab === TAB.LIVE ? styles.tabBtnActive : {}) }}
          onClick={() => setActiveTab(TAB.LIVE)}
        >
          Live Bench
        </button>
        <button
          style={{ ...styles.tabBtn, ...(activeTab === TAB.ANALYSIS ? styles.tabBtnActive : {}) }}
          onClick={() => setActiveTab(TAB.ANALYSIS)}
        >
          Data Analysis
        </button>
      </nav>

      {activeTab === TAB.ANALYSIS && <AnalysisTab theme={theme} />}

      {activeTab === TAB.LIVE && (
      <main style={styles.main}>

        <div style={styles.metricRow}>
          <MetricCard
            label="TORQUE"
            value={torqueDisp.value}
            unit={torqueDisp.unit}
            accent="var(--accent-torque)"
            decimals={torqueDisp.decimals}
            sub={spinDir}
            subColour={spinCol}
            subBadge
          />
          <MetricCard
            label="THRUST"
            value={thrustDisp.value}
            unit={thrustDisp.unit}
            accent="var(--accent-thrust)"
            decimals={thrustDisp.decimals}
          />
          <MetricCard
            label="RPM"
            value={rpm}
            unit="rpm"
            accent="var(--cw-colour)"
            decimals={0}
            sub={rpmSub}
            subColour={rpmNoisy ? "var(--status-offline)" : "var(--text-tertiary)"}
            subTitle={rpmNoisy
              ? "Raw RPM is far from the filtered value — check the IR sensor for reflections or ESC noise"
              : "Unfiltered sensor reading and its difference from the filtered value"}
          />
          <MetricCard
            label="CURRENT"
            value={current}
            unit="A"
            accent="var(--accent-thrust)"
            decimals={2}
          />
          <MetricCard
            label="VOLTAGE"
            value={voltage}
            unit="V"
            accent="var(--accent-load)"
            decimals={2}
          />
          <MetricCard
            label="POWER"
            value={current !== null && voltage !== null ? current * voltage : null}
            unit="W"
            accent="var(--accent-torque)"
            decimals={1}
          />
          {/* Reads 0.00 until that hardware is fitted — see
              SENSOR2_ENABLED in esp32/main.c */}
          <MetricCard
            label="CURRENT 2"
            value={current2}
            unit="A"
            accent="var(--accent-thrust)"
            decimals={2}
          />
          <MetricCard
            label="VOLTAGE 2"
            value={voltage2}
            unit="V"
            accent="var(--accent-load)"
            decimals={2}
          />
        </div>

        <div style={styles.secondRow}>
          <div style={styles.chartCol}>
            <LiveChart history={history} profile={profile} unitSystem={unitSystem} />
          </div>

          <div style={styles.diagCol}>
            <ThrottleSlider
              currentThrottleUs={confirmedThrottleUs ?? latest?.throttle_us}
              onSetThrottle={sendThrottle}
              disabled={!connected}
              commandsMayBeIgnored={commandsMayBeIgnored}
              profile={profile}
              profileError={profileError}
              onStartProfile={startProfile}
              onStopProfile={stopProfile}
            />
            <CellGrid data={latest} />
          </div>
        </div>

        <footer style={styles.footer}>
          <span>Nautical Wings Aerospace Pvt. Ltd.</span>
          <span style={styles.footerDot}>·</span>
          <span>NWA Bench Console</span>
          <span style={styles.footerDot}>·</span>
          {/* The live figure comes from the backend's own frame counter,
              so a link running slow shows it here rather than claiming
              the nominal 20 Hz */}
          <span>
            {health?.frame_rate_hz ? `${health.frame_rate_hz} Hz` : "~20 Hz"} · UART Serial
          </span>
        </footer>

      </main>
      )}
    </div>
  );
}

const styles = {
  shell: {
    display:        "flex",
    flexDirection:  "column",
    height:         "100vh",
    width:          "100vw",
    overflow:       "hidden",
  },
  booting: {
    height:         "100vh",
    display:        "flex",
    alignItems:     "center",
    justifyContent: "center",
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.95rem",
    letterSpacing:  "0.06em",
    textTransform:  "uppercase",
    color:          "var(--text-tertiary)",
    background:     "var(--bg-base)",
  },
  linkBanner: {
    display:        "flex",
    alignItems:     "center",
    gap:            "0.5rem",
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.82rem",
    fontWeight:     600,
    padding:        "0.45rem 1.4rem",
    flexShrink:     0,
    animation:      "fadeIn 160ms ease",
  },
  bannerDot: {
    width:          "8px",
    height:         "8px",
    borderRadius:   "50%",
    flexShrink:     0,
  },
  errorBanner: {
    background:     "rgba(232,80,80,0.12)",
    borderBottom:   "1px solid var(--status-offline)",
    color:          "var(--status-offline)",
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.8rem",
    padding:        "0.35rem 1.4rem",
    flexShrink:     0,
  },
  savedBanner: {
    background:     "rgba(76,186,106,0.10)",
    borderBottom:   "1px solid var(--status-stable)",
    color:          "var(--status-stable)",
    fontFamily:     "var(--font-mono)",       // It's a file path — readable and selectable
    fontSize:       "0.76rem",
    padding:        "0.35rem 1.4rem",
    flexShrink:     0,
    wordBreak:      "break-all",
    animation:      "fadeIn 160ms ease",
  },
  tabBar: {
    display:        "flex",
    gap:            "0.4rem",
    padding:        "0.6rem 1.4rem 0",
    flexShrink:     0,
  },
  tabBtn: {
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.86rem",
    fontWeight:     600,
    letterSpacing:  "0.04em",
    color:          "var(--text-secondary)",
    background:     "transparent",
    border:         "1px solid var(--bg-border)",
    borderBottom:   "none",
    borderRadius:   "var(--radius-sm) var(--radius-sm) 0 0",
    padding:        "0.45rem 1.1rem",
    cursor:         "pointer",
    transition:     "var(--transition)",
  },
  tabBtnActive: {
    color:          "var(--text-primary)",
    background:     "var(--bg-surface)",
    borderColor:    "var(--bg-border)",
  },
  main: {
    flex:           1,
    overflow:       "hidden",                 // Everything below is sized to fit — no page scroll
    padding:        "0.9rem 1.4rem",
    display:        "flex",
    flexDirection:  "column",
    gap:            "0.7rem",
    minHeight:      0,
  },
  metricRow: {
    display:        "flex",
    flexWrap:       "wrap",
    gap:            "0.7rem",
    flexShrink:     0,
  },
  secondRow: {
    display:        "flex",
    gap:            "0.9rem",
    flex:           1,
    minHeight:      0,
  },
  chartCol: {
    flex:           "1 1 0",
    display:        "flex",
    flexDirection:  "column",
    minWidth:       0,
    minHeight:      0,
  },
  diagCol: {
    width:          "260px",
    flexShrink:     0,
    display:        "flex",
    flexDirection:  "column",
    gap:            "0.7rem",
    minHeight:      0,
    overflowY:      "auto",                   // Safety net only — sized to fit in normal use
  },
  footer: {
    display:        "flex",
    gap:            "0.5rem",
    justifyContent: "center",
    alignItems:     "center",
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.72rem",
    letterSpacing:  "0.04em",
    color:          "var(--text-tertiary)",
    padding:        "0.15rem 0 0",
    flexShrink:     0,
  },
  footerDot: {
    color:          "var(--bg-border)",
  },
};
