# Realtime ingest Phase 4 handoff

## Objective and scope

Implement the binding Phase 4 of `docs/REALTIME_INGEST_DESIGN.md` without a
commit or push: move Canvas reconciliation and conversation search/lexicon
refresh out of the synchronous ingest commit into a durable, revision-fenced
projector. Messages, read model, prompts/tasks, dashboard, delivery, and sync
stay in the main commit.

The task named branch `perf/collector-steady-state`. This worktree was on
`main` tracking `fork/main` when the session started. Confirm the intended
branch before you commit.

Do not deploy from this handoff. WSL deploy files and `.env` were not
modified. (Correction from review: repo `docker-compose.yml` IS modified in
this change — it back-fills the Phase 3 `realtime-ingest-drain` service and
raw-writer env plumbing; the projector service is intentionally not added.)

## What moved

Flag **off** (default): legacy/Core ingest still reconciles Canvas and
refreshes `content_tsv` plus `conversation_search_terms` inside the same
commit as messages. Behavior is unchanged.

Flag **on**: those two projections are skipped in the writer. The same
transaction inserts real outbox rows (`ingest_projection_candidates`). A
long-lived projector process (`python -m server.services.realtime_ingest_projector`)
collapses pending work per `(document_id, kind)` to the **current** delivery
revision, applies Canvas/search once, then marks older fences `superseded_at`
and all claimed rows `completed_at`.

| Path | Canvas | Search / lexicon |
| --- | --- | --- |
| Legacy / Core, flag off | `project_message_canvases` / `reconcile_message_canvases` in `_stage_new_conversation_messages` and `_extract_messages` | last-200 FTS + `upsert_search_terms` in `ingest_file` / `_extract_messages` |
| Legacy / Core, flag on | skip those calls; set `_memento_canvas_projection_candidate` when a mutation can change Canvas (new `.canvas.tsx` body or an updated existing row in `canvas_reconcile_rows`) | skip FTS write and lexicon upsert; enqueue when `_conversation_search_index_needs_refresh` is true |
| Raw writer, flag off | **still does not** write Canvas/search (pre-Phase-4 gap) | same |
| Raw writer, flag on | `enqueue_projection_candidates_raw` in `_apply` when `canvas_candidate` / `search_candidate` | same; this is how the drain/raw path first gains those projections |

Idempotent / stale / superseded dispositions do not enqueue. `pg_notify` on
`memento_ingest_projections` is a wake; pending-row scan is authoritative.

## Schema of `ingest_projection_candidates`

Identity is `(document_id, revision_hash, kind)`:

- `id BIGSERIAL PRIMARY KEY`
- `document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE`
- `revision_hash VARCHAR(64) NOT NULL` (delivery revision / content hash)
- `kind VARCHAR(32) NOT NULL` — `'canvas'` or `'search'`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `claimed_at`, `completed_at`, `superseded_at` (nullable timestamptz)
- `CONSTRAINT uq_ingest_projection_candidate_fence UNIQUE (document_id, revision_hash, kind)`
- `CONSTRAINT ck_ingest_projection_candidate_kind CHECK (kind IN ('canvas', 'search'))`
- partial index `idx_ingest_projection_candidates_pending` on
  `(document_id, kind, created_at) WHERE completed_at IS NULL AND superseded_at IS NULL`

ORM: `IngestProjectionCandidate` in `server/server/db/models.py`.
Production apply for existing databases: `CREATE TABLE IF NOT EXISTS` in
`server.main._run_migrations` (same pattern as delivery-state DDL). Fresh
installs and the task test DB get the table from SQLAlchemy `create_all`.

Alembic revision `20260827_01` under `server/server/db/migrations/versions/`
documents the same idempotent DDL. This repo’s live schema path is still
`_run_migrations` + `create_all`, not `alembic upgrade`. Do not run that
upgrade as the deploy mechanism: it is a root revision (`down_revision = None`)
and would not create the rest of the schema. `server/alembic.ini`
`script_location = server/db/migrations` is correct when cwd is `server/`.

## Flag name and rollout order

