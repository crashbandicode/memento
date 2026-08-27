"""Reproducer + fix-validation for the live MissingGreenlet at
canvas_artifact_store.py:127 (reconcile_message_canvases).

Root cause (proven by the instrumentation below):
  * The full/authoritative-rebase suffix-comparison path in ``_extract_messages``
    loads existing ``ConversationMessage`` rows with ``load_only(id, line_number,
    message_type, role, content, metadata_, timestamp)`` -- note ``document_id``
    is NOT loaded.
  * A row whose stored ``source_id`` matches the incoming payload is mutated in
    place and appended to ``canvas_reconcile_rows`` (ingest_service.py:5306).
  * A later differing row triggers an ORM-enabled
    ``delete(ConversationMessage).where(document_id == doc.id,
    line_number >= line_num)`` (ingest_service.py:5311).  This DELETE uses the
    default ``synchronize_session="auto"`` -> "evaluate".  Because the deferred
    ``document_id`` column is referenced by the WHERE clause, SQLAlchemy's
    in-Python evaluator returns ``_EXPIRED_OBJECT`` for *every* loaded
    ConversationMessage (bulk_persistence._get_matched_objects_on_criteria), and
    ``_do_post_synchronize_evaluate`` then calls ``state._expire`` on each --
    fully expiring the mutated row still queued in ``canvas_reconcile_rows``.
  * ``reconcile_message_canvases`` then evaluates ``int(message.id)`` on that
    fully-expired instance; the identity refresh runs outside the async greenlet
    -> ``sqlalchemy.exc.MissingGreenlet``.

This is a NEW diagnostic test (not part of the shipped suite).  It models
production faithfully: each "request" gets its own fresh session with
``expire_on_commit=False`` (the real get_db factory value).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select, text
from sqlalchemy.exc import MissingGreenlet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm.state import InstanceState
from sqlalchemy.sql.dml import Delete

from server.config import settings
from server.db.models import Base, ConversationMessage, Machine, User
from server.db.session import TransactionalAsyncSession
from server.services import cache, sse_service
from server.services.content_sanitizer import sanitize_content_file
from server.services.conversation_stream import ConversationFileSource
from server.services.ingest_service import ingest_file
from server.services.large_content_store import (
    DocumentContentPointer,
    document_content_key,
)

TEST_DATABASE_URL = os.environ.get("MEMENTO_TASK_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="isolated PostgreSQL task test database is not configured",
)

CANVAS_PATH = "/home/me/.cursor/projects/work/canvases/repro.canvas.tsx"


def _claude_record(role: str, content: object, *, source_id: str, timestamp: str) -> dict:
    return {
        "type": role,
        "uuid": source_id,
        "timestamp": timestamp,
        "message": {"role": role, "content": content},
    }


def _payload(records: list[dict]) -> bytes:
    return (
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
    ).encode("utf-8")


class _Redis:
    def __init__(self) -> None:
        self.increments: list[str] = []

    async def incr(self, key: str) -> int:
        self.increments.append(key)
        return len(self.increments)


@pytest.fixture
def expire_tracer(monkeypatch):
    """Capture every full expire of a ConversationMessage with its stack."""
    events: list[dict] = []
    orig_expire = InstanceState._expire

    def traced_expire(self, *args, **kwargs):
        obj = self.obj()
        if obj is not None and type(obj).__name__ == "ConversationMessage":
            events.append(
                {
                    "line_number": self.dict.get("line_number"),
                    "stack": "".join(traceback.format_stack(limit=25)),
                }
            )
        return orig_expire(self, *args, **kwargs)

    monkeypatch.setattr(InstanceState, "_expire", traced_expire)
    return events


@pytest_asyncio.fixture
async def make_session_factory():
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await connection.run_sync(Base.metadata.create_all)

    def _factory(expire_on_commit: bool = False) -> async_sessionmaker:
        return async_sessionmaker(
            engine,
            class_=TransactionalAsyncSession,
            expire_on_commit=expire_on_commit,
        )

    yield _factory
    await engine.dispose()


def _install_ingest_monkeypatches(monkeypatch):
    monkeypatch.setattr(cache, "_client", _Redis())

    async def publish_event(event_type, data, user_id=None):
        return None

    monkeypatch.setattr(sse_service, "publish_event", publish_event)
    monkeypatch.setattr(settings, "document_content_minio_enabled", True)

    async def finalize_content(*, document_id, content=None, payload_path=None, **_kw):
        payload = content.encode("utf-8") if content is not None else payload_path.read_bytes()
        sha256 = hashlib.sha256(payload).hexdigest()
        return DocumentContentPointer(
            key=document_content_key(document_id=document_id, sha256=sha256),
            sha256=sha256,
            size_bytes=len(payload),
            verified_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(
        "server.services.ingest_service.finalize_document_content", finalize_content
    )

    async def no_raw_object_read(*_a, **_k):
        return ""

    monkeypatch.setattr(
        "server.services.embedding_service.document_content", no_raw_object_read
    )


def _install_reconcile_probe(monkeypatch, snapshots):
    import server.services.canvas_artifact_store as cas

    orig = cas.reconcile_message_canvases

    async def wrapper(db, document, messages):
        snapshots.append(
            [
                {"expired": sa_inspect(m).expired, "id_loaded": "id" in sa_inspect(m).dict}
                for m in messages
            ]
        )
        return await orig(db, document, messages)

    monkeypatch.setattr(cas, "reconcile_message_canvases", wrapper)


def _install_load_document_id_fix(monkeypatch):
    """ALT PROPOSED FIX (validation): include document_id in every CM load_only,
    so the DELETE's evaluate-sync can reason about the rows instead of expiring
    them wholesale."""
    import server.services.ingest_service as isvc
    from sqlalchemy.orm import load_only as _load_only

    def patched(*attrs, **kwargs):
        try:
            owns_cm = any(getattr(a, "class_", None) is ConversationMessage for a in attrs)
            has_doc = any(
                getattr(a, "key", None) == "document_id"
                and getattr(a, "class_", None) is ConversationMessage
                for a in attrs
            )
            if owns_cm and not has_doc:
                attrs = attrs + (ConversationMessage.document_id,)
        except Exception:
            pass
        return _load_only(*attrs, **kwargs)

    monkeypatch.setattr(isvc, "load_only", patched)


def _install_load_document_id_omission(monkeypatch):
    """Re-introduce the original bug: strip document_id from every CM
    load_only so the DELETE's evaluate-sync expires the loaded rows.  Keeps
    the mechanism provable now that the production query loads document_id."""
    import server.services.ingest_service as isvc
    from sqlalchemy.orm import load_only as _load_only

    def patched(*attrs, **kwargs):
        try:
            filtered = tuple(
                a
                for a in attrs
                if not (
                    getattr(a, "key", None) == "document_id"
                    and getattr(a, "class_", None) is ConversationMessage
                )
            )
        except Exception:
            filtered = attrs
        return _load_only(*filtered, **kwargs)

    monkeypatch.setattr(isvc, "load_only", patched)


def _install_synchronize_false_fix(monkeypatch):
    """PROPOSED FIX (validation): disable session sync on the CM bulk DELETE."""
    orig_execute = AsyncSession.execute

    async def execute(self, statement, *args, **kwargs):
        if isinstance(statement, Delete):
            tbl = getattr(statement, "table", None)
            if getattr(tbl, "name", None) == "conversation_messages":
                statement = statement.execution_options(synchronize_session=False)
        return await orig_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", execute)


async def _run_two_request_rebase(session_factory):
    """Initial FULL in one session, authoritative-rebase FULL in a fresh session.

    Returns (reproduced: bool | None, relative_path, ids...).  Raises only for
    unexpected errors; MissingGreenlet is caught and reported as reproduced.
    """
    initial_records = [
        _claude_record(
            "user",
            f"Draft the canvas at [{CANVAS_PATH}]({CANVAS_PATH}). original prompt",
            source_id="user-1", timestamp="2026-08-07T12:00:00Z",
        ),
        _claude_record("assistant", "assistant original two", source_id="asst-2",
                       timestamp="2026-08-07T12:00:01Z"),
        _claude_record("assistant", "assistant original three", source_id="asst-3",
                       timestamp="2026-08-07T12:00:02Z"),
    ]
    # Rebase FULL:
    #  line1: same source_id (user-1), CHANGED content -> suffix-compare update
    #         mutates in place + appends to canvas_reconcile_rows (5306).
    #  line2: DIFFERENT source_id + changed content -> triggers the
    #         delete(ConversationMessage).where(line_number >= 2) at 5311.
    #  line3/4: rows -> _stage_new_conversation_messages.
    rebase_records = [
        _claude_record(
            "user",
            f"Draft the canvas at [{CANVAS_PATH}]({CANVAS_PATH}). REBASED prompt text",
            source_id="user-1", timestamp="2026-08-07T12:00:00Z",
        ),
        _claude_record("assistant", "assistant rebased two", source_id="asst-2b",
                       timestamp="2026-08-07T12:00:01Z"),
        _claude_record("assistant", "assistant rebased three", source_id="asst-3b",
                       timestamp="2026-08-07T12:00:02Z"),
        _claude_record("user", "appended user row after rebase", source_id="user-4",
                       timestamp="2026-08-07T12:00:03Z"),
    ]

    initial_payload = _payload(initial_records)
    rebase_payload = _payload(rebase_records)
    initial_hash = hashlib.sha256(initial_payload).hexdigest()
    rebase_hash = hashlib.sha256(rebase_payload).hexdigest()

    with tempfile.TemporaryDirectory() as tmp:
        tp = Path(tmp)
        (tp / "i.jsonl").write_bytes(initial_payload)
        (tp / "r.jsonl").write_bytes(rebase_payload)
        san_i = sanitize_content_file(tp / "i.jsonl", tp / "i.san.jsonl")
        san_r = sanitize_content_file(tp / "r.jsonl", tp / "r.san.jsonl")
        src_i = ConversationFileSource.inspect(san_i.path)
        src_r = ConversationFileSource.inspect(san_r.path)
        relative_path = f"sessions/{uuid4()}.jsonl"

        async with session_factory() as session_a:
            user = User(id=uuid4(), email=f"{uuid4()}@example.test",
                        role="viewer", status="active")
            machine = Machine(id=uuid4(), name="repro",
                              collector_token_hash=str(uuid4()), user_id=user.id)
            session_a.add_all([user, machine])
            await session_a.flush()
            machine_id, user_id = str(machine.id), str(user.id)

            document = await ingest_file(
                session_a, tool_id="claude_code", category="conversation",
                content_type="jsonl", relative_path=relative_path, content="",
                content_hash=initial_hash, file_size=src_i.size, mode="full",
                offset=len(initial_payload), metadata={}, machine_id=machine_id,
                user_id=user_id, schedule_post_ingest=False, persist_content=False,
                content_s3_key="raw/private/initial.txt", content_already_sanitized=True,
                content_had_sensitive=san_i.had_sensitive, conversation_source=src_i,
            )
            document_id = document.id
            await session_a.commit()
            rows = (
                (await session_a.execute(
                    select(ConversationMessage)
                    .where(ConversationMessage.document_id == document_id)
                    .order_by(ConversationMessage.line_number)
                )).scalars().all()
            )
            assert [m.metadata_.get("source_id") for m in rows] == ["user-1", "asst-2", "asst-3"]

        # Fresh session (production per-request semantics).
        async with session_factory() as session_b:
            try:
                await ingest_file(
                    session_b, tool_id="claude_code", category="conversation",
                    content_type="jsonl", relative_path=relative_path, content="",
                    content_hash=rebase_hash, file_size=src_r.size, mode="full",
                    offset=len(rebase_payload), metadata={}, machine_id=machine_id,
                    user_id=user_id, schedule_post_ingest=False, persist_content=False,
                    content_s3_key="raw/private/rebase.txt", content_already_sanitized=True,
                    content_had_sensitive=san_r.had_sensitive, conversation_source=src_r,
                    authoritative_rebase=True,
                )
                return False
            except MissingGreenlet:
                return True


@pytest.mark.asyncio
@pytest.mark.parametrize("expire_on_commit", [False, True])
async def test_authoritative_rebase_reconcile_keeps_rows_loaded(
    make_session_factory, expire_tracer, monkeypatch, expire_on_commit
) -> None:
    """Regression gate for the shipped fix: the rebase DELETE's evaluate-sync
    must not expire the rows queued for canvas reconcile (the load_only now
    includes document_id), regardless of expire_on_commit."""
    _install_ingest_monkeypatches(monkeypatch)
    snapshots: list = []
    _install_reconcile_probe(monkeypatch, snapshots)

    reproduced = await _run_two_request_rebase(make_session_factory(expire_on_commit))

    saw_expired = any(row["expired"] for snap in snapshots for row in snap)
    print(f"\n[expire_on_commit={expire_on_commit}] reproduced={reproduced} "
          f"saw_expired_row_at_reconcile={saw_expired}")

    assert reproduced is False, "fixed code must not raise MissingGreenlet at reconcile"
    assert saw_expired is False, "reconcile rows must stay live after the rebase DELETE"


@pytest.mark.asyncio
async def test_reintroduced_document_id_omission_reproduces_expiry(
    make_session_factory, expire_tracer, monkeypatch
) -> None:
    """Mechanism proof: stripping document_id from the CM load_only brings the
    original MissingGreenlet back via the bulk-DELETE evaluate synchronize."""
    _install_ingest_monkeypatches(monkeypatch)
    snapshots: list = []
    _install_reconcile_probe(monkeypatch, snapshots)
    _install_load_document_id_omission(monkeypatch)

    reproduced = await _run_two_request_rebase(make_session_factory(False))

    saw_expired = any(row["expired"] for snap in snapshots for row in snap)
    print(f"\n[re-omitted document_id] reproduced={reproduced} "
          f"saw_expired_row_at_reconcile={saw_expired} "
          f"expire_events={len(expire_tracer)}")
    if expire_tracer:
        print("  --- expiry culprit stack (last event) ---")
        print(expire_tracer[-1]["stack"])

    assert reproduced is True, "expected MissingGreenlet at reconcile_message_canvases:127"
    assert saw_expired is True
    # The expiry is the ORM DELETE's evaluate-sync, not any commit.
    assert any(
        "_do_post_synchronize_evaluate" in e["stack"] for e in expire_tracer
    ), "expected the bulk-DELETE evaluate synchronize to be the expiry source"


@pytest.mark.asyncio
async def test_synchronize_session_false_prevents_missing_greenlet(
    make_session_factory, expire_tracer, monkeypatch
) -> None:
    """PROPOSED FIX validation: synchronize_session=False on the CM DELETE stops
    the collateral expiry, so reconcile no longer crashes."""
    _install_ingest_monkeypatches(monkeypatch)
    snapshots: list = []
    _install_reconcile_probe(monkeypatch, snapshots)
    _install_synchronize_false_fix(monkeypatch)

    reproduced = await _run_two_request_rebase(make_session_factory(False))

    saw_expired = any(row["expired"] for snap in snapshots for row in snap)
    print(f"\n[FIX synchronize_session=False] reproduced={reproduced} "
          f"saw_expired_row_at_reconcile={saw_expired} "
          f"expire_events={len(expire_tracer)}")

    assert reproduced is False, "fix should prevent the MissingGreenlet"
    assert saw_expired is False, "fix should leave the reconcile row live/unexpired"


@pytest.mark.asyncio
async def test_load_document_id_prevents_missing_greenlet(
    make_session_factory, expire_tracer, monkeypatch
) -> None:
    """ALT FIX validation: loading document_id lets the evaluate-sync match only
    the intended rows, leaving the reconcile row live."""
    _install_ingest_monkeypatches(monkeypatch)
    snapshots: list = []
    _install_reconcile_probe(monkeypatch, snapshots)
    _install_load_document_id_fix(monkeypatch)

    reproduced = await _run_two_request_rebase(make_session_factory(False))

    saw_expired = any(row["expired"] for snap in snapshots for row in snap)
    print(f"\n[FIX load document_id] reproduced={reproduced} "
          f"saw_expired_row_at_reconcile={saw_expired} "
          f"expire_events={len(expire_tracer)}")

    assert reproduced is False, "loading document_id should prevent the MissingGreenlet"
    assert saw_expired is False
