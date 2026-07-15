from server.tool_catalog import tool_display_name


def test_tool_display_name_uses_canonical_catalog() -> None:
    assert tool_display_name("claude_code") == "Claude Code"
    assert tool_display_name("vscode") == "VS Code"


def test_tool_display_name_formats_unknown_identifiers() -> None:
    assert tool_display_name("future_agent") == "Future Agent"
