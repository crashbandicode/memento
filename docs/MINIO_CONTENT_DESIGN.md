# Externalizing document content to MinIO

## Decision and scope

Move the logical value currently stored in `documents.content` to immutable objects in MinIO and keep only a verified object pointer and integrity fields in PostgreSQL. The API continues to return content, not a redirect or presigned URL; object storage is an internal implementation detail.

Although conversation JSONL is the reason for the change, the migration must cover **every non-NULL `documents.content` value**. Dropping the column while leaving memory, plan, discovery, state, or other document categories inline would lose data and change API results. `conversation_messages` remains the primary read model for conversation APIs and is not changed by this project. It is not a lossless substitute for the raw source and must not be used to reconstruct objects.

The recommended consistency model is:

1. Objects are immutable and addressed by the SHA-256 of the exact UTF-8 bytes Memento would previously have stored in `documents.content`.
2. MinIO PUT and verification finish before the PostgreSQL transaction that publishes the pointer commits.
3. A reader trusts an object only after checking its recorded byte length and SHA-256.
4. During rollout, reads prefer MinIO and fall back to PostgreSQL. PostgreSQL content is nulled only after an independent object GET hashes to the same value.
5. Superseded and failed-write objects are deleted only by a delayed, reference-aware garbage collector.

## Problem

Production measurements show that `documents` occupies 8.2 GB of a 14 GB database and that nearly all of that space is TOAST for `documents.content`. The observed window had 4,166 UPDATEs versus 344 INSERTs. An active-session sync can therefore rewrite a compressed multi-megabyte value, producing WAL and dead TOAST churn even though normal conversation reads use parsed `conversation_messages` rows.

MinIO is already deployed. API and Celery processes have `MEMENTO_S3_*` credentials, `server/server/services/large_content_store.py` stores large ingest payloads there, and `server/server/tasks/db_backup.py` writes daily database backups to a separate `memento-backups` bucket. The existing large-content path is only partial: it applies to large full conversation uploads, uses `raw/{user}/{device-hash}/{job}.txt` keys, and several readers still select `documents.content` directly.

The client-visible invariants are:

- Existing endpoints, exports, direct MCP tools, summaries, embeddings, graph extraction, and maintenance scripts receive the same logical text as before.
- Conversation message ordering, parsed content, metadata, and timestamps do not change.
- Raw/reparse consumers receive the exact stored source bytes. Normalized message rows are never used as a raw-content fallback.
- Authorization remains in PostgreSQL/API code. MinIO objects stay private and no new object URL is exposed to clients.

## Object layout

Use the configured content bucket (the current `MEMENTO_S3_BUCKET` may be retained) and reserve this prefix:

```text
document-content/v1/{document_id}/{content_object_sha256}
```

`content_object_sha256` is the SHA-256 of the exact stored UTF-8 bytes, not necessarily `documents.content_hash`. The latter is a collector/source revision and, for conversations using `document_delivery_state`, may represent a later delivery than the last durable full raw snapshot. Sanitization/redaction can also make source revision identity differ from stored bytes.

Store these object metadata fields for diagnostics, but do not treat metadata or ETag as the integrity proof:

- `memento-document-id`
- `memento-content-sha256`
- `memento-content-size`
- `content-type: text/plain; charset=utf-8`

Multipart ETags are not SHA-256 values. Verification must hash a streamed GET.

### PostgreSQL representation

During the expand migration, retain the existing nullable `content_s3_key` and add explicit fields:

- `content_object_sha256 varchar(64) NULL`
- `content_object_size_bytes bigint NULL`
- `content_object_verified_at timestamptz NULL`

Add a check constraint requiring the key, SHA, and size to be either all NULL or all non-NULL, a non-negative size, and a unique index on `content_s3_key`. A NULL key means the logical content is NULL. A logical empty string is a real zero-byte object with the SHA-256 of empty bytes, preserving the existing NULL-versus-empty distinction.

Do not hide these fields in `documents.metadata`; they are storage correctness state. Keep the collector `content_hash` and the existing stored-source proof metadata for their current revision/fencing semantics.

