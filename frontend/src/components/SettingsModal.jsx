import React, { useEffect, useMemo, useState } from "react";
import { CAL_STATUS, NO_VALUE, SETTINGS_TAB } from "../constants";
import FirmwarePanel from "./FirmwarePanel";
import UpdatesPanel from "./UpdatesPanel";

/**
 * SettingsModal — editor for the ESP32's runtime calibration table.
 *
 * The device owns these values and persists them in its own NVS. This
 * panel never stores a calibration: it renders what the ESP32 last
 * reported, sends edits through the backend, and re-renders the
 * device's read-back — so a value the firmware refused visibly stays at
 * its old number instead of looking saved while the rig runs something
 * else. The field list comes from /calibration rather than a hardcoded
 * table here, so adding a field means touching main.c and main.py only.
 *
 * Props:
 *   open / onClose
 *   calibration     — { fields: [...], values: {k:v}, defaults: {k:v} }
 *   status          — idle | loading | saving | saved | error
 *   error           — last error string from the hook, or ""
 *   serialConnected — edits need the ESP32 link up
 *   onReload        — re-read the table from the device
 *   onSave          — (changedValues) => Promise<boolean>
 *   onReset         — revert every value to the firmware default
 */
export default function SettingsModal({
  open,
  onClose,
  calibration,
  status,
  error,
  serialConnected,
  onReload,
  onSave,
  onReset,
}) {
  const { fields = [], values = {}, defaults = {} } = calibration ?? {};

  // Draft edits keyed like `values`, held as STRINGS so the operator can
  // type "1", "1.", "1.8e4" or clear the box without the input fighting
  // them — a number-typed draft would snap "18178." back mid-keystroke.
  const [draft, setDraft] = useState({});
  const [confirmingReset, setConfirmingReset] = useState(false);

  // Which tab is showing. Calibration is the default because it is what
  // the panel is opened for between runs; updates are occasional.
  const [tab, setTab] = useState(SETTINGS_TAB.CALIBRATION);

  // Merges rather than replaces: an unrelated background dump must not
  // wipe half-typed input for a key the operator is mid-edit on.
  useEffect(() => {
    if (!open) return;
    setDraft((prev) => {
      const next = { ...prev };
      for (const f of fields) {
        if (next[f.key] === undefined && values[f.key] !== undefined) {
          next[f.key] = String(values[f.key]);
        }
      }
      return next;
    });
  }, [open, fields, values]);

  useEffect(() => {
    if (open) {
      setDraft({});                     // Leftover edits lose to the device's values
      setConfirmingReset(false);
      onReload?.();
    }
  }, [open, onReload]);

  // Mirrors the firmware's own checks; the firmware still validates
  const problems = useMemo(() => {
    const out = {};
    for (const f of fields) {
      const raw = draft[f.key];
      if (raw === undefined) continue;
      const trimmed = String(raw).trim();
      if (trimmed === "") { out[f.key] = "Required"; continue; }
      const n = Number(trimmed);
      if (!Number.isFinite(n)) { out[f.key] = "Not a number"; continue; }
      if (n < f.min || n > f.max) {
        out[f.key] = `Must be between ${fmt(f.min)} and ${fmt(f.max)}`;
      }
    }
    return out;
  }, [draft, fields]);

  // Comparing parsed numbers rather than strings means retyping
  // "18178.50" over "18178.5" doesn't trigger a pointless flash write.
  const changed = useMemo(() => {
    const out = {};
    for (const f of fields) {
      const raw = draft[f.key];
      if (raw === undefined || problems[f.key]) continue;
      const n = Number(String(raw).trim());
      if (values[f.key] === undefined || n !== Number(values[f.key])) out[f.key] = n;
    }
    return out;
  }, [draft, fields, values, problems]);

  const changedCount = Object.keys(changed).length;
  const hasProblems  = Object.keys(problems).length > 0;
  const busy         = status === CAL_STATUS.LOADING || status === CAL_STATUS.SAVING;

  // Groups the flat list into the sections the backend named, keeping
  // the order fields arrive in.
  const groups = useMemo(() => {
    const order = [];
    const byGroup = new Map();
    for (const f of fields) {
      if (!byGroup.has(f.group)) { byGroup.set(f.group, []); order.push(f.group); }
      byGroup.get(f.group).push(f);
    }
    return order.map((name) => ({ name, fields: byGroup.get(name) }));
  }, [fields]);

  if (!open) return null;

  const handleSave = async () => {
    if (!changedCount || hasProblems) return;
    const ok = await onSave?.(changed);
    if (ok) setDraft({});               // Re-seed from the device's read-back
  };

  const handleReset = async () => {
    if (!confirmingReset) { setConfirmingReset(true); return; }
    setConfirmingReset(false);
    await onReset?.();
    setDraft({});
  };

  const statusText =
    status === CAL_STATUS.LOADING ? "Reading from device…" :
    status === CAL_STATUS.SAVING  ? "Writing to device…"   :
    status === CAL_STATUS.SAVED   ? "Saved to device ✓"    :
    changedCount ? `${changedCount} unsaved change${changedCount > 1 ? "s" : ""}` :
    "";

  return (
    <div style={styles.overlay} onClick={onClose}>
      <div style={styles.panel} onClick={(e) => e.stopPropagation()}>

        <header style={styles.header}>
          <div>
            <h2 style={styles.title}>Settings</h2>
            <p style={styles.subtitle}>
              {tab === SETTINGS_TAB.CALIBRATION
                ? "Live values on the ESP32. Saved to the device's flash — they survive a power cycle, and no reflash is needed."
                : tab === SETTINGS_TAB.FIRMWARE
                ? "Download the ESP32 firmware source from GitHub."
                : "Check for and install a new version of the console."}
            </p>
          </div>
          <button style={styles.closeBtn} onClick={onClose} title="Close">✕</button>
        </header>

        <nav style={styles.tabs}>
          {[
            [SETTINGS_TAB.CALIBRATION, "Calibration"],
            [SETTINGS_TAB.FIRMWARE,    "Firmware"],
            [SETTINGS_TAB.UPDATES,     "Updates"],
          ].map(([key, label]) => (
            <button
              key={key}
              style={{
                ...styles.tab,
                ...(tab === key ? styles.tabActive : null),
              }}
              onClick={() => setTab(key)}
            >
              {label}
            </button>
          ))}
        </nav>

        {tab === SETTINGS_TAB.CALIBRATION && !serialConnected && (
          <div style={{ ...styles.banner, ...styles.bannerWarn }}>
            Not connected to the ESP32 — values below may be stale, and edits can't be saved.
          </div>
        )}
        {tab === SETTINGS_TAB.CALIBRATION && error && (
          <div style={{ ...styles.banner, ...styles.bannerError }}>{error}</div>
        )}

        <div style={styles.body}>
          {tab === SETTINGS_TAB.FIRMWARE && <FirmwarePanel />}
          {tab === SETTINGS_TAB.UPDATES && <UpdatesPanel />}

          {tab === SETTINGS_TAB.CALIBRATION && (<>
          {groups.length === 0 && (
            <p style={styles.empty}>
              {busy ? "Reading calibration from the device…"
                    : "No calibration data — connect to the ESP32 and reload."}
            </p>
          )}

          {groups.map((g) => (
            <section key={g.name} style={styles.group}>
              <h3 style={styles.groupTitle}>{g.name}</h3>
              <div style={styles.grid}>
                {g.fields.map((f) => {
                  const raw       = draft[f.key] ?? "";
                  const problem   = problems[f.key];
                  const isChanged = Object.prototype.hasOwnProperty.call(changed, f.key);
                  const def       = defaults[f.key];
                  const atDefault = def !== undefined && Number(raw) === Number(def);

                  return (
                    <div key={f.key} style={styles.field}>
                      <label style={styles.label} htmlFor={`cal-${f.key}`}>
                        {f.label}
                        {f.unit && <span style={styles.unit}> ({f.unit})</span>}
                      </label>
                      <input
                        id={`cal-${f.key}`}
                        style={{
                          ...styles.input,
                          borderColor: problem   ? "var(--status-offline)"
                                     : isChanged ? "var(--accent-torque)"
                                     : "var(--bg-border)",
                        }}
                        value={raw}
                        inputMode="decimal"
                        spellCheck={false}
                        onChange={(e) =>
                          setDraft((d) => ({ ...d, [f.key]: e.target.value }))
                        }
                        title={f.key}
                      />
                      <div style={styles.fieldFoot}>
                        {problem ? (
                          <span style={styles.problem}>{problem}</span>
                        ) : isChanged ? (
                          /* Showing what a value replaces makes a
                             slipped decimal point obvious BEFORE Save,
                             not after it reached the device's flash. */
                          <span style={styles.diffHint}>
                            <span style={styles.diffOld}>
                              {values[f.key] === undefined ? "unset" : fmt(Number(values[f.key]))}
                            </span>
                            <span style={styles.diffArrow}>→</span>
                            <span style={styles.diffNew}>{fmt(changed[f.key])}</span>
                          </span>
                        ) : (
                          <span style={styles.defaultHint}>
                            firmware default {def === undefined ? NO_VALUE : fmt(def)}
                            {def !== undefined && !atDefault && (
                              <button
                                style={styles.revertBtn}
                                onClick={() =>
                                  setDraft((d) => ({ ...d, [f.key]: String(def) }))
                                }
                                title={`Put ${f.key} back to the value compiled into the firmware`}
                              >
                                revert
                              </button>
                            )}
                          </span>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </section>
          ))}
          </>)}
        </div>

        {/* Save / Reload / Reset act on the calibration table only, so
            they are part of that tab rather than the modal chrome. */}
        {tab === SETTINGS_TAB.CALIBRATION && (
        <footer style={styles.footer}>
          <span style={{
            ...styles.status,
            color: status === CAL_STATUS.SAVED ? "var(--status-stable)"
                 : status === CAL_STATUS.ERROR ? "var(--status-offline)"
                 : "var(--text-secondary)",
          }}>
            {statusText}
          </span>

          <div style={styles.footerBtns}>
            <button
              style={styles.secondaryBtn}
              onClick={onReload}
              disabled={busy}
              title="Discard edits and re-read the table from the ESP32"
            >
              Reload
            </button>
            <button
              style={{
                ...styles.secondaryBtn,
                borderColor: confirmingReset ? "var(--status-offline)" : "var(--bg-border)",
                color:       confirmingReset ? "var(--status-offline)" : "var(--text-secondary)",
              }}
              onClick={handleReset}
              onBlur={() => setConfirmingReset(false)}
              disabled={busy || !serialConnected}
              title="Restore every value to the constant compiled into the firmware"
            >
              {confirmingReset ? "Confirm reset — all fields" : "Reset to firmware defaults"}
            </button>
            <button
              style={{
                ...styles.primaryBtn,
                opacity: !changedCount || hasProblems || busy || !serialConnected ? 0.5 : 1,
                cursor:  !changedCount || hasProblems || busy || !serialConnected ? "not-allowed" : "pointer",
              }}
              onClick={handleSave}
              disabled={!changedCount || hasProblems || busy || !serialConnected}
            >
              {status === CAL_STATUS.SAVING ? "Saving…" : `Save${changedCount ? ` (${changedCount})` : ""}`}
            </button>
          </div>
        </footer>
        )}
      </div>
    </div>
  );
}

// Values span 0.28 to 18178.5, so plain toString is right for the
// common case; exponent form only kicks in for the range bounds (1e7).
function fmt(v) {
  if (typeof v !== "number" || !Number.isFinite(v)) return String(v);
  return Math.abs(v) >= 1e6 ? v.toExponential(0) : String(v);
}

const styles = {
  overlay: {
    position:       "fixed",
    inset:          0,
    background:     "rgba(0,0,0,0.55)",
    display:        "flex",
    alignItems:     "center",
    justifyContent: "center",
    padding:        "2rem 1rem",
    zIndex:         50,
  },
  panel: {
    background:     "var(--bg-surface)",
    border:         "1px solid var(--bg-border)",
    borderRadius:   "var(--radius-lg)",
    boxShadow:      "var(--shadow-card)",
    width:          "min(920px, 100%)",
    maxHeight:      "100%",
    display:        "flex",
    flexDirection:  "column",
    overflow:       "hidden",
  },
  header: {
    display:        "flex",
    alignItems:     "flex-start",
    justifyContent: "space-between",
    gap:            "1rem",
    padding:        "1.1rem 1.4rem 0.9rem",
    borderBottom:   "1px solid var(--bg-border)",
    flexShrink:     0,
  },
  title: {
    margin:         0,
    fontFamily:     "var(--font-ui)",
    fontSize:       "1.05rem",
    fontWeight:     700,
    color:          "var(--text-primary)",
    letterSpacing:  "0.01em",
  },
  subtitle: {
    margin:         "0.3rem 0 0",
    fontSize:       "0.78rem",
    color:          "var(--text-secondary)",
    maxWidth:       "60ch",
    lineHeight:     1.45,
  },
  closeBtn: {
    background:     "transparent",
    border:         "1px solid var(--bg-border)",
    borderRadius:   "var(--radius-sm)",
    color:          "var(--text-secondary)",
    fontSize:       "0.85rem",
    lineHeight:     1,
    padding:        "0.4rem 0.55rem",
    cursor:         "pointer",
    flexShrink:     0,
  },
  tabs: {
    display:        "flex",
    gap:            "0.3rem",
    padding:        "0.7rem 1.4rem 0",
    borderBottom:   "1px solid var(--bg-border)",
    flexShrink:     0,
  },
  tab: {
    background:     "transparent",
    border:         "none",
    borderBottom:   "2px solid transparent",
    color:          "var(--text-secondary)",
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.74rem",
    fontWeight:     700,
    letterSpacing:  "0.08em",
    textTransform:  "uppercase",
    padding:        "0.45rem 0.7rem",
    cursor:         "pointer",
    marginBottom:   "-1px",
  },
  tabActive: {
    color:          "var(--text-primary)",
    borderBottom:   "2px solid var(--accent, #3b82f6)",
  },
  banner: {
    margin:         "0.8rem 1.4rem 0",
    padding:        "0.55rem 0.75rem",
    borderRadius:   "var(--radius-sm)",
    fontSize:       "0.78rem",
    lineHeight:     1.4,
  },
  bannerWarn: {
    background:     "rgba(232,160,32,0.10)",
    border:         "1px solid var(--status-settling)",
    color:          "var(--status-settling)",
  },
  bannerError: {
    background:     "rgba(232,80,80,0.10)",
    border:         "1px solid var(--status-offline)",
    color:          "var(--status-offline)",
  },
  body: {
    padding:        "1rem 1.4rem 1.2rem",
    overflowY:      "auto",
    flex:           1,
  },
  empty: {
    color:          "var(--text-secondary)",
    fontSize:       "0.85rem",
    textAlign:      "center",
    padding:        "2rem 0",
  },
  group: {
    marginBottom:   "1.3rem",
  },
  groupTitle: {
    margin:         "0 0 0.6rem",
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.72rem",
    fontWeight:     700,
    letterSpacing:  "0.09em",
    textTransform:  "uppercase",
    color:          "var(--text-secondary)",
  },
  grid: {
    display:              "grid",
    gridTemplateColumns:  "repeat(auto-fill, minmax(210px, 1fr))",
    gap:                  "0.8rem",
  },
  field: {
    display:        "flex",
    flexDirection:  "column",
    gap:            "0.3rem",
  },
  label: {
    fontSize:       "0.76rem",
    color:          "var(--text-primary)",
  },
  unit: {
    color:          "var(--text-tertiary)",
  },
  input: {
    fontFamily:     "var(--font-mono)",
    fontSize:       "0.85rem",
    padding:        "0.45rem 0.55rem",
    background:     "var(--bg-base)",
    color:          "var(--text-primary)",
    border:         "1px solid var(--bg-border)",
    borderRadius:   "var(--radius-sm)",
    outline:        "none",
    width:          "100%",
    boxSizing:      "border-box",
    transition:     "var(--transition)",
  },
  fieldFoot: {
    minHeight:      "1.05rem",
    fontSize:       "0.68rem",
  },
  defaultHint: {
    color:          "var(--text-tertiary)",
    fontFamily:     "var(--font-mono)",
  },
  diffHint: {
    display:        "inline-flex",
    alignItems:     "baseline",
    gap:            "0.3rem",
    fontFamily:     "var(--font-mono)",
  },
  diffOld: {
    color:          "var(--text-tertiary)",
    textDecoration: "line-through",
  },
  diffArrow: {
    color:          "var(--text-tertiary)",
  },
  diffNew: {
    color:          "var(--accent-torque)",
    fontWeight:     600,
  },
  problem: {
    color:          "var(--status-offline)",
  },
  revertBtn: {
    marginLeft:     "0.4rem",
    background:     "transparent",
    border:         "none",
    padding:        0,
    color:          "var(--accent-thrust)",
    fontSize:       "0.68rem",
    fontFamily:     "var(--font-ui)",
    cursor:         "pointer",
    textDecoration: "underline",
  },
  footer: {
    display:        "flex",
    alignItems:     "center",
    justifyContent: "space-between",
    gap:            "1rem",
    padding:        "0.85rem 1.4rem",
    borderTop:      "1px solid var(--bg-border)",
    background:     "var(--bg-elevated)",
    flexShrink:     0,
    flexWrap:       "wrap",
  },
  status: {
    fontSize:       "0.78rem",
    fontFamily:     "var(--font-mono)",
  },
  footerBtns: {
    display:        "flex",
    gap:            "0.5rem",
    flexWrap:       "wrap",
  },
  secondaryBtn: {
    background:     "transparent",
    border:         "1px solid var(--bg-border)",
    borderRadius:   "var(--radius-sm)",
    color:          "var(--text-secondary)",
    fontSize:       "0.76rem",
    padding:        "0.45rem 0.7rem",
    cursor:         "pointer",
    transition:     "var(--transition)",
  },
  primaryBtn: {
    background:     "rgba(45,168,240,0.12)",
    border:         "1px solid var(--accent-thrust)",
    borderRadius:   "var(--radius-sm)",
    color:          "var(--accent-thrust)",
    fontSize:       "0.76rem",
    fontWeight:     600,
    padding:        "0.45rem 0.9rem",
    cursor:         "pointer",
    transition:     "var(--transition)",
  },
};
