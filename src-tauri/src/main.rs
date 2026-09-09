// NWA Bench Console — Tauri desktop shell.
//
// This process owns the window and the lifecycle of the frozen FastAPI
// backend ("the sidecar"). It deliberately does very little else: the
// dashboard is the same React app as always, and it still talks to the
// backend over HTTP/WebSocket on loopback.
//
// The one thing here that is genuinely safety-critical is the shutdown
// handshake (DEPLOYMENT.md §9). The stand produces up to 100 kgf, and
// the backend's shutdown handler is what aborts a running throttle
// profile and writes THR_MIN to the ESC. Killing the sidecar outright
// skips that handler, and the ESC keeps driving whatever it last
// received — there is no firmware-side watchdog to catch it. So every
// path that stops the sidecar (window close, update install, orphan
// cleanup) goes through `stop_sidecar_gracefully`, never a bare kill.

// Release builds open no console window; without this Windows attaches
// one behind the app and it flashes on every launch.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tauri::{AppHandle, Emitter, Manager, WindowEvent};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};
use tauri_plugin_updater::UpdaterExt;

// Must match backend/constants.py — DEFAULT_HOST / DEFAULT_PORT and the
// SHUTDOWN_TOKEN_ENV / SHUTDOWN_TOKEN_HEADER pair. Hand-duplicated, like
// THR_MIN/THR_MAX already are across firmware, backend and frontend;
// backend/tests/test_deployment.py fails if these drift.
const BACKEND_HOST: &str = "127.0.0.1";
const BACKEND_PORT: u16 = 8000;
const SHUTDOWN_TOKEN_ENV: &str = "NWA_SHUTDOWN_TOKEN";
const SHUTDOWN_TOKEN_HEADER: &str = "X-Shutdown-Token";

/// How long to wait for the sidecar to start answering /health before
/// telling the operator it failed. Generous: a cold start on a slow disk
/// with matplotlib and pandas in the bundle is not instant.
const STARTUP_TIMEOUT: Duration = Duration::from_secs(60);
const HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(250);

/// How long to let the backend shut down cleanly before force-killing.
/// The graceful path aborts the profile, cuts throttle, flushes the
/// recording and closes the port; that is fast, but it does perform a
/// serial write, so this is not tight.
const SHUTDOWN_GRACE: Duration = Duration::from_secs(10);

/// Last lines of the sidecar's own stdout/stderr, kept so a failed
/// startup can show the operator something better than a blank window.
const LOG_TAIL_LINES: usize = 60;

fn backend_url(path: &str) -> String {
    format!("http://{BACKEND_HOST}:{BACKEND_PORT}{path}")
}

/// Random hex token, fresh per launch, passed to the sidecar in its
/// environment. It gates /shutdown — that endpoint stops a machine that
/// may be driving a motor, so it must not be callable by any other local
/// process that happens to find the port.
fn generate_token() -> String {
    let mut bytes = [0u8; 32];
    getrandom::getrandom(&mut bytes).expect("OS CSPRNG unavailable");
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Everything the shell needs to manage the backend process.
struct Sidecar {
    child: Mutex<Option<Child>>,
    token: String,
    log_tail: Arc<Mutex<Vec<String>>>,
}

impl Sidecar {
    fn new(token: String) -> Self {
        Self {
            child: Mutex::new(None),
            token,
            log_tail: Arc::new(Mutex::new(Vec::new())),
        }
    }

    fn logs(&self) -> String {
        self.log_tail
            .lock()
            .map(|l| l.join("\n"))
            .unwrap_or_else(|_| "(log unavailable)".into())
    }
}

/// Drain a child pipe into the shared tail buffer.
///
/// This thread is not optional bookkeeping: with `Stdio::piped()` an
/// undrained pipe fills its buffer and then blocks the child on its next
/// write. The backend prints on every connect/disconnect, so a sidecar
/// nobody was reading would eventually wedge mid-test.
fn drain_pipe<R: std::io::Read + Send + 'static>(reader: R, tail: Arc<Mutex<Vec<String>>>) {
    std::thread::spawn(move || {
        for line in BufReader::new(reader).lines().map_while(Result::ok) {
            if let Ok(mut buf) = tail.lock() {
                buf.push(line);
                let overflow = buf.len().saturating_sub(LOG_TAIL_LINES);
                if overflow > 0 {
                    buf.drain(0..overflow);
                }
            }
        }
    });
}