### Single replace versus versioned immutable objects

| Layout | Benefit | Consistency cost | Decision |
|---|---|---|---|
| `.../{document_id}/current`, overwritten on every full sync | Only one visible object per document | PUT-before-commit changes bytes behind the old DB pointer; commit-before-PUT publishes a missing/stale object. Bucket versioning helps only if PostgreSQL also stores and always requests the exact MinIO version ID. Rollback and concurrent retries are harder. | Reject |
| `.../{document_id}/{exact-byte-sha256}`, never overwritten | Retry is idempotent; the DB pointer switch is atomic; an uncommitted PUT is merely an orphan; old content remains available for rollback | Superseded versions consume storage until GC | Use |

Enable MinIO bucket versioning as defense against operator overwrite/delete, but do not use “latest object version” as the application consistency mechanism. The application key is already immutable.

### Superseded-object garbage collection

Do not delete the old object on ingest. A scheduled mark-and-sweep job should:

1. List only `document-content/v1/` keys and compare them with all live `documents.content_s3_key` values.
2. Record the first time an unreferenced key is observed in a small PostgreSQL `document_content_gc_candidates` table. Do not use object `Last-Modified` as the superseded time; an old object may have become unreferenced only moments ago.
3. Remove a candidate when it becomes referenced again.
4. Delete only after at least 30 days unreferenced, after rechecking the live pointer in the deletion transaction. Thirty days exceeds the current 14-day database-backup retention and leaves rollback margin.
5. Coordinate ingest/re-reference and delete with the same advisory lock derived from the document/key. Ingest verifies/recreates the object while holding that lock through pointer commit; GC holds it while rechecking and deleting.

This handles both superseded versions and objects uploaded by transactions that never committed. GC remains disabled through migration and the rollback observation window. With bucket versioning enabled, configure noncurrent-version expiry only after the replicated-backup retention; otherwise a delete marker does not reclaim bytes.

## Write path

### Canonical ingest sequence

Unify inline, multipart, and durable-spool ingestion behind one content-store finalizer. `server/server/api/ingest.py` and `server/server/tasks/ingest_spool.py` should pass a sanitized path/stream (or the exact small in-memory bytes) to `ingest_file`; they should no longer independently create a job-keyed final object.

For each accepted write:

1. Sanitize exactly as today. The object contains exactly the bytes that would have been assigned to `documents.content`; do not normalize newlines, reserialize JSONL, or compress into a different logical byte sequence.
2. Compute SHA-256 and byte count while streaming/inspecting the sanitized payload. For a new document, allocate the UUID in the application; for an existing document, resolve it under the existing source advisory lock and row lock.
3. Perform all stale, idempotent, and supersession checks before uploading. The existing source lock serializes writers for a logical file/session.
4. Parse and stage normalized/message changes in the still-uncommitted PostgreSQL transaction.
5. PUT the immutable final key if it is absent. If it already exists, verify size and hash and reuse it. Never overwrite a key whose bytes do not match its hash.
6. Stream GET the object, calculate SHA-256 and size, and compare them with the local payload. Only then set `content_s3_key`, `content_object_sha256`, `content_object_size_bytes`, and `content_object_verified_at` in the transaction.
7. In compatibility mode, also retain the exact text in `documents.content`. In S3-only write mode, leave it NULL.
8. Commit the document pointer, normalized rows, sync state, delivery state, and other ingest changes together. Publish post-ingest/cache events only after that commit, as today.

The final PUT is deliberately before the pointer commit. MinIO and PostgreSQL cannot form one atomic transaction; this order makes every externally visible DB pointer readable. The only extra state produced by a failed transaction is an unreferenced immutable object, which is safe for delayed GC.

Do not delete the object when a commit call errors or times out: commit outcome can be ambiguous. Re-read the document in a new transaction. A matching revision and pointer means success; otherwise retry the idempotent PUT/transaction and let GC handle any orphan.

### FULL and DELTA behavior

