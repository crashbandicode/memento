from __future__ import annotations

import sys
import unittest
import uuid
import os
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.db.models import (  # noqa: E402
    Base,
    ClaudeConversationLineageRecord,
    Document,
    DocumentDeliveryState,
    Machine,
    Tool,
    User,
)
from server.services.claude_lineage import (  # noqa: E402
    INTERACTION_ORIGIN_KEY,
    active_lineage_record_ids,
    backfill_legacy_interaction_origins,
    canonical_permission_fingerprint,
    delta_continuation_chain,
    delta_has_eligible_record,
    history_entry_is_visible,
    legacy_permission_interaction_id,
    load_lineage_active_states,
    normalize_interaction_origin,
    refresh_claude_lineage,
)
from server.services.document_delivery import (  # noqa: E402
    attach_document_delivery,
    document_metadata,
)

TEST_DATABASE_URL = os.environ.get("MEMENTO_TASK_TEST_DATABASE_URL")
requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="isolated PostgreSQL lineage test database is not configured",
)


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


async def _db_document(session, *, child: bool = False) -> Document:
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@example.test",
        role="viewer",
        status="active",
    )
    machine = Machine(
        id=uuid.uuid4(),
        name="claude-lineage-test",
        collector_token_hash=str(uuid.uuid4()),
        user_id=user.id,
    )
    if await session.get(Tool, "claude_code") is None:
        session.add(Tool(id="claude_code", display_name="Claude Code"))
    document = Document(
        id=uuid.uuid4(),
        tool_id="claude_code",
        machine_id=machine.id,
        relative_path=(
            "projects/thread/subagents/agent-child.jsonl"
            if child
            else "projects/thread.jsonl"
        ),
        category="conversation",
        content_type="jsonl",
        title="Claude lineage",
        content_hash=uuid.uuid4().hex,
        file_size_bytes=1,
        metadata_={"is_subagent": child},
    )
    session.add_all([user, machine, document])
    await session.flush()
    return document


def _record(
    record_uuid: str,
    source_order: int,
    *,
    parent_uuid: str | None = None,
    eligible: bool = True,
    sidechain: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        record_uuid=record_uuid,
        source_order=source_order,
        parent_uuid=parent_uuid,
        is_eligible=eligible,
        is_sidechain=sidechain,
    )


def _origin(kind: str, record_uuid: str = "record-a") -> dict:
    return {
        "version": 1,
        "kind": kind,
        "record_uuid": record_uuid,
        "parent_uuid": "",
        "tool_use_id": "tool-use",
        "fingerprint": "a" * 64,
        "agent_id": "",
        "is_sidechain": False,
    }


