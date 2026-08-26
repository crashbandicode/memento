from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from server.db.models import Document
from server.services.ingest_service import (
    _claude_subagent_sidecar_evidence,
    _reconcile_claude_subagent_launch_metadata,
    _reconcile_idempotent_claude_ingest,
)


ROOT = "projects/yoga/root-session/subagents"
AGENT_ID = "afceda9d5a896fb52"
TRANSCRIPT_PATH = f"{ROOT}/agent-{AGENT_ID}.jsonl"
SIDECAR_PATH = f"{ROOT}/agent-{AGENT_ID}.meta.json"
DESCRIPTION = "Hoist wave engine into WaveDrainEngine mixin"
TOOL_USE_ID = "toolu_01NWNur9BbxShAwjsiieU9EM"


class _ScalarResult:
    def __init__(self, value=None) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Session:
    def __init__(self, counterpart: Document | None = None) -> None:
        self.counterpart = counterpart
        self.statements = []
        self.flush_count = 0
        self.info = {}
        self.entities = {}

    async def execute(self, statement, parameters=None):
        self.statements.append((statement, parameters))
        if parameters is not None:
            return _ScalarResult()
        return _ScalarResult(self.counterpart)

    async def flush(self) -> None:
        self.flush_count += 1

    async def get(self, model, identity):
        return self.entities.get((model, identity))

    def add(self, value) -> None:
        identity = getattr(value, "document_id", getattr(value, "id", None))
        self.entities[(type(value), identity)] = value


@pytest.fixture(autouse=True)
def _mock_document_content_accessor():
    async def read_content(_db, document):
        return getattr(document, "_test_content", None)

    with patch(
        "server.services.ingest_service.document_content",
        new=AsyncMock(side_effect=read_content),
    ):
        yield


def _sidecar_payload(
    *,
    agent_id: str = AGENT_ID,
    description: str = DESCRIPTION,
) -> str:
    return json.dumps({
        "agentId": agent_id,
        "toolUseId": TOOL_USE_ID,
        "description": description,
        "agentType": "general-purpose",
    })


def _document(
    path: str,
    *,
    category: str,
    content: str,
    metadata: dict | None = None,
) -> Document:
    document = Document(
        id=uuid.uuid4(),
        tool_id="claude_code",
        machine_id=uuid.uuid4(),
        relative_path=path,
        category=category,
        content_type="json" if category == "state" else "jsonl",
        title=path.rsplit("/", 1)[-1],
        content_hash="a" * 64,
        file_size_bytes=len(content),
        metadata_=metadata or {},
        needs_review=False,
    )
    document._test_content = content
    return document


def _sidecar() -> Document:
    return _document(
        SIDECAR_PATH,
        category="state",
        content=_sidecar_payload(),
        metadata={"is_subagent_meta": True},
    )


def _transcript(path: str = TRANSCRIPT_PATH) -> Document:
    return _document(
        path,
        category="conversation",
        content='{"type":"user","message":{"content":"hello"}}',
        metadata={
            "session_id": f"agent-{AGENT_ID}",
            "model": "claude-opus-4-1",
            "_assistant_reasoning_effort": "high",
        },
    )


def _assert_enriched(transcript: Document) -> None:
    assert transcript.metadata_["agent_id"] == AGENT_ID
    assert transcript.metadata_["agent_launch_description"] == DESCRIPTION
    assert transcript.metadata_["agent_tool_use_id"] == TOOL_USE_ID
    assert transcript.metadata_["agent_type"] == "general-purpose"
    assert (
        transcript.metadata_["agent_launch_metadata_source"]
        == "claude_subagent_sidecar"
    )
    assert transcript.metadata_["agent_launch_metadata_version"] == 1
    assert transcript.metadata_["model"] == "claude-opus-4-1"
    assert transcript.metadata_["_assistant_reasoning_effort"] == "high"


def test_current_sidecar_uses_validated_filename_agent_identity() -> None:
    evidence = _claude_subagent_sidecar_evidence(
        SIDECAR_PATH,
        json.dumps({
            "toolUseId": TOOL_USE_ID,
            "description": DESCRIPTION,
            "agentType": "general-purpose",
            "spawnDepth": 1,
        }),
    )

    assert evidence is not None
    transcript_path, metadata = evidence
    assert transcript_path == TRANSCRIPT_PATH
    assert metadata["agent_id"] == AGENT_ID
    assert metadata["agent_tool_use_id"] == TOOL_USE_ID
    assert metadata["agent_launch_description"] == DESCRIPTION


@pytest.mark.asyncio
async def test_sidecar_before_transcript_enriches_on_transcript_ingest() -> None:
    sidecar = _sidecar()
    transcript = _transcript()
    db = _Session(sidecar)

    enriched = await _reconcile_claude_subagent_launch_metadata(
        db,
        transcript,
        machine_id=str(transcript.machine_id),
        user_id=str(uuid.uuid4()),
    )

    assert enriched is transcript
    _assert_enriched(transcript)


@pytest.mark.asyncio
async def test_transcript_before_sidecar_enriches_exact_sibling() -> None:
    transcript = _transcript()
    sidecar = _sidecar()
    sidecar.machine_id = transcript.machine_id
    db = _Session(transcript)

    enriched = await _reconcile_claude_subagent_launch_metadata(
        db,
        sidecar,
        machine_id=str(sidecar.machine_id),
        user_id=str(uuid.uuid4()),
    )

    assert enriched is transcript
    _assert_enriched(transcript)


