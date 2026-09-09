// The dashboard's only contact with the Tauri shell.
//
// Everything else in the frontend talks to the backend over HTTP/WS and
// has no idea it is running inside a desktop app. This module is the one
// exception — updates are the shell's job, not the backend's, because
// the shell is the process that gets replaced.
//
// The same bundle also runs in a plain browser during `npm run dev`,
// where none of this exists. So every entry point here answers safely
// when there is no shell, and `isDesktop()` lets the UI say "only
// available in the desktop app" instead of failing on a click.

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

import { UPDATE_STATE, EVT_UPDATE_PROGRESS } from "./constants";

/** True only inside the Tauri webview. Vite's dev server sets nothing. */
export function isDesktop() {
  return typeof window !== "undefined" && !!window.__TAURI_INTERNALS__;
}

/**
 * Ask the release channel whether a newer version is published.
 *
 * Only ever called from an explicit operator click — nothing polls. That
 * is what lets the repository stay private between releases: the update
 * endpoint has to be reachable in this moment, not permanently.
 *
 * Resolves to { state, current_version, version, title, size_bytes, detail }
 * where state is one of UPDATE_STATE. Never rejects for an unreachable
 * channel — that arrives as state "unreachable", because a private repo
 * and a network outage are normal conditions here, not errors.
 */
export async function checkForUpdate() {
  if (!isDesktop()) {
    return { state: UPDATE_STATE.UNSUPPORTED };
  }
  try {
    return await invoke("updates_check");
  } catch (e) {
    return { state: UPDATE_STATE.UNREACHABLE, detail: String(e) };
  }
}

/**
 * Download and install. Rejects with a message meant to be shown as-is:
 * the shell refuses while the ESP32 is connected or a run is live, and
 * names which one, since the operator is at the bench and can fix it.
 *
 * On success the app is replaced and restarted, so nothing after this
 * resolves in practice.
 */
export async function installUpdate() {
  if (!isDesktop()) throw new Error("Updates are only available in the desktop app.");
  return invoke("updates_install");
}

/**
 * Subscribe to download progress. Returns an unlisten function, or a
 * no-op outside the desktop app so callers can always clean up.
 */
export async function onUpdateProgress(handler) {
  if (!isDesktop()) return () => {};
  return listen(EVT_UPDATE_PROGRESS, (event) => handler(event.payload));
}
