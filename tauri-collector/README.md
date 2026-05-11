# Memento — desktop app (Tauri)

A native desktop wrapper around the Python `memento-collector` daemon. End
users install a single `.msi` / `.dmg` / `.AppImage`, configure their
server URL + token in the Settings window, and the collector runs in the
background — no Python, no terminal commands, no pyenv.

> Status: **Phase 1a skeleton** — window + tray + sidecar IPC works
> against a pip-installed `memento-collector` on the dev machine. The
> PyInstaller sidecar (no-Python-needed) is wired but not yet built. See
> `sidecar/README.md` for the freezing pipeline.

## Architecture at a glance

```
┌────────────────────────────────────────────────────────────────┐
│  Tauri shell  (Rust + WebView)                                 │
│                                                                │
│  ┌───────────────────┐    spawn / signal    ┌───────────────┐  │
│  │ Settings UI       │ ────────────────────▶│  sidecar      │  │
│  │ dist/index.html   │◀───── logs / status ─│  collector    │  │
│  │ (vanilla HTML/CSS)│                      │  (frozen .exe) │ │
│  └───────────────────┘                      └───────────────┘  │
│         │                                          │           │
│         │ IPC (tauri::command)                     │ HTTPS     │
│         ▼                                          ▼           │
│  ┌───────────────────┐                      ┌───────────────┐  │
│  │ Tauri Rust core   │                      │ Memento API   │  │
│  │ - tray menu       │                      │ (server)      │  │
│  │ - autolaunch      │                      └───────────────┘  │
│  │ - config persist  │                                         │
│  └───────────────────┘                                         │
└────────────────────────────────────────────────────────────────┘
```

The collector is **the same Python code** shipped to PyPI as
`memento-brain-collector`. Tauri's only job is the GUI, lifecycle, and
packaging.

## Layout

```
tauri-collector/
├── README.md                  ← you are here
├── src-tauri/                 Rust + Tauri 2.x project
│   ├── Cargo.toml
│   ├── tauri.conf.json        bundle id / icons / installer config
│   ├── build.rs
│   ├── icons/                 .ico / .png / .icns (TODO: replace placeholders)
│   └── src/
│       ├── main.rs            entry · window · tray · plugins
│       ├── sidecar.rs         spawn / monitor / signal the collector child
│       ├── config.rs          read/write config shared with the Python side
│       └── ipc.rs             #[tauri::command] handlers called by the UI
├── dist/                      Vanilla HTML/CSS/JS frontend (NO bundler)
│   ├── index.html
│   ├── styles.css             Aurora palette (matches web/)
│   └── app.js                 ~150 LOC, no framework
└── sidecar/                   PyInstaller pipeline
    ├── README.md
    ├── collector.spec         PyInstaller spec
    └── build_sidecar.py       one-command freezer
```

## Development (macOS dev → Windows release)

Prerequisites:
- Rust 1.75+  (`curl https://sh.rustup.rs -sSf | sh`)
- Node 20+ (only for `cargo tauri` CLI; the dist/ frontend is plain HTML)
- `cargo install tauri-cli --version "^2.0"`
- For sidecar building: Python 3.11+ with `pip install pyinstaller` on the
  **target** platform (PyInstaller doesn't cross-compile)

Run in dev mode (live-reload):

```sh
cd tauri-collector
cargo tauri dev
```

Build a release artifact for the current platform:

```sh
cargo tauri build
```

On macOS this produces `.dmg` + `.app`. On Windows it produces `.msi`.

### Building the Windows installer

PyInstaller can't cross-compile, so the Windows sidecar must be built on a
Windows machine (or via GitHub Actions Windows runner).

On the Windows target machine:

```powershell
# 1. Build the frozen Python collector (one-shot)
cd tauri-collector\sidecar
python build_sidecar.py

# 2. Build the Tauri installer
cd ..
cargo tauri build
# → src-tauri/target/release/bundle/msi/Memento_*.msi
```

## Coexistence with the existing pip-installed collector

If a user already has `memento-collector` installed via pip (and
launchd / systemd / schtasks), the Tauri app will **detect and refuse to
start** until the legacy install is removed (`memento-collector
uninstall`). Running both produces double sync + duplicate documents.

## Roadmap

- **Phase 1a** (current): Skeleton — window opens, tray appears, can
  spawn pip-installed `memento-collector` and stream its log
- **Phase 1b**: PyInstaller sidecar — fully self-contained binary
- **Phase 2**: Settings UI complete (per-tool toggles, Obsidian vault
  picker, log viewer, autostart toggle)
- **Phase 3**: Auto-update via Tauri's updater
- **Phase 4**: macOS + Linux packaging
- **Phase 5**: Code-signing + notarization for distribution
