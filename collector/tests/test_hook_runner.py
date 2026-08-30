from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector import hook_runner  # noqa: E402


def test_dispatches_pending_hook_with_remaining_arguments(
    monkeypatch,
) -> None:
    observed: list[list[str]] = []
    pending = types.ModuleType("collector.claude_pending_hook")
    pending.main = lambda arguments: observed.append(arguments) or 17
    monkeypatch.setitem(sys.modules, "collector.claude_pending_hook", pending)

    assert hook_runner.main(["claude-hook", "--test"]) == 17
    assert observed == [["--test"]]


def test_dispatches_governor_hook_with_remaining_arguments(
    monkeypatch,
) -> None:
    observed: list[list[str]] = []
    governor = types.ModuleType("collector.handoff_governor_hook")
    governor.main = lambda arguments: observed.append(arguments) or 23
    monkeypatch.setitem(sys.modules, "collector.handoff_governor_hook", governor)

    assert hook_runner.main(["claude-governor-hook", "--enabled"]) == 23
    assert observed == [["--enabled"]]


def test_rejects_unknown_or_missing_hook_command() -> None:
    assert hook_runner.main([]) == 2
    assert hook_runner.main(["not-a-hook"]) == 2
