import React, { useState, useMemo, useCallback } from "react";
import { api } from "../api";
import {
  EP, METHOD_POST, DIR_CW, DIR_CCW, NO_VALUE, MSG_BACKEND_UNREACHABLE,
} from "../constants";

const EXPORT_FILENAME = "NWA-Bench-Console_Analysis.xlsx";

// Percent-error shading thresholds, in whole percent
const PCT_GOOD = 10;
const PCT_MARGINAL = 25;

/**
 * AnalysisTab — Tab 2. Offline comparison of NW rig test logs against
 * benchmark ("tito") reference data: upload both sides, auto-pair by
 * filename, run the analysis, review the tables and charts, export to
 * Excel. Self-contained — no shared state with Tab 1.
 */
export default function AnalysisTab() {
  const [testFiles, setTestFiles] = useState([]);
  const [benchmarkFiles, setBenchmarkFiles] = useState([]);
  const [manualLinks, setManualLinks] = useState({});    // { testFileIndex: benchmarkFileIndex } overrides

  const [results, setResults] = useState(null);
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState("");

  const addFiles = (setter) => (e) => {
    const incoming = Array.from(e.target.files || []);
    if (!incoming.length) return;
    setter((prev) => {
      const existingKeys = new Set(prev.map((f) => `${f.name}:${f.size}`));
      const merged = [...prev];
      for (const f of incoming) {
        const key = `${f.name}:${f.size}`;
        if (!existingKeys.has(key)) {
          merged.push(f);
          existingKeys.add(key);
        }
      }
      return merged;
    });
    e.target.value = "";              // Lets the same file be picked again later
  };

  const removeFile = (setter) => (idx) => {
    setter((prev) => prev.filter((_, i) => i !== idx));
  };

  // Auto-pairing matches each test file to the benchmark whose filename
  // shares the same propeller size and/or rotation direction, falling
  // back to positional. Manual overrides always take precedence.
  const extractSize = (name) => {
    const m = name.match(/(2[0-9]|3[0-9])/);
    return m ? m[1] : null;
  };
  const extractDir = (name) => {
    if (/ccw/i.test(name)) return DIR_CCW;
    if (/\bcw\b/i.test(name)) return DIR_CW;
    return null;
  };

  const pairs = useMemo(() => {
    return testFiles.map((tf, ti) => {
      if (manualLinks[ti] !== undefined && manualLinks[ti] !== "") {
        const bi = Number(manualLinks[ti]);
        return { testIndex: ti, benchmarkIndex: bi };
      }
      const size = extractSize(tf.name);
      const dir = extractDir(tf.name);
      let bi = benchmarkFiles.findIndex(
        (bf) => size && extractSize(bf.name) === size && (!dir || extractDir(bf.name) === dir)
      );
      if (bi === -1) {
        bi = benchmarkFiles.findIndex((bf) => size && extractSize(bf.name) === size);
      }
      if (bi === -1 && benchmarkFiles.length === testFiles.length) {
        bi = ti;                      // Positional fallback, only when counts line up 1:1
      }
      return { testIndex: ti, benchmarkIndex: bi };
    });
  }, [testFiles, benchmarkFiles, manualLinks]);

  const validPairs = pairs.filter((p) => p.benchmarkIndex !== -1 && p.benchmarkIndex !== undefined);

  // Shared by /analysis/run and /analysis/export-excel
  const buildFormData = useCallback(() => {
    const fd = new FormData();
    testFiles.forEach((f) => fd.append("test_files", f));
    benchmarkFiles.forEach((f) => fd.append("benchmark_files", f));
    fd.append(
      "pairs",
      JSON.stringify(
        validPairs.map((p) => ({
          test_index: p.testIndex,
          benchmark_index: p.benchmarkIndex,
          label: guessLabel(testFiles[p.testIndex]?.name, benchmarkFiles[p.benchmarkIndex]?.name),
        }))
      )
    );
    return fd;
  }, [testFiles, benchmarkFiles, validPairs]);

  const guessLabel = (testName, benchName) => {
    if (!testName) return "";
    const size = extractSize(testName);
    const dir = extractDir(testName) || extractDir(benchName || "");
    return [size ? `${size}"` : null, dir].filter(Boolean).join(" ") || testName;
  };

  const runAnalysis = async () => {
    if (!validPairs.length) return;
    setLoading(true);
    setError("");
    setResults(null);
    try {
      const data = await api(EP.ANALYSIS_RUN, { method: METHOD_POST, body: buildFormData() });
      setResults(data.pairs);
    } catch (e) {
      setError(e.message || `Analysis failed — ${MSG_BACKEND_UNREACHABLE}.`);
    } finally {
      setLoading(false);
    }
  };

  const exportExcel = async () => {
    if (!validPairs.length) return;
    setExporting(true);
    setError("");
    try {
      const res = await api(EP.ANALYSIS_EXPORT, {
        method: METHOD_POST, body: buildFormData(), raw: true,
      });
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = EXPORT_FILENAME;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(e.message || `Export failed — ${MSG_BACKEND_UNREACHABLE}.`);
    } finally {
      setExporting(false);
    }
  };

  const downloadChart = (base64png, filename) => {
    const a = document.createElement("a");
    a.href = `data:image/png;base64,${base64png}`;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  return (
    <main style={styles.main}>
      <div style={styles.uploadRow}>
        <UploadCard
          title="NW Test Files"
          hint="Our rig's raw logs (.csv)"
          files={testFiles}
          onAdd={addFiles(setTestFiles)}
          onRemove={removeFile(setTestFiles)}
        />
        <UploadCard
          title="Benchmark Files"
          hint="Tito reference data (.csv / .xlsx)"
          files={benchmarkFiles}
          onAdd={addFiles(setBenchmarkFiles)}
          onRemove={removeFile(setBenchmarkFiles)}
        />
      </div>

      {testFiles.length > 0 && (
        <div style={styles.pairCard}>
          <div style={styles.pairHeader}>Pairing</div>
          {pairs.map((p, i) => (
            <div key={i} style={styles.pairRow}>
              <span style={styles.pairTestName}>{testFiles[p.testIndex].name}</span>
              <span style={styles.pairArrow}>→</span>
              <select
                style={styles.pairSelect}
                value={p.benchmarkIndex === -1 ? "" : p.benchmarkIndex}
                onChange={(e) =>
                  setManualLinks((prev) => ({ ...prev, [p.testIndex]: e.target.value }))
                }
              >
                <option value="">— no match —</option>
                {benchmarkFiles.map((bf, bi) => (
                  <option key={bi} value={bi}>
                    {bf.name}
                  </option>
                ))}
              </select>
            </div>
          ))}
        </div>
      )}

      <div style={styles.actionsRow}>
        <button
          style={{ ...styles.primaryBtn, ...(validPairs.length === 0 || loading ? styles.btnDisabled : {}) }}
          onClick={runAnalysis}
          disabled={validPairs.length === 0 || loading}
        >
          {loading ? "Analysing…" : `Run Analysis (${validPairs.length} pair${validPairs.length === 1 ? "" : "s"})`}
        </button>
        {error && <span style={styles.errorText}>{error}</span>}
      </div>

      {results && (
        <div style={styles.resultsWrap}>
          {results.map((entry, i) => (
            <ResultCard key={i} entry={entry} onDownloadChart={downloadChart} />
          ))}

          <div style={styles.exportRow}>
            <button
              style={{ ...styles.exportBtn, ...(exporting ? styles.btnDisabled : {}) }}
              onClick={exportExcel}
              disabled={exporting}
            >
              {exporting ? "Building workbook…" : "Export to Excel"}
            </button>
          </div>
        </div>
      )}
    </main>
  );
}

function UploadCard({ title, hint, files, onAdd, onRemove }) {
  return (
    <div style={styles.uploadCard}>
      <div style={styles.uploadTitle}>{title}</div>
      <div style={styles.uploadHint}>{hint}</div>
      <label style={styles.dropZone}>
        <input type="file" multiple accept=".csv,.xlsx,.xls" style={{ display: "none" }} onChange={onAdd} />
        <span>+ Add files</span>
      </label>
      {files.length > 0 && (
        <div style={styles.chipList}>
          {files.map((f, i) => (
            <div key={i} style={styles.chip}>
              <span style={styles.chipName}>{f.name}</span>
              <button style={styles.chipRemove} onClick={() => onRemove(i)} title="Remove">
                ×
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function pctFill(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "transparent";
  const av = Math.abs(v);
  if (av <= PCT_GOOD)     return "rgba(76,186,106,0.25)";
  if (av <= PCT_MARGINAL) return "rgba(232,160,32,0.25)";
  return "rgba(232,80,80,0.25)";
}

const COLS = [
  { key: "tito_rpm", label: "Tito RPM" },
  { key: "tested_rpm", label: "Tested RPM" },
  { key: "rpm_pct_error", label: "RPM % Err", pct: true },
  { key: "tito_thrust_N", label: "Tito Thrust (N)" },
  { key: "tested_thrust_N", label: "Tested Thrust (N)" },
  { key: "thrust_pct_error", label: "Thrust % Err", pct: true },
  { key: "tito_torque_Nm", label: "Tito Torque (Nm)" },
  { key: "tested_torque_Nm", label: "Tested Torque (Nm)" },
  { key: "torque_deviation_Nm", label: "Torque Dev (Nm)" },
  { key: "torque_pct_error", label: "Torque % Err", pct: true },
  { key: "in_range", label: "In Range" },
];

function ResultCard({ entry, onDownloadChart }) {
  if (entry.error) {
    return (
      <div style={styles.resultCard}>
        <div style={styles.resultTitle}>{entry.label}</div>
        <div style={styles.resultError}>Couldn't analyse this pair: {entry.error}</div>
      </div>
    );
  }

  const { summary } = entry;
  return (
    <div style={styles.resultCard}>
      <div style={styles.resultHeader}>
        <div>
          <div style={styles.resultTitle}>{entry.label}</div>
          <div style={styles.resultSub}>
            {entry.test_filename} vs {entry.benchmark_filename}
          </div>
        </div>
        <div style={styles.summaryBadges}>
          <Badge label="Avg Thrust Err" value={summary.avg_thrust_pct_error != null ? `${summary.avg_thrust_pct_error}%` : NO_VALUE} />
          <Badge label="Avg Torque Err" value={summary.avg_torque_pct_error != null ? `${summary.avg_torque_pct_error}%` : NO_VALUE} />
          <Badge label="Points" value={summary.points_compared} />
        </div>
      </div>

      <div style={styles.tableWrap}>
        <table style={styles.table}>
          <thead>
            <tr>
              {COLS.map((c) => (
                <th key={c.key} style={styles.th}>
                  {c.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {entry.rows.map((row, ri) => (
              <tr key={ri}>
                {COLS.map((c) => {
                  const v = row[c.key];
                  if (c.key === "in_range") {
                    return (
                      <td key={c.key} style={styles.td}>
                        {v ? "Yes" : "No (extrapolated)"}
                      </td>
                    );
                  }
                  return (
                    <td key={c.key} style={{ ...styles.td, background: c.pct ? pctFill(v) : "transparent" }}>
                      {v}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div style={styles.chartsRow}>
        <ChartBlock
          title="Thrust vs RPM"
          png={entry.thrust_chart_png}
          onDownload={() => onDownloadChart(entry.thrust_chart_png, `${entry.label}_Thrust_vs_RPM.png`)}
        />
        <ChartBlock
          title="Torque vs RPM"
          png={entry.torque_chart_png}
          onDownload={() => onDownloadChart(entry.torque_chart_png, `${entry.label}_Torque_vs_RPM.png`)}
        />
      </div>
    </div>
  );
}

function Badge({ label, value }) {
  return (
    <div style={styles.badge}>
      <span style={styles.badgeValue}>{value}</span>
      <span style={styles.badgeLabel}>{label}</span>
    </div>
  );
}

function ChartBlock({ title, png, onDownload }) {
  return (
    <div style={styles.chartBlock}>
      <div style={styles.chartHeader}>
        <span style={styles.chartTitle}>{title}</span>
        <button style={styles.downloadBtn} onClick={onDownload} title="Download chart as PNG">
          ⬇
        </button>
      </div>
      {png ? (
        <img src={`data:image/png;base64,${png}`} alt={title} style={styles.chartImg} />
      ) : (
        <div style={styles.chartPlaceholder}>No chart</div>
      )}
    </div>
  );
}

// Reuses the app's CSS custom properties, for theme parity
const styles = {
  main: {
    flex: 1,
    overflowY: "auto",
    padding: "0.9rem 1.4rem 2rem",
    display: "flex",
    flexDirection: "column",
    gap: "1rem",
  },
  uploadRow: {
    display: "flex",
    gap: "0.9rem",
    flexWrap: "wrap",
  },
  uploadCard: {
    flex: "1 1 280px",
    background: "var(--bg-surface)",
    border: "1px solid var(--bg-border)",
    borderRadius: "var(--radius-md)",
    boxShadow: "var(--shadow-card)",
    padding: "0.9rem 1rem",
    display: "flex",
    flexDirection: "column",
    gap: "0.5rem",
  },
  uploadTitle: {
    fontFamily: "var(--font-ui)",
    fontSize: "0.85rem",
    fontWeight: 700,
    color: "var(--text-primary)",
  },
  uploadHint: {
    fontFamily: "var(--font-ui)",
    fontSize: "0.7rem",
    color: "var(--text-tertiary)",
  },
  dropZone: {
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    border: "1px dashed var(--bg-border)",
    borderRadius: "var(--radius-sm)",
    padding: "0.5rem 0.8rem",
    fontFamily: "var(--font-ui)",
    fontSize: "0.75rem",
    fontWeight: 600,
    color: "var(--accent-thrust)",
    cursor: "pointer",
    width: "fit-content",
  },
  chipList: {
    display: "flex",
    flexWrap: "wrap",
    gap: "0.35rem",
    marginTop: "0.2rem",
  },
  chip: {
    display: "flex",
    alignItems: "center",
    gap: "0.3rem",
    background: "var(--bg-elevated)",
    border: "1px solid var(--bg-border)",
    borderRadius: "var(--radius-sm)",
    padding: "0.2rem 0.4rem 0.2rem 0.6rem",
    fontFamily: "var(--font-mono)",
    fontSize: "0.68rem",
    color: "var(--text-secondary)",
  },
  chipName: { maxWidth: "220px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  chipRemove: {
    background: "transparent",
    border: "none",
    color: "var(--text-tertiary)",
    cursor: "pointer",
    fontSize: "0.9rem",
    lineHeight: 1,
    padding: "0 0.15rem",
  },
  pairCard: {
    background: "var(--bg-surface)",
    border: "1px solid var(--bg-border)",
    borderRadius: "var(--radius-md)",
    boxShadow: "var(--shadow-card)",
    padding: "0.8rem 1rem",
    display: "flex",
    flexDirection: "column",
    gap: "0.4rem",
  },
  pairHeader: {
    fontFamily: "var(--font-ui)",
    fontSize: "0.75rem",
    fontWeight: 700,
    color: "var(--text-secondary)",
    textTransform: "uppercase",
    letterSpacing: "0.08em",
  },
  pairRow: {
    display: "flex",
    alignItems: "center",
    gap: "0.6rem",
    fontFamily: "var(--font-mono)",
    fontSize: "0.75rem",
  },
  pairTestName: { color: "var(--text-primary)", minWidth: "220px" },
  pairArrow: { color: "var(--text-tertiary)" },
  pairSelect: {
    background: "var(--bg-elevated)",
    color: "var(--text-primary)",
    border: "1px solid var(--bg-border)",
    borderRadius: "var(--radius-sm)",
    padding: "0.25rem 0.5rem",
    fontFamily: "var(--font-ui)",
    fontSize: "0.75rem",
    flex: 1,
  },
  actionsRow: {
    display: "flex",
    alignItems: "center",
    gap: "0.8rem",
  },
  primaryBtn: {
    background: "var(--accent-thrust)",
    color: "#fff",
    border: "none",
    borderRadius: "var(--radius-sm)",
    padding: "0.55rem 1.2rem",
    fontFamily: "var(--font-ui)",
    fontSize: "0.8rem",
    fontWeight: 700,
    cursor: "pointer",
  },
  btnDisabled: { opacity: 0.5, cursor: "not-allowed" },
  errorText: { color: "var(--status-offline)", fontFamily: "var(--font-ui)", fontSize: "0.75rem" },
  resultsWrap: { display: "flex", flexDirection: "column", gap: "1rem" },
  resultCard: {
    background: "var(--bg-surface)",
    border: "1px solid var(--bg-border)",
    borderRadius: "var(--radius-md)",
    boxShadow: "var(--shadow-card)",
    padding: "1rem 1.1rem",
    display: "flex",
    flexDirection: "column",
    gap: "0.7rem",
  },
  resultHeader: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "flex-start",
    flexWrap: "wrap",
    gap: "0.6rem",
  },
  resultTitle: { fontFamily: "var(--font-ui)", fontSize: "1rem", fontWeight: 700, color: "var(--text-primary)" },
  resultSub: { fontFamily: "var(--font-mono)", fontSize: "0.68rem", color: "var(--text-tertiary)" },
  resultError: { fontFamily: "var(--font-ui)", fontSize: "0.8rem", color: "var(--status-offline)" },
  summaryBadges: { display: "flex", gap: "0.6rem" },
  badge: {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    background: "var(--bg-elevated)",
    borderRadius: "var(--radius-sm)",
    padding: "0.3rem 0.7rem",
    minWidth: "72px",
  },
  badgeValue: { fontFamily: "var(--font-mono)", fontSize: "0.9rem", fontWeight: 700, color: "var(--text-primary)" },
  badgeLabel: { fontFamily: "var(--font-ui)", fontSize: "0.6rem", color: "var(--text-tertiary)", textTransform: "uppercase" },
  tableWrap: { overflowX: "auto" },
  table: { borderCollapse: "collapse", width: "100%", fontFamily: "var(--font-mono)", fontSize: "0.72rem" },
  th: {
    background: "var(--bg-elevated)",
    color: "var(--text-secondary)",
    padding: "0.4rem 0.5rem",
    border: "1px solid var(--bg-border)",
    fontFamily: "var(--font-ui)",
    fontWeight: 700,
    whiteSpace: "nowrap",
  },
  td: {
    padding: "0.35rem 0.5rem",
    border: "1px solid var(--bg-border)",
    textAlign: "center",
    color: "var(--text-primary)",
    whiteSpace: "nowrap",
  },
  chartsRow: { display: "flex", gap: "0.9rem", flexWrap: "wrap" },
  chartBlock: {
    flex: "1 1 320px",
    background: "var(--bg-elevated)",
    border: "1px solid var(--bg-border)",
    borderRadius: "var(--radius-sm)",
    padding: "0.5rem",
  },
  chartHeader: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.3rem" },
  chartTitle: { fontFamily: "var(--font-ui)", fontSize: "0.72rem", fontWeight: 700, color: "var(--text-secondary)" },
  downloadBtn: {
    background: "transparent",
    border: "1px solid var(--bg-border)",
    borderRadius: "var(--radius-sm)",
    color: "var(--text-secondary)",
    cursor: "pointer",
    fontSize: "0.75rem",
    padding: "0.1rem 0.4rem",
    lineHeight: 1.4,
  },
  chartImg: { width: "100%", height: "auto", borderRadius: "4px", display: "block" },
  chartPlaceholder: { color: "var(--text-tertiary)", fontSize: "0.75rem", textAlign: "center", padding: "2rem 0" },
  exportRow: { display: "flex", justifyContent: "center", padding: "0.4rem 0" },
  exportBtn: {
    background: "var(--accent-load)",
    color: "#0a0c0f",
    border: "none",
    borderRadius: "var(--radius-sm)",
    padding: "0.6rem 1.6rem",
    fontFamily: "var(--font-ui)",
    fontSize: "0.85rem",
    fontWeight: 700,
    cursor: "pointer",
  },
};
