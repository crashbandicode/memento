"""Entry point for the frozen MCP server sidecar.

This is the MCP-protocol-speaking memory server bundled inside the
Tauri app. AI IDEs (Claude Code, Cursor, Codex, ...) spawn it as a
child process and talk to it over stdio.

We just defer to `mcp_server.__main__:main`. The sidecar reads
MEMENTO_SERVER_URL + MEMENTO_SERVER_TOKEN from env (set by the AI
IDE config that the desktop app writes during Save), connects to
the Memento API, and exposes memory_search / memory_recall /
daily_summary tools over MCP stdio.
"""

from __future__ import annotations

import sys

# Windows stdio gotcha: AI IDEs launch the sidecar with line-buffered
# pipes for MCP framing. PyInstaller-frozen Python on Windows defaults
# stdout/stderr to whatever code page the parent uses (often cp936),
# breaking JSON encoding for non-ASCII memory content. Force utf-8.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from mcp_server.__main__ import main


if __name__ == "__main__":
    main()
