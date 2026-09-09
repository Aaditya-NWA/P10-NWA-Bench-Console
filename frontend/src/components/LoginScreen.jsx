import React, { useState } from "react";
import { API_BASE } from "../constants";

const MIN_PASSWORD_LEN = 6;
const DEFAULT_LOGIN_PATH = "Documents/NWA Bench Console/Login/login.json";

/**
 * LoginScreen — sign-in gate, and the first-run screen that creates the
 * very first operator account. Which one renders is driven by the
 * backend (setupRequired), not by a local toggle.
 *
 * The account isn't about secrecy — the bench is a local machine — it's
 * attribution: every throttle command and calibration write is recorded
 * against whoever is signed in.
 *
 * Props:
 *   setupRequired — render the create-first-account variant
 *   busy          — a request is in flight
 *   error         — message from the last attempt, or ""
 *   backendDown   — backend unreachable (distinct from bad credentials)
 *   loginFile     — path to login.json, shown on the setup screen
 *   onLogin / onCreate — (username, password) => Promise<bool>
 *   onRetry       — re-check backend reachability
 */
export default function LoginScreen({
  setupRequired,
  busy,
  error,
  backendDown,
  loginFile,
  onLogin,
  onCreate,
  onRetry,
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm]   = useState("");
  const [localError, setLocalError] = useState("");
  const [showLogo, setShowLogo] = useState(true);

  // Caught here rather than at the backend so the operator doesn't
  // create an account with a password they mistyped twice.
  const mismatch = setupRequired && confirm !== "" && password !== confirm;

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLocalError("");
    if (!username.trim() || !password) {
      setLocalError("Enter a username and password");
      return;
    }
    if (setupRequired) {
      if (password !== confirm) { setLocalError("Passwords don't match"); return; }
      if (password.length < MIN_PASSWORD_LEN) {
        setLocalError(`Password must be at least ${MIN_PASSWORD_LEN} characters`);
        return;
      }
      await onCreate(username.trim(), password);
    } else {
      await onLogin(username.trim(), password);
    }
    setPassword("");
    setConfirm("");
  };

  const shownError = localError || error;

  return (
    <div style={styles.shell}>
      <form style={styles.card} onSubmit={handleSubmit}>

        <div style={styles.brand}>
          {showLogo ? (
            <img
              src="/logo.png"
              alt=""
              style={styles.logoImg}
              onError={() => setShowLogo(false)}   // Falls back to the text badge if public/logo.png is absent
            />
          ) : (
            <div style={styles.logoFallback}>NWA</div>
          )}
          <div>
            <h1 style={styles.title}>NWA Bench Console</h1>
            <p style={styles.subtitle}>Motor Torque &amp; Thrust Test Stand</p>
          </div>
        </div>

        {backendDown ? (
          <>
            <div style={{ ...styles.banner, ...styles.bannerError }}>
              Can't reach the backend on {API_BASE}. Start it with{" "}
              <code style={styles.code}>python -m uvicorn main:app --port 8000</code>{" "}
              from the <code style={styles.code}>backend</code> folder.
            </div>
            <button type="button" style={styles.primaryBtn} onClick={onRetry}>
              Retry
            </button>
          </>
        ) : (
          <>
            {setupRequired && (
              <div style={{ ...styles.banner, ...styles.bannerInfo }}>
                No account exists yet. Create the first one — it's stored on this
                machine at{" "}
                <span style={styles.path}>{loginFile || DEFAULT_LOGIN_PATH}</span>.
              </div>
            )}

            <label style={styles.label} htmlFor="nwa-username">Operator name</label>
            <input
              id="nwa-username"
              style={styles.input}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              autoFocus
              spellCheck={false}
              disabled={busy}
            />

            <label style={styles.label} htmlFor="nwa-password">Password</label>
            <input
              id="nwa-password"
              type="password"
              style={styles.input}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={setupRequired ? "new-password" : "current-password"}
              disabled={busy}
            />

            {setupRequired && (
              <>
                <label style={styles.label} htmlFor="nwa-confirm">Confirm password</label>
                <input
                  id="nwa-confirm"
                  type="password"
                  style={{
                    ...styles.input,
                    borderColor: mismatch ? "var(--status-offline)" : "var(--bg-border)",
                  }}
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  autoComplete="new-password"
                  disabled={busy}
                />
              </>
            )}

            {shownError && (
              <div style={{ ...styles.banner, ...styles.bannerError }}>{shownError}</div>
            )}

            <button
              type="submit"
              style={{
                ...styles.primaryBtn,
                opacity: busy ? 0.6 : 1,
                cursor:  busy ? "wait" : "pointer",
              }}
              disabled={busy}
            >
              {busy ? "Working…" : setupRequired ? "Create account & sign in" : "Sign in"}
            </button>
          </>
        )}

        <p style={styles.foot}>Nautical Wings Aerospace Pvt. Ltd.</p>
      </form>
    </div>
  );
}

