"""Canonical metadata for tool identifiers understood by the server."""

TOOL_DISPLAY_NAMES: dict[str, str] = {
    "antigravity": "Antigravity",
    "claude_code": "Claude Code",
    "codex": "Codex",
    "cursor": "Cursor",
    "hermes": "Hermes",
    "obsidian": "Obsidian",
    "openclaw": "OpenClaw",
    "vscode": "VS Code",
    "windsurf": "Windsurf",
}


def tool_display_name(tool_id: str) -> str:
    """Return a stable label while remaining useful for unknown tool IDs."""
    return TOOL_DISPLAY_NAMES.get(tool_id, tool_id.replace("_", " ").title())
