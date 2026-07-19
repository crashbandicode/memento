from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from server.scripts.reparse_conversations import (
    cutover_manifest_error,
    source_payload_error,
)
from server.services.ingest_service import (
    CURRENT_ASSISTANT_MODEL_KEY,
    CURRENT_ASSISTANT_REASONING_KEY,
    CURRENT_ASSISTANT_SERVICE_TIER_KEY,
    STORED_SOURCE_HASH_KEY,
    STORED_SOURCE_REVISION_KEY,
    STORED_SOURCE_SIZE_KEY,
    _assistant_identity_for_ingest,
    _set_stored_source_identity,
    _store_assistant_identity,
    _stored_source_is_current,
    iter_stored_conversation_messages,
)


def _jsonl(*records: dict) -> str:
    return "\n".join(json.dumps(record) for record in records)


def test_source_payload_requires_exact_size_and_hash() -> None:
    payload = '{"type":"user"}\n'
    encoded = payload.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()

    assert source_payload_error(
        payload,
        expected_hash=digest,
        expected_size=len(encoded),
    ) is None
    assert "byte size" in source_payload_error(
        payload,
        expected_hash=digest,
        expected_size=len(encoded) + 1,
    )
    assert "SHA-256" in source_payload_error(
        payload,
        expected_hash="0" * 64,
        expected_size=len(encoded),
    )


def test_cutover_can_preserve_only_accounted_unverified_sources() -> None:
    assert cutover_manifest_error(
        eligible=923,
        staged=910,
        unverified=13,
        extra_manifest=0,
        preserve_unverified=True,
    ) is None
    assert cutover_manifest_error(
        eligible=923,
        staged=910,
        unverified=13,
        extra_manifest=0,
        preserve_unverified=False,
    ) is not None
    assert cutover_manifest_error(
        eligible=923,
        staged=909,
        unverified=13,
        extra_manifest=0,
        preserve_unverified=True,
    ) is not None
    assert cutover_manifest_error(
        eligible=923,
        staged=910,
        unverified=13,
        extra_manifest=1,
        preserve_unverified=True,
    ) is not None


def test_stored_source_identity_fences_full_snapshot_revision() -> None:
    document = SimpleNamespace(
        category="conversation",
        content="sanitized transcript",
        content_s3_key=None,
        metadata_={},
    )

    assert not _stored_source_is_current(document, "raw-revision")

    _set_stored_source_identity(
        document,
        document.content,
        revision_hash="raw-revision",
    )

    assert _stored_source_is_current(document, "raw-revision")
    assert not _stored_source_is_current(document, "newer-revision")
    assert document.metadata_[STORED_SOURCE_REVISION_KEY] == "raw-revision"
    assert document.metadata_[STORED_SOURCE_SIZE_KEY] == len(document.content)
    assert document.metadata_[STORED_SOURCE_HASH_KEY] == hashlib.sha256(
        document.content.encode("utf-8")
    ).hexdigest()


def test_reparse_uses_live_storage_projection_for_codex_pairs() -> None:
    content = _jsonl(
        {
            "timestamp": "2026-07-13T10:00:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "same prompt"}],
            },
        },
        {
            "timestamp": "2026-07-13T10:00:00.001Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "same prompt",
                "client_id": "6e0dde7e-81d2-43ea-ae39-f743f14d20ac",
            },
        },
    )

    rows = list(iter_stored_conversation_messages(content, "codex"))

    assert len(rows) == 1
    normalized, stored_content, metadata, timestamp = rows[0]
    assert normalized.raw_type == "user_message"
    assert stored_content == "same prompt"
    assert metadata["source_id"] == "6e0dde7e-81d2-43ea-ae39-f743f14d20ac"
    assert timestamp.isoformat() == "2026-07-13T10:00:00.001000+00:00"


def test_reparse_persists_codex_model_and_reasoning_metadata() -> None:
    content = _jsonl(
        {
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "effort": "xhigh"},
        },
        {
            "timestamp": "2026-07-17T20:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "id": "assistant-model-test",
                "content": [{"type": "output_text", "text": "Ready"}],
            },
        },
    )

    rows = list(iter_stored_conversation_messages(content, "codex"))

    assert len(rows) == 1
    assert rows[0][2]["model"] == "gpt-5.6-sol"
    assert rows[0][2]["reasoning_effort"] == "xhigh"


def test_delta_ingest_carries_assistant_identity_between_chunks() -> None:
    document = SimpleNamespace(metadata_={"unrelated": "preserved"})
    identity = _assistant_identity_for_ingest(document, "delta")
    identity.model = "gpt-5.6-sol"
    identity.reasoning_effort = "high"
    identity.service_tier = "priority"

    _store_assistant_identity(document, identity)
    next_delta = _assistant_identity_for_ingest(document, "delta")

    assert next_delta.model == "gpt-5.6-sol"
    assert next_delta.reasoning_effort == "high"
    assert next_delta.service_tier == "priority"
    assert document.metadata_["unrelated"] == "preserved"
    assert document.metadata_[CURRENT_ASSISTANT_MODEL_KEY] == "gpt-5.6-sol"
    assert document.metadata_[CURRENT_ASSISTANT_REASONING_KEY] == "high"
    assert document.metadata_[CURRENT_ASSISTANT_SERVICE_TIER_KEY] == "priority"
    assert _assistant_identity_for_ingest(document, "full").model == ""


def test_reparse_preserves_repeated_cursor_source_rows() -> None:
    content = _jsonl(
        {"role": "user", "message": "repeat me"},
        {"role": "user", "message": "repeat me"},
    )

    rows = list(iter_stored_conversation_messages(content, "cursor"))

    assert [row[1] for row in rows] == ["repeat me", "repeat me"]
