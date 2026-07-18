from server.scripts.backfill_assistant_identity import (
    AssistantIdentityRow,
    plan_identity_overlay,
)


def _row(
    line: int,
    content: str,
    *,
    model: str = "",
    effort: str = "",
) -> AssistantIdentityRow:
    metadata = {}
    if model:
        metadata["model"] = model
    if effort:
        metadata["reasoning_effort"] = effort
    return AssistantIdentityRow(line, "agent_message", content, metadata)


def test_overlay_uses_exact_line_without_overwriting_existing_identity() -> None:
    existing = [_row(4, "first"), _row(8, "second", model="kept")]
    parsed = [
        _row(4, "first", model="gpt-5.6-sol", effort="xhigh"),
        _row(8, "second", model="different", effort="high"),
    ]

    updates = plan_identity_overlay(existing, parsed)

    assert updates[0].line_number == 4
    assert updates[0].metadata_patch == {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
    }
    assert updates[0].match_kind == "line"
    assert updates[1].metadata_patch == {"reasoning_effort": "high"}


def test_overlay_recovers_a_unique_row_after_parser_line_shift() -> None:
    existing = [_row(10, "stable content")]
    parsed = [_row(12, "stable content", model="gpt-5.6-sol")]

    updates = plan_identity_overlay(existing, parsed)

    assert len(updates) == 1
    assert updates[0].line_number == 10
    assert updates[0].match_kind == "unique_content"


def test_overlay_rejects_ambiguous_shifted_duplicates() -> None:
    existing = [_row(10, "same"), _row(20, "same")]
    parsed = [
        _row(11, "same", model="gpt-5.6-sol"),
        _row(21, "same", model="gpt-5.6-sol"),
    ]

    assert plan_identity_overlay(existing, parsed) == []


def test_overlay_requires_identical_content_even_if_line_matches() -> None:
    existing = [_row(10, "current")]
    parsed = [_row(10, "older", model="gpt-5.6-sol")]

    assert plan_identity_overlay(existing, parsed) == []
