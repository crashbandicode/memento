# PyInstaller spec for the lightweight per-invocation Claude hook runner.
#
# Unlike collector.spec this is deliberately an onedir build.  Hook processes
# start directly from their installed directory and never unpack a onefile
# archive into a _MEI temp directory.

import sys
from pathlib import Path

REPO_ROOT = Path(SPECPATH).resolve().parents[1]
COLLECTOR_SOURCE = REPO_ROOT / "collector"
sys.path.insert(0, str(COLLECTOR_SOURCE))

block_cipher = None

# hook_runner imports the implementations lazily, so name both explicitly.
# Do not collect the collector package wholesale: the daemon, parsers, MCP,
# HTTP client, and sync stack are intentionally absent from this artifact.
a = Analysis(
    ["hook_entry.py"],
    pathex=[str(COLLECTOR_SOURCE)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "collector.claude_pending_hook",
        "collector.handoff_governor_hook",
        "orjson",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "pytest",
        "IPython",
        "memento_brain_memory",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="memento-hook-runner",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="memento-hook-runner",
)