- A FULL write creates/reuses an object for the complete sanitized snapshot and atomically advances the pointer.
- A conversation DELTA keeps the pointer to the last durable FULL snapshot, matching the current ingest behavior, while normalized `conversation_messages` and `document_delivery_state` advance. It must not label the old snapshot with the DELTA revision hash.
- A non-conversation DELTA that currently appends to `documents.content` must construct the same logical old-content + newline + delta value in a bounded spool, write a new immutable object, and advance the pointer. It must not concatenate by loading an unbounded object into an ORM attribute.
- An idempotent same-content retry reuses the same key and does not create another object.

### Failure modes and recovery

| Failure window | Visible state | Required recovery |
|---|---|---|
| Crash before PUT | No DB pointer change; no final object | Existing request/spool retry resumes normally. |
| PUT fails definitively | PostgreSQL transaction is rolled back; old pointer remains | Return/retry as ingest failure. Do not accept a PG-only new revision once S3 writes are required. |
| PUT times out with unknown outcome | DB is still uncommitted; object may exist | HEAD/GET the deterministic key. If exact hash/size match, continue; if absent, retry PUT; if mismatched, raise an integrity alarm and never overwrite. |
| Process crashes after PUT but before DB commit | Old pointer remains; new object is unreferenced | Retry is idempotent. Mark-and-sweep collects it after the grace period. |
| Parse, flush, or constraint failure after PUT | All DB changes roll back; object is unreferenced | Fix/retry source; delayed GC removes the orphan. |
| DB commit fails definitively after verified PUT | Old pointer remains; object is unreferenced | Retry transaction; never compensate with immediate object delete. |
| DB commit outcome is ambiguous / response is lost | Either old state plus orphan or fully committed new pointer | Re-read by document/revision. Return success if pointer matches; otherwise retry. Collector retries remain idempotent. |
| Crash after DB commit | New pointer references an already verified object | No repair. Post-ingest recovery scanners handle optional downstream work as today. |
| Two writers for one source | Existing advisory/row lock admits one writer at a time | The second writer reevaluates stale/idempotent rules after the first commits; immutable keys prevent overwrite. |
| Reader sees missing/corrupt MinIO object during dual-read | PG content still exists | Record a high-severity metric and use PG fallback; enqueue object repair. Never serve corrupt bytes. |
| Reader sees missing/corrupt object after PG content was nulled | Raw content is unavailable, although normalized conversation reads may still work | Fail explicitly (normally 503), alert, and restore the exact object from replicated backup. Do not silently synthesize raw content from messages. |
| GC races with reuse of an old hash | Potential delete of a key about to be referenced | Shared advisory lock plus a final reference recheck; ingest re-verifies/re-PUTs before committing the pointer. |

## Read path and fallback

Create one async document-content abstraction with full-read, bounded-prefix, and line-streaming operations. It accepts the document ID and pointer/integrity fields; callers do not use `doc.content` or `read_large_content` directly.

Use one rollout setting with three states:

- `postgres`: read only `documents.content` (initial compatibility state).
- `prefer_minio`: if a key is present, GET and verify MinIO first; on missing key, GET error, size mismatch, or SHA mismatch, fall back to PostgreSQL if the row still contains content. Emit separate counters for missing pointer, MinIO failure, integrity failure, and successful PG fallback.
- `minio_only`: use only verified object pointers. A NULL key returns logical NULL; an invalid/missing referenced object is an error.

While the column exists, map `Document.content` as deferred so `select(Document)` no longer pulls TOAST implicitly. The fallback helper explicitly selects it only when needed. This also makes missed direct accesses fail in tests instead of quietly causing a large lazy load in async code. Remove the mapped attribute in the contract migration that drops the column.

For a successful MinIO read, always compare streamed byte count and SHA-256 with the explicit DB fields. Prefix and line APIs must still validate the full object when used for a migration verifier or reparse; a bounded operational prefix read can rely on an object previously marked verified and report transport failures.

Conversation APIs continue to use `conversation_messages`. Their legacy “no normalized rows” branches call the content abstraction. Reparse and source-identity tools use the full/line-stream operations and never fall back to parsed rows. API responses retain the same fields and values; no consumer receives a key, bucket name, URL, or storage-specific status.

## Why `document_delivery_state` is not the content mechanism