@pytest.mark.asyncio
async def test_mismatched_agent_id_and_cross_path_document_are_rejected() -> None:
    mismatched_sidecar = _document(
        SIDECAR_PATH,
        category="state",
        content=_sidecar_payload(agent_id="different-agent"),
    )
    db = _Session(_transcript())

    assert (
        await _reconcile_claude_subagent_launch_metadata(
            db,
            mismatched_sidecar,
            machine_id=str(mismatched_sidecar.machine_id),
            user_id=str(uuid.uuid4()),
        )
        is None
    )
    assert db.statements == []

    sidecar = _sidecar()
    wrong_path = _transcript(f"{ROOT}/nested/agent-{AGENT_ID}.jsonl")
    db = _Session(wrong_path)
    assert (
        await _reconcile_claude_subagent_launch_metadata(
            db,
            sidecar,
            machine_id=str(sidecar.machine_id),
            user_id=str(uuid.uuid4()),
        )
        is None
    )
    assert "agent_launch_description" not in wrong_path.metadata_


def test_duplicate_descriptions_remain_bound_to_distinct_agent_paths() -> None:
    other_id = "1234567890abcdef"
    first = _claude_subagent_sidecar_evidence(SIDECAR_PATH, _sidecar_payload())
    second = _claude_subagent_sidecar_evidence(
        f"{ROOT}/agent-{other_id}.meta.json",
        _sidecar_payload(agent_id=other_id),
    )

    assert first is not None
    assert second is not None
    assert first[0] == TRANSCRIPT_PATH
    assert second[0] == f"{ROOT}/agent-{other_id}.jsonl"
    assert first[0] != second[0]
    assert (
        first[1]["agent_launch_description"]
        == second[1]["agent_launch_description"]
        == DESCRIPTION
    )


def test_sidecar_launch_fields_are_bounded_before_persistence() -> None:
    evidence = _claude_subagent_sidecar_evidence(
        SIDECAR_PATH,
        json.dumps({
            "agentId": AGENT_ID,
            "description": "d" * 2_000,
            "toolUseId": "t" * 500,
            "agentType": "a" * 500,
        }),
    )

    assert evidence is not None
    metadata = evidence[1]
    assert len(metadata["agent_launch_description"]) == 1_024
    assert len(metadata["agent_tool_use_id"]) == 256
    assert len(metadata["agent_type"]) == 128


@pytest.mark.asyncio
async def test_existing_child_enrichment_publishes_one_child_event() -> None:
    transcript = _transcript()
    sidecar = _sidecar()
    sidecar.machine_id = transcript.machine_id
    db = _Session(transcript)

    with (
        patch(
            "server.services.ingest_service._invalidate_ingest_read_caches",
            new=AsyncMock(),
        ),
        patch("server.db.session.queue_realtime_event") as queue_event,
        patch("server.services.sse_service.publish_event") as publish_event,
    ):
        await _reconcile_idempotent_claude_ingest(
            db,
            sidecar,
            machine_id=str(sidecar.machine_id),
            user_id="user-id",
        )

    assert db.flush_count == 1
    publish_event.assert_not_called()
    queue_event.assert_called_once()
    _, event_type, event_data = queue_event.call_args.args
    assert event_type == "file_synced"
    assert event_data["document_id"] == str(transcript.id)
    assert event_data["category"] == "conversation"
    assert event_data["relative_path"] == TRANSCRIPT_PATH
    assert event_data["changes"] == [
        "conversation.metadata",
        "dashboard",
    ]


@pytest.mark.asyncio
async def test_matching_metadata_does_not_publish_duplicate_event() -> None:
    transcript = _transcript()
    sidecar = _sidecar()
    sidecar.machine_id = transcript.machine_id
    first_db = _Session(transcript)
    await _reconcile_claude_subagent_launch_metadata(
        first_db,
        sidecar,
        machine_id=str(sidecar.machine_id),
        user_id="user-id",
    )
    db = _Session(transcript)

    with (
        patch("server.db.session.queue_realtime_event") as queue_event,
        patch("server.services.sse_service.publish_event") as publish_event,
    ):
        await _reconcile_idempotent_claude_ingest(
            db,
            sidecar,
            machine_id=str(sidecar.machine_id),
            user_id="user-id",
        )

    assert db.flush_count == 0
    queue_event.assert_not_called()
    publish_event.assert_not_called()


@pytest.mark.asyncio
async def test_child_terminal_reconciliation_publishes_parent_companion_event() -> None:
    transcript = _transcript()
    sidecar = _sidecar()
    sidecar.machine_id = transcript.machine_id
    await _reconcile_claude_subagent_launch_metadata(
        _Session(sidecar),
        transcript,
        machine_id=str(transcript.machine_id),
        user_id="user-id",
    )
    transcript._test_content = json.dumps({
        "type": "assistant",
        "timestamp": "2026-08-01T12:05:00Z",
        "message": {
            "role": "assistant",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "Done"}],
        },
    })
    db = _Session(sidecar)

    with (
        patch(
            "server.services.ingest_service._invalidate_ingest_read_caches",
            new=AsyncMock(),
        ),
        patch("server.db.session.queue_realtime_event") as queue_event,
        patch("server.services.sse_service.publish_event") as publish_event,
    ):
        await _reconcile_idempotent_claude_ingest(
            db,
            transcript,
            machine_id=str(transcript.machine_id),
            user_id="user-id",
        )

    assert transcript.metadata_["subagent_lifecycle_status"] == "completed"
    assert db.flush_count == 1
    publish_event.assert_not_called()
    queue_event.assert_called_once()
    _, _, event_data = queue_event.call_args.args
    assert event_data["document_id"] == str(transcript.id)
    assert event_data["relative_path"] == TRANSCRIPT_PATH