class ClaudeLineageTests(unittest.TestCase):
    def test_model_has_durable_composite_identity_and_branch_columns(self) -> None:
        table = ClaudeConversationLineageRecord.__table__
        self.assertEqual(
            {column.name for column in table.primary_key.columns},
            {"document_id", "record_uuid"},
        )
        self.assertTrue(
            {"parent_uuid", "source_order", "is_sidechain", "is_eligible", "active"}
            <= {column.name for column in table.columns}
        )

    def test_full_lineage_uses_latest_eligible_main_leaf(self) -> None:
        rows = [
            _record("root", 1),
            _record("assistant", 2, parent_uuid="root"),
            _record("progress", 3, parent_uuid="assistant", eligible=False),
            _record("history", 4, parent_uuid="assistant", eligible=False),
            _record("side", 5, parent_uuid="assistant", sidechain=True),
        ]
        self.assertEqual(
            active_lineage_record_ids(rows),
            {"root", "assistant"},
        )

    def test_delta_rewind_replaces_abandoned_suffix_without_line_heuristics(
        self,
    ) -> None:
        # A -> B -> C was the prior branch. The DELTA's new D -> A is later
        # in source order, so it becomes terminal and B/C are inactive.
        rows = [
            _record("A", 1),
            _record("B", 2, parent_uuid="A"),
            _record("C", 3, parent_uuid="B"),
            _record("D", 4, parent_uuid="A"),
        ]
        self.assertEqual(active_lineage_record_ids(rows), {"A", "D"})

    def test_ordinary_delta_append_proves_its_suffix_without_a_full_tree_scan(
        self,
    ) -> None:
        records = [
            {"uuid": "D", "parentUuid": "C", "type": "user"},
            {"uuid": "E", "parentUuid": "D", "type": "assistant"},
        ]
        self.assertEqual(
            delta_continuation_chain(
                records,
                current_terminal_uuid="C",
                include_sidechain=False,
            ),
            {"D", "E"},
        )
        self.assertIsNone(
            delta_continuation_chain(
                [{"uuid": "rewind", "parentUuid": "A", "type": "user"}],
                current_terminal_uuid="C",
                include_sidechain=False,
            )
        )
        self.assertFalse(
            delta_has_eligible_record(
                [{"uuid": "progress", "parentUuid": "C", "type": "progress"}],
                include_sidechain=False,
            )
        )
        # An eligible child whose parent is that prior progress node is not a
        # proven direct append and therefore correctly takes the recompute path.
        self.assertIsNone(
            delta_continuation_chain(
                [{"uuid": "later", "parentUuid": "progress", "type": "assistant"}],
                current_terminal_uuid="C",
                include_sidechain=False,
            )
        )

    def test_history_visibility_is_fail_open_except_authoritative_inactive_or_subagent(
        self,
    ) -> None:
        base = {"anchor_line_number": 3}
        interaction = {
            "interaction_type": "permission_request",
            "requested_tool": "Bash",
            "tool_input": {"command": "ls"},
        }
        bound_fingerprint = canonical_permission_fingerprint(
            interaction["requested_tool"],
            interaction["tool_input"],
        )
        main_origin = {
            **_origin("claude_record"),
            "fingerprint": bound_fingerprint,
        }
        main_entry = {
            **base,
            "interaction": interaction,
            INTERACTION_ORIGIN_KEY: main_origin,
        }
        self.assertTrue(
            history_entry_is_visible(
                base,
                projected_through_line=3,
                lineage_state=None,
            )
        )
        self.assertFalse(
            history_entry_is_visible(
                main_entry,
                projected_through_line=3,
                lineage_state=SimpleNamespace(
                    active=False,
                    is_sidechain=False,
                    is_subagent=False,
                    agent_id="",
                ),
            )
        )
        self.assertTrue(
            history_entry_is_visible(
                main_entry,
                projected_through_line=3,
                lineage_state=None,
            )
        )
        # A forged/mistyped origin is not allowed to suppress a real card.
        self.assertTrue(
            history_entry_is_visible(
                {
                    **main_entry,
                    INTERACTION_ORIGIN_KEY: _origin("claude_record"),
                },
                projected_through_line=3,
                lineage_state=SimpleNamespace(
                    active=False,
                    is_sidechain=False,
                    is_subagent=False,
                    agent_id="",
                ),
            )
        )
        subagent_origin = {
            **_origin("claude_subagent_record"),
            "fingerprint": bound_fingerprint,
            "agent_id": "agent-child",
            "is_sidechain": True,
        }
        subagent_entry = {
            **base,
            "interaction": interaction,
            INTERACTION_ORIGIN_KEY: subagent_origin,
        }
        subagent_state = SimpleNamespace(
            active=True,
            is_sidechain=True,
            is_subagent=True,
            agent_id="agent-child",
        )
        self.assertTrue(
            history_entry_is_visible(
                subagent_entry,
                projected_through_line=3,
                lineage_state=subagent_state,
                document_is_subagent=True,
            )
        )
        self.assertFalse(
            history_entry_is_visible(
                subagent_entry,
                projected_through_line=3,
                lineage_state=subagent_state,
                document_is_subagent=False,
            )
        )
        self.assertTrue(
            history_entry_is_visible(
                subagent_entry,
                projected_through_line=3,
                lineage_state=None,
            )
        )
        # A child transcript path is authoritative subagent scope even when
        # Claude omitted both the per-record agentId and isSidechain fields.
        path_scoped_origin = {
            **_origin("claude_subagent_record"),
            "fingerprint": bound_fingerprint,
        }
        path_scoped_entry = {
            **base,
            "interaction": interaction,
            INTERACTION_ORIGIN_KEY: path_scoped_origin,
        }
        self.assertTrue(
            history_entry_is_visible(
                path_scoped_entry,
                projected_through_line=3,
                lineage_state=SimpleNamespace(
                    active=True,
                    is_sidechain=False,
                    is_subagent=True,
                    agent_id="",
                ),
                document_is_subagent=True,
            )
        )
        self.assertFalse(
            history_entry_is_visible(
                path_scoped_entry,
                projected_through_line=3,
                lineage_state=SimpleNamespace(
                    active=False,
                    is_sidechain=False,
                    is_subagent=True,
                    agent_id="",
                ),
                document_is_subagent=True,
            )
        )
        self.assertTrue(
            history_entry_is_visible(
                {**base, INTERACTION_ORIGIN_KEY: _origin("hook_only", "")},
                projected_through_line=3,
                lineage_state=None,
            )
        )
        self.assertFalse(
            history_entry_is_visible(
                base,
                projected_through_line=2,
                lineage_state=None,
            )
        )

    def test_origin_validation_and_fingerprint_are_exact_and_versioned(self) -> None:
        origin = _origin("claude_record")
        self.assertEqual(normalize_interaction_origin(origin), origin)
        self.assertIsNone(normalize_interaction_origin({**origin, "version": 2}))
        self.assertEqual(
            canonical_permission_fingerprint("Bash", {"command": "ls"}),
            "679eb83c897d20b481ff8e75961f8076d6721d232a7ad433cf4528bdeaf4099e",
        )
        self.assertNotEqual(
            canonical_permission_fingerprint("Bash", {"command": "ls"}),
            canonical_permission_fingerprint("Bash", {"command": "dir"}),
        )

    def test_full_legacy_backfill_only_annotates_one_exact_raw_tool_use(self) -> None:
        metadata = {
            "_interaction_history": [
                {
                    "interaction": {
                        "interaction_type": "permission_request",
                        "requested_tool": "Bash",
                        "tool_input": {"command": "ls"},
                    }
                }
            ]
        }
        records = [
            {
                "uuid": "record-1",
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        }
                    ]
                },
            }
        ]
        self.assertTrue(backfill_legacy_interaction_origins(metadata, records))
        origin = metadata["_interaction_history"][0][INTERACTION_ORIGIN_KEY]
        self.assertEqual(origin["record_uuid"], "record-1")
        self.assertEqual(origin["tool_use_id"], "tool-1")
        self.assertFalse(backfill_legacy_interaction_origins(metadata, records))

        ambiguous = {
            "_interaction_history": [
                {
                    "interaction": {
                        "interaction_type": "permission_request",
                        "requested_tool": "Bash",
                        "tool_input": {"command": "ls"},
                    }
                }
            ]
        }
        self.assertFalse(
            backfill_legacy_interaction_origins(
                ambiguous,
                records + [{**records[0], "uuid": "record-2"}],
            )
        )

    def test_legacy_backfill_uses_exact_collector_interaction_id_without_payload(
        self,
    ) -> None:
        session_id = "session-legacy"
        interaction_id = legacy_permission_interaction_id(
            session_id,
            "Bash",
            {"command": "ls"},
        )
        assert interaction_id is not None
        history = {
            "_interaction_history": [
                {
                    "interaction": {
                        "id": interaction_id,
                        "interaction_type": "permission_request",
                        "requested_tool": "Bash",
                    }
                }
            ]
        }
        record = {
            "uuid": "legacy-record",
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "legacy-tool",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    }
                ]
            },
        }

        self.assertTrue(
            backfill_legacy_interaction_origins(
                history,
                [record],
                session_id=session_id,
            )
        )
        repaired_entry = history["_interaction_history"][0]
        self.assertEqual(
            repaired_entry["interaction"]["tool_input"],
            {"command": "ls"},
        )
        self.assertEqual(
            repaired_entry[INTERACTION_ORIGIN_KEY]["record_uuid"],
            "legacy-record",
        )
        self.assertFalse(
            history_entry_is_visible(
                repaired_entry,
                projected_through_line=0,
                lineage_state=SimpleNamespace(
                    active=False,
                    is_sidechain=False,
                    is_subagent=False,
                    agent_id="",
                ),
            )
        )

        wrong_session = {
            "_interaction_history": [
                {
                    "interaction": {
                        "id": interaction_id,
                        "interaction_type": "permission_request",
                        "requested_tool": "Bash",
                    }
                }
            ]
        }
        self.assertFalse(
            backfill_legacy_interaction_origins(
                wrong_session,
                [record],
                session_id="different-session",
            )
        )

        ambiguous = {
            "_interaction_history": [
                {
                    "interaction": {
                        "id": interaction_id,
                        "interaction_type": "permission_request",
                        "requested_tool": "Bash",
                    }
                }
            ]
        }
        self.assertFalse(
            backfill_legacy_interaction_origins(
                ambiguous,
                [record, {**record, "uuid": "duplicate-record"}],
                session_id=session_id,
            )
        )

    def test_legacy_backfill_keeps_dict_history_shape_and_rejects_user_tool_rows(
        self,
    ) -> None:
        entry = {
            "interaction": {
                "interaction_type": "permission_request",
                "requested_tool": "Bash",
                "tool_input": {"command": "ls"},
            }
        }
        assistant_record = {
            "uuid": "assistant-record",
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "assistant-tool",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    }
                ]
            },
        }
        user_record = {**assistant_record, "uuid": "user-record", "type": "user"}
        user_only = {"_interaction_history": {"permission": dict(entry)}}
        self.assertFalse(backfill_legacy_interaction_origins(user_only, [user_record]))
        self.assertNotIn(
            INTERACTION_ORIGIN_KEY,
            user_only["_interaction_history"]["permission"],
        )

        mapped_history = {
            "_interaction_history": {
                "permission": dict(entry),
                "unrelated": "retain this exact non-entry value",
            }
        }
        self.assertTrue(
            backfill_legacy_interaction_origins(
                mapped_history,
                [assistant_record],
            )
        )
        self.assertIsInstance(mapped_history["_interaction_history"], dict)
        self.assertEqual(
            mapped_history["_interaction_history"]["unrelated"],
            "retain this exact non-entry value",
        )
        self.assertEqual(
            mapped_history["_interaction_history"]["permission"][
                INTERACTION_ORIGIN_KEY
            ]["record_uuid"],
            "assistant-record",
        )