`server/server/services/document_delivery.py` describes `documents` as the canonical full snapshot and routes conversation DELTA revision, file size, metadata, activity, and sync timestamps into a narrow hot row. `DocumentDeliveryState` has no content bytes, object key, exact object SHA, byte size, verification state, or superseded-object history. `Document._apply_delivery_projection` can also project `revision_hash` over the instance's `content_hash`, even when the raw pointer still represents an older FULL snapshot.

It is therefore a red herring for externalization. It remains useful for its existing purpose—avoiding wide/indexed `documents` updates on hot DELTAs—and its revision must still fence post-ingest work. The canonical raw-content pointer belongs on `documents` (with explicit exact-byte integrity fields), and a conversation DELTA must leave that pointer unchanged until a new FULL snapshot is stored. Adding a key to delivery state would conflate latest delivery with latest restorable full source and would not solve PUT/commit crash windows, fallback, backfill, or GC.

## Migration and rollout

Collection is currently stopped fleet-wide. Keep it stopped through the expand deploy, backfill verification, and first S3-only ingest canary. The implementation remains safe if collection is resumed early, but the stopped window removes avoidable churn.

### Phase 0: preflight and protection

1. Record counts and bytes by category for NULL, empty, inline, and already-externalized rows. Record the current DB size, documents heap/TOAST size, and WAL baseline.
2. Take a fresh database backup and verify it can be restored.
3. Audit MinIO durability. The repository shows `miniodata` as one Docker volume and shows only the database dump job; it contains no evidence of bucket versioning, replication, or an independent MinIO backup. Do not assume the content bucket is backed up.
4. Enable/version the content bucket and establish off-host/independent replication or storage snapshots before any PG content is nulled. Verify API, general Celery, ingest worker, maintenance jobs, and direct MCP mode have least-privilege access.

**Go/no-go:** collection is confirmed stopped; current DB backup restores; the content bucket is provisioned; independent backup/replication has passed a small object restore drill; there is enough MinIO capacity for current content plus rollout versions and enough PostgreSQL free disk for the chosen repack method.

### Phase 1: expand schema and compatible software

1. Add integrity columns, constraints/index, the GC-candidate table, and the read/write modes.
2. Deploy the centralized reader and all site changes below with reads set to `postgres` and writes set to `postgres`.
3. Mark the ORM content column deferred and confirm metadata/list/message endpoints no longer select it.
4. Add the new GC table to the static `db_backup.TABLES` allow-list, but leave GC disabled.

**Go/no-go:** contract tests are unchanged; a grep/SQL-capture test finds no runtime direct access to `Document.content` outside the fallback/migration layer; all services start with old rows.

### Phase 2: dual write and MinIO-first read

1. Set writes to `minio_and_postgres`; a MinIO failure fails/rolls back ingest rather than creating a new PG-only revision.
2. Set reads to `prefer_minio`.
3. With collection still stopped, run synthetic FULL, DELTA, idempotent retry, failed-PUT, and lost-response canaries through direct multipart and durable spool paths.

**Go/no-go:** every canary pointer passes GET hash/size verification; client outputs match the PostgreSQL path; commit-failure injection produces only unreferenced objects; PG fallback and integrity-error metrics are understood and alerting is live.

### Phase 3: background mover

The mover is restartable and processes small batches. It must cover both inline rows and existing legacy `raw/...` objects:

1. Select a candidate's ID, content/pointer, and revision. For inline content, encode the exact PostgreSQL string as UTF-8 and hash it. For a legacy external object, stream it, verify existing stored-source proof where available, and hash/copy those exact bytes to the new key layout.
2. PUT/reuse the immutable key and stream GET it to verify SHA and byte size.
3. Re-lock the document. Re-read and re-hash the current logical content; if it changed, do not move the pointer and retry the new revision. This second check makes the mover online-safe instead of relying only on `updated_at` or collector `content_hash`.
4. Set the new pointer/integrity fields but keep `documents.content` unchanged. Record progress and rejection reason; never skip an unverifiable row silently.