/// Launch the frozen backend from the bundled resource directory.
fn spawn_sidecar(app: &AppHandle, sidecar: &Sidecar) -> Result<(), String> {
    let exe = app
        .path()
        .resource_dir()
        .map_err(|e| format!("Could not resolve the resource directory: {e}"))?
        .join("backend")
        .join("thrustbench-backend.exe");

    if !exe.exists() {
        return Err(format!(
            "The backend executable is missing from this install:\n{}",
            exe.display()
        ));
    }

    let mut cmd = Command::new(&exe);
    cmd.env(SHUTDOWN_TOKEN_ENV, &sidecar.token)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        // PyInstaller resolves _internal/ relative to the executable, so
        // the working directory must be the bundle's own folder.
        .current_dir(exe.parent().unwrap_or_else(|| std::path::Path::new(".")));

    // CREATE_NO_WINDOW — otherwise the console subsystem child pops a
    // black window in front of the dashboard on every launch.
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000);
    }

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("Could not start the backend process: {e}"))?;

    if let Some(out) = child.stdout.take() {
        drain_pipe(out, sidecar.log_tail.clone());
    }
    if let Some(err) = child.stderr.take() {
        drain_pipe(err, sidecar.log_tail.clone());
    }

    *sidecar.child.lock().unwrap() = Some(child);
    Ok(())
}

fn http() -> reqwest::blocking::Client {
    reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(3))
        .build()
        .expect("failed to build HTTP client")
}

/// Poll /health until the sidecar answers, or the timeout expires.
///
/// A readiness probe rather than a fixed sleep: cold-start time varies
/// with disk speed and antivirus, and a sleep long enough to be safe on
/// a slow machine is a sleep wasted on every launch of a fast one.
fn wait_for_health(deadline: Instant) -> bool {
    let client = http();
    while Instant::now() < deadline {
        if let Ok(res) = client.get(backend_url("/health")).send() {
            if res.status().is_success() {
                return true;
            }
        }
        std::thread::sleep(HEALTH_POLL_INTERVAL);
    }
    false
}

/// What the backend is currently doing — the gate for updates (§6).
#[derive(Default, Clone, Copy)]
struct BenchState {
    serial_connected: bool,
    profile_running: bool,
    recording: bool,
}

fn read_bench_state() -> Option<BenchState> {
    let res = http().get(backend_url("/health")).send().ok()?;
    let body: serde_json::Value = res.json().ok()?;
    Some(BenchState {
        serial_connected: body["serial_connected"].as_bool().unwrap_or(false),
        profile_running: body["profile_running"].as_bool().unwrap_or(false),
        recording: body["recording"].as_bool().unwrap_or(false),
    })
}

/// Ask the backend to stop, wait for it, and only then kill.
///
/// Returns true if it exited on its own. This is the function the whole
/// §9 argument is about — see the module header.
fn stop_sidecar_gracefully(sidecar: &Sidecar) -> bool {
    // If it already exited (crash, or a previous call), there is nothing
    // to do and nothing to warn about.
    {
        let mut guard = sidecar.child.lock().unwrap();
        match guard.as_mut() {
            None => return true,
            Some(child) => {
                if matches!(child.try_wait(), Ok(Some(_))) {
                    *guard = None;
                    return true;
                }
            }
        }
    }

    // Ask. A failure here is not fatal — the wait/kill below still runs —
    // but it is the path that cuts the throttle, so it goes first.
    let asked = http()
        .post(backend_url("/shutdown"))
        .header(SHUTDOWN_TOKEN_HEADER, &sidecar.token)
        .send()
        .map(|r| r.status().is_success())
        .unwrap_or(false);

    if !asked {
        eprintln!("[Shell] /shutdown did not accept the request; waiting anyway before killing");
    }

    // Wait. try_wait rather than wait, so the grace period is bounded:
    // a backend wedged on a serial write must not hang the close forever.
    let deadline = Instant::now() + SHUTDOWN_GRACE;
    let mut guard = sidecar.child.lock().unwrap();
    if let Some(child) = guard.as_mut() {
        while Instant::now() < deadline {
            match child.try_wait() {
                Ok(Some(_)) => {
                    *guard = None;
                    return true;
                }
                Ok(None) => std::thread::sleep(Duration::from_millis(100)),
                Err(_) => break,
            }
        }
        // Last resort. Reaching here means the throttle cut may not have
        // happened, so it is worth a line in the log.
        eprintln!("[Shell] Backend did not stop within the grace period — killing it");
        let _ = child.kill();
        let _ = child.wait();
    }
    *guard = None;
    false
}

