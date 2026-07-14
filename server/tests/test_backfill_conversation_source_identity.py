from server.scripts.backfill_conversation_source_identity import (
    collect_jsonl_evidence,
    legacy_snapshot_proof,
)


FULL = "\n".join([
    '{"type":"session_meta","timestamp":"2026-01-01T00:00:00Z"}',
    '{"type":"event_msg","timestamp":"2026-01-01T00:00:01Z"}',
    "not-json-but-still-a-source-line",
])


def test_collect_jsonl_evidence_matches_collector_rules():
    assert collect_jsonl_evidence(FULL) == collect_jsonl_evidence(FULL + "\n\n")
    evidence = collect_jsonl_evidence(FULL)
    assert evidence.total_lines == 3
    assert evidence.message_types == {"session_meta": 1, "event_msg": 1}
    assert evidence.first_timestamp == "2026-01-01T00:00:00Z"
    assert evidence.last_timestamp == "2026-01-01T00:00:01Z"


def test_legacy_snapshot_proof_accepts_exact_cumulative_metadata():
    proved, reason = legacy_snapshot_proof(
        content_type="jsonl",
        metadata={
            "total_lines": 3,
            "message_types": {"session_meta": 1, "event_msg": 1},
            "first_timestamp": "2026-01-01T00:00:00Z",
            "last_timestamp": "2026-01-01T00:00:01Z",
        },
        payload=FULL,
    )
    assert proved is True
    assert reason == "metadata matches stored snapshot"


def test_legacy_snapshot_proof_rejects_tail_and_stale_snapshots():
    metadata = {
        "total_lines": 3,
        "message_types": {"session_meta": 1, "event_msg": 1},
        "first_timestamp": "2026-01-01T00:00:00Z",
        "last_timestamp": "2026-01-01T00:00:01Z",
    }
    assert legacy_snapshot_proof(
        content_type="jsonl",
        metadata=metadata,
        payload=FULL.split("\n", 1)[1],
    ) == (False, "total_lines mismatch")
    assert legacy_snapshot_proof(
        content_type="jsonl",
        metadata={**metadata, "last_timestamp": "2026-01-01T00:00:02Z"},
        payload=FULL,
    ) == (False, "last_timestamp mismatch")


def test_legacy_snapshot_proof_accepts_nonempty_full_strategy_sidecar():
    assert legacy_snapshot_proof(
        content_type="json",
        metadata={},
        payload='{"agentType":"Explore"}',
    ) == (True, "full-strategy sidecar")