Run an independent verifier over all pointers. For inline rows, compare the GET hash/size with `content.encode('utf-8')`; for pre-existing external-only rows, compare with their stored-source hash/size and reparse proof. Run reparse in comparison mode on a representative set and all previously external-only outliers.

**Go/no-go:** every logical non-NULL/non-empty content value has a verified pointer; every empty string has a verified zero-byte pointer; genuine logical NULLs are explicitly accounted for; mismatch/missing/unverified counts are zero; byte totals and per-category counts reconcile; reparse results are identical.

### Phase 4: null PostgreSQL content

1. Keep `prefer_minio` enabled and observe zero PG fallbacks for a full operational interval and all scheduled jobs (including daily digest/backup and representative maintenance scripts).
2. Take a DB backup plus content manifest and complete an isolated restore verification.
3. Null `documents.content` in small committed batches. Immediately before each batch update, require the row's currently verified key/SHA/size to match a streamed GET; fence against pointer/revision change. Do not null rejected rows.
4. Run `VACUUM (ANALYZE) documents` after the batches so dead heap/TOAST space becomes reusable internally. This ordinary VACUUM does **not** return most of the 8 GB to the filesystem.
5. Switch new writes to `minio_only`. Collection may resume after a canary device completes FULL/DELTA/reparse verification.

**Go/no-go:** no row with logical content lacks a pointer; `count(*) WHERE content IS NOT NULL` is zero; no fallback or integrity errors; backup restore still passes; normal API latency/results and ingest/message counts are unchanged.

### Phase 5: contract schema and physical reclaim

1. Deploy code and direct MCP models with no mapped/reference to `documents.content`; set reads to `minio_only` permanently.
2. Run a final repository grep and query-capture suite, then drop `documents.content` in a short schema migration.
3. Reclaim physical space after the drop:
   - Preferred: run `pg_repack` on `documents` during the stopped/quiet window. It rewrites the heap, TOAST table, and indexes and needs temporary disk roughly comparable to the rewritten relation plus indexes/WAL; check capacity first. Expect a brief lock at the final swap.
   - Fallback: with API/collector writes stopped, run `VACUUM FULL documents`. It takes an exclusive table lock and rewrites the table/TOAST storage. Do not run it as an “online” operation.
   - Regular VACUUM alone is useful cleanup but is not a claim that ~8 GB has been returned to the OS.
4. Run `ANALYZE documents`, verify constraints/indexes and FK counts, compare relation sizes, then re-enable normal traffic. Enable GC only after the rollback window.

**Go/no-go:** schema contains no content column; no deployed binary references it; the reclaimed size is observed in `pg_total_relation_size`/filesystem metrics; smoke tests and restore manifest checks pass.

## Complete content-site inventory and required changes

The inventory below covers literal `Document.content`/`documents.content` accesses, legacy `content_s3_key` branches, raw SQL, and full-entity ORM loads that currently hydrate the content column implicitly.

### Storage and write sites

| Site | Current use | Required change |
|---|---|---|
| `server/server/db/models.py` | Maps inline `content`, partial `content_s3_key`, and delivery projection | Add explicit object integrity fields; defer inline content during compatibility; remove `content` in Phase 5. Keep delivery projection separate from object identity. |
| `server/server/services/large_content_store.py` | Threshold-specific `raw/.../{job}.txt` put/read helpers with size-only PUT verification | Become the central immutable document-content store with full/prefix/line reads, exact SHA/size verification, deterministic keys, and legacy-key copy support. Bucket creation belongs in provisioning, not a request hot path. |
| `server/server/api/ingest.py` | Multipart uploads externalize only large FULL conversations before `ingest_file` | Pass the exact sanitized source into the transaction finalizer; do not publish a job-keyed object independently. Apply the same policy regardless of size/category. |
| `server/server/tasks/ingest_spool.py` | Large FULL conversations are uploaded before the DB transaction; small content remains inline | Pass the assembled sanitized file to the unified finalizer; commit only after final-key verification. Preserve durable retry behavior. |
| `server/server/services/ingest_service.py` | Creates/replaces/appends/clears `doc.content`; reads it for stored-source identity, Claude sidecar/lifecycle reconciliation, non-conversation FTS/embedding inputs | Keep incoming bytes transient, publish the object pointer on FULL/non-conversation append, and use the content reader for an existing sidecar/lifecycle source. Conversation DELTAs retain the last FULL pointer. Build FTS from normalized messages or bounded object content as appropriate. |
| `server/server/services/import_service.py` | Reads `documents.jsonl` content and inserts it inline | Preserve archive format, but allocate the new document UUID, PUT/verify that exact imported text, and insert the pointer. Compute FTS from the imported text already in memory. Import rollback leaves only GC-safe orphans. |

