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
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
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

/// Update cadence (DEPLOYMENT.md §6). The first check is deliberately
/// delayed — an update prompt in the first seconds of launch is exactly
/// when the operator is trying to start work.
const FIRST_UPDATE_CHECK_DELAY: Duration = Duration::from_secs(120);
const UPDATE_CHECK_INTERVAL: Duration = Duration::from_secs(3 * 24 * 60 * 60);

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

impl BenchState {
    /// "Never prompt or install while the ESP32 is connected or a profile
    /// is running." Recording is included on the same reasoning: an
    /// update mid-recording ends the run.
    fn busy(&self) -> bool {
        self.serial_connected || self.profile_running || self.recording
    }
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
fn spawn_update_loop(app: AppHandle) {
    // A plain thread rather than an async task: the only thing this loop
    // does is sleep for days at a time, and block_on keeps the updater's
    // async API usable without pulling tokio in as a direct dependency.
    std::thread::spawn(move || {
        std::thread::sleep(FIRST_UPDATE_CHECK_DELAY);
        loop {
            tauri::async_runtime::block_on(check_for_update(&app));
            std::thread::sleep(UPDATE_CHECK_INTERVAL);
        }
    });
}

async fn check_for_update(app: &AppHandle) {
    // Gate #1 — before we even ask GitHub. Checking is harmless, but
    // prompting is not, and there is no reason to do the work.
    match read_bench_state() {
        Some(state) if state.busy() => return,
        Some(_) => {}
        // Backend unreachable: it may be mid-restart. Skip this round
        // rather than updating an app whose state we cannot read.
        None => return,
    }

    let updater = match app.updater() {
        Ok(u) => u,
        Err(e) => {
            eprintln!("[Shell] Updater unavailable: {e}");
            return;
        }
    };

    // No internet, GitHub down, malformed manifest — all land here and
    // are deliberately silent to the operator (§6).
    let update = match updater.check().await {
        Ok(Some(update)) => update,
        Ok(None) => return,
        Err(e) => {
            eprintln!("[Shell] Update check failed: {e}");
            return;
        }
    };

    let notes = update.body.clone().unwrap_or_default();
    let prompt = format!(
        "Version {} is available.\n\n{}\n\nThe console will restart to finish installing.",
        update.version,
        if notes.trim().is_empty() { "No release notes." } else { notes.trim() }
    );

    let accepted = app
        .dialog()
        .message(prompt)
        .title("Update available")
        .kind(MessageDialogKind::Info)
        .buttons(MessageDialogButtons::OkCancelCustom(
            "Install and restart".into(),
            "Not now".into(),
        ))
        .blocking_show();

    if !accepted {
        return;
    }

    // Gate #2 — re-checked immediately before installing, because the
    // operator may have connected the rig or started a profile while the
    // dialog was open. §6 calls this out specifically.
    match read_bench_state() {
        Some(state) if state.busy() => {
            app.dialog()
                .message(
                    "The update was not installed: the test stand is connected or a run is in \
                     progress.\n\nDisconnect the ESP32 and stop any running profile, then try \
                     again.",
                )
                .title("Update postponed")
                .kind(MessageDialogKind::Warning)
                .blocking_show();
            return;
        }
        Some(_) => {}
        None => return,
    }

    if let Err(e) = update.download_and_install(|_, _| {}, || {}).await {
        eprintln!("[Shell] Update install failed: {e}");
        app.dialog()
            .message(format!("The update could not be installed:\n{e}"))
            .title("Update failed")
            .kind(MessageDialogKind::Error)
            .blocking_show();
        return;
    }

    // Same handshake as closing the window — the installer is about to
    // replace the process that owns the serial port (§9, "Shutdown via
    // update"), so the backend must be stopped properly first.
    if let Some(sidecar) = app.try_state::<Sidecar>() {
        stop_sidecar_gracefully(&sidecar);
    }
    app.restart();
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
                    spawn_update_loop(watcher);
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
