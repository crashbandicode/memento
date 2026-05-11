# Sidecar — frozen Python collector

PyInstaller pipeline that turns `memento-brain-collector` into a single
executable that ships inside the Tauri `.msi` / `.dmg`. End users never
see a `pip install`, never see a Python interpreter, never see a console
window.

## One-shot build

```sh
# Prerequisites on the build machine: Python 3.11+, pip
pip install -e ../../collector pyinstaller
python build_sidecar.py
```

This writes the binary to
`tauri-collector/src-tauri/binaries/memento-collector-sidecar-<triple>.exe`
(or no extension on POSIX), matching the path Tauri expects from the
`externalBin` declaration in `tauri.conf.json`.

The `<triple>` suffix is required by Tauri's sidecar resolver — it
picks the right binary at install time based on the target OS+arch. On
this machine the script prints the detected triple and writes the file
accordingly.

## Why PyInstaller and not Nuitka / pyoxidizer?

- **PyInstaller**: simplest, most mature, ~80 MB binary. Wins here.
- **Nuitka**: smaller (~30 MB) but compile times are 20× longer and
  per-tool parsers occasionally trip the C++ output.
- **pyoxidizer**: nice idea, dead upstream.

## Notes on Windows AV false-positives

PyInstaller-frozen Python apps that talk to the file system + network
are a classic shape for AV heuristics. We can defang most of this by:

1. Setting a proper version resource (handled in `collector.spec`).
2. Code-signing the binary before bundling into the .msi (handled in
   the Tauri build, not here).
3. Pinning to a stable PyInstaller release rather than `--onedir`.

If a specific AV still flags it, submit a false-positive report — the
sidecar doesn't do anything exotic, it just watches files and POSTs
JSON.