| Setting | Env | Default |
| --- | --- | --- |
| `realtime_ingest_deferred_projections` | `MEMENTO_REALTIME_INGEST_DEFERRED_PROJECTIONS` | `false` |
| `realtime_ingest_projector_poll_seconds` | `MEMENTO_REALTIME_INGEST_PROJECTOR_POLL_SECONDS` | `0.10` |

**Rollout (deploy owns compose/.env):**

1. Ship code with the flag still false. Restart **api** so `_run_migrations`
   creates the outbox table.
2. Start **realtime-ingest-projector**. It idles: there are no candidates yet.
   The process does not need the flag to consume the outbox.
3. Set `MEMENTO_REALTIME_INGEST_DEFERRED_PROJECTIONS=true` on every process
   that **commits** conversation ingest, then restart those processes:
   **api**, **realtime-ingest-drain**, **celery-ingest-worker** (chunked
   FULLs). Include **celery-worker** only if it still calls `ingest_file`.
4. Flag on without a running projector stalls Canvas/search. Flag off with a
   running projector is safe (empty outbox).

Turning the flag on is also what gives the raw/drain path Canvas and
conversation FTS at all. Leaving it off keeps legacy/Core synchronous
projections and leaves raw without those writes (the pre-Phase-4 gap).

## Compose service spec for deploy

Do not add this here; mirror `realtime-ingest-drain`. Suggested block:

```yaml
  realtime-ingest-projector:
    build:
      context: .
      dockerfile: server/Dockerfile
      args:
        PIP_INDEX_URL: ${PIP_INDEX_URL:-}
    container_name: memento_realtime_ingest_projector
    restart: unless-stopped
    command: python -m server.services.realtime_ingest_projector
    environment:
      MEMENTO_DATABASE_URL: postgresql+asyncpg://postgres:${POSTGRES_PASSWORD:-postgres}@postgres:5432/memento
      MEMENTO_REDIS_URL: redis://redis:6379/0
      MEMENTO_REALTIME_INGEST_DEFERRED_PROJECTIONS: ${MEMENTO_REALTIME_INGEST_DEFERRED_PROJECTIONS:-false}
      MEMENTO_REALTIME_INGEST_PROJECTOR_POLL_SECONDS: ${MEMENTO_REALTIME_INGEST_PROJECTOR_POLL_SECONDS:-0.10}
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
```

Unlike the drain, this service does **not** need the `ingest_spool` volume or
MinIO. Redis is only for post-projection `file_synced` SSE
(`conversation.canvas` / `conversation.search`) when the snapshot actually
changed. Single-instance lock is `pg_try_advisory_lock` on
`memento:ingest-projector:v1` (same idea as the drain’s spool lock). SIGINT /
SIGTERM stop after the current cycle. Poll plus `LISTEN memento_ingest_projections`.

Also add the deferred-projections env var to **api**, **realtime-ingest-drain**,
and **celery-ingest-worker** (still default `false`).

## Projector apply semantics

- Groups pending rows by `(document_id, kind)`, `FOR UPDATE SKIP LOCKED`.
- Re-reads current `document_delivery_state.revision_hash` (else
  `documents.content_hash`) and **always applies against that current
  revision**, even when no candidate hash equals it (R1 canvas + R2 without a
  canvas candidate still reconciles at R2).
- Canvas: load messages that already have refs **or** `content ILIKE %.canvas.tsx%`,
  then `reconcile_message_canvases`.
- Search: same last-200 `left(content, 2048)` user/assistant recipe as
  `ingest_file`, `tokenize_for_index(title + body)`, `to_tsvector('simple', ...)`,
  lexicon via `extract_search_terms` + `upsert_search_terms` capped at
  `MAX_LEXICON_TERMS_PER_INGEST`.
- Apply + outbox completion share one transaction. A crash before commit
  leaves rows pending; replay is the same work at the current fence.
- Restart after completion is a no-op on those identities (pending filter).
  Reconcile and tsv write are idempotent.

## Tests

All commands used
`C:\Users\intpa\OneDrive\Documents\test\memento-canvas-fix\.venv\Scripts\python.exe`
with `MEMENTO_TASK_TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:55437/postgres`,
cwd `server/`, `--basetemp` under the repo. Pytest emitted only the
pre-existing Windows `.pytest_cache` permission warnings.

