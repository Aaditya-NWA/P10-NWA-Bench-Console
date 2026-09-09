import React, { useCallback, useEffect, useRef, useState } from "react";

import { MSG_NO_UPDATES, UPDATE_STATE } from "../constants";
import { checkForUpdate, installUpdate, isDesktop, onUpdateProgress } from "../desktop";

/**
 * UpdatesPanel — the Updates tab of SettingsModal.
 *
 * Nothing here happens on its own. The app never checks for updates in
 * the background; it asks the release channel only when the operator
 * presses the button. That is what allows the repository to be private
 * between releases — the update endpoint has to be reachable in the
 * moment of the click, rather than permanently.
 *
 * The consequence, and the reason "unreachable" is its own state: while
 * the repository is private, a check answers exactly as it would if
 * nothing had ever been published. Reporting that as "up to date" would
 * hide a closed release window, so it gets its own wording.
 */
export default function UpdatesPanel() {
  const [checking, setChecking]   = useState(false);
  const [result, setResult]       = useState(null);   // last check outcome
  const [installing, setInstalling] = useState(false);
  const [progress, setProgress]   = useState(null);   // { downloaded, total }
  const [error, setError]         = useState("");

  // The unlisten handle from the Tauri event subscription. Kept in a ref
  // so the cleanup effect can reach it without re-subscribing on every
  // progress tick.
  const unlistenRef = useRef(null);

  useEffect(() => () => { unlistenRef.current?.(); }, []);

  const handleCheck = useCallback(async () => {
    setChecking(true);
    setError("");
    setProgress(null);
    try {
      setResult(await checkForUpdate());
    } finally {
      setChecking(false);
    }
  }, []);

  const handleInstall = useCallback(async () => {
    setInstalling(true);
    setError("");
    setProgress({ downloaded: 0, total: result?.size_bytes ?? null });

    // Subscribed before the download starts, or the first chunks land
    // with nobody listening and the bar jumps from 0 to part-way.
    unlistenRef.current = await onUpdateProgress(setProgress);

    try {
      await installUpdate();
      // Not reached in practice — the installer replaces this process.
    } catch (e) {
      // The shell's refusals are written to be shown verbatim: they name
      // which condition blocks the install so the operator can clear it.
      setError(String(e?.message ?? e));
      setInstalling(false);
      unlistenRef.current?.();
      unlistenRef.current = null;
    }
  }, [result]);

  if (!isDesktop()) {
    return (
      <p style={styles.muted}>
        Updates are handled by the desktop application. This dashboard is running
        in a browser, so there is nothing to update here.
      </p>
    );
  }

  const available = result?.state === UPDATE_STATE.AVAILABLE;
  const pct = progressPct(progress);

  return (
    <div>
      <div style={styles.headRow}>
        <div>
          <h3 style={styles.groupTitle}>Application updates</h3>
          <p style={styles.help}>
            Checked only when you ask — the console never contacts the update
            server on its own.
          </p>
        </div>
        <button
          style={{
            ...styles.primaryBtn,
            opacity: checking || installing ? 0.5 : 1,
            cursor:  checking || installing ? "not-allowed" : "pointer",
          }}
          onClick={handleCheck}
          disabled={checking || installing}
        >
          {checking ? "Checking…" : "Check for Updates"}
        </button>
      </div>

      {result?.current_version && (
        <p style={styles.version}>Installed version {result.current_version}</p>
      )}

      {available && (
        <section style={styles.card}>
          <div style={styles.cardHead}>
            <span style={styles.badge}>Version {result.version}</span>
            {result.size_bytes ? (
              <span style={styles.size}>{formatMb(result.size_bytes)}</span>
            ) : null}
          </div>

          {result.title && <p style={styles.notes}>{result.title}</p>}

          {installing && (
            <div style={styles.progressWrap}>
              <div style={styles.progressTrack}>
                <div
                  style={{
                    ...styles.progressFill,
                    // Indeterminate when the server sent no Content-Length:
                    // a bar pinned at 0% reads as "stuck" rather than "working".
                    width: pct === null ? "100%" : `${pct}%`,
                    opacity: pct === null ? 0.45 : 1,
                  }}
                />
              </div>
              <span style={styles.progressLabel}>
                {pct === null
                  ? `Downloading… ${formatMb(progress?.downloaded ?? 0)}`
                  : `${pct}% — ${formatMb(progress?.downloaded ?? 0)} of ${formatMb(progress?.total)}`}
              </span>
            </div>
          )}

          <button
            style={{
              ...styles.primaryBtn,
              marginTop: "0.9rem",
              opacity: installing ? 0.5 : 1,
              cursor:  installing ? "not-allowed" : "pointer",
            }}
            onClick={handleInstall}
            disabled={installing}
          >
            {installing ? "Installing…" : "Update Now"}
          </button>

          <p style={styles.warn}>
            Installing restarts the console. Disconnect the ESP32 and stop any
            running profile first.
          </p>
        </section>
      )}

      {result?.state === UPDATE_STATE.NONE && (
        <p style={styles.none}>{MSG_NO_UPDATES}</p>
      )}

      {result?.state === UPDATE_STATE.UNREACHABLE && (
        <div style={styles.unreachable}>
          <strong>Could not reach the update server.</strong>
          <p style={styles.unreachableBody}>
            This is also what you see while the release repository is private, so
            it does not necessarily mean anything is wrong. Check your connection,
            or ask whether a release window is open.
          </p>
        </div>
      )}

      {error && <p style={styles.error}>{error}</p>}
    </div>
  );
}

