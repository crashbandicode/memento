"""Phase 4 durable Canvas/search projector: fencing, replay, and golden match."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.config import settings
from server.db.models import (
    Base,
    CanvasArtifactReference,
    ConversationMessage,
    ConversationSearchTerm,
    Document,
    DocumentDeliveryState,
    IngestProjectionCandidate,
    Machine,
    Tool,
    User,
)
from server.services.ingest_service import ingest_file
from server.services.realtime_ingest_projector import (
    KIND_CANVAS,
    KIND_SEARCH,
    RealtimeIngestProjector,
    deferred_projections_enabled,
)


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

TEST_DATABASE_URL = os.environ.get("MEMENTO_TASK_TEST_DATABASE_URL")
CANVAS_PATH = (
    "/home/me/.cursor/projects/phase4/canvases/phase4needle.canvas.tsx"
)
LEXICON_TERM = "phase4lexiconneedle"

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="isolated PostgreSQL task test database is not configured",
)


def _load_parity_module():
    """Load the Phase 0 golden helper without putting tests/ on sys.path.

    ``exec_module`` must register the module in ``sys.modules`` first:
    ``@dataclass`` looks up ``sys.modules[cls.__module__]``.  Prefer the
    already-collected pytest module when both files run in one process so
    monkeypatches land on the same globals ``_run_sequence`` uses.
    """
    import importlib.util

    cached = getattr(_load_parity_module, "_module", None)
    if cached is not None:
        return cached
    for name, module in sys.modules.items():
        if name.endswith("test_realtime_ingest_parity") and hasattr(
            module, "_run_sequence"
        ):
            _load_parity_module._module = module
            return module
    parity_path = Path(__file__).parent / "test_realtime_ingest_parity.py"
    spec = importlib.util.spec_from_file_location(
        "realtime_ingest_parity_mod", parity_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _load_parity_module._module = module
    return module


@pytest_asyncio.fixture
async def session_factory():
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _json_line(row: dict[str, object]) -> str:
    return json.dumps(row, separators=(",", ":"), sort_keys=True)


def _hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cursor_transcript(*contents: str) -> str:
    rows = []
    for index, content in enumerate(contents, start=1):
        role = "user" if index == 1 else "assistant"
        rows.append(
            {
                "type": role,
                "role": role,
                "id": f"phase4-{role}-{index}",
                "timestamp": f"2026-08-27T12:00:0{index}Z",
                "message": {"content": content},
            }
        )
    return "\n".join(_json_line(row) for row in rows)


async def _seed_owner(session, *, suffix: str):
    user = User(
        id=uuid.uuid4(),
        email=f"phase4-{suffix}-{uuid.uuid4()}@example.test",
        role="viewer",
        status="active",
    )
    machine = Machine(
        id=uuid.uuid4(),
        name=f"phase4-{suffix}",
        collector_token_hash=str(uuid.uuid4()),
        user_id=user.id,
    )
    if await session.get(Tool, "cursor") is None:
        session.add(Tool(id="cursor", display_name="cursor"))
    session.add_all((user, machine))
    await session.commit()
    return user, machine


async def _projection_snapshot(
    session,
    document_id: uuid.UUID,
    *,
    lexicon_term: str,
) -> dict[str, object]:
    references = (
        await session.execute(
            select(
                ConversationMessage.line_number,
                CanvasArtifactReference.path_hash,
                CanvasArtifactReference.recorded_path,
                CanvasArtifactReference.status,
            )
            .join(
                ConversationMessage,
                ConversationMessage.id == CanvasArtifactReference.message_id,
            )
            .where(CanvasArtifactReference.document_id == document_id)
            .order_by(
                ConversationMessage.line_number,
                CanvasArtifactReference.path_hash,
            )
        )
    ).all()
    tsv = (
        await session.execute(
            text("SELECT content_tsv::text FROM documents WHERE id = :id"),
            {"id": document_id},
        )
    ).scalar()
    term_present = await session.scalar(
        select(ConversationSearchTerm.term).where(
            ConversationSearchTerm.term == lexicon_term
        )
    )
    title = await session.scalar(
        select(Document.title).where(Document.id == document_id)
    )
    return {
        "title": title,
        "canvas": [
            {
                "line_number": int(row.line_number),
                "path_hash": str(row.path_hash),
                "recorded_path": str(row.recorded_path),
                "status": str(row.status),
            }
            for row in references
        ],
        "content_tsv": tsv,
        "lexicon_term": term_present,
    }


async def _pending_candidates(session, document_id: uuid.UUID):
    return (
        (
            await session.execute(
                select(IngestProjectionCandidate)
                .where(
                    IngestProjectionCandidate.document_id == document_id,
                    IngestProjectionCandidate.completed_at.is_(None),
                    IngestProjectionCandidate.superseded_at.is_(None),
                )
                .order_by(
                    IngestProjectionCandidate.kind,
                    IngestProjectionCandidate.created_at,
                )
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_projector_exits_when_another_process_owns_advisory_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncpg

    calls: list[str] = []

    class _FakeConnection:
        async def fetchval(self, *_args, **_kwargs):
            return False

        async def execute(self, *_args, **_kwargs):
            return None

        async def close(self):
            return None

    async def fake_connect(_dsn):
        return _FakeConnection()

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    projector = RealtimeIngestProjector()

    async def run_once(**_kwargs):
        calls.append("run")
        return []

    monkeypatch.setattr(projector, "run_once", run_once)
    await projector.run()
    assert calls == []


def test_deferred_projections_flag_defaults_off() -> None:
    assert deferred_projections_enabled() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ("legacy", "core", "raw"))
async def test_deferred_canvas_search_matches_synchronous_after_projector(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
    writer: str,
) -> None:
    nonce = f"phase4lex{uuid.uuid4().hex[:12]}"
    full = _cursor_transcript(
        f"See [phase4needle.canvas.tsx]({CANVAS_PATH}) {nonce}",
        "Canvas and search should match after the projector.",
    )
    use_core = writer != "legacy"
    ingest_writer = None if writer == "core" else writer

    async def run(*, deferred: bool) -> dict[str, object]:
        monkeypatch.setattr(settings, "realtime_ingest_deferred_projections", deferred)
        async with session_factory() as session:
            user, machine = await _seed_owner(
                session, suffix=f"{writer}-{'on' if deferred else 'off'}"
            )
            path = f"phase4/{writer}-canvas-search.jsonl"
            document = await ingest_file(
                session,
                tool_id="cursor",
                category="conversation",
                content_type="jsonl",
                relative_path=path,
                content=full,
                content_hash=_hash(full),
                file_size=len(full.encode("utf-8")),
                mode="full",
                offset=len(full.encode("utf-8")),
                metadata={"session_id": f"phase4-{path}"},
                timestamp=1_785_672_000.0,
                machine_id=machine.id,
                user_id=str(user.id),
                schedule_post_ingest=False,
                use_core_delta_message_staging=False if not deferred else use_core,
                writer="legacy" if not deferred else ingest_writer,
            )
            await session.commit()
            document_id = document.id
            if deferred:
                pending = await _pending_candidates(session, document_id)
                kinds = {row.kind for row in pending}
                assert KIND_CANVAS in kinds
                assert KIND_SEARCH in kinds
                before = await _projection_snapshot(
                    session, document_id, lexicon_term=nonce
                )
                assert before["canvas"] == []
                assert before["lexicon_term"] is None
            else:
                assert await _pending_candidates(session, document_id) == []
        if deferred:
            projector = RealtimeIngestProjector(session_factory=session_factory)
            await projector.run_until_quiescent(document_ids=(document_id,))
        async with session_factory() as session:
            return await _projection_snapshot(
                session, document_id, lexicon_term=nonce
            )

    projected = await run(deferred=True)
    synchronous = await run(deferred=False)
    assert projected["title"] == synchronous["title"]
    assert projected["canvas"] == synchronous["canvas"]
    assert projected["canvas"], "fixture must produce at least one Canvas reference"
    assert projected["content_tsv"] == synchronous["content_tsv"]
    assert projected["content_tsv"]
    assert projected["lexicon_term"] == nonce
    assert synchronous["lexicon_term"] == nonce


@pytest.mark.asyncio
async def test_projector_restart_replay_is_idempotent(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "realtime_ingest_deferred_projections", True)
    full_bodies = [
        _cursor_transcript(
            f"Replay {LEXICON_TERM} document {index} [phase4needle.canvas.tsx]({CANVAS_PATH})",
            f"Projector crash recovery {index}",
        )
        for index in range(3)
    ]
    document_ids: list[uuid.UUID] = []
    async with session_factory() as session:
        user, machine = await _seed_owner(session, suffix="replay")
        for index, body in enumerate(full_bodies):
            path = f"phase4/replay-{index}-{uuid.uuid4()}.jsonl"
            document = await ingest_file(
                session,
                tool_id="cursor",
                category="conversation",
                content_type="jsonl",
                relative_path=path,
                content=body,
                content_hash=_hash(body),
                file_size=len(body.encode("utf-8")),
                mode="full",
                offset=len(body.encode("utf-8")),
                metadata={"session_id": path},
                timestamp=1_785_672_000.0 + index,
                machine_id=machine.id,
                user_id=str(user.id),
                schedule_post_ingest=False,
                writer="legacy",
            )
            document_ids.append(document.id)
        await session.commit()

    projector = RealtimeIngestProjector(session_factory=session_factory)
    first = await projector.run_once(limit=1, document_ids=document_ids)
    assert len(first) == 1
    async with session_factory() as session:
        pending = (
            await session.execute(
                select(func.count())
                .select_from(IngestProjectionCandidate)
                .where(
                    IngestProjectionCandidate.document_id.in_(document_ids),
                    IngestProjectionCandidate.completed_at.is_(None),
                    IngestProjectionCandidate.superseded_at.is_(None),
                )
            )
        ).scalar_one()
        completed = (
            await session.execute(
                select(func.count())
                .select_from(IngestProjectionCandidate)
                .where(
                    IngestProjectionCandidate.document_id.in_(document_ids),
                    IngestProjectionCandidate.completed_at.is_not(None),
                )
            )
        ).scalar_one()
        assert completed == 1
        assert pending >= 1

    await projector.run_until_quiescent(document_ids=document_ids)
    snapshots = []
    async with session_factory() as session:
        remaining = (
            await session.execute(
                select(func.count())
                .select_from(IngestProjectionCandidate)
                .where(
                    IngestProjectionCandidate.document_id.in_(document_ids),
                    IngestProjectionCandidate.completed_at.is_(None),
                    IngestProjectionCandidate.superseded_at.is_(None),
                )
            )
        ).scalar_one()
        assert remaining == 0
        for document_id in document_ids:
            snapshots.append(
                await _projection_snapshot(
                    session, document_id, lexicon_term=LEXICON_TERM
                )
            )
            refs = (
                await session.execute(
                    select(func.count())
                    .select_from(CanvasArtifactReference)
                    .where(CanvasArtifactReference.document_id == document_id)
                )
            ).scalar_one()
            assert refs == 1

    await projector.run_until_quiescent(document_ids=document_ids)
    async with session_factory() as session:
        replayed = [
            await _projection_snapshot(
                session, document_id, lexicon_term=LEXICON_TERM
            )
            for document_id in document_ids
        ]
        for document_id in document_ids:
            refs = (
                await session.execute(
                    select(func.count())
                    .select_from(CanvasArtifactReference)
                    .where(CanvasArtifactReference.document_id == document_id)
                )
            ).scalar_one()
            assert refs == 1
    assert replayed == snapshots


@pytest.mark.asyncio
async def test_revision_fence_projects_latest_once_and_supersedes_older(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "realtime_ingest_deferred_projections", True)
    full = _cursor_transcript(
        f"Fence {LEXICON_TERM} [phase4needle.canvas.tsx]({CANVAS_PATH})",
        "First revision.",
    )
    delta_row = _json_line(
        {
            "type": "assistant",
            "role": "assistant",
            "id": "phase4-assistant-2",
            "timestamp": "2026-08-27T12:00:03Z",
            "message": {"content": f"Second revision {LEXICON_TERM} still open."},
        }
    )
    async with session_factory() as session:
        user, machine = await _seed_owner(session, suffix="fence")
        path = f"phase4/fence-{uuid.uuid4()}.jsonl"
        document = await ingest_file(
            session,
            tool_id="cursor",
            category="conversation",
            content_type="jsonl",
            relative_path=path,
            content=full,
            content_hash=_hash(full),
            file_size=len(full.encode("utf-8")),
            mode="full",
            offset=len(full.encode("utf-8")),
            metadata={"session_id": path},
            timestamp=1_785_672_000.0,
            machine_id=machine.id,
            user_id=str(user.id),
            schedule_post_ingest=False,
            writer="legacy",
        )
        first_hash = _hash(full)
        snapshot = f"{full}\n{delta_row}"
        second_hash = _hash(snapshot)
        await ingest_file(
            session,
            tool_id="cursor",
            category="conversation",
            content_type="jsonl",
            relative_path=path,
            content=delta_row,
            content_hash=second_hash,
            file_size=len(delta_row.encode("utf-8")),
            mode="delta",
            offset=len(snapshot.encode("utf-8")),
            base_hash=first_hash,
            base_offset=len(full.encode("utf-8")),
            metadata={"session_id": path},
            timestamp=1_785_672_001.0,
            machine_id=machine.id,
            user_id=str(user.id),
            schedule_post_ingest=False,
            writer="legacy",
        )
        await session.commit()
        document_id = document.id
        pending = await _pending_candidates(session, document_id)
        revisions = {(row.kind, row.revision_hash) for row in pending}
        assert (KIND_SEARCH, first_hash) in revisions
        assert (KIND_SEARCH, second_hash) in revisions

    projector = RealtimeIngestProjector(session_factory=session_factory)
    results = await projector.run_until_quiescent(document_ids=(document_id,))
    search_applies = [
        result
        for result in results
        if result["document_id"] == str(document_id) and result["kind"] == KIND_SEARCH
    ]
    assert len(search_applies) == 1
    assert search_applies[0]["revision_hash"] == second_hash
    assert search_applies[0]["superseded"] >= 1
    canvas_applies = [
        result
        for result in results
        if result["document_id"] == str(document_id) and result["kind"] == KIND_CANVAS
    ]
    assert len(canvas_applies) == 1
    assert canvas_applies[0]["revision_hash"] == second_hash

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(IngestProjectionCandidate).where(
                        IngestProjectionCandidate.document_id == document_id
                    )
                )
            )
            .scalars()
            .all()
        )
        by_identity = {
            (row.kind, row.revision_hash): row for row in rows
        }
        older_search = by_identity[(KIND_SEARCH, first_hash)]
        latest_search = by_identity[(KIND_SEARCH, second_hash)]
        assert older_search.superseded_at is not None
        assert older_search.completed_at is not None
        assert latest_search.completed_at is not None
        assert latest_search.superseded_at is None
        delivery = await session.get(DocumentDeliveryState, document_id)
        assert delivery is not None
        assert delivery.revision_hash == second_hash


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("use_core_delta_message_staging", "writer"),
    (
        (False, "legacy"),
        (True, "core"),
        (True, "raw"),
    ),
)
async def test_deferred_ingest_live_fields_match_golden(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
    use_core_delta_message_staging: bool,
    writer: str,
) -> None:
    parity = _load_parity_module()
    GOLDEN_PATH = parity.GOLDEN_PATH
    RECORDED_DELTA_SEQUENCES = parity.RECORDED_DELTA_SEQUENCES
    _first_difference = parity._first_difference
    _run_sequence = parity._run_sequence
    original_snapshot = parity._snapshot

    async def snapshot_after_projector(session, **kwargs):
        document_id = kwargs["document_id"]
        from server.services.realtime_ingest_projector import (
            process_pending_candidates,
        )

        await process_pending_candidates(session, document_ids=(document_id,))
        return await original_snapshot(session, **kwargs)

    monkeypatch.setattr(parity, "_snapshot", snapshot_after_projector)
    monkeypatch.setattr(settings, "realtime_ingest_deferred_projections", True)
    actual = {
        sequence.name: await _run_sequence(
            session_factory,
            sequence,
            use_core_delta_message_staging=use_core_delta_message_staging,
            writer=None if writer == "core" else writer,
        )
        for sequence in RECORDED_DELTA_SEQUENCES
    }
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    live_actual = {
        name: {key: value for key, value in snapshot.items() if key != "staged_sse_events"}
        for name, snapshot in actual.items()
    }
    live_expected = {
        name: {key: value for key, value in snapshot.items() if key != "staged_sse_events"}
        for name, snapshot in expected.items()
    }
    difference = _first_difference(live_actual, live_expected)
    assert difference is None, (
        f"{writer} deferred live fields drifted from the Phase 0 golden at "
        f"{difference[0]}: expected {difference[1]!r}, got {difference[2]!r}"
    )


@pytest.mark.asyncio
async def test_poison_projection_group_does_not_starve_other_documents(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A group whose apply always raises must roll back only its own
    SAVEPOINT, surface status=error, back off, and leave every other
    document draining (the drain crash-loop shape, one layer up)."""
    from server.services import realtime_ingest_projector as projector_module

    monkeypatch.setattr(settings, "realtime_ingest_deferred_projections", True)
    bodies = [
        _cursor_transcript(
            f"Poison isolation {LEXICON_TERM} document {index}",
            f"projector poison fixture {index}",
        )
        for index in range(2)
    ]
    document_ids: list[uuid.UUID] = []
    async with session_factory() as session:
        user, machine = await _seed_owner(session, suffix="poison")
        for index, body in enumerate(bodies):
            path = f"phase4/poison-{index}-{uuid.uuid4()}.jsonl"
            document = await ingest_file(
                session,
                tool_id="cursor",
                category="conversation",
                content_type="jsonl",
                relative_path=path,
                content=body,
                content_hash=_hash(body),
                file_size=len(body.encode("utf-8")),
                mode="full",
                offset=len(body.encode("utf-8")),
                metadata={"session_id": path},
                timestamp=1_785_672_000.0 + index,
                machine_id=machine.id,
                user_id=str(user.id),
                schedule_post_ingest=False,
                writer="legacy",
            )
            document_ids.append(document.id)
        await session.commit()

    poison_id = document_ids[0]
    healthy_id = document_ids[1]
    real_apply_search = projector_module._apply_search

    async def failing_apply_search(db, document_id):
        if document_id == poison_id:
            raise RuntimeError("poison projection group")
        return await real_apply_search(db, document_id)

    monkeypatch.setattr(projector_module, "_apply_search", failing_apply_search)

    clock = [0.0]
    projector = RealtimeIngestProjector(
        session_factory=session_factory, clock=lambda: clock[0]
    )
    results = await projector.run_until_quiescent(document_ids=document_ids)
    statuses = {
        (uuid.UUID(result["document_id"]), result["kind"]): result["status"]
        for result in results
    }
    assert statuses[(poison_id, KIND_SEARCH)] == "error"
    assert statuses[(healthy_id, KIND_SEARCH)] == "applied"
    async with session_factory() as session:
        assert await _pending_candidates(session, poison_id) != []
        assert await _pending_candidates(session, healthy_id) == []

    # Backoff: an immediate rerun skips the failing pair entirely instead of
    # retrying it at poll frequency.
    assert await projector.run_once(document_ids=document_ids) == []

    # Once the failure is gone and the backoff window has elapsed, the group
    # recovers and drains normally.
    monkeypatch.setattr(projector_module, "_apply_search", real_apply_search)
    clock[0] = 120.0
    recovered = await projector.run_until_quiescent(document_ids=document_ids)
    assert any(
        uuid.UUID(result["document_id"]) == poison_id
        and result["status"] == "applied"
        for result in recovered
    )
    async with session_factory() as session:
        assert await _pending_candidates(session, poison_id) == []