### Server read sites

| Site | Current use | Required change |
|---|---|---|
| `server/server/api/documents.py` | Both document detail and `/raw` return `doc.content` | Authorize using metadata, then hydrate through the reader. Return the same `content` and `content_type` fields. |
| `server/server/api/devices.py` | Parses system discovery JSON from `doc.content` | Fetch/verify through the reader, then run the same JSON parsing and empty fallback. |
| `server/server/api/projects.py` | `include_content`, plan/timeline previews, related artifacts, bootstrap/context export, and curated docs read full or sliced content | Select metadata only, batch/bounded-concurrency hydrate only documents whose output includes content, and apply the same existing slice/truncation after hydration. |
| `server/server/api/conversations.py` | Four legacy no-normalized-row fallbacks parse raw content; related plan artifacts slice `p.content` | Keep normalized-row paths unchanged. Legacy fallbacks and plan hydration call the centralized reader and preserve counts, offsets, prompts, latest-agent line, and truncation. |
| `server/server/api/conversation_exports.py` | Raw parser fallback when normalized message count is zero | Use the full verified reader. Do not reconstruct from normalized rows when the existing branch requires raw parsing. |
| `server/server/api/search.py` | SQL computes a bounded non-conversation snippet from `Document.content` | Keep candidate matching on title/path/`content_tsv`; hydrate only final-page non-conversation results and compute the same bounded snippet in application code. Conversation snippets remain on `conversation_messages`. |
| `server/server/services/export_service.py` | User ZIP embeds every `d.content` in `documents.jsonl` | Hydrate each document through the reader and keep the archive schema/content identical. Stream/batch within the existing export byte budget rather than loading all object bodies in one ORM query. |
| `server/server/tasks/summary_tasks.py` | Summarizes the first 50,000 characters | Use a verified bounded-prefix read and preserve the same character cap. |
| `server/server/tasks/daily_digest.py` | Sends the first 1,000 characters per document | Use bounded-prefix reads with bounded concurrency; output is unchanged. |
| `server/server/tasks/tsvector_backfill.py` | SQL `left(Document.content, ...)` for non-conversations | Continue to use normalized messages for conversations; prefix-read object content for other categories before tokenization. |
| `server/server/services/embedding_service.py` | Uses raw content for non-conversations; conversations fall back to normalized rows when content is empty | Preserve the existing category semantics with the reader. Conversation model input should remain normalized-row based; non-conversation chunking sees the identical text. |
| `server/server/services/graph_service.py` | Uses a raw prefix for non-conversations and as a conversation parse-miss fallback | Prefix-read through the reader; keep normalized conversation rows as the preferred path and the same 4,000-character cap. |

### Maintenance and backfill sites

| Site | Current use | Required change |
|---|---|---|
| `server/server/scripts/reparse_conversations.py` | Reads `content_s3_key` first, otherwise raw SQL `SELECT content`; verifies stored hash/size before staging | Route both old and new layouts through the exact full/line reader. Fence on canonical stored-object SHA/size plus the applicable full-source revision. Remove arbitrary whole-object caps by streaming where the parser permits. Assert staged `line_number`, role, content, metadata, and timestamp output is byte-for-byte/field-for-field identical before cutover. |
| `server/server/scripts/backfill_conversation_source_identity.py` | Locks rows and reads inline content or a legacy object | Use the verified reader without holding a row lock across a long network GET; re-lock and fence before applying proof metadata. |
| `server/server/scripts/backfill_conversation_token_usage.py` | Selects inline content plus key and scans either `StringIO` or MinIO lines | Select pointer/integrity fields only and always use the line-stream reader, retaining the revision fence before metadata/event writes. |
| `server/server/scripts/backfill_conversation_presentation.py` | Mixes SQL length/left on inline content with prefix/line reads for external content | Remove inline/external branches; use prefix/line readers. Its “has inline embedding content” distinction becomes category/normalized-message logic, not storage location. |
| `server/server/scripts/backfill_subagent_lifecycle.py` | Reads Claude state sidecars and child transcript content from full `Document` entities | Hydrate only the state/child rows that need lifecycle evidence through the reader; preserve the current metadata write fences. |