/** Whole-percent download progress, or null when the total is unknown. */
function progressPct(p) {
  if (!p?.total) return null;
  return Math.min(100, Math.round((p.downloaded / p.total) * 100));
}

function formatMb(bytes) {
  if (typeof bytes !== "number" || !Number.isFinite(bytes)) return "—";
  return `${(bytes / 1e6).toFixed(1)} MB`;
}

const styles = {
  headRow: {
    display: "flex", alignItems: "flex-start",
    justifyContent: "space-between", gap: "1rem", marginBottom: "0.4rem",
  },
  groupTitle: {
    margin: 0, fontFamily: "var(--font-ui)", fontSize: "0.72rem",
    fontWeight: 700, letterSpacing: "0.09em", textTransform: "uppercase",
    color: "var(--text-secondary)",
  },
  help: {
    margin: "0.35rem 0 0", fontSize: "0.78rem",
    color: "var(--text-secondary)", maxWidth: "48ch", lineHeight: 1.45,
  },
  version: {
    margin: "0.9rem 0 0", fontFamily: "var(--font-mono)",
    fontSize: "0.78rem", color: "var(--text-secondary)",
  },
  card: {
    marginTop: "1rem", padding: "1rem",
    border: "1px solid var(--bg-border)", borderRadius: "var(--radius-sm)",
    background: "var(--bg-base)",
  },
  cardHead: {
    display: "flex", alignItems: "center",
    justifyContent: "space-between", gap: "0.75rem",
  },
  badge: {
    fontFamily: "var(--font-mono)", fontSize: "0.85rem",
    fontWeight: 700, color: "var(--text-primary)",
  },
  size: {
    fontFamily: "var(--font-mono)", fontSize: "0.78rem",
    color: "var(--text-secondary)",
  },
  notes: {
    margin: "0.6rem 0 0", fontSize: "0.82rem",
    color: "var(--text-primary)", lineHeight: 1.5, whiteSpace: "pre-wrap",
  },
  progressWrap: { marginTop: "0.9rem" },
  progressTrack: {
    height: "6px", borderRadius: "3px",
    background: "var(--bg-border)", overflow: "hidden",
  },
  progressFill: {
    height: "100%", background: "var(--accent, #3b82f6)",
    transition: "width 0.2s ease",
  },
  progressLabel: {
    display: "block", marginTop: "0.4rem",
    fontFamily: "var(--font-mono)", fontSize: "0.72rem",
    color: "var(--text-secondary)",
  },
  warn: {
    margin: "0.7rem 0 0", fontSize: "0.74rem",
    color: "var(--status-settling)", lineHeight: 1.45,
  },
  none: {
    margin: "1.1rem 0 0", fontSize: "0.85rem", color: "var(--text-secondary)",
  },
  unreachable: {
    marginTop: "1rem", padding: "0.7rem 0.85rem",
    borderRadius: "var(--radius-sm)",
    background: "rgba(232,160,32,0.10)",
    border: "1px solid var(--status-settling)",
    color: "var(--status-settling)", fontSize: "0.8rem",
  },
  unreachableBody: { margin: "0.35rem 0 0", lineHeight: 1.45 },
  error: {
    marginTop: "0.9rem", padding: "0.6rem 0.75rem",
    borderRadius: "var(--radius-sm)",
    background: "rgba(232,80,80,0.10)",
    border: "1px solid var(--status-offline)",
    color: "var(--status-offline)", fontSize: "0.8rem", lineHeight: 1.45,
  },
  muted: {
    color: "var(--text-secondary)", fontSize: "0.85rem",
    lineHeight: 1.5, padding: "1.5rem 0", maxWidth: "52ch",
  },
  primaryBtn: {
    background: "var(--accent, #3b82f6)", border: "none",
    borderRadius: "var(--radius-sm)", color: "#fff",
    fontFamily: "var(--font-ui)", fontSize: "0.8rem", fontWeight: 700,
    letterSpacing: "0.03em", padding: "0.5rem 0.9rem",
    cursor: "pointer", flexShrink: 0,
  },
};
