// Backend endpoints & authenticated fetch.
// The token is a module variable, not props/context, so useSerialData's
// callbacks can stay dependency-free (a changing dependency there once
// tore down the WebSocket mid-recording).

import {
  API_BASE, WS_BASE, STORAGE_TOKEN, EVT_AUTH_EXPIRED,
  METHOD_GET, HTTP_UNAUTHORIZED,
  MSG_BACKEND_UNREACHABLE, MSG_SESSION_EXPIRED,
} from "./constants";

let authToken = localStorage.getItem(STORAGE_TOKEN) || null;

export function getAuthToken() {
  return authToken;
}

export function setAuthToken(token) {
  authToken = token || null;
  if (token) localStorage.setItem(STORAGE_TOKEN, token);
  else localStorage.removeItem(STORAGE_TOKEN);
}

/** WebSocket URL carrying the session token — a browser can't set
 *  headers on a WS handshake, so it rides in the query string. */
export function wsUrl() {
  return authToken ? `${WS_BASE}?token=${encodeURIComponent(authToken)}` : WS_BASE;
}

/** fetch() with the session token attached and JSON bodies handled.
 *  Throws { status, message } so callers can tell "signed out" (401)
 *  from "backend is down" (network error). */
export async function api(path, { method = METHOD_GET, body, raw = false } = {}) {
  const headers = {};
  if (authToken) headers.Authorization = `Bearer ${authToken}`;

  // FormData carries its own multipart boundary; setting Content-Type
  // by hand would overwrite it and the server couldn't parse the upload.
  const isForm = typeof FormData !== "undefined" && body instanceof FormData;
  if (body !== undefined && !isForm) headers["Content-Type"] = "application/json";

  let res;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : isForm ? body : JSON.stringify(body),
    });
  } catch {
    throw { status: 0, message: MSG_BACKEND_UNREACHABLE };
  }

  if (res.status === HTTP_UNAUTHORIZED) {
    // Sessions are in-memory by design, so a backend restart invalidates
    // them. Drop the dead token instead of retrying with it forever.
    setAuthToken(null);
    window.dispatchEvent(new CustomEvent(EVT_AUTH_EXPIRED));
    throw { status: HTTP_UNAUTHORIZED, message: MSG_SESSION_EXPIRED };
  }

  if (raw) {
    if (!res.ok) {
      // A failed file download still answers in JSON; its detail says why.
      let detail = null;
      try { detail = (await res.json())?.detail; } catch { /* not JSON */ }
      throw { status: res.status, message: detail || `Request failed (${res.status})` };
    }
    return res;
  }

  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!res.ok) {
    throw { status: res.status, message: data?.detail || data?.error || `Request failed (${res.status})` };
  }
  return data;
}