| Suite | Result |
| --- | --- |
| `python -m py_compile server/services/realtime_ingest_projector.py` | OK |
| `tests/test_realtime_ingest_projector.py -q` | **10 passed** in 218.29s |
| `tests/test_realtime_ingest_parity.py -q` | **6 passed** in 215.46s (flag off; goldens not regenerated) |
| `tests/test_realtime_ingest_drain.py -q` | **5 passed** in 2.12s |
| `tests/test_ingest_spool.py -q` | **42 passed**, 16 subtests, in 3.32s |

Projector gates (all in `test_realtime_ingest_projector.py`):

1. Flag defaults off; advisory lock owner skips `run_once`.
2. Legacy / Core / raw with flag on: no Canvas/lexicon before the projector;
   after `run_until_quiescent`, Canvas snapshot + `content_tsv::text` + a
   unique lexicon nonce match a **legacy flag-off** synchronous ingest of the
   same transcript (stable relative path so title tokens match).
3. Crash/replay: three documents, `run_once(limit=1)`, then quiescence, then a
   second quiescence — identical snapshots, one Canvas ref per document, no
   pending rows.
4. Revision fence: FULL then DELTA without running the projector; both search
   fences queued; one apply at R2; R1 `superseded_at` set.
5. Flag on + projector inside `_snapshot`: live golden fields except
   `staged_sse_events` still match `realtime_ingest_parity_golden.json` for
   legacy/Core/raw. SSE is excluded because the projector may add
   `conversation.canvas` / `conversation.search` after commit.

Goldens were not regenerated. Canvas/search are not keys in the Phase 0
golden; the dedicated match test is the Canvas/search equality gate.

## Known gaps / risks

- **Raw + flag off** still does not write Canvas/search. That is pre-existing.
  Flag on + projector is the intended fill-in.
- **Canvas removal on raw** enqueues when the mutation is an `update` of a
  canvas-capable role, or when new content contains `.canvas.tsx`. Legacy also
  flags `canvas_reconcile_rows` for any updated existing message. A raw
  insert-only mutation that never mentioned Canvas will not enqueue a removal
  pass; if an older canvas candidate is still pending, apply-at-current-revision
  still reconciles.
- **Lexicon corpus:** ingest upserts terms extracted while parsing **this**
  batch; the projector extracts from the **last 200** FTS rows (same bound as
  `content_tsv`). Small fixtures match. A huge FULL vs a later DELTA can
  insert extra older-window terms on the projector path. `content_tsv` itself
  uses the same last-200 recipe on both paths.
- **Duplicate SSE:** ingest may still publish `conversation.search` from
  `search_text` in the main commit; the projector publishes again only if
  Canvas refs or `content_tsv::text` changed. Duplicate namespaces are
  acceptable.
- **NOTIFY before commit** can wake the projector on an empty outbox; poll
  recovers. Same shape as Phase 3 drain.
- **Title in tsv:** projector tokenizes `document.title` as committed by the
  writer. Raw does not run `_apply_friendly_conversation_title`; the match
  test uses a stable filename so both titles are the basename.
- **Alembic** is documentation, not the live migrator.

## Reviewer scrutiny

- Confirm outbox inserts are inside the writer transaction (SQLAlchemy
  `enqueue_projection_candidates` before `ingest_file` returns; asyncpg
  `enqueue_projection_candidates_raw` inside `_apply` before COMMIT).
- Confirm flag off does not enqueue and does not skip legacy Canvas/FTS.
- Confirm collapse-to-current-revision (not “skip apply when no candidate
  hash equals current”).
- Confirm `document_ids=` isolation in projector tests so a shared task DB
  cannot drain leftover outbox rows.
- Do not regenerate `realtime_ingest_parity_golden.json` if SSE-only fields
  differ; live-field comparison already drops `staged_sse_events`.
- Single-instance advisory lock + LISTEN/poll lifecycle vs the drain module.
- Compose/env additions above; no compose file is in this diff.

No commit and no push were performed.
