"""Entry point for the frozen collector sidecar.

PyInstaller can't directly freeze a `python -m collector.main` invocation;
it needs a real script file. This wraps `collector.main:main` and accepts
the same `run` subcommand the Tauri shell spawns.
"""

from __future__ import annotations

import sys


def cli() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "claude-hook":
        from collector.claude_pending_hook import main as hook_main

        sys.exit(hook_main([]))
    if len(sys.argv) >= 2 and sys.argv[1] == "install-claude-hooks":
        from collector.claude_pending_hook import main as hook_main

        sys.exit(hook_main(["--install"]))
    # Mimic `memento-collector run` — the only entry path the desktop app
    # uses. All other subcommands (setup, install, uninstall, status) are
    # owned by the Tauri shell; we never want the frozen binary to mess
    # with launchd / systemd / schtasks on its own.
    if len(sys.argv) >= 2 and sys.argv[1] != "run":
        print(
            f"sidecar: ignoring subcommand {sys.argv[1]!r}; only 'run' is supported "
            "(service install/uninstall is handled by the desktop app)",
            file=sys.stderr,
        )
        sys.exit(2)
    from collector.main import main

    main()


if __name__ == "__main__":
    cli()
