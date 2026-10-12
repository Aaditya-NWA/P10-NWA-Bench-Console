import React, { useCallback, useState } from "react";

import { api } from "../api";
import {
  EP, FIRMWARE_BRANCH, FIRMWARE_REPO, FIRMWARE_ZIP_NAME, MSG_BACKEND_UNREACHABLE,
} from "../constants";

/**
 * FirmwarePanel — the Firmware tab of SettingsModal.
 *
 * One button: download the esp32/ folder from the repository's main
 * branch as esp32.zip. Nothing is flashed or built here — the zip is the
 * source, for building with ESP-IDF.
 *
 * Fetched only on click, like updates: opening Settings makes no
 * network request.
 */
export default function FirmwarePanel() {
  const [downloading, setDownloading] = useState(false);
  const [error, setError]             = useState("");
  const [done, setDone]               = useState(false);

  const handleDownload = useCallback(async () => {
    setDownloading(true);
    setError("");
    setDone(false);
    try {
      const res  = await api(EP.FIRMWARE_SOURCE, { raw: true });
      const blob = await res.blob();
      const url  = URL.createObjectURL(blob);
      const a    = document.createElement("a");
      a.href = url;
      a.download = FIRMWARE_ZIP_NAME;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setDone(true);
    } catch (e) {
      setError(e?.message || `Download failed — ${MSG_BACKEND_UNREACHABLE}.`);
    } finally {
      setDownloading(false);
    }
  }, []);

  return (
    <div>
      <div style={styles.headRow}>
        <div>
          <h3 style={styles.groupTitle}>ESP32 firmware source</h3>
          <p style={styles.help}>
            Downloads the <code style={styles.code}>esp32/</code> folder as it is
            on GitHub right now, as {FIRMWARE_ZIP_NAME}. Build and flash it with
            ESP-IDF.
          </p>
          <p style={styles.source}>
            {FIRMWARE_REPO} · {FIRMWARE_BRANCH}
          </p>
        </div>
        <button
          style={{
            ...styles.primaryBtn,
            opacity: downloading ? 0.5 : 1,
            cursor:  downloading ? "not-allowed" : "pointer",
          }}
          onClick={handleDownload}
          disabled={downloading}
        >
          {downloading ? "Downloading…" : `Download ${FIRMWARE_ZIP_NAME}`}
        </button>
      </div>

      {done && <p style={styles.ok}>{FIRMWARE_ZIP_NAME} downloaded — check your Downloads folder.</p>}
      {error && <p style={styles.error}>{error}</p>}
    </div>
  );
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
    color: "var(--text-secondary)", maxWidth: "52ch", lineHeight: 1.45,
  },
  code: { fontFamily: "var(--font-mono)", fontSize: "0.74rem" },
  source: {
    margin: "0.6rem 0 0", fontFamily: "var(--font-mono)",
    fontSize: "0.74rem", color: "var(--text-tertiary)",
  },
  ok: {
    margin: "1rem 0 0", fontSize: "0.8rem", color: "var(--status-stable)",
  },
  error: {
    marginTop: "0.9rem", padding: "0.6rem 0.75rem",
    borderRadius: "var(--radius-sm)",
    background: "rgba(232,80,80,0.10)",
    border: "1px solid var(--status-offline)",
    color: "var(--status-offline)", fontSize: "0.8rem", lineHeight: 1.45,
  },
  primaryBtn: {
    background: "var(--accent, #3b82f6)", border: "none",
    borderRadius: "var(--radius-sm)", color: "#fff",
    fontFamily: "var(--font-ui)", fontSize: "0.8rem", fontWeight: 700,
    letterSpacing: "0.03em", padding: "0.5rem 0.9rem",
    cursor: "pointer", flexShrink: 0,
  },
};