### Direct MCP sites

| Site | Current use | Required change |
|---|---|---|
| `mcp_server/mcp_server/db.py` | Separate read-only `Document` mapping includes `content` but not the existing S3 key | Map pointer/integrity fields during rollout, defer/remove `content` on the same schedule as the server model, and configure direct mode with read-only MinIO credentials. Remote mode needs no storage exposure. |
| `mcp_server/mcp_server/server.py` | Direct `memory_recall`, `memory_open`, and legacy conversation branches select/read content | Use the same verified content-reader contract in direct mode; keep remote API mode unchanged. |
| `mcp_server/mcp_server/search.py` | Direct full-text search filters with `Document.content ILIKE` and returns a raw snippet | Use `content_tsv`/existing embedding chunks to select candidates, then hydrate only final non-conversation snippets. Conversation search stays on normalized/embedding content. |
| `mcp_server/mcp_server/graph.py` | Direct project context reads memory/plan/identity content | Hydrate the selected bounded set through the direct-mode reader and preserve the 1,000-character output cap. |

### Implicit full-entity loads and non-runtime references

`select(Document)`, `select(..., Document)`, and `db.get(Document, ...)` currently include the mapped content column even when code never reads it. Model-level deferral prevents that globally. Convert hot/bulk queries to explicit columns or `load_only` as practical, especially in:

- `server/server/api/control.py`, `daily.py`, `pins.py`, and `tools.py`
- the metadata-only branches of `server/server/api/conversations.py` and `projects.py`
- `server/server/services/canvas_artifact_store.py`, `conversation_read_model.py`, `conversation_tasks.py`, `dashboard_projection.py`, `orchestration_events.py`, and `thread_metadata_service.py`
- `server/server/tasks/embedding_retry.py` and `knowledge_retry.py`

`thread_metadata_service.py` is not a semantic raw-content consumer: its full-entity selects currently hydrate content incidentally while it updates titles/metadata and search projections. `canvas_artifact_store.py` similarly selects `(ConversationMessage, Document)` in the serialized-reference repair but uses message content and document identity. Both should become metadata-only loads; neither should fetch MinIO content.

Update direct-content tests in `test_graph_service.py`, `test_ingest_ordering.py`, `test_ingest_spool_streaming.py`, `test_ingest_multipart_streaming.py`, `test_large_content_store.py`, `test_task_projection_integration.py`, `test_reparse_conversations.py`, and `test_streaming_ingest_integration.py` to assert pointer/object behavior. Update SQL-shape assertions in `test_browse_activity_api.py`, `test_conversations_normalized_api.py`, `test_projects_device_scope.py`, and `test_tasks_api.py` to require that normal queries do not reference the dropped column. Comments in `server/server/main.py` and `server/server/db/online_migrations.py` about the retired content trigram index should be revised when the column is removed; they are not runtime readers.

## Backups and restore

After migration, `server/server/tasks/db_backup.py` will dump document pointers and integrity fields, not content bytes. The current job uploads DB data to `memento-backups` on the same MinIO service whose data lives in the `miniodata` Docker volume. That protects against PostgreSQL-volume loss but is not evidence of a backup of MinIO itself, and a MinIO-volume/host loss would take both content and the in-cluster DB backups.

Required backup design:

1. Enable versioning on the content bucket.
2. Continuously replicate the content bucket and `memento-backups` to an independent host/account/storage system, or take independently retained storage snapshots. A second bucket on the same MinIO volume is not a backup.
3. For each daily DB backup, write a companion manifest from the same database snapshot containing document ID, key, exact SHA, and size for every live pointer. Add new content/GC tables to the backup table allow-list.
4. Mark the daily backup successful only after every manifest key exists, its metadata/size is plausible, and the backup system reports it replicated. Run sampled streamed SHA verification daily and a full verifier on a slower cadence.
5. Restore by restoring PostgreSQL, restoring/replicating the named object versions, checking the entire manifest, and only then opening API/ingest traffic.
6. Keep unreferenced application objects at least 30 days, longer than the 14-day DB backup retention. Align object-version lifecycle with the longest restorable DB backup so GC cannot delete content needed by a retained snapshot.

The PUT-before-commit invariant means a consistent DB snapshot cannot legitimately reference an object that had not completed verification. The manifest and independent replication cover later loss or operator deletion.

## Test plan

1. **Content-store unit tests:** deterministic keys; Unicode and zero-byte exactness; multipart-size handling; existing-key reuse; hash/size mismatch rejection; prefix/full/line reads; legacy-key copy.
2. **Read-mode matrix:** no key, valid key, missing key, transport error, wrong size, wrong hash, inline fallback present/absent, and logical NULL/empty. Verify metrics and identical API payloads in `postgres` and `prefer_minio` modes.
3. **Write crash injection:** inject every failure row in the failure-mode table around PUT, verification, flush, and commit. Assert the DB never publishes an unreadable pointer and retries converge on one key.
4. **Ingest semantics:** direct JSON, multipart, and durable spool; new/existing FULL; conversation and non-conversation DELTA; stale/superseded input; idempotent retry; same-source concurrency; sanitization/redaction. Compare documents, delivery state, messages, sync state, and events with the pre-change path.
5. **Consumer contracts:** document detail/raw, project include-content/timeline/context/bootstrap, related plans, legacy conversation count/messages/latest/prompts/export, search snippets, discovery, user ZIP export/import, direct and remote MCP, summaries, digest, FTS, embeddings, and graph extraction.
6. **Reparse fidelity:** for each supported tool and size class, run the current parser on PG text and the new parser on the object bytes. Compare exact normalized row count and every `line_number`, `message_type`, role, content, metadata, timestamp, stored-source SHA, and size. Include legacy external-only rows and transcripts larger than the old whole-read limit.
7. **Mover safety:** interruption/restart, parallel workers, already-moved rows, legacy objects, a row changed between upload and pointer update, and an unverifiable row. Only the last must remain non-null and block the gate.
8. **GC:** failed-transaction orphan, superseded object, candidate becoming referenced again, advisory-lock race, 30-day boundary, and backup-retention hold.
9. **Operational verification:** compare endpoint response fixtures, ingest latency, MinIO error rate, WAL bytes per sync, dead TOAST, DB relation size, PG fallback count, and object integrity failures. The client-result diff must be empty.
10. **Backup drill:** restore a DB snapshot and content replica into an isolated environment, verify the full manifest, run representative raw reads and reparses, then document recovery time.

## Rollback plan

Before any PostgreSQL content is nulled, rollback is a flag change: stop S3-required writes, switch reads to `postgres`, and deploy the previous-compatible application. Leave uploaded immutable objects in place; GC is disabled.

After some rows are nulled but before the column is dropped, do not switch to PG-only reads immediately. First stream each referenced object, verify SHA/size, rehydrate `documents.content` in small fenced batches, confirm every logical content value is restored, and then change the read flag. This is also a useful rollback drill before Phase 5.

After the column is dropped, rollback requires an expand migration that re-adds nullable `content`, followed by verified rehydration from objects and only then a PG-only application deploy. A database backup by itself is no longer sufficient; the matching content manifest/replica is required.

At every stage, an integrity mismatch or missing object stops nulling/drop/repack. Do not roll back by reconstructing raw transcripts from `conversation_messages`, and do not delete new objects as compensation for an ambiguous commit. Keep collection stopped again if rollback touches schema or bulk rehydration, and retain all versions until the rollback and restore checks pass.
