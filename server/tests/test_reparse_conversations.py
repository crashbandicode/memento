from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from server.scripts.reparse_conversations import source_payload_error
from server.services.ingest_service import (
    STORED_SOURCE_HASH_KEY,
    STORED_SOURCE_REVISION_KEY,
    STORED_SOURCE_SIZE_KEY,
    _set_stored_source_identity,
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


def test_reparse_preserves_repeated_cursor_source_rows() -> None:
    content = _jsonl(
        {"role": "user", "message": "repeat me"},
        {"role": "user", "message": "repeat me"},
    )

    rows = list(iter_stored_conversation_messages(content, "cursor"))

    assert [row[1] for row in rows] == ["repeat me", "repeat me"]
