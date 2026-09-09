import React, { useState, useCallback, useRef } from "react";
import { THR_MIN, THR_MAX, PHASE, NO_VALUE } from "../constants";

const SEND_THROTTLE_MS  = 80;   // Minimum gap between sends while dragging, so serial isn't flooded
const DEFAULT_RAMP_RATE = 100;  // µs/s — a full 1000→2000µs ramp takes 10 s at this rate
const DEFAULT_HOLD_S    = 5;
const STEP_SEED_US      = 1200; // First step, and the fallback when the previous one is unparseable
const STEP_INCREMENT_US = 200;  // Each added step starts this far above the one before

/**
 * ThrottleSlider — manual throttle for the U15II KV100 via ESC on
 * GPIO21, plus the automated profile builder.
 *
 * No arm/disarm gate in the UI: the ESC's own arming handshake runs
 * automatically on ESP32 boot.
 *
 * A range input can only reach a value by passing through every
 * intermediate one, and each gets sent as its own command. For an
 * instant jump to an exact value use the numeric field + SET, which
 * sends exactly one command (esc_set_throttle_us has no ramp).
 *
 * The profile sequence runs server-side (backend/main.py) so it
 * survives a page refresh or a closed tab; this component only builds
 * the plan and renders progress.
 *
 * Props:
 *   currentThrottleUs — last confirmed value from the ESP32
 *   onSetThrottle     — callback(us)
 *   disabled          — serial isn't connected
 *   commandsMayBeIgnored — the connected firmware build looks like it
 *                     ignores throttle commands (RPM test pass). Shown
 *                     as a note rather than hard-disabling the control,
 *                     since a future flash may re-enable it.
 *   profile / profileError / onStartProfile / onStopProfile
 */
