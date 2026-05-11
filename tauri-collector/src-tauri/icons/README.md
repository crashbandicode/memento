# Icons — replace these placeholders

Tauri's bundler needs platform-specific icons in this folder. The
`tauri.conf.json` references them by name, and the build fails fast if
they're missing.

The fastest way to populate this folder is from the Tauri CLI:

```sh
cd tauri-collector
cargo install tauri-cli --version "^2.0"
cargo tauri icon ../../web/public/favicon.png
```

Tauri will derive every required size + format (`.png`, `.ico`, `.icns`)
from the source PNG and write them here. We use the web favicon as the
source so the desktop app and the web UI stay visually consistent.

Files needed (Tauri creates all of these from one source):

- `32x32.png`
- `128x128.png`
- `128x128@2x.png`
- `icon.icns`     (macOS)
- `icon.ico`      (Windows)
- `icon.png`      (Linux + tray icon)