@requires_postgres
@pytest.mark.asyncio
async def test_durable_full_delta_rewind_and_child_sidechain_lineage(
    session_factory,
) -> None:
    async with session_factory() as session:
        document = await _db_document(session)
        await refresh_claude_lineage(
            session,
            document,
            [
                {"uuid": "A", "type": "user"},
                {"uuid": "B", "parentUuid": "A", "type": "assistant"},
                {"uuid": "progress", "parentUuid": "B", "type": "progress"},
                {"uuid": "C", "parentUuid": "B", "type": "user"},
            ],
            mode="full",
        )
        await session.flush()
        rows = (
            (
                await session.execute(
                    select(ClaudeConversationLineageRecord).where(
                        ClaudeConversationLineageRecord.document_id == document.id
                    )
                )
            )
            .scalars()
            .all()
        )
        self_by_id = {row.record_uuid: row for row in rows}
        assert {key for key, row in self_by_id.items() if row.active} == {"A", "B", "C"}
        assert self_by_id["progress"].active is False

        # Direct append takes the narrow fast path and keeps the old ancestry.
        assert await refresh_claude_lineage(
            session,
            document,
            [{"uuid": "D", "parentUuid": "C", "type": "assistant"}],
            mode="delta",
        )
        await session.flush()
        rows = (
            (
                await session.execute(
                    select(ClaudeConversationLineageRecord).where(
                        ClaudeConversationLineageRecord.document_id == document.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {row.record_uuid for row in rows if row.active} == {"A", "B", "C", "D"}

        # Rewinding to A forces the authoritative recomputation and retires
        # the abandoned B/C/D suffix regardless of message line positions.
        assert await refresh_claude_lineage(
            session,
            document,
            [{"uuid": "E", "parentUuid": "A", "type": "user"}],
            mode="delta",
        )
        await session.flush()
        rows = (
            (
                await session.execute(
                    select(ClaudeConversationLineageRecord).where(
                        ClaudeConversationLineageRecord.document_id == document.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {row.record_uuid for row in rows if row.active} == {"A", "E"}

        child = await _db_document(session, child=True)
        await refresh_claude_lineage(
            session,
            child,
            [
                {"uuid": "child-A", "type": "user", "isSidechain": True},
                {
                    "uuid": "child-B",
                    "parentUuid": "child-A",
                    "type": "assistant",
                    "isSidechain": True,
                },
            ],
            mode="full",
            document_is_subagent=True,
        )
        await session.flush()
        child_rows = (
            (
                await session.execute(
                    select(ClaudeConversationLineageRecord).where(
                        ClaudeConversationLineageRecord.document_id == child.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {row.record_uuid for row in child_rows if row.active} == {
            "child-A",
            "child-B",
        }


@requires_postgres
@pytest.mark.asyncio
async def test_lineage_state_lookup_binds_uuid_document_keys(session_factory) -> None:
    async with session_factory() as session:
        document = await _db_document(session, child=True)
        await refresh_claude_lineage(
            session,
            document,
            [
                {"uuid": "record-root", "type": "user"},
                {
                    "uuid": "record-inactive",
                    "parentUuid": "record-root",
                    "type": "assistant",
                },
                {
                    "uuid": "record-active",
                    "parentUuid": "record-root",
                    "type": "assistant",
                },
            ],
            mode="full",
        )
        await session.flush()
        entries = [
            {
                INTERACTION_ORIGIN_KEY: {
                    **_origin("claude_subagent_record", record_uuid),
                }
            }
            for record_uuid in ("record-inactive", "record-active")
        ]
        # Passing a UUID here must remain a UUID bind for UUID(as_uuid=True),
        # while the returned API map intentionally uses its string key. The
        # path-derived child scope must survive the query even without literal
        # agentId/isSidechain fields on either raw record.
        states = await load_lineage_active_states(
            session,
            [(document.id, entry) for entry in entries],
        )
        inactive = states[(str(document.id), "record-inactive")]
        active = states[(str(document.id), "record-active")]
        assert inactive.active is False
        assert inactive.is_subagent is True
        assert active.active is True
        assert active.is_subagent is True


@requires_postgres
@pytest.mark.asyncio
async def test_full_ingest_backfill_writes_effective_delivery_metadata(
    session_factory,
) -> None:
    from server.services.ingest_service import _extract_messages

    async with session_factory() as session:
        document = await _db_document(session)
        session_id = "full-ingest-legacy-session"
        delivery = DocumentDeliveryState(
            document_id=document.id,
            revision_hash=document.content_hash,
            file_size_bytes=document.file_size_bytes,
            delivery_metadata={
                "session_id": session_id,
                "_interaction_history": [
                    {
                        "interaction": {
                            "id": legacy_permission_interaction_id(
                                session_id,
                                "Bash",
                                {"command": "ls"},
                            ),
                            "interaction_type": "permission_request",
                            "requested_tool": "Bash",
                        },
                        "anchor_line_number": 0,
                        "status": "answered",
                    }
                ],
            },
        )
        session.add(delivery)
        await session.flush()
        attach_document_delivery(document, delivery, runtime_only=True)
        source = "\n".join(
            json.dumps(record)
            for record in [
                {
                    "uuid": "raw-root",
                    "type": "user",
                    "message": {"role": "user", "content": "root"},
                },
                {
                    "uuid": "raw-record",
                    "parentUuid": "raw-root",
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "raw-tool",
                                "name": "Bash",
                                "input": {"command": "ls"},
                            }
                        ],
                    },
                },
                {
                    "uuid": "current-record",
                    "parentUuid": "raw-root",
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": "current branch",
                    },
                },
            ]
        )

        await _extract_messages(session, document, source, "full")
        await session.flush()

        history = document_metadata(document)["_interaction_history"]
        assert history[0]["interaction"]["tool_input"] == {"command": "ls"}
        origin = history[0][INTERACTION_ORIGIN_KEY]
        assert origin["record_uuid"] == "raw-record"
        assert origin["tool_use_id"] == "raw-tool"
        assert (
            delivery.delivery_metadata["_interaction_history"][0][
                INTERACTION_ORIGIN_KEY
            ]
            == origin
        )
        lineage_states = await load_lineage_active_states(
            session,
            [(document.id, history[0])],
        )
        lineage_state = lineage_states[(str(document.id), "raw-record")]
        assert lineage_state.active is False
        assert (
            history_entry_is_visible(
                history[0],
                projected_through_line=0,
                lineage_state=lineage_state,
            )
            is False
        )