/// Background loop: check for updates, respecting the bench-busy gate.
// ================================================================
// UPDATES — checked only when the operator asks
// ================================================================
// Deliberately manual. The earlier design polled GitHub every 3 days.
//
// It was first made on-demand so the repository could sit private
// between releases; that plan was dropped, because GitHub Actions is
// free for public repos and metered for private ones, and going private
// would have billed the very Windows builds that produce the releases.
// The repo stays public (DEPLOYMENT.md §6, §12 O4).
//
// The manual design was kept regardless, on reasons that never depended
// on visibility: nothing reaches the network in the background from a
// bench machine, and no update prompt can interrupt a session — the
// operator picks the moment, the same instinct that refuses calibration
// writes mid-profile.
//
// What has NOT changed is the safety rule (DEPLOYMENT.md §6): an update
// replaces the process that owns the serial port and the throttle cut,
// so it must never install mid-test. The manual button removes the
// "don't interrupt them" half of that rule — the operator chose this
// moment — but not the "don't kill a live run" half, which is enforced
// in `updates_install` and re-checked immediately before installing.

/// Progress events pushed to the dashboard while a download runs.
const EVENT_UPDATE_PROGRESS: &str = "update://progress";

#[derive(Clone, serde::Serialize)]
struct UpdateProgress {
    downloaded: u64,
    total: Option<u64>,
}

/// The result of a check, in the shape the Updates panel renders.
///
/// `state` separates three outcomes that are easy to conflate and need
/// different words in front of the operator:
///
///   "available"   — a newer version exists
///   "none"        — the manifest was read and we are current
///   "unreachable" — the manifest could not be read at all
///
/// GitHub answers 404 both for a repo the caller cannot see and for one
/// with no releases yet, so "unreachable" cannot tell those apart — but
/// it is very much distinguishable from "you are up to date", and
/// reporting it as that would turn a real connectivity failure into
/// false reassurance. With the repo public this should be rare, which is
/// exactly why it is worth naming when it happens.
#[derive(Default, serde::Serialize)]
struct UpdateInfo {
    state: String,
    current_version: String,
    version: Option<String>,
    title: Option<String>,
    size_bytes: Option<u64>,
    detail: Option<String>,
}

/// Ask the release channel what the newest published version is.
#[tauri::command]
async fn updates_check(app: AppHandle) -> Result<UpdateInfo, String> {
    let current = app.package_info().version.to_string();
    let updater = app.updater().map_err(|e| e.to_string())?;

    match updater.check().await {
        Ok(Some(update)) => {
            // Size is best-effort. GitHub redirects release assets to a
            // storage host that does report Content-Length, but a HEAD
            // that fails is not worth failing the whole check over — the
            // panel simply omits the size.
            let url = update.download_url.to_string();
            let size = tauri::async_runtime::spawn_blocking(move || content_length(&url))
                .await
                .ok()
                .flatten();

            Ok(UpdateInfo {
                state: "available".into(),
                current_version: current,
                version: Some(update.version.clone()),
                // The release body, which the release workflow seeds
                // from the tagged commit's subject line.
                title: update.body.clone().filter(|b| !b.trim().is_empty()),
                size_bytes: size,
                detail: None,
            })
        }
        Ok(None) => Ok(UpdateInfo {
            state: "none".into(),
            current_version: current,
            ..Default::default()
        }),
        Err(e) => Ok(UpdateInfo {
            state: "unreachable".into(),
            current_version: current,
            detail: Some(e.to_string()),
            ..Default::default()
        }),
    }
}

/// HEAD the asset to learn its size before committing to a download.
fn content_length(url: &str) -> Option<u64> {
    reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .ok()?
        .head(url)
        .send()
        .ok()
        .filter(|r| r.status().is_success())?
        .headers()
        .get(reqwest::header::CONTENT_LENGTH)?
        .to_str()
        .ok()?
        .parse()
        .ok()
}