const styles = {
  shell: {
    height:         "100vh",
    width:          "100vw",
    display:        "flex",
    alignItems:     "center",
    justifyContent: "center",
    background:     "var(--bg-base)",
    padding:        "1.5rem",
  },
  card: {
    width:          "min(420px, 100%)",
    background:     "var(--bg-surface)",
    border:         "1px solid var(--bg-border)",
    borderRadius:   "var(--radius-lg)",
    boxShadow:      "var(--shadow-card)",
    padding:        "1.8rem 1.6rem 1.4rem",
    display:        "flex",
    flexDirection:  "column",
    gap:            "0.5rem",
    animation:      "fadeIn 200ms ease",
  },
  brand: {
    display:        "flex",
    alignItems:     "center",
    gap:            "0.8rem",
    marginBottom:   "0.7rem",
  },
  logoImg: {
    width:          "52px",
    height:         "52px",
    flexShrink:     0,
  },
  logoFallback: {
    fontFamily:     "var(--font-mono)",
    fontWeight:     700,
    fontSize:       "1.1rem",
    color:          "var(--accent-torque)",
    background:     "rgba(232,160,32,0.12)",
    border:         "1px solid rgba(232,160,32,0.3)",
    borderRadius:   "var(--radius-sm)",
    padding:        "0.6rem 0.55rem",
    letterSpacing:  "0.04em",
    flexShrink:     0,
  },
  title: {
    margin:         0,
    fontFamily:     "var(--font-ui)",
    fontSize:       "1.5rem",
    fontWeight:     800,
    letterSpacing:  "0.01em",
    color:          "var(--text-primary)",
    lineHeight:     1.1,
  },
  subtitle: {
    margin:         "0.15rem 0 0",
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.82rem",
    color:          "var(--text-tertiary)",
    letterSpacing:  "0.04em",
    textTransform:  "uppercase",
  },
  label: {
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.78rem",
    fontWeight:     600,
    letterSpacing:  "0.06em",
    textTransform:  "uppercase",
    color:          "var(--text-secondary)",
    marginTop:      "0.35rem",
  },
  input: {
    fontFamily:     "var(--font-mono)",
    fontSize:       "0.92rem",
    padding:        "0.55rem 0.65rem",
    background:     "var(--bg-base)",
    color:          "var(--text-primary)",
    border:         "1px solid var(--bg-border)",
    borderRadius:   "var(--radius-sm)",
    outline:        "none",
    width:          "100%",
    transition:     "var(--transition)",
  },
  primaryBtn: {
    marginTop:      "1rem",
    background:     "rgba(45,168,240,0.12)",
    border:         "1px solid var(--accent-thrust)",
    borderRadius:   "var(--radius-sm)",
    color:          "var(--accent-thrust)",
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.9rem",
    fontWeight:     700,
    letterSpacing:  "0.07em",
    textTransform:  "uppercase",
    padding:        "0.6rem 1rem",
    cursor:         "pointer",
    transition:     "var(--transition)",
  },
  banner: {
    padding:        "0.55rem 0.7rem",
    borderRadius:   "var(--radius-sm)",
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.82rem",
    lineHeight:     1.45,
    marginTop:      "0.5rem",
  },
  bannerError: {
    background:     "rgba(232,80,80,0.10)",
    border:         "1px solid var(--status-offline)",
    color:          "var(--status-offline)",
  },
  bannerInfo: {
    background:     "rgba(45,168,240,0.10)",
    border:         "1px solid var(--accent-thrust)",
    color:          "var(--text-secondary)",
  },
  path: {
    fontFamily:     "var(--font-mono)",
    fontSize:       "0.74rem",
    color:          "var(--text-primary)",
    wordBreak:      "break-all",
  },
  code: {
    fontFamily:     "var(--font-mono)",
    fontSize:       "0.76rem",
    background:     "var(--bg-elevated)",
    borderRadius:   "3px",
    padding:        "0.05rem 0.25rem",
  },
  foot: {
    margin:         "1.1rem 0 0",
    textAlign:      "center",
    fontFamily:     "var(--font-ui)",
    fontSize:       "0.72rem",
    letterSpacing:  "0.05em",
    color:          "var(--text-tertiary)",
  },
};
