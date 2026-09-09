import { useCallback, useEffect, useState } from "react";
import { api, setAuthToken, getAuthToken } from "../api";
import {
  EP, EVT_AUTH_EXPIRED, METHOD_POST,
  MSG_SESSION_EXPIRED, MSG_SIGN_IN_FAILED,
} from "../constants";

const RECHECK_INTERVAL_MS = 30000; // Backend restarts sign everyone out; notice on our own

/**
 * useAuth — owns the operator session: whether the console still needs
 * its first account, who is signed in, and sign-in / sign-out.
 * Sessions live only in the backend's memory, so a restart is treated
 * as "show the sign-in screen", not as an error to display.
 */
export function useAuth() {
  const [checking, setChecking]           = useState(true);   // Until the first /auth/status lands
  const [setupRequired, setSetupRequired] = useState(false);  // No account exists yet
  const [username, setUsername]           = useState(null);
  const [error, setError]                 = useState("");
  const [busy, setBusy]                   = useState(false);
  const [backendDown, setBackendDown]     = useState(false);  // Distinct from "not signed in"
  const [loginFile, setLoginFile]         = useState("");     // Shown on the setup screen

  const refresh = useCallback(async () => {
    try {
      const data = await api(EP.AUTH_STATUS);
      setBackendDown(false);
      setSetupRequired(!!data.setup_required);
      setLoginFile(data.login_file || "");
      setUsername(data.authenticated ? data.username : null);
      // A token the backend no longer recognises is worse than none —
      // every later call would 401.
      if (!data.authenticated && getAuthToken()) setAuthToken(null);
    } catch (e) {
      if (e.status === 0) setBackendDown(true);
      setUsername(null);
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  useEffect(() => {
    const onExpired = () => { setUsername(null); setError(MSG_SESSION_EXPIRED); };
    window.addEventListener(EVT_AUTH_EXPIRED, onExpired);
    return () => window.removeEventListener(EVT_AUTH_EXPIRED, onExpired);
  }, []);

  useEffect(() => {
    if (!username) return;
    const id = setInterval(refresh, RECHECK_INTERVAL_MS);
    return () => clearInterval(id);
  }, [username, refresh]);

  const submit = useCallback(async (path, credentials) => {
    setBusy(true);
    setError("");
    try {
      const data = await api(path, { method: METHOD_POST, body: credentials });
      if (!data.ok) {
        setError(data.error || MSG_SIGN_IN_FAILED);
        return false;
      }
      setAuthToken(data.token);
      setUsername(data.username);
      setSetupRequired(false);
      setBackendDown(false);
      return true;
    } catch (e) {
      setError(e.message || MSG_SIGN_IN_FAILED);
      return false;
    } finally {
      setBusy(false);
    }
  }, []);

  const login = useCallback(
    (u, p) => submit(EP.AUTH_LOGIN, { username: u, password: p }), [submit]);

  const createFirstAccount = useCallback(
    (u, p) => submit(EP.AUTH_SETUP, { username: u, password: p }), [submit]);

  const logout = useCallback(async () => {
    try {
      await api(EP.AUTH_LOGOUT, { method: METHOD_POST });
    } catch {
      // Signing out locally matters more than the server acknowledging
      // it — a dropped backend must not strand the operator signed in.
    }
    setAuthToken(null);
    setUsername(null);
    setError("");
    refresh();
  }, [refresh]);

  return {
    checking, setupRequired, username, error, busy, backendDown, loginFile,
    login, createFirstAccount, logout, refresh,
    authenticated: !!username,
  };
}