export default function ThrottleSlider({
  currentThrottleUs,
  onSetThrottle,
  disabled,
  commandsMayBeIgnored,
  profile,
  profileError,
  onStartProfile,
  onStopProfile,
}) {
  const [sliderUs, setSliderUs] = useState(THR_MIN);
  const [exactInput, setExactInput] = useState("");
  const lastSentRef = useRef(0);

  const [steps, setSteps] = useState([{ us: String(STEP_SEED_US), hold_s: String(DEFAULT_HOLD_S) }]);
  const [rampRate, setRampRate] = useState(String(DEFAULT_RAMP_RATE));

  const profileRunning = !!profile?.running;

  const usToPercent = (us) =>
    Math.round(((us - THR_MIN) / (THR_MAX - THR_MIN)) * 100);

  const handleChange = useCallback((e) => {
    const us = parseInt(e.target.value, 10);
    setSliderUs(us);                                            // Local update first, so dragging stays smooth

    const now = performance.now();
    if (now - lastSentRef.current >= SEND_THROTTLE_MS) {
      lastSentRef.current = now;
      onSetThrottle(us);
    }
  }, [onSetThrottle]);

  // Guarantees the exact released value reaches the ESC even if the
  // rate limit swallowed the last onChange.
  const handleCommit = useCallback(() => {
    lastSentRef.current = performance.now();
    onSetThrottle(sliderUs);
  }, [sliderUs, onSetThrottle]);

  const applyExactValue = useCallback(() => {
    const us = parseInt(exactInput, 10);
    if (Number.isNaN(us)) return;
    const clamped = Math.min(THR_MAX, Math.max(THR_MIN, us));   // Firmware clamps too
    setSliderUs(clamped);
    lastSentRef.current = performance.now();
    onSetThrottle(clamped);
    setExactInput("");
  }, [exactInput, onSetThrottle]);

  const handleExactKeyDown = useCallback((e) => {
    if (e.key === "Enter") applyExactValue();
  }, [applyExactValue]);

  // Also aborts a running profile: the backend treats any manual
  // throttle command as an operator takeover.
  const handleCutThrottle = useCallback(() => {
    setSliderUs(THR_MIN);
    onSetThrottle(THR_MIN);
  }, [onSetThrottle]);

  const addStep = useCallback(() => {
    setSteps((prev) => {
      // Seed above the previous target — a step-up sweep is what this
      // almost always wants: 1200 → 1400 → 1600 …
      const last = prev[prev.length - 1];
      const lastUs = last ? parseInt(last.us, 10) : THR_MIN;
      const next = Number.isNaN(lastUs) ? STEP_SEED_US : Math.min(THR_MAX, lastUs + STEP_INCREMENT_US);
      return [...prev, { us: String(next), hold_s: last?.hold_s ?? String(DEFAULT_HOLD_S) }];
    });
  }, []);

  const removeStep = useCallback((idx) => {
    setSteps((prev) => (prev.length <= 1 ? prev : prev.filter((_, i) => i !== idx)));
  }, []);

  const updateStep = useCallback((idx, field, value) => {
    setSteps((prev) => prev.map((s, i) => (i === idx ? { ...s, [field]: value } : s)));
  }, []);

  // Mirrors the backend's _validate_profile so the operator sees the
  // error inline instead of after a round trip. The backend still
  // re-checks — this is a convenience, not the safety boundary.
  const stepErrors = steps.map((s) => {
    const us = parseInt(s.us, 10);
    const hold = parseFloat(s.hold_s);
    if (Number.isNaN(us) || us < THR_MIN || us > THR_MAX) return `must be ${THR_MIN}-${THR_MAX}µs`;
    if (Number.isNaN(hold) || hold <= 0) return "hold must be > 0 s";
    return null;
  });
  const rampRateNum = parseFloat(rampRate);
  const rampRateError =
    Number.isNaN(rampRateNum) || rampRateNum <= 0 ? "ramp rate must be > 0" : null;
  const profileValid = stepErrors.every((e) => e === null) && !rampRateError;

  // Total run time, so the operator knows what they're committing to
  // before pressing RUN. Each ramp leg is |Δµs| / rate seconds,
  // including the final one back down to idle.
  const estimatedSeconds = (() => {
    if (!profileValid) return null;
    let total = 0;
    let cur = THR_MIN;
    for (const s of steps) {
      const target = parseInt(s.us, 10);
      total += Math.abs(target - cur) / rampRateNum;
      total += parseFloat(s.hold_s);
      cur = target;
    }
    total += Math.abs(cur - THR_MIN) / rampRateNum;
    return total;
  })();

  const handleRunProfile = useCallback(() => {
    if (!profileValid) return;
    onStartProfile(
      steps.map((s) => ({ us: parseInt(s.us, 10), hold_s: parseFloat(s.hold_s) })),
      rampRateNum
    );
  }, [profileValid, steps, rampRateNum, onStartProfile]);

  const phaseLabel = (() => {
    if (!profile) return null;
    const n = profile.step_count ?? 0;
    const i = (profile.step_index ?? 0) + 1;
    switch (profile.phase) {
      case PHASE.RAMPING:      return `Ramping to ${profile.target_us}µs — step ${i}/${n}`;
      case PHASE.HOLDING:      return `Holding ${profile.target_us}µs — step ${i}/${n} · ${profile.hold_remaining_s?.toFixed(1)}s left`;
      case PHASE.RAMPING_DOWN: return "Ramping down to idle";
      case PHASE.DONE:         return "Profile complete ✓";
      case PHASE.ABORTED:      return profile.message || "Profile stopped";
      case PHASE.ERROR:        return `Profile error: ${profile.message}`;
      default:                 return null;
    }
  })();

  return (
    <div style={styles.wrapper}>
      <div style={styles.header}>
        <span style={styles.heading}>THROTTLE — U15II KV100</span>
        <span style={styles.percent}>{usToPercent(sliderUs)}%</span>
      </div>
      {commandsMayBeIgnored && (
        <span
          style={styles.warnNote}
          title="This firmware build's ESC init and USB command listener are disabled (RPM test pass) — the slider will not move the motor."
        >
          ⚠ ESC control disabled in current firmware build
        </span>
      )}

      <input
        type="range"
        min={THR_MIN}
        max={THR_MAX}
        step={1}
        value={sliderUs}
        onChange={handleChange}
        onMouseUp={handleCommit}
        onTouchEnd={handleCommit}
        disabled={disabled}
        style={styles.slider}
      />

      {/* One instant command — bypasses dragging through intermediates */}
      <div style={styles.exactRow}>
        <input
          type="number"
          min={THR_MIN}
          max={THR_MAX}
          step={1}
          placeholder={`${THR_MIN}-${THR_MAX}`}
          value={exactInput}
          onChange={(e) => setExactInput(e.target.value)}
          onKeyDown={handleExactKeyDown}
          disabled={disabled}
          style={styles.exactInput}
        />
        <button
          style={styles.exactBtn}
          onClick={applyExactValue}
          disabled={disabled || exactInput === ""}
          title="Jump straight to this value — sent as a single command, no intermediate steps"
        >
          SET
        </button>
      </div>

      <div style={styles.profileBlock}>
        <div style={styles.profileHeader}>
          <span style={styles.heading}>AUTO PROFILE</span>
          <span style={styles.profileHint}>
            {estimatedSeconds !== null ? `≈ ${estimatedSeconds.toFixed(0)}s total` : NO_VALUE}
          </span>
        </div>

        {steps.map((s, i) => {
          const err = stepErrors[i];
          const isActive = profileRunning && profile?.step_index === i;
          return (
            <div key={i} style={styles.stepRow}>
              <span style={{
                ...styles.stepNum,
                ...(isActive ? styles.stepNumActive : {}),
              }}>
                {i + 1}
              </span>
              <input
                type="number"
                min={THR_MIN}
                max={THR_MAX}
                step={10}
                value={s.us}
                onChange={(e) => updateStep(i, "us", e.target.value)}
                disabled={profileRunning}
                title={`Target throttle for step ${i + 1} (${THR_MIN}-${THR_MAX}µs)`}
                style={{
                  ...styles.stepInput,
                  borderColor: err ? "var(--status-offline)" : "var(--bg-border)",
                }}
              />
              <span style={styles.stepUnit}>µs</span>
              <input
                type="number"
                min={0.1}
                step={0.5}
                value={s.hold_s}
                onChange={(e) => updateStep(i, "hold_s", e.target.value)}
                disabled={profileRunning}
                title={`How long to hold at this throttle`}
                style={{
                  ...styles.stepInput,
                  borderColor: err ? "var(--status-offline)" : "var(--bg-border)",
                }}
              />
              <span style={styles.stepUnit}>s</span>
              <button
                style={{
                  ...styles.stepRemove,
                  opacity: steps.length <= 1 || profileRunning ? 0.3 : 1,
                }}
                onClick={() => removeStep(i)}
                disabled={steps.length <= 1 || profileRunning}
                title="Remove this step"
              >
                ×
              </button>
            </div>
          );
        })}

        {stepErrors.some((e) => e) && (
          <span style={styles.errText}>
            Step {stepErrors.findIndex((e) => e) + 1}: {stepErrors.find((e) => e)}
          </span>
        )}

        <div style={styles.profileControls}>
          <button
            style={styles.addBtn}
            onClick={addStep}
            disabled={profileRunning}
            title="Add another throttle step to the sequence"
          >
            + STEP
          </button>
          <input
            type="number"
            min={1}
            step={10}
            value={rampRate}
            onChange={(e) => setRampRate(e.target.value)}
            disabled={profileRunning}
            title="Ramp rate — how fast throttle moves between steps, and back down at the end"
            style={{
              ...styles.rampInput,
              borderColor: rampRateError ? "var(--status-offline)" : "var(--bg-border)",
            }}
          />
          <span style={styles.stepUnit}>µs/s</span>
        </div>

        <button
          style={{
            ...styles.runBtn,
            ...(profileRunning ? styles.runBtnStop : {}),
            opacity: !profileRunning && (disabled || !profileValid) ? 0.5 : 1,
            cursor: !profileRunning && (disabled || !profileValid) ? "not-allowed" : "pointer",
          }}
          onClick={profileRunning ? onStopProfile : handleRunProfile}
          disabled={!profileRunning && (disabled || !profileValid)}
          title={profileRunning
            ? "Abort the profile and cut throttle to idle"
            : "Run the sequence: ramp → hold → next step → ramp down to idle"}
        >
          {profileRunning ? "■ STOP PROFILE" : "▶ RUN PROFILE"}
        </button>

        {phaseLabel && (
          <span style={{
            ...styles.phaseText,
            color: profile?.phase === PHASE.ERROR   ? "var(--status-offline)"
                 : profile?.phase === PHASE.DONE    ? "var(--status-stable)"
                 : profile?.phase === PHASE.ABORTED ? "var(--status-settling)"
                 : "var(--cw-colour)",
          }}>
            {phaseLabel}
          </span>
        )}
        {profileError && <span style={styles.errText}>{profileError}</span>}
      </div>

      <div style={styles.footer}>
        <span style={styles.usReadout}>
          {sliderUs} µs <span style={styles.usLabel}>commanded</span>
        </span>
        <span style={styles.usReadout}>
          {currentThrottleUs ?? NO_VALUE} µs <span style={styles.usLabel}>confirmed</span>
        </span>
        <button
          style={styles.cutBtn}
          onClick={handleCutThrottle}
          disabled={disabled}
          title="Immediately set throttle to 0%. Also bound to the spacebar, which works anywhere on the page — this button can be scrolled out of view, a keypress can't."
        >
          CUT THROTTLE <span style={styles.cutKey}>SPACE</span>
        </button>
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
  header: {
    display:        "flex",
    justifyContent: "space-between",
    alignItems:     "baseline",
  },
  warnNote: {
    fontFamily:    "var(--font-ui)",
    fontSize:      "0.6rem",
    color:         "var(--status-settling)",
  },
  heading: {
    fontFamily:    "var(--font-ui)",
    fontSize:      "0.64rem",
    fontWeight:    600,
    letterSpacing: "0.1em",
    textTransform: "uppercase",
    color:         "var(--text-tertiary)",
  },
  percent: {
    fontFamily:    "var(--font-mono)",
    fontSize:      "1.3rem",
    fontWeight:    600,
    color:         "var(--cw-colour)",           // Reuses the amber CW accent
    lineHeight:    1,
  },
  slider: {
    width:         "100%",
    height:        "6px",
    accentColor:   "var(--cw-colour)",
    cursor:        "pointer",
  },
  exactRow: {
    display:       "flex",
    gap:           "0.4rem",
  },
  exactInput: {
    flex:          1,
    minWidth:      0,
    background:    "var(--bg-elevated)",
    border:        "1px solid var(--bg-border)",
    borderRadius:  "var(--radius-sm)",
    color:         "var(--text-primary)",
    fontFamily:    "var(--font-mono)",
    fontSize:      "0.72rem",
    padding:       "0.3rem 0.5rem",
  },
  exactBtn: {
    background:     "rgba(232,160,32,0.10)",
    border:         "1px solid var(--cw-colour)",
    borderRadius:   "var(--radius-sm)",
    color:          "var(--cw-colour)",
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.68rem",
    fontWeight:     700,
    letterSpacing:  "0.06em",
    padding:        "0.3rem 0.8rem",
    cursor:         "pointer",
    whiteSpace:     "nowrap",
  },
  profileBlock: {
    display:       "flex",
    flexDirection: "column",
    gap:           "0.35rem",
    paddingTop:    "0.5rem",
    borderTop:     "1px solid var(--bg-border)",
  },
  profileHeader: {
    display:        "flex",
    justifyContent: "space-between",
    alignItems:     "baseline",
  },
  profileHint: {
    fontFamily:    "var(--font-mono)",
    fontSize:      "0.62rem",
    color:         "var(--text-tertiary)",
  },
  stepRow: {
    display:       "flex",
    alignItems:    "center",
    gap:           "0.25rem",
  },
  stepNum: {
    fontFamily:    "var(--font-mono)",
    fontSize:      "0.62rem",
    fontWeight:    700,
    color:         "var(--text-tertiary)",
    width:         "14px",
    flexShrink:    0,
    textAlign:     "center",
  },
  stepNumActive: {
    color:         "var(--cw-colour)",           // Whichever step the backend is executing
  },
  stepInput: {
    flex:          1,
    minWidth:      0,
    width:         "100%",
    background:    "var(--bg-elevated)",
    border:        "1px solid var(--bg-border)",
    borderRadius:  "var(--radius-sm)",
    color:         "var(--text-primary)",
    fontFamily:    "var(--font-mono)",
    fontSize:      "0.68rem",
    padding:       "0.22rem 0.35rem",
  },
  stepUnit: {
    fontFamily:    "var(--font-ui)",
    fontSize:      "0.58rem",
    color:         "var(--text-tertiary)",
    flexShrink:    0,
  },
  stepRemove: {
    background:    "transparent",
    border:        "1px solid var(--bg-border)",
    borderRadius:  "var(--radius-sm)",
    color:         "var(--status-offline)",
    fontSize:      "0.75rem",
    lineHeight:    1,
    padding:       "0.15rem 0.35rem",
    cursor:        "pointer",
    flexShrink:    0,
  },
  profileControls: {
    display:       "flex",
    alignItems:    "center",
    gap:           "0.3rem",
  },
  addBtn: {
    background:    "rgba(232,160,32,0.10)",
    border:        "1px dashed var(--cw-colour)",
    borderRadius:  "var(--radius-sm)",
    color:         "var(--cw-colour)",
    fontFamily:    "var(--font-ui)",
    fontSize:      "0.64rem",
    fontWeight:    700,
    letterSpacing: "0.05em",
    padding:       "0.25rem 0.5rem",
    cursor:        "pointer",
    whiteSpace:    "nowrap",
  },
  rampInput: {
    flex:          1,
    minWidth:      0,
    background:    "var(--bg-elevated)",
    border:        "1px solid var(--bg-border)",
    borderRadius:  "var(--radius-sm)",
    color:         "var(--text-primary)",
    fontFamily:    "var(--font-mono)",
    fontSize:      "0.68rem",
    padding:       "0.22rem 0.35rem",
  },
  runBtn: {
    background:    "rgba(76,186,106,0.10)",
    border:        "1px solid var(--status-stable)",
    borderRadius:  "var(--radius-sm)",
    color:         "var(--status-stable)",
    fontFamily:    "var(--font-ui)",
    fontSize:      "0.68rem",
    fontWeight:    700,
    letterSpacing: "0.06em",
    padding:       "0.35rem 0.6rem",
    cursor:        "pointer",
  },
  runBtnStop: {
    background:    "rgba(232,80,80,0.10)",
    borderColor:   "var(--status-offline)",
    color:         "var(--status-offline)",
  },
  phaseText: {
    fontFamily:    "var(--font-mono)",
    fontSize:      "0.62rem",
    lineHeight:    1.3,
  },
  errText: {
    fontFamily:    "var(--font-ui)",
    fontSize:      "0.6rem",
    color:         "var(--status-offline)",
  },
  footer: {
    display:        "flex",
    justifyContent: "space-between",
    alignItems:     "center",
    gap:            "0.6rem",
    flexWrap:       "wrap",
  },
  usReadout: {
    fontFamily:    "var(--font-mono)",
    fontSize:      "0.72rem",
    color:         "var(--text-secondary)",
  },
  usLabel: {
    fontFamily:    "var(--font-ui)",
    fontSize:      "0.62rem",
    color:         "var(--text-tertiary)",
    textTransform: "uppercase",
    letterSpacing: "0.06em",
  },
  cutBtn: {
    background:     "rgba(232,80,80,0.10)",
    border:         "1px solid var(--status-offline)",
    borderRadius:   "var(--radius-sm)",
    color:          "var(--status-offline)",
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.74rem",
    fontWeight:     700,
    letterSpacing:  "0.06em",
    padding:        "0.4rem 0.8rem",
    cursor:         "pointer",
    whiteSpace:     "nowrap",
    display:        "flex",
    alignItems:     "center",
    gap:            "0.4rem",
  },
  cutKey: {
    fontFamily:     "var(--font-mono)",
    fontSize:       "0.6rem",
    fontWeight:     600,
    letterSpacing:  "0.04em",
    border:         "1px solid currentColor",
    borderRadius:   "3px",
    padding:        "0 0.25rem",
    opacity:        0.75,
  },
};