/// Download and install, reporting progress as it goes.
#[tauri::command]
async fn updates_install(app: AppHandle) -> Result<(), String> {
    // Gate #1. The operator asked for this, so the answer is a sentence
    // they can act on — "disconnect and try again" — not silence.
    if let Some(reason) = busy_reason() {
        return Err(reason);
    }

    let updater = app.updater().map_err(|e| e.to_string())?;
    let update = updater
        .check()
        .await
        .map_err(|e| format!("Could not reach the update channel: {e}"))?
        .ok_or_else(|| "There is no update to install.".to_string())?;

    let downloaded = std::sync::atomic::AtomicU64::new(0);
    let bytes = update
        .download(
            |chunk, total| {
                let so_far = downloaded
                    .fetch_add(chunk as u64, std::sync::atomic::Ordering::Relaxed)
                    + chunk as u64;
                let _ = app.emit(
                    EVENT_UPDATE_PROGRESS,
                    UpdateProgress { downloaded: so_far, total },
                );
            },
            || {},
        )
        .await
        .map_err(|e| format!("Download failed: {e}"))?;

    // Gate #2 — re-checked after the download, because it takes time and
    // the operator may have reconnected the rig or started a run while
    // it ran. §6 calls this re-check out specifically.
    if let Some(reason) = busy_reason() {
        return Err(reason);
    }

    // The installer is about to replace the process holding the serial
    // port, so the backend stops through the same handshake a window
    // close uses — never a kill (§9, "Shutdown via update").
    if let Some(sidecar) = app.try_state::<Sidecar>() {
        stop_sidecar_gracefully(&sidecar);
    }

    update.install(bytes).map_err(|e| format!("Install failed: {e}"))?;
    Ok(())
}

/// Why an update must not install right now, if it must not.
///
/// Returns None when it is safe. A backend we cannot reach is treated as
/// unsafe: it may be mid-restart with a profile still running, and
/// guessing "probably idle" is the wrong way to be wrong here.
fn busy_reason() -> Option<String> {
    match read_bench_state() {
        Some(state) if state.profile_running => {
            Some("A throttle profile is running. Stop it before installing an update.".into())
        }
        Some(state) if state.recording => {
            Some("A recording is in progress. Stop it before installing an update.".into())
        }
        Some(state) if state.serial_connected => Some(
            "The ESP32 is still connected. Disconnect it before installing an update — \
             installing restarts the console, which drops the link to the stand."
                .into(),
        ),
        Some(_) => None,
        None => Some(
            "Cannot read the backend's state, so it is not safe to install right now. \
             Try again in a moment."
                .into(),
        ),
    }
}

fn main() {
    let token = generate_token();

    tauri::Builder::default()
        // Registered first, as the plugin's docs require. A second copy
        // would spawn a second sidecar, and the two would fight over
        // COM6 — the exact failure the desktop shape exists to prevent.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_process::init())
        .manage(Sidecar::new(token))
        .invoke_handler(tauri::generate_handler![updates_check, updates_install])
        .setup(|app| {
            let handle = app.handle().clone();
            let sidecar = app.state::<Sidecar>();

            if let Err(err) = spawn_sidecar(&handle, &sidecar) {
                // Off the main thread: blocking_show() here would be
                // waiting on the event loop that has not started yet.
                let reporter = handle.clone();
                std::thread::spawn(move || {
                    reporter
                        .dialog()
                        .message(format!("{err}\n\nThe console cannot start."))
                        .title("Could not start NWA Bench Console")
                        .kind(MessageDialogKind::Error)
                        .blocking_show();
                    reporter.exit(1);
                });
                return Ok(());
            }

            // Readiness is watched off-thread so the window paints
            // immediately. The dashboard already handles "backend not
            // reachable yet" by retrying, so there is nothing to wait
            // for visually — this thread exists only to report failure.
            let watcher = handle.clone();
            std::thread::spawn(move || {
                let deadline = Instant::now() + STARTUP_TIMEOUT;
                if wait_for_health(deadline) {
                    let _ = watcher.emit("backend-ready", ());
                    return;
                }

                let sidecar = watcher.state::<Sidecar>();
                let logs = sidecar.logs();
                let detail = if logs.trim().is_empty() {
                    "The backend produced no output.".to_string()
                } else {
                    logs
                };
                watcher
                    .dialog()
                    .message(format!(
                        "The backend did not start within {} seconds.\n\n\
                         Its last output was:\n\n{detail}",
                        STARTUP_TIMEOUT.as_secs()
                    ))
                    .title("Could not start NWA Bench Console")
                    .kind(MessageDialogKind::Error)
                    .blocking_show();
                stop_sidecar_gracefully(&sidecar);
                watcher.exit(1);
            });

            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                // Hold the window open until the backend has actually
                // stopped. Closing first and cleaning up after would
                // race the throttle cut against process teardown.
                api.prevent_close();

                let app = window.app_handle().clone();
                let window = window.clone();
                // Off the UI thread: the graceful stop can take seconds,
                // and a frozen window during it looks like a hang.
                std::thread::spawn(move || {
                    if let Some(sidecar) = app.try_state::<Sidecar>() {
                        stop_sidecar_gracefully(&sidecar);
                    }
                    let _ = window.destroy();
                    app.exit(0);
                });
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running NWA Bench Console");
}
