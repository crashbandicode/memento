from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.ingest_service import normalize_ingest_category  # noqa: E402
from server.scripts.reclassify_claude_sidecars import _predicate  # noqa: E402


def test_legacy_claude_sidecar_is_reclassified_at_ingest_boundary() -> None:
    assert normalize_ingest_category(
        "claude_code",
        "conversation",
        "projects/demo/session/subagents/agent-abc.meta.json",
    ) == "state"


def test_claude_transcript_and_unrelated_metadata_keep_their_categories() -> None:
    assert normalize_ingest_category(
        "claude_code",
        "conversation",
        "projects/demo/session/subagents/agent-abc.jsonl",
    ) == "conversation"
    assert normalize_ingest_category(
        "claude_code",
        "conversation",
        "projects/demo/session.meta.json",
    ) == "conversation"
    assert normalize_ingest_category(
        "cursor",
        "conversation",
        "projects/demo/subagents/agent-abc.meta.json",
    ) == "conversation"


def test_sidecar_repair_predicate_qualifies_every_document_column() -> None:
    predicate = _predicate("d")

    assert "d.tool_id" in predicate
    assert "d.category" in predicate
    assert "d.relative_path" in predicate
    assert "d.metadata" in predicate
