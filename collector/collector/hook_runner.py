"""Minimal command dispatcher for the standalone Claude hook runner.

This module intentionally imports neither hook implementation until the
corresponding command is invoked.  The PyInstaller onedir artifact that uses
it therefore starts with only this small dispatcher and the selected hook's
runtime dependencies.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Dispatch one supported Claude hook command."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        return 2
    command, *command_arguments = arguments
    if command == "claude-hook":
        from .claude_pending_hook import main as pending_main

        return pending_main(command_arguments)
    if command == "claude-governor-hook":
        from .handoff_governor_hook import main as governor_main

        return governor_main(command_arguments)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
