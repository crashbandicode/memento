# PyInstaller spec for the Memento MCP server sidecar.
#
# Produces a single executable that AI IDEs (Claude Code, Cursor,
# Codex, ...) launch over stdio. Bundled inside the Tauri app — the
# desktop app's Rust layer writes MCP config entries pointing at this
# binary, so users don't need pip install memento-brain-memory.
#
# Don't invoke directly — use build_sidecar.py.

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# mcp_server depends on a lot of stuff that's loaded by name at runtime
# (FastMCP tool registration, openai async client variants, asyncpg
# native extension, jsonschema validators). Use collect_all to grab the
# full transitive pile, otherwise the runtime imports fail downstream.
hidden = collect_submodules("mcp_server")

extra_datas = []
extra_binaries = []
for pkg in (
    "mcp",            # MCP SDK
    "mcp_server",
    "starlette",
    "uvicorn",
    "anyio",
    "httpx",
    "httpcore",
    "h11",
    "openai",
    "asyncpg",
    "sqlalchemy",
    "pgvector",
    "pydantic",
    "pydantic_core",
    "pydantic_settings",
    "cryptography",
    "jsonschema",
    "jsonschema_specifications",
    "referencing",
    "rpds",
):
    try:
        d, b, h = collect_all(pkg)
        extra_datas.extend(d)
        extra_binaries.extend(b)
        hidden.extend(h)
    except Exception:
        pass

a = Analysis(
    ["mcp_entry.py"],
    pathex=[],
    binaries=extra_binaries,
    datas=extra_datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "pytest",
        "IPython",
        "collector",                # the other sidecar lives in its own .exe
        "memento_brain_collector",
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="memento-mcp-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # UPX triggers Windows AV heuristics
    upx_exclude=[],
    runtime_tmpdir=None,
    # MCP server runs in stdio mode — needs an actual console attached
    # to flush stdin/stdout. `console=True` here is critical: with
    # `console=False`, PyInstaller wraps in pythonw.exe which has NO
    # stdio handles, so MCP framing immediately breaks. The AI IDE
    # spawns us with redirected pipes, so the user never sees a window
    # even though `console=True`.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
