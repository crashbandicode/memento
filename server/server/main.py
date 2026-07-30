"""FastAPI application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import admin, auth, conversation_exports, conversations, daily, dashboard, data_io, devices, documents, events, hierarchy, ingest, install_bootstrap, memory, projects, public, search, share, tasks, tools
from .config import settings
from .db.models import Base
from .db.session import engine
from .logging_filters import install_sensitive_query_filter
from .services.conversation_identity import (
    CODEX_SESSION_UNIQUE_INDEX_SQL,
    CURSOR_SESSION_UNIQUE_INDEX_SQL,
)
from .services.device_service import DeviceOwnershipError


install_sensitive_query_filter()


def _run_migrations(conn) -> None:
    """Add missing columns to existing tables (lightweight migration)."""
    import secrets
    from sqlalchemy import text, inspect

    # Production connections use a defensive 120-second statement timeout.
    # One-time backfills can legitimately exceed that on multi-gigabyte
    # transcript stores, and a cancellation rolls back the DDL with it so the
    # restart policy repeats the same work forever. Limit this opt-out to the
    # surrounding startup transaction; normal API queries keep their timeout.
    conn.execute(text("SET LOCAL statement_timeout = 0"))
    insp = inspect(conn)

    # Enable pgvector extension
    try:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    except Exception:
        pass  # May not have pgvector installed

    # Required by both document search and the compact conversation spelling
    # lexicon. Enable it before the fresh-install early return so create_all()
    # can create trigram-backed model indexes on the first boot.
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))

    tables = insp.get_table_names()
    if "machines" not in tables or "users" not in tables:
        return  # Fresh install — create_all will handle everything

    # Machine.user_id
    machine_cols = {c["name"] for c in insp.get_columns("machines")}
    if "user_id" not in machine_cols:
        conn.execute(text("ALTER TABLE machines ADD COLUMN user_id UUID REFERENCES users(id)"))

    # A collector starts with a concurrent upload burst.  Enforce one machine
    # row per persistent device ID at the database boundary; ensure_device()
    # also takes a transaction-scoped advisory lock to avoid insert races.
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_machines_collector_token_hash "
        "ON machines (collector_token_hash)"
    ))

    # User.collector_token
    user_cols = {c["name"] for c in insp.get_columns("users")}
    if "collector_token" not in user_cols:
        conn.execute(text("ALTER TABLE users ADD COLUMN collector_token VARCHAR(64) UNIQUE"))

    # User.github_id — GitHub OAuth login. Partial unique index: one account
    # per GitHub identity, while the many github_id IS NULL rows stay allowed.
    if "github_id" not in user_cols:
        conn.execute(text("ALTER TABLE users ADD COLUMN github_id VARCHAR(50)"))
    sp_gh = conn.begin_nested()
    try:
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_github_id "
            "ON users (github_id) WHERE github_id IS NOT NULL"
        ))
        sp_gh.commit()
    except Exception:
        sp_gh.rollback()

    if "totp_secret" not in user_cols:
        conn.execute(text("ALTER TABLE users ADD COLUMN totp_secret TEXT"))
    if "totp_enabled" not in user_cols:
        conn.execute(text(
            "ALTER TABLE users ADD COLUMN totp_enabled BOOLEAN NOT NULL DEFAULT FALSE"
        ))

    # Document.embedding_status + embedding_attempts: tracks whether the
    # embedding pipeline produced vectors so failures can be retried instead
    # of silently dropped. Existing rows get 'ok' if they already have any
    # embedding rows, else 'pending' — the periodic retry task picks those up.
    doc_cols = {c["name"] for c in insp.get_columns("documents")}
    if "activity_at" not in doc_cols:
        conn.execute(text(
            "ALTER TABLE documents ADD COLUMN activity_at TIMESTAMPTZ"
        ))
        # One-time backfill from normalized transcript time, never from the
        # collector delivery time.  Tool/system rows are omitted because they
        # can be synthetic context and should not make a dormant thread look
        # newly active.  Future ingests maintain this value incrementally.
        if "conversation_messages" in tables:
            conn.execute(text(
                "UPDATE documents AS d SET activity_at = ("
                "  SELECT cm.timestamp "
                "  FROM conversation_messages AS cm "
                "  WHERE cm.document_id = d.id "
                "    AND cm.timestamp IS NOT NULL "
                "    AND cm.role IN ('user', 'assistant') "
                "  ORDER BY cm.timestamp DESC "
                "  LIMIT 1"
                ") "
                "WHERE d.category = 'conversation'"
            ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_documents_activity_at "
        "ON documents (activity_at DESC)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_documents_project_activity "
        "ON documents (project_id, activity_at DESC)"
    ))
    if "embedding_status" not in doc_cols:
        conn.execute(text(
            "ALTER TABLE documents ADD COLUMN embedding_status VARCHAR(20) "
            "NOT NULL DEFAULT 'pending'"
        ))
        # Classify existing rows so retry loop (which scans 'failed' only)
        # picks up historical ingest failures without blasting the embedding
        # server with docs that were legitimately skipped.
        conn.execute(text(
            "UPDATE documents SET embedding_status = 'ok' "
            "WHERE id IN (SELECT DISTINCT document_id FROM document_embeddings)"
        ))
        conn.execute(text(
            "UPDATE documents SET embedding_status = 'skipped' "
            "WHERE embedding_status = 'pending' "
            "AND (content IS NULL OR LENGTH(content) < 100 "
            "     OR content_type IN ('sqlite', 'sqlite_export', 'binary'))"
        ))
        conn.execute(text(
            "UPDATE documents SET embedding_status = 'failed' "
            "WHERE embedding_status = 'pending'"
        ))
    if "embedding_attempts" not in doc_cols:
        conn.execute(text(
            "ALTER TABLE documents ADD COLUMN embedding_attempts INTEGER "
            "NOT NULL DEFAULT 0"
        ))
    if "embedding_claim_token" not in doc_cols:
        conn.execute(text(
            "ALTER TABLE documents ADD COLUMN embedding_claim_token VARCHAR(36)"
        ))
    if "embedding_claimed_at" not in doc_cols:
        conn.execute(text(
            "ALTER TABLE documents ADD COLUMN embedding_claimed_at TIMESTAMPTZ"
        ))
    if "embedding_content_hash" not in doc_cols:
        # Leave historical rows NULL. On their first changed ingest the server
        # derives the old input from the still-current content/messages before
        # replacing them, so existing vectors can be preserved when the
        # bounded model input did not actually change. Pending/failed rows also
        # populate this lazily when an embedding worker claims them.
        conn.execute(text(
            "ALTER TABLE documents ADD COLUMN embedding_content_hash VARCHAR(64)"
        ))
    if "embedding_tier" not in doc_cols:
        # Additive: existing corpus stays on the quality (1024-d) path. No
        # mass re-embed. Fast tier is opt-in via MEMENTO_EMBEDDING_TIERING_ENABLED
        # for newly routed documents only.
        conn.execute(text(
            "ALTER TABLE documents ADD COLUMN embedding_tier VARCHAR(20) "
            "NOT NULL DEFAULT 'quality'"
        ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_documents_embedding_retry "
        "ON documents (embedding_status, embedding_attempts, embedding_claimed_at)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_documents_embedding_tier "
        "ON documents (embedding_tier, embedding_status)"
    ))

    # Per-chunk model identity enables incremental embedding updates. Historical
    # rows stay NULL and are safely recognized by exact chunk text until their
    # document next changes; no corpus-wide vector rewrite is required.
    if "document_embeddings" in tables:
        embedding_cols = {c["name"] for c in insp.get_columns("document_embeddings")}
        if "chunk_hash" not in embedding_cols:
            conn.execute(text(
                "ALTER TABLE document_embeddings ADD COLUMN chunk_hash VARCHAR(64)"
            ))
        if "model_name" not in embedding_cols:
            conn.execute(text(
                "ALTER TABLE document_embeddings ADD COLUMN model_name VARCHAR(200)"
            ))
        if "backend" not in embedding_cols:
            conn.execute(text(
                "ALTER TABLE document_embeddings ADD COLUMN backend VARCHAR(32)"
            ))
        if "profile_signature" not in embedding_cols:
            conn.execute(text(
                "ALTER TABLE document_embeddings "
                "ADD COLUMN profile_signature VARCHAR(80)"
            ))

    # Fast-tier table (384-d). Separate from document_embeddings so dimensions
    # are never padded or mixed in one HNSW index. create_all also defines this
    # model; the DDL here covers existing deployments before metadata create.
    if "document_embeddings_fast" not in tables:
        sp_fast = conn.begin_nested()
        try:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS document_embeddings_fast ("
                "id UUID PRIMARY KEY, "
                "document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE, "
                "chunk_index INTEGER NOT NULL, "
                "chunk_text TEXT NOT NULL, "
                "chunk_hash VARCHAR(64), "
                "model_name VARCHAR(200), "
                "backend VARCHAR(32), "
                "profile_signature VARCHAR(80), "
                "embedding vector(384) NOT NULL, "
                "created_at TIMESTAMPTZ DEFAULT now(), "
                "CONSTRAINT uq_doc_embedding_fast_chunk "
                "UNIQUE (document_id, chunk_index)"
                ")"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_doc_embedding_fast_doc "
                "ON document_embeddings_fast (document_id)"
            ))
            sp_fast.commit()
        except Exception:
            sp_fast.rollback()
    else:
        fast_embedding_cols = {
            c["name"] for c in insp.get_columns("document_embeddings_fast")
        }
        if "profile_signature" not in fast_embedding_cols:
            conn.execute(text(
                "ALTER TABLE document_embeddings_fast "
                "ADD COLUMN profile_signature VARCHAR(80)"
            ))

    # knowledge_status / knowledge_attempts: same pattern as the embedding
    # pair, added later to give a way to retry LLM extraction. Existing rows
    # get classified the same way: 'ok' when there's already at least one
    # observation pointing to them, 'skipped' for short/binary content,
    # everything else 'failed' so the knowledge_retry beat picks them up.
    if "knowledge_status" not in doc_cols:
        conn.execute(text(
            "ALTER TABLE documents ADD COLUMN knowledge_status VARCHAR(20) "
            "NOT NULL DEFAULT 'pending'"
        ))
        conn.execute(text(
            "UPDATE documents SET knowledge_status = 'ok' "
            "WHERE id IN (SELECT DISTINCT source_document_id FROM "
            "knowledge_observations WHERE source_document_id IS NOT NULL)"
        ))
        conn.execute(text(
            "UPDATE documents SET knowledge_status = 'skipped' "
            "WHERE knowledge_status = 'pending' "
            "AND (content IS NULL OR LENGTH(content) < 200 "
            "     OR category NOT IN ('conversation', 'memory', 'learning', 'plan'))"
        ))
        conn.execute(text(
            "UPDATE documents SET knowledge_status = 'failed' "
            "WHERE knowledge_status = 'pending'"
        ))
    if "knowledge_attempts" not in doc_cols:
        conn.execute(text(
            "ALTER TABLE documents ADD COLUMN knowledge_attempts INTEGER "
            "NOT NULL DEFAULT 0"
        ))

    # Document.content_tsv: tsvector of jieba-tokenized content+title for
    # full-text search fallback when the embedding server is slow/down. We
    # populate it from Python (jieba) on ingest; Postgres just stores +
    # indexes. Backfill is done by a one-shot script, not here, to avoid
    # blocking startup on large tables.
    if "content_tsv" not in doc_cols:
        conn.execute(text("ALTER TABLE documents ADD COLUMN content_tsv tsvector"))
    sp3 = conn.begin_nested()
    try:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_documents_content_tsv "
            "ON documents USING gin (content_tsv)"
        ))
        sp3.commit()
    except Exception:
        sp3.rollback()

    # DailySummary.user_id + swap unique index so each user has their own digest
    # per date+tool. Before this, the table was globally scoped and any user's
    # call to /generate-summary wrote a summary visible to every other user.
    if "daily_summaries" in tables:
        ds_cols = {c["name"] for c in insp.get_columns("daily_summaries")}
        if "user_id" not in ds_cols:
            conn.execute(text(
                "ALTER TABLE daily_summaries ADD COLUMN user_id UUID "
                "REFERENCES users(id) ON DELETE CASCADE"
            ))
        # Drop old (summary_date, tool_id) unique index if present; create the
        # user-scoped one. Wrapped in savepoints so the overall tx doesn't abort
        # if the old index name varies or already exists.
        for stmt in (
            "DROP INDEX IF EXISTS uq_daily_summary_date_tool",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_daily_summary_user_date_tool "
            "ON daily_summaries (user_id, summary_date, tool_id)",
            "CREATE INDEX IF NOT EXISTS idx_daily_summary_user "
            "ON daily_summaries (user_id)",
        ):
            sp2 = conn.begin_nested()
            try:
                conn.execute(text(stmt))
                sp2.commit()
            except Exception:
                sp2.rollback()

    # ShareLink.target_user_id: when set, the share is only viewable by that
    # logged-in user (vs the legacy "anyone with the link" public default).
    # Lets owners forward project timelines / dailies / memory to specific
    # viewer accounts without exposing them anonymously.
    if "share_links" in tables:
        sl_cols = {c["name"] for c in insp.get_columns("share_links")}
        if "target_user_id" not in sl_cols:
            conn.execute(text(
                "ALTER TABLE share_links ADD COLUMN target_user_id UUID "
                "REFERENCES users(id) ON DELETE CASCADE"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_share_target_user "
                "ON share_links (target_user_id)"
            ))

    # Current task-list projection. ConversationMessage rows remain immutable
    # history; this one-row-per-document table is the indexed query authority.
    task_projection_dependencies_exist = {
        "documents",
        "conversation_messages",
    }.issubset(tables)
    if task_projection_dependencies_exist:
        if "conversation_task_states" not in tables:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS conversation_task_states ("
                "document_id UUID PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE, "
                "machine_id UUID REFERENCES machines(id) ON DELETE CASCADE, "
                "user_id UUID REFERENCES users(id) ON DELETE CASCADE, "
                "tool_id VARCHAR(50) NOT NULL, "
                "thread_id VARCHAR(512), "
                "root_thread_id VARCHAR(512), "
                "parent_thread_id VARCHAR(512), "
                "agent_id VARCHAR(512), "
                "agent_path TEXT, "
                "agent_depth INTEGER NOT NULL DEFAULT 0, "
                "source_message_id BIGINT REFERENCES conversation_messages(id) "
                "ON DELETE SET NULL, "
                "source_line_number INTEGER, "
                "source_ids JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "revision INTEGER NOT NULL DEFAULT 0, "
                "state JSONB NOT NULL, "
                "state_hash VARCHAR(64) NOT NULL, "
                "explicit_current BOOLEAN NOT NULL DEFAULT FALSE, "
                "quality VARCHAR(32) NOT NULL DEFAULT 'partial', "
                "projection_version INTEGER NOT NULL DEFAULT 1, "
                "pending_count INTEGER NOT NULL DEFAULT 0, "
                "in_progress_count INTEGER NOT NULL DEFAULT 0, "
                "blocked_count INTEGER NOT NULL DEFAULT 0, "
                "completed_count INTEGER NOT NULL DEFAULT 0, "
                "cancelled_count INTEGER NOT NULL DEFAULT 0, "
                "outstanding_count INTEGER NOT NULL DEFAULT 0, "
                "total_count INTEGER NOT NULL DEFAULT 0, "
                "observed_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                "verified_at TIMESTAMPTZ, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
                "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                ")"
            ))
        for task_index_sql in (
            "CREATE INDEX IF NOT EXISTS idx_task_state_machine "
            "ON conversation_task_states (machine_id)",
            "CREATE INDEX IF NOT EXISTS idx_task_state_user "
            "ON conversation_task_states (user_id)",
            "CREATE INDEX IF NOT EXISTS idx_task_state_thread "
            "ON conversation_task_states (machine_id, tool_id, thread_id)",
            "CREATE INDEX IF NOT EXISTS idx_task_state_root "
            "ON conversation_task_states (machine_id, tool_id, root_thread_id)",
            "CREATE INDEX IF NOT EXISTS idx_task_state_parent "
            "ON conversation_task_states (machine_id, tool_id, parent_thread_id)",
            "CREATE INDEX IF NOT EXISTS idx_task_state_agent "
            "ON conversation_task_states (machine_id, tool_id, agent_id)",
            "CREATE INDEX IF NOT EXISTS idx_task_state_outstanding "
            "ON conversation_task_states "
            "(machine_id, outstanding_count, observed_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_task_state_status_counts "
            "ON conversation_task_states (tool_id, outstanding_count)",
            "CREATE INDEX IF NOT EXISTS idx_task_state_pending "
            "ON conversation_task_states (machine_id, pending_count)",
            "CREATE INDEX IF NOT EXISTS idx_task_state_in_progress "
            "ON conversation_task_states (machine_id, in_progress_count)",
            "CREATE INDEX IF NOT EXISTS idx_task_state_blocked "
            "ON conversation_task_states (machine_id, blocked_count)",
            "CREATE INDEX IF NOT EXISTS idx_task_state_completed "
            "ON conversation_task_states (machine_id, completed_count)",
            "CREATE INDEX IF NOT EXISTS idx_task_state_cancelled "
            "ON conversation_task_states (machine_id, cancelled_count)",
        ):
            conn.execute(text(task_index_sql))

    # Data migration: assign owner token + bind existing machines to owner
    result = conn.execute(text(
        "SELECT id, collector_token FROM users WHERE role = 'owner' AND status = 'active' LIMIT 1"
    ))
    owner = result.first()
    if owner:
        owner_id, owner_token = owner[0], owner[1]
        if not owner_token:
            token = secrets.token_hex(32)
            conn.execute(text(
                "UPDATE users SET collector_token = :token WHERE id = :uid AND collector_token IS NULL"
            ), {"token": token, "uid": owner_id})
        conn.execute(text("UPDATE machines SET user_id = :uid WHERE user_id IS NULL"),
                     {"uid": owner_id})

    # Older ingest code created every SyncState with machine_id=NULL even
    # though the corresponding Document was device-scoped. Backfill only
    # one-to-one, unambiguous tool/path identities. Ambiguous legacy data is
    # deliberately left untouched for manual review rather than guessed.
    if "sync_state" in tables and "documents" in tables:
        conn.execute(text("""
            WITH document_identity AS (
                SELECT
                    tool_id,
                    relative_path,
                    MAX(machine_id::text)::uuid AS machine_id,
                    MAX(content_hash) AS content_hash
                FROM documents
                WHERE machine_id IS NOT NULL
                GROUP BY tool_id, relative_path
                HAVING COUNT(DISTINCT machine_id) = 1
            ), unique_null_state AS (
                SELECT tool_id, relative_path
                FROM sync_state
                WHERE machine_id IS NULL
                GROUP BY tool_id, relative_path
                HAVING COUNT(*) = 1
            )
            UPDATE sync_state AS state
            SET machine_id = identity.machine_id
            FROM document_identity AS identity
            JOIN unique_null_state AS legacy
              ON legacy.tool_id IS NOT DISTINCT FROM identity.tool_id
             AND legacy.relative_path = identity.relative_path
            WHERE state.machine_id IS NULL
              AND state.tool_id IS NOT DISTINCT FROM identity.tool_id
              AND state.relative_path = identity.relative_path
              AND state.last_hash IS NOT DISTINCT FROM identity.content_hash
              AND NOT EXISTS (
                  SELECT 1
                  FROM sync_state AS scoped
                  WHERE scoped.machine_id = identity.machine_id
                    AND scoped.tool_id IS NOT DISTINCT FROM identity.tool_id
                    AND scoped.relative_path = identity.relative_path
              )
        """))

    # Performance indexes (idempotent). Each runs in its own savepoint so a
    # single failure doesn't abort the whole migration tx.
    for stmt in (
        # These append/update-heavy tables need vacuum/analyze decisions based
        # on a small absolute fraction of the table. PostgreSQL's default 20%
        # scale factor leaves hundreds of thousands of dead message tuples
        # behind and makes ingest/search bursts unnecessarily CPU-heavy.
        "ALTER TABLE conversation_messages SET ("
        "autovacuum_vacuum_scale_factor = 0.02, "
        "autovacuum_vacuum_threshold = 10000, "
        "autovacuum_analyze_scale_factor = 0.01, "
        "autovacuum_analyze_threshold = 5000"
        ")",
        "ALTER TABLE document_embeddings SET ("
        "autovacuum_vacuum_scale_factor = 0.05, "
        "autovacuum_vacuum_threshold = 1000, "
        "autovacuum_analyze_scale_factor = 0.02, "
        "autovacuum_analyze_threshold = 500"
        ")",
        "ALTER TABLE document_embeddings_fast SET ("
        "autovacuum_vacuum_scale_factor = 0.05, "
        "autovacuum_vacuum_threshold = 1000, "
        "autovacuum_analyze_scale_factor = 0.02, "
        "autovacuum_analyze_threshold = 500"
        ")",
        "ALTER TABLE documents SET ("
        "autovacuum_vacuum_scale_factor = 0.05, "
        "autovacuum_vacuum_threshold = 250, "
        "autovacuum_analyze_scale_factor = 0.02, "
        "autovacuum_analyze_threshold = 100"
        ")",
        "CREATE INDEX IF NOT EXISTS idx_conv_msg_timestamp ON conversation_messages (timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_conv_msg_doc_ts ON conversation_messages (document_id, timestamp)",
        # Partial index for the daily / dashboard hot path: filter user+assistant
        # messages by recent timestamp. Without it the planner seq-scans the
        # whole 117K+ row conversation_messages table and cold-cache first hits
        # take ~6s; with it, the same query is <200ms cold.
        "CREATE INDEX IF NOT EXISTS idx_conv_msg_role_ts "
        "ON conversation_messages (role, timestamp DESC) "
        "WHERE role IN ('user', 'assistant')",
        # Message-level search is deliberately partial: ordinary searches
        # cover the human/assistant conversation, while large tool payloads
        # do not inflate the indexes or dominate fuzzy matches. Existing
        # production instances create these concurrently before rollout; the
        # idempotent definitions here cover fresh installs and restores.
        "CREATE INDEX IF NOT EXISTS idx_conv_msg_content_fts "
        "ON conversation_messages USING gin "
        "(to_tsvector('simple', content)) "
        "WHERE role IN ('user', 'assistant')",
        "CREATE INDEX IF NOT EXISTS idx_conv_msg_content_trgm "
        "ON conversation_messages USING gin (content gin_trgm_ops) "
        "WHERE role IN ('user', 'assistant')",
        "CREATE INDEX IF NOT EXISTS idx_documents_tool_synced ON documents (tool_id, synced_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_documents_project_synced ON documents (project_id, synced_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_documents_project_category ON documents (project_id, category)",
        # Cursor can relocate one stable thread from empty-window to its real
        # project, or promote a nested subagent transcript to a root path.
        # The server upserts these by the verified session UUID; enforce that
        # invariant in PostgreSQL as the final concurrent-writer guard.
        CURSOR_SESSION_UNIQUE_INDEX_SQL,
        # Codex moves a rollout between sessions/ and archived_sessions/ while
        # retaining one thread UUID. Keep that move as one logical document.
        CODEX_SESSION_UNIQUE_INDEX_SQL,
        "CREATE INDEX IF NOT EXISTS idx_documents_title_trgm ON documents USING gin (title gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS idx_documents_path_trgm ON documents USING gin (relative_path gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS idx_documents_content_trgm ON documents USING gin (content gin_trgm_ops)",
        # Vector ANN index for semantic search. Without this, /api/memory/semantic
        # seq-scans document_embeddings — fine at 50 rows, fatal at 1M. HNSW
        # preferred over IVFFlat: no training step, better recall, pgvector
        # 0.5+ required. Dim must match Vector(1024) in DocumentEmbedding.
        "CREATE INDEX IF NOT EXISTS idx_doc_embedding_hnsw "
        "ON document_embeddings USING hnsw (embedding vector_cosine_ops)",
        # Separate HNSW for the fast (384-d) table. Never share an index with
        # the 1024-d quality vectors.
        "CREATE INDEX IF NOT EXISTS idx_doc_embedding_fast_hnsw "
        "ON document_embeddings_fast USING hnsw (embedding vector_cosine_ops)",
    ):
        sp = conn.begin_nested()
        try:
            conn.execute(text(stmt))
            sp.commit()
        except Exception:
            sp.rollback()


async def _schedule_daily_compaction():
    """Run memory compaction once per day in background."""
    import asyncio
    await asyncio.sleep(60)  # Wait for startup to complete
    while True:
        try:
            from .db.session import async_session_factory
            from .services.memory_compaction import run_compaction
            async with async_session_factory() as db:
                await run_compaction(db)
        except Exception as e:
            import logging
            logging.getLogger("compaction").info("Compaction skipped: %s", e)
        await asyncio.sleep(86400)  # Every 24 hours


async def _warm_embedding_server() -> None:
    """Send a single tiny encode request shortly after boot so the first
    real /api/memory/semantic call doesn't pay the BGE-M3 model's
    first-encode cost (model is loaded at embedding-server startup but
    the actual encode pipeline has JIT/cache warmup that can push 5-10 s
    on a cold CPU). 5 s delay lets the api itself finish lifespan first.
    """
    import asyncio
    import logging
    log = logging.getLogger("memento.warmup")
    await asyncio.sleep(5)
    try:
        from .services.embedding_service import _call_embedding_server
        v = await _call_embedding_server(
            ["warmup"],
            timeout=30.0,
            purpose="query",
        )
        if v and v[0]:
            log.info("embedding server warmed (dim=%d)", len(v[0]))
        else:
            log.info("embedding server warmup returned empty — service likely down")
    except Exception as e:
        log.info("embedding warmup skipped: %s", e)


async def _repair_pending_question_badges() -> None:
    """Reconcile stale attention badges after the interactive-inbox rollout."""
    import asyncio
    import logging

    await asyncio.sleep(3)
    try:
        from .db.session import async_session_factory
        from .services.ingest_service import reconcile_pending_question_metadata

        async with async_session_factory() as db:
            updated = await reconcile_pending_question_metadata(db)
        logging.getLogger("memento.pending_questions").info(
            "reconciled pending-question metadata for %d conversations",
            updated,
        )
    except Exception:
        logging.getLogger("memento.pending_questions").exception(
            "pending-question metadata reconciliation failed"
        )


async def _require_fast_embedding_server() -> None:
    """Reject a half-enabled tiering deployment with a clear startup error."""
    import asyncio

    from .services.embedding_service import (
        TIER_FAST,
        embedding_profile,
        embedding_tiering_enabled,
        tiers_with_searchable_rows,
        validate_embedding_profile_server,
    )

    fast_required = embedding_tiering_enabled()
    if not fast_required:
        from .db.session import async_session_factory

        async with async_session_factory() as db:
            fast_required = TIER_FAST in await tiers_with_searchable_rows(
                db,
                machine_ids=None,
            )
    if not fast_required:
        return
    profile = embedding_profile(TIER_FAST)
    last_error: Exception | None = None
    # Compose starts profiled services concurrently. Give the baked model time
    # to load, but do not accept traffic that would be routed into a dead tier.
    for _ in range(30):
        try:
            await validate_embedding_profile_server(profile, timeout=3.0)
            return
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(2)
    raise RuntimeError(
        "The fast embedding service is required by tiering or persisted fast "
        "vectors, but it is not ready. "
        "Start the deployment with `docker compose --profile embedding-fast "
        "up -d`; disabling MEMENTO_EMBEDDING_TIERING_ENABLED is sufficient "
        "only after no searchable fast-tier rows remain."
    ) from last_error


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    import asyncio
    settings.validate_production()
    async with engine.begin() as conn:
        await conn.run_sync(_run_migrations)
        await conn.run_sync(Base.metadata.create_all)
    await _require_fast_embedding_server()
    # Start daily compaction in background
    compaction_task = asyncio.create_task(_schedule_daily_compaction())
    # Fire-and-forget warmup of the embedding server (5s after boot)
    warmup_task = asyncio.create_task(_warm_embedding_server())
    pending_question_repair_task = asyncio.create_task(
        _repair_pending_question_badges()
    )
    yield
    compaction_task.cancel()
    warmup_task.cancel()
    pending_question_repair_task.cancel()
    await engine.dispose()


app = FastAPI(
    title="Memento",
    description="A shared brain for your AI coding tools — collects, indexes and surfaces conversations, memory, plans across every AI IDE and every device.",
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(DeviceOwnershipError)
async def device_ownership_error_handler(
    _request: Request, _exc: DeviceOwnershipError,
) -> JSONResponse:
    """Reject cross-account collector device reuse without exposing details."""
    return JSONResponse(status_code=403, content={"detail": "Collector device is not authorized"})

# CORS — regex source is settings.cors_allow_origin_regex (see config.py).
# Self-hosted deployments on LAN IPs (192.168.x / 10.x / 172.16-31.x) are
# allowed by default; users with a public custom domain set
# MEMENTO_CORS_ALLOW_ORIGIN_REGEX in .env.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=settings.cors_allow_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(dashboard.router)
app.include_router(ingest.router)
app.include_router(tools.router)
app.include_router(documents.router)
app.include_router(conversations.router)
app.include_router(conversation_exports.router)
app.include_router(projects.router)
app.include_router(daily.router)
app.include_router(search.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(events.router)
app.include_router(devices.router)
app.include_router(hierarchy.router)
app.include_router(tasks.router)
app.include_router(memory.router)
app.include_router(install_bootstrap.router)
app.include_router(public.router)
app.include_router(share.router)
app.include_router(data_io.router)

# Mount MCP Memory Server (best-effort, skip if deps not available)
try:
    from .api.mcp_mount import mount_mcp
    mount_mcp(app)
except Exception:
    pass


@app.get("/")
async def root() -> dict:
    return {
        "name": "Memento",
        "version": "0.1.0",
        "docs": "/docs",
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
