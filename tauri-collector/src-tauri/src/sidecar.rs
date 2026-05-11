//! Lifecycle for the collector child process.
//!
//! Phase 1a (now): spawns the pip-installed `memento-collector` from PATH.
//! Phase 1b (next): switch the binary path to Tauri's `externalBin`
//! resolved path (PyInstaller frozen collector ships inside the .msi).
//!
//! Stdout/stderr from the child are line-buffered and forwarded to the
//! Tauri frontend via an event channel so the Logs tab can scroll them
//! live.

use std::collections::VecDeque;
use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::thread;

use anyhow::{anyhow, Context, Result};
use parking_lot::Mutex;
use serde::Serialize;
use tauri::{AppHandle, Emitter};

const MAX_LOG_LINES: usize = 500;

#[derive(Debug, Clone, Serialize)]
pub struct Status {
    pub running: bool,
    pub pid: Option<u32>,
    pub started_at: Option<u64>,
    pub exit_code: Option<i32>,
    pub last_error: Option<String>,
}

impl Default for Status {
    fn default() -> Self {
        Self {
            running: false,
            pid: None,
            started_at: None,
            exit_code: None,
            last_error: None,
        }
    }
}

pub struct Sidecar {
    child: Mutex<Option<Child>>,
    status: Mutex<Status>,
    /// Ring buffer of the most recent stdout/stderr lines.
    log_tail: Mutex<VecDeque<String>>,
}

impl Sidecar {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            child: Mutex::new(None),
            status: Mutex::new(Status::default()),
            log_tail: Mutex::new(VecDeque::with_capacity(MAX_LOG_LINES)),
        })
    }

    pub fn status(&self) -> Status {
        self.status.lock().clone()
    }

    pub fn log_snapshot(&self) -> Vec<String> {
        self.log_tail.lock().iter().cloned().collect()
    }

    /// Start the collector. No-op if already running.
    pub fn start(self: &Arc<Self>, app: AppHandle) -> Result<()> {
        let mut child_guard = self.child.lock();
        if child_guard.is_some() {
            return Ok(()); // already running
        }

        // Phase 1a: rely on PATH-installed `memento-collector`. Phase 1b
        // will switch this to the externalBin path resolved via
        // `app.path().resolve(...)`.
        let bin = which_collector()?;
        let mut child = Command::new(&bin)
            .arg("run")
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .with_context(|| format!("spawn {}", bin.display()))?;

        let pid = child.id();
        let stdout = child.stdout.take().expect("stdout was piped");
        let stderr = child.stderr.take().expect("stderr was piped");
        *child_guard = Some(child);

        *self.status.lock() = Status {
            running: true,
            pid: Some(pid),
            started_at: Some(now_unix()),
            exit_code: None,
            last_error: None,
        };
        drop(child_guard);

        // Forward stdout/stderr lines to the UI.
        let app_out = app.clone();
        let me_out = Arc::clone(self);
        thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines().map_while(|r| r.ok()) {
                me_out.push_log(&line, &app_out);
            }
        });
        let app_err = app.clone();
        let me_err = Arc::clone(self);
        thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line in reader.lines().map_while(|r| r.ok()) {
                me_err.push_log(&line, &app_err);
            }
        });

        // Wait-for-exit watcher so status reflects unexpected death.
        let me_wait = Arc::clone(self);
        let app_wait = app.clone();
        thread::spawn(move || {
            // Drop guard before waiting to avoid holding the mutex across
            // a blocking syscall.
            let mut child = me_wait.child.lock().take();
            if let Some(c) = child.as_mut() {
                let exit = c.wait().ok();
                let code = exit.and_then(|s| s.code());
                let mut st = me_wait.status.lock();
                st.running = false;
                st.pid = None;
                st.exit_code = code;
                let snapshot = st.clone();
                drop(st);
                let _ = app_wait.emit("sidecar:status", snapshot);
            }
        });

        let _ = app.emit("sidecar:status", self.status());
        Ok(())
    }

    pub fn stop(&self) -> Result<()> {
        let mut guard = self.child.lock();
        if let Some(mut child) = guard.take() {
            // Best-effort: ask politely first (SIGTERM on POSIX,
            // CTRL_BREAK on Windows would be cleaner — Rust's stdlib
            // only exposes `kill` which sends SIGKILL/TerminateProcess).
            // The collector's signal handler covers SIGINT/SIGTERM on
            // POSIX; on Windows it doesn't, but TerminateProcess
            // disconnects file watchers immediately so this is OK.
            child.kill().ok();
            child.wait().ok();
        }
        let mut st = self.status.lock();
        st.running = false;
        st.pid = None;
        Ok(())
    }

    fn push_log(&self, line: &str, app: &AppHandle) {
        let mut buf = self.log_tail.lock();
        if buf.len() == MAX_LOG_LINES {
            buf.pop_front();
        }
        buf.push_back(line.to_owned());
        let _ = app.emit("sidecar:log", line.to_owned());
    }
}

/// Phase 1a: locate the pip-installed `memento-collector` on PATH.
/// Phase 1b will replace this with the bundled externalBin path.
fn which_collector() -> Result<std::path::PathBuf> {
    let exe_name = if cfg!(windows) {
        "memento-collector.exe"
    } else {
        "memento-collector"
    };
    // PATH lookup
    if let Ok(path) = std::env::var("PATH") {
        let sep = if cfg!(windows) { ';' } else { ':' };
        for dir in path.split(sep) {
            let candidate = std::path::Path::new(dir).join(exe_name);
            if candidate.is_file() {
                return Ok(candidate);
            }
        }
    }
    Err(anyhow!(
        "memento-collector not found on PATH. Install it first \
         (pip install memento-brain-collector) or wait for the \
         Phase 1b sidecar build."
    ))
}

fn now_unix() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}
