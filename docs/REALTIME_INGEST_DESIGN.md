# High-volume real-time transcript ingestion

## Decision

Memento should replace the conversation ingest hot path with a **durable,
coalescing, single-writer pipeline whose PostgreSQL transaction uses plain data
and raw `asyncpg` throughout**. The existing shared, fsynced ingest spool and
serialized ingest worker are the right starting point, but today they cover
mainly chunked/deferred work and the worker still calls the ORM-heavy
`ingest_file` implementation.

The target is:

1. Admit every guarded conversation DELTA into the shared durable spool and
   return an *accepted* receipt quickly. The collector retains its local queue
   item until it observes a *committed* receipt.
2. Let one long-lived ingest writer wait for a short per-source quiet window,
   coalesce only a contiguous DELTA chain, and make one commit for the chain.
3. Normalize on the server into plain dictionaries/dataclasses.
4. Load scalar state with `asyncpg`, stage message mutations with
   `copy_records_to_table` (or `executemany` for a genuinely small batch), and
   update delivery, sync, read-model, task, prompt, and dashboard rows with
   explicit SQL. No mapped instance or SQLAlchemy session participates in this
   transaction.
5. Publish one user-scoped SSE invalidation after the coalesced commit.

This is a justified rewrite of the ingest subsystem. Merely changing
`db.add_all()` to one bulk insert while leaving document, tail, projection, and
dashboard ORM hydration in place will not reach the requested cost reduction.

The raw-source and delivery invariants are not part of the rewrite. In
particular, a FULL still follows `docs/MINIO_CONTENT_DESIGN.md`: parse and stage,
PUT the immutable content object, verify it with a streamed GET, then commit its
pointer together with normalized rows and delivery state. A conversation DELTA
continues to advance `document_delivery_state` while retaining the most recent
verified FULL object pointer.

## Scope and non-negotiable invariants

The optimized path is the append-heavy conversation JSONL path. Other document
categories and unusual FULL/rebase operations can continue through the current
path until migrated deliberately; they do not need to block the high-volume
DELTA cutover.

The following must remain true:

- The server is authoritative for source identity, authorization, semantic
  normalization, and document ownership.
- Writers for one logical source are serialized with the same source advisory
  lock domain used today.
- A DELTA is accepted for commit only when its `base_hash` and `base_offset`
  exactly match committed delivery/sync state. A coalesced chain must be exact
  at every adjacent hash/offset link.
- Retries are at-least-once and idempotent. A lost response or ambiguous commit
  is resolved by rereading committed delivery/sync state, not by assuming
  failure.
- The collector advances its committed local base only after a database commit
  receipt, never after a queue-admission response.
- A FULL content pointer is never visible until the immutable MinIO object has
  passed exact SHA-256 and byte-length verification. Failed or ambiguous writes
  leave only safe, delayed-GC orphans.
- Normalized messages, delivery state, sync state, correctness-critical read
  projections, and the revision fence become visible in one PostgreSQL commit.
- SSE is published only after that commit and is scoped to the authenticated
  owner. `user_id` is derived from collector authentication/machine ownership,
  never trusted from the frame.
- A conversation page receives a visible update within roughly 2–3 seconds.

## Current per-sync cost model

### Measured CPU evidence

The production sample identifies an unusually clear target:

| Profile frame | Sample share | What the current path is doing |
|---|---:|---|
| SQLAlchemy mapped `__init__` | 19.0% | Constructing `ConversationMessage` and other mapped objects rather than plain rows. |
| SQLAlchemy `run` | 9.0% | Async/greenlet execution and SQLAlchemy result/statement machinery; some remains with Core, little remains with raw `asyncpg`. |
| descriptor `__get__` | 8.3% | Reading mapped attributes during comparison, reducers, projection building, and change tracking. |
| `_raw_all_rows` | 7.5% | Materializing query results that are commonly turned into ORM instances. |
| `_organize_states_for_save` | 2.9% | Unit-of-work bookkeeping before flush. |
| `_populate_full` / `_instance` | about 5% | Hydrating ORM state from rows, including rows just written and reread for projections. |
| `_extract_messages` | 2.8% | The visible self-cost of server semantic extraction. Downstream calls made by it appear in their own frames. |

Excluding the ambiguous 9% `run` bucket, **42.7% of all sampled API CPU is
directly attributable to ORM construction, hydration, descriptors, and
unit-of-work bookkeeping**. Including the portion of `run` that raw `asyncpg`
removes puts the immediately addressable share near 43–52%. These sample shares
are directional rather than a claim that every reported stack bucket is
strictly additive.

The 2.8% `_extract_messages` frame is also important evidence against moving
normalization to collectors as the first optimization: the measured parser
self-cost is small compared with object construction and database/result
machinery.

### Stage-by-stage behavior

| Stage | Current work per sync | Scaling problem |
|---|---|---|
| HTTP admission | FastAPI/Pydantic request, collector auth, device lookup, body decode. HTTP/2 already removes repeated connection setup. | One request per sync, but request dispatch is not prominent in the profile. |
| Source fence and lookup | Transaction advisory lock; `SyncState`, path document, optional session-identity documents, and delivery-state reads; mapped-row hydration and row locks. | Fixed ORM/result cost repeats even for a tiny delta. |
| Semantic parse | `orjson` JSONL decode plus tool-specific stateful normalization, tail/mirror reconciliation, usage extraction, hierarchy/lifecycle handling, and search text collection. | Mostly proportional to genuinely new source records and therefore unavoidable. |
| Message staging | Construct one `ConversationMessage` instance per row, `add_all`, flush in batches of 100, and sometimes hydrate/update recent rows. | The 19% constructor and 2.9% unit-of-work samples grow with messages; repeated flushes add PostgreSQL and Python overhead. |
| Canvas projection | After each message batch flush, query existing references and reconcile mapped reference rows. | Adds a query/flush cycle even though almost every transcript batch contains no Canvas reference. |
| Read-model/task/prompt projection | Reselect normalized rows as ORM instances, fold them into `ConversationReadModel`, update prompt/task projections, then flush. | Rows already present in Python are read back and hydrated. Fixed projection work repeats per filesystem sync. |
| Activity/title/search/dashboard | Query activity/title inputs, sometimes reread recent message text for FTS, upsert search terms, load the read model and dashboard row, compare mapped attributes, and update the projection. | Produces both API and PostgreSQL bursts in the same sync wave. |
| Raw content finalization | On FULL, PUT/reuse and streamed-GET verify the immutable MinIO object before publishing its pointer. DELTAs retain the last FULL pointer. | Required integrity work. It is not the high-frequency DELTA bottleneck and must not be weakened. |
| Delivery, sync, cache, SSE | Update delivery/sync/project state, stage cache invalidations, commit, then publish an SSE event through the user Redis stream. | Correct shape, but it is performed once for every admitted delta rather than once per visible refresh interval. |

The current collector already does two useful kinds of coalescing: a 0.3-second
filesystem-event debounce and replacement of one not-yet-uploaded pending tail
from its earliest base. Those mechanisms remove duplicate local events, but
they cannot merge revisions that have already become in-flight, arrivals from
different collector processes, or several committed rapid deltas on the same
server document.

## Option verdicts

| Option | Verdict | Reason |
|---|---|---|
| 1. Remove the ORM from the ingest write path | **DO** | It attacks at least 42.7% of measured samples directly and also permits set-based projection updates and fewer round trips. SQLAlchemy Core is a safe transition, but raw `asyncpg` is the recommended final writer because Core retains some `run`, compilation, and result machinery. |
| 2. Server-side coalescing | **DO** | It amortizes source lookup, locking, projection, commit, and SSE over several frames and smooths PostgreSQL bursts. Use the durable spool/writer, not a volatile process-local buffer. Its benefit depends on actual same-source inter-arrival times; it gives no CPU win when frames are farther apart than the window. |
| 3. Collector-side normalized rows | **DON'T BOTHER** | The collector JSONL parser currently validates/decodes lines and extracts light metadata; it does not own the server's stateful semantic normalization. Moving that logic saves the small measured extraction self-cost while creating a trust boundary, duplicated parser releases, old-collector skew, and cross-record/tail-state problems. FULL raw bytes are still required. |
| 4. Dedicated lightweight ingest worker | **DO** | This is the correct place to coalesce and to isolate/smooth writes. Extend the existing shared spool and serialized worker concept, but make it a long-lived drain loop using the new raw writer. Do not start by inventing a custom indefinite NDJSON stream: HTTP/2 is already long-lived at the connection layer, and FastAPI request handling is not the measured bottleneck. |

## Recommended target architecture

### Data flow

```text
 AI tool JSONL append
        |
        v
 collector watcher
 (0.3 s debounce, sanitize, existing durable local SyncQueue)
        |
        | one HTTP/2 frame; collector keeps item until COMMITTED
        v
 thin ingest admission endpoint
 - authenticate collector and bind owner/machine
 - validate bounded envelope and payload
 - compute payload SHA / deterministic delivery identity
 - fsync payload + manifest to shared ingest spool
        |
        | ACCEPTED receipt (not a committed base)
        v
 per-source ready queue ------------------------------+
        |                                               |
        | 1.25 s quiet window, 2.0 s hard deadline      | crash/restart scan
        v                                               |
 long-lived single ingest writer <---------------------+
 - recover FIFO source head
 - verify contiguous hash/offset chain
 - acquire PostgreSQL source advisory lock
 - load scalar writer/tail/projection state
 - normalize server-side into plain rows
 - COPY/apply message mutations and direct projection SQL
 - FULL only: immutable PUT + streamed GET verification
 - atomically update delivery + sync + receipt state
        |
        +-----------------------+-----------------------+
        |                       |                       |
        v                       v                       v
 PostgreSQL commit       COMMITTED receipts      one post-commit SSE
 messages + fences       for every input frame   Redis Stream, owner-scoped
 + live projections                              -> conversation refetch
        |
        v
 durable deferred projector
 Canvas/search work only when candidate rows require it
```

### Admission and receipt protocol

Each frame carries the existing source fields plus a server-computed payload
proof:

- authenticated owner and machine;
- tool, category, relative path, and stable session/source identity;
- mode, revision hash, logical offset, base hash, and base offset;
- source timestamp and bounded collector metadata;
- exact payload byte length and server-computed SHA-256.

A deterministic delivery identity should bind all of the above source/revision
fields and the payload SHA. A retry of the same frame returns the same accepted
or committed status. The same source/revision envelope with different payload
bytes is an integrity error, not a last-write-wins update.

The admission endpoint writes the payload, manifest, and ready marker using the
existing spool's fsync/atomic-rename discipline. Redis/Celery may wake the
writer, but Redis does not need to hold transcript bodies and a lost wake-up is
recovered by scanning ready markers. This avoids duplicating large deltas in
Redis memory and preserves the current durable-spool operational model.

`ACCEPTED` means only that the server can retry the work. The existing
collector `SyncQueue` continues to own the source bytes and must not advance
`synced_hash/synced_offset` until it receives `COMMITTED`, `IDEMPOTENT`, or a
defined stale/superseded terminal result. This makes a lost server spool entry
recoverable from the collector and keeps queue admission out of the delivery
fence.

There is one necessary collector interaction: today the watcher deliberately
refuses to capture another same-source DELTA while one revision is pending or
uploading. If that rule remains unchanged, a server quiet window can coalesce
only cross-collector duplicates or an offline server-spool backlog. Under a
negotiated asynchronous-ingest capability, the collector may instead build a
**bounded speculative accepted chain**: an admitted successor uses the prior
`ACCEPTED` frame's hash/offset as its base, while the collector retains every
constituent payload and keeps the durable committed cursor unchanged. The
server validates and commits the chain atomically. If its head fails, is lost,
or conflicts, the collector discards the speculative chain and follows the
existing FULL-rebase recovery. This separation between `accepted` and
`committed` cursors is required for same-collector live coalescing; treating an
accepted cursor as committed is forbidden.

### Coalescing policy

Use a **1.25-second trailing quiet window with a 2.0-second hard deadline from
the first ready frame**. This leaves normal parse/commit/SSE time inside the
2–3 second freshness target and prevents continuous activity from postponing a
visible update indefinitely. Flush earlier at a bounded frame/byte limit.

Frames coalesce only when all of these hold:

- same authenticated owner, machine, tool, and stable logical source;
- every frame is a DELTA;
- `next.base_hash == previous.hash` and
  `next.base_offset == previous.offset` for every link;
- no failed/quarantined source revision forms a barrier;
- frame-specific metadata can be applied in order without loss.

Keep the ordered envelopes while combining the payload work. Plain append
JSONL can be concatenated exactly. A tool-specific projection frame whose
ordering hint is not safely composable is a barrier or is reduced sequentially
inside the same transaction; it must not be flattened by keeping only the last
metadata object. FULL/rebase is a barrier and retains the existing authoritative
snapshot/supersession rules.

The 0.3-second client debounce should remain. Raising it to 2–3 seconds would
delay capture on every collector and still would not coalesce cross-collector
or already-admitted revisions. The capability-gated speculative chain changes
the current one-uncommitted-revision rule, not the filesystem debounce; old
collectors retain the current synchronous behavior.

### Plain-data normalizer and writer contract

Refactor the current semantic behavior into two interfaces with no mapped
objects:

```text
WriterState + ordered source frames
    -> IngestMutation

IngestMutation:
    document scalar changes
    delivery/sync final revision and offset
    message inserts, updates, deletes/line shifts
    usage-event upserts
    read-model/task/prompt accumulator result
    dashboard value/change flags
    candidate Canvas/search work
    union of SSE change namespaces
```

`WriterState` is assembled from scalar `asyncpg.Record` values: canonical
document identity, current delivery/sync fence, the bounded recent tail,
read-model/task/dashboard rows, and only the tool-specific state needed by the
normalizer. The existing Codex mirror-pair, Claude queued-user/lifecycle, Cursor
stable-source update, usage, pending-interaction, and recovered-history rules
remain server rules; they change representation, not semantics.

The transaction performs:

1. `BEGIN` and the existing source advisory transaction lock.
2. Load current scalar document/delivery/sync and bounded tail/projection state.
3. Re-evaluate idempotent, stale, superseded, and exact-base decisions under
   the lock.
4. Normalize the accepted chain into one `IngestMutation`.
5. COPY message mutations into a connection-local temporary stage table. Give
   every staged row an ordinal and operation. Apply line shifts/deletes,
   updates, and inserts with explicit set-based statements. Return
   `(id, line_number, ordinal)` for rows whose generated IDs feed prompt or
   interaction projections.
6. Upsert usage events and correctness-critical projections from the mutation,
   without selecting inserted messages back into Python.
7. For FULL only, run the unchanged MinIO finalizer and set the verified pointer
   fields only after streamed-GET verification.
8. Update delivery state, sync state, document fields that legitimately change,
   dashboard projection, and completion status with explicit revision predicates.
9. Commit. Only then remove/complete spool jobs and publish one SSE event.

For small batches, `asyncpg.executemany` may be cheaper than setting up COPY;
for normal/high-volume batches, `copy_records_to_table` into a reusable
connection-local temporary table is the target. This is an implementation
threshold to benchmark, not a semantic branch.

Do not use `ON CONFLICT DO NOTHING` as a general substitute for correct
fencing. The unique `(document_id, line_number)` constraint remains an error
detector. Idempotency is decided from delivery/sync state and stable source
identity before applying the mutation; only explicitly modeled source-ID
upserts may use conflict handling.

### Projection and SSE placement

All projection work moves **behind the coalescing window**. At drain time it is
split by whether a reader must observe it atomically with the new transcript:

| Work | Placement | Reason |
|---|---|---|
| Normalized messages | Same coalesced transaction | They are the live conversation source of truth. |
| Delivery and sync state | Same coalesced transaction | They are the retry/idempotency fence. |
| Conversation read-model accumulator | Same coalesced transaction, folded from staged rows | Pending interactions, latest assistant, hierarchy/runtime, and counts must agree with the message revision when SSE causes a refetch. Do not reread/hydrate the inserted rows. |
| Prompt and task projections | Same coalesced transaction, direct set/upsert SQL | They are visible conversation state and already derive from the staged rows. |
| Activity/title changes | Same coalesced transaction | They affect visible ordering and metadata. Calculate them from staged rows/state. |
| Dashboard projection | Once per coalesced transaction | It is one narrow replacement/upsert. Preserve the current selective dashboard-event rules, including the message-count bucket, but do not refresh per input frame. |
| SSE/cache invalidation | One unioned event after commit | One document refetch exposes the entire coalesced revision. Preserve owner scoping and existing resumable Redis Stream behavior. |
| Canvas reconcile | Durable post-commit projector, only for staged messages that can contain `.canvas.tsx` | It is not required for transcript ordering or the delivery fence. Enqueue its candidate identity in the commit, revision-fence the projector, and publish a Canvas-specific change only if its output changes. |
| Conversation FTS/search lexicon | Post-commit and revision-fenced unless a product requirement makes search freshness synchronous | Search lag does not block the 2–3 second conversation live view. Keep the bounded input and coalesce it by document revision. |
| Embeddings/knowledge | Existing quiet-window path | Already asynchronous and coalesced; do not bring it back into real-time ingest. |

If moving Canvas/search behind a durable projector is too large for the first
release, keeping them in the coalesced transaction is still a net improvement.
They must at least run once per drain, never once per constituent frame or once
per 100-row ORM flush.

### Failure and recovery behavior

- **Crash before ready marker:** no accepted receipt; collector retries.
- **Crash after admission but before writer claim:** fsynced ready job is found
  by recovery scan.
- **Crash during parse/transaction:** PostgreSQL rolls back; spool jobs remain.
- **FULL MinIO PUT/GET failure:** transaction rolls back and the old pointer
  remains. Retry uses the deterministic immutable key.
- **Crash after verified PUT but before commit:** old pointer remains; object is
  an orphan eligible only for delayed reference-aware GC.
- **Ambiguous PostgreSQL commit:** reopen a connection and compare final
  delivery/sync revision and offset. Matching state is success; otherwise retry.
- **Crash after commit before spool completion/SSE:** the recovered writer sees
  the final revision as idempotent, completes every constituent receipt, and
  may safely republish the invalidation. Duplicate invalidations cause a
  refetch and are acceptable; publishing before commit is not.
- **Base mismatch:** commit nothing from the invalid chain, report the exact
  current hash/offset, and use the collector's existing authoritative FULL
  recovery.
- **Spool pressure:** reject admission before writing when byte/free-space
  limits are reached. The collector retains and retries its durable item.

## Expected cost and capacity

### Cost formula

Let `F` be admitted frames for one document in a drain and `M` the normalized
message rows produced by them.

Current work is approximately:

```text
F * (request + source lookup/lock + ORM state + projections + commit + SSE)
+ M * (normalize + mapped-row construction/change tracking/index work)
```

The target is:

```text
F * (thin admission + spool append)
+ 1 * (source lookup/lock + projection upserts + commit + SSE)
+ M * (normalize + plain reducer + COPY/set-based PostgreSQL work)
```

The JSON decode and semantic normalization remain proportional to real new
records. Nearly every other fixed cost becomes once per visible refresh, and
the Python cost of persisting each row loses mapped construction, descriptors,
hydration, and unit-of-work bookkeeping.

### Planning estimates

These are rollout targets to verify under the production fixture, not promises
derived from a synthetic microbenchmark:

| Configuration | Expected application CPU per original sync-equivalent | Interpretation |
|---|---:|---|
| End-to-end raw writer, no coalescing (`F=1`) | 0.20–0.35 of current | About 3–5× cheaper. It removes the measured 43–52% SQLAlchemy share plus ORM-driven rereads/flushes, but still parses and commits once. |
| Raw writer, two frames per drain | 0.12–0.22 of current | About 5–8× cheaper; fixed transaction/projection/SSE work is shared. |
| Raw writer, three to five frames per drain | 0.07–0.15 of current | About 7–14× cheaper and the intended order-of-magnitude operating region. |
| Message persistence component alone | 0.05–0.20 of current ORM row CPU | COPY/set-based SQL avoids Python objects, though PostgreSQL still maintains constraints, FKs, and indexes. |

Coalescing gain must be measured from a per-source arrival histogram. If the
37 current syncs/minute are evenly separated by more than two seconds for each
document, the window will not provide a large factor; the raw writer must carry
the load by itself. If the observed traffic is wave-shaped, as the CPU/PG
bursts suggest, a mean of two to four frames per drain is plausible but must be
demonstrated in canary metrics.

At 150 syncs/minute the arrival rate is only 2.5 frames/second. Thousands of
messages/minute are tens of rows/second, a small COPY workload. The under-one-
core goal is credible if the canary shows total cost at or below roughly 0.20
of the current per-sync cost (because the requested frame rate is about four
times today's 37/minute). The release gate should use measured CPU-seconds per
1,000 admitted source records and per 1,000 committed normalized messages,
not peak container percentage alone.

Target database/client shape for an ordinary coalesced DELTA is a bounded
number of round trips independent of `M`: lock/state load, bounded tail load,
one stage transfer/application, a small projection/delivery statement group,
and commit. PostgreSQL index/WAL bytes per normalized message should not rise
relative to today, while commits and projection updates per admitted frame
should fall approximately with the observed coalescing factor.

## Safe migration path

Each phase is independently deployable and retains a rollback to the previous
writer for collectors that have not negotiated the new behavior.

### Phase 0 — establish semantic and performance gates

- Record admitted frames, committed drains, frames per drain, source-specific
  inter-arrival time, normalized rows, CPU-seconds, PostgreSQL statements/WAL,
  spool lag, and commit-to-SSE latency.
- Build a golden comparison from current ingest outputs: messages excluding
  generated row IDs, usage events, delivery/sync state, read model, prompt/task
  projections, dashboard values, and SSE change namespaces.
- Include current tool-specific FULL, DELTA, idempotent retry, stale retry,
  base mismatch, and authoritative rebase fixtures.

**Ship gate:** measurement overhead is bounded and the comparison can identify
field-level drift. No behavior changes yet.

### Phase 1 — plain message staging with SQLAlchemy Core

- Make semantic extraction return plain row/mutation values.
- Replace `ConversationMessage(...)`, `add_all`, and per-100-row ORM flushes
  with a Core bulk insert on the current transaction/connection.
- Feed the current projection code through a compatibility adapter if needed;
  do not change transport or acknowledgement semantics yet.

**Expected independent win:** remove the 19% mapped-constructor and 2.9%
unit-of-work hot frames for new messages and reduce flush count.

**Ship gate:** golden output is identical, and no regression exists in
line-number uniqueness, stable-source updates, pins, usage, or FULL rebuilds.

### Phase 2 — end-to-end raw writer behind the synchronous endpoint

- Introduce `WriterState -> IngestMutation` pure reducers.
- Implement the `asyncpg` transaction, temporary staging/COPY, explicit
  delivery/sync/document updates, and direct correctness-critical projection
  upserts.
- Keep the current endpoint waiting for commit and publish SSE exactly as today.
- Canary by owner/device/tool; on an unhandled mutation shape, fail safely and
  retry through the old path only before any new-path commit.
- Exercise FULL through the same raw transaction while calling the unchanged
  verified MinIO finalizer.

**Expected independent win:** eliminate the remaining ORM hydration,
descriptors, unit of work, and most SQLAlchemy `run` cost without requiring a
collector release.

**Ship gate:** field-level shadow/golden diff is empty, ambiguous-commit tests
converge, MinIO failure injection never publishes an unreadable pointer, and
the `F=1` cost is at most 0.35 of current.

### Phase 3 — asynchronous admission and long-lived coalescing writer

- Route guarded conversation DELTAs to the existing shared fsynced spool even
  when they are smaller than the chunk threshold.
- Generalize the existing completion-receipt/status flow so the collector
  distinguishes `ACCEPTED` from `COMMITTED` and retains its local payload/base
  until commit.
- Under the negotiated capability, add a separate bounded accepted cursor so
  the watcher can capture successors behind an admitted frame without
  advancing the durable committed cursor. Retain the whole speculative chain
  until its atomic commit receipt; on head failure/conflict, discard it and use
  the existing FULL recovery.
- Replace one-Celery-task-per-job finalization for this path with a long-lived
  single drain process. Celery/Redis may wake it, but ready-marker recovery is
  authoritative. This avoids routing high-volume small frames through the
  current `--max-tasks-per-child=1` worker lifecycle.
- Add the 1.25-second quiet/2.0-second maximum coalescing policy and complete a
  receipt for every constituent frame after the one commit.
- Retain the synchronous raw writer as a feature-flag fallback during rollout.

**Expected independent win:** smooth API/PostgreSQL waves and amortize fixed
work. FastAPI becomes authentication, validation, and bounded spool I/O rather
than a database writer.

**Ship gate:** p95 admission-to-SSE is below 2.5 seconds under target load,
spool lag is bounded, no collector advances on `ACCEPTED`, and average total
cost is at most 0.20 of current at 150+ admitted frames/minute.

### Phase 4 — defer sparse, non-live projections

- Move Canvas reconciliation and conversation search/lexicon refresh to a
  durable revision-fenced projector.
- Enqueue only real candidates from the coalesced mutation; collapse projector
  work to the latest document revision.
- Keep messages, read model, prompts/tasks, dashboard, delivery, and sync in the
  main commit.

**Ship gate:** conversation freshness is unchanged, Canvas/search eventually
match the golden output, and projector restart/replay is idempotent.

### Phase 5 — retire the old conversation DELTA path

- Make durable spool admission the only admission path for
  capability-negotiated conversation DELTAs. The drain fallback bridge is
  load-bearing by design: it attempts raw ingest, then applies an unsupported
  frame chain through the legacy reducer.
- The retirement gate is a full operational interval (at least a 60-minute
  steady-state soak plus a restart/recovery drill) in which raw-writer
  fallbacks are limited to the explicit legacy-forever set: Claude subagent
  transcript/sidecar pairing, Cursor projection reordering, stable-identity
  relocation/alias, and delta-without-committed-base. Each shape must remain
  below 1 fallback/minute and below 2% of drained frames. See the
  [unsupported-shapes report §5](raw-writer-unsupported-shapes-report.md#5-recommended-phase-5-gate-definition).
- Phase 5 hard-requires
  `MEMENTO_REALTIME_INGEST_DEFERRED_PROJECTIONS=true` and a running projector.
  Turning the spool flag off does not restore synchronous projections; see the
  [deferred-projections one-way door](realtime-ingest-phase45-handoff.md#rollback-constraint).
- Keep FULL and non-conversation migration explicit; do not delete the old
  implementation merely because DELTA is stable.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Semantic drift while replacing ORM mutation code | Extract pure reducers before transport changes; golden-diff every normalized/projection field; canary by tool because Codex, Claude, and Cursor tail rules differ. |
| Generated message IDs are needed by prompt/interaction/Canvas references | Give staged rows stable ordinals and use `INSERT ... RETURNING` to map ordinal/line to ID. Do not reread full message objects. |
| Coalescing hides a base discontinuity | Validate every adjacent hash/offset link and revalidate the first base under the PostgreSQL source lock. A discontinuity is a barrier, not a best-effort merge. |
| Continuous activity violates freshness | Hard-flush at 2.0 seconds regardless of new arrivals; separately bound frames and bytes. |
| Volatile queue acknowledgment loses data | Fsync before `ACCEPTED`; collector retains the local item until committed receipt; ready-marker recovery does not depend on Redis. |
| Single writer falls behind | Observe oldest-ready age and drain CPU. If target load exceeds one writer, partition sources by a stable hash across a small fixed writer count; keep one source on one partition and retain advisory locks. Do not add concurrency preemptively. |
| Raw SQL bypasses model-level defaults/validators | Make all columns/defaults explicit in the writer, keep database constraints authoritative, and add schema-contract tests around every statement. |
| COPY masks bad rows as a batch failure | Treat batch failure as no commit; retain spool frames, log the bounded failing delivery/source, and use deterministic replay to reproduce. Do not drop individual rows. |
| Moving Canvas/search post-commit creates lag or missed work | Persist candidate/outbox identity in the main commit, fence by document revision, and make the projector replayable/idempotent. Keep these synchronous until that durability exists. |
| FULL finalization regresses while the writer is rewritten | Reuse the existing content-store finalizer unchanged and retain all failure-injection gates from `MINIO_CONTENT_DESIGN.md`. FULL is a coalescing barrier. |
| Mixed collector versions mishandle accepted receipts | Capability-negotiate asynchronous receipt and speculative-chain support; old collectors keep the current one-uncommitted-revision rule and synchronous raw writer until upgraded. |
| SSE is accidentally broadened across users | Resolve owner from the authenticated machine in admission/writer state and preserve one Redis stream per user. Never accept a user scope from collector metadata. |

## What not to optimize first

- Do not build a custom bidirectional or indefinite NDJSON transport before the
  raw writer is measured. HTTP/2 clients already reuse/multiplex connections,
  and the production profile points to database object machinery, not HTTP
  parsing.
- Do not move semantic normalization to collectors. Optional versioned hints
  could be added later, but the server must remain able to derive and verify the
  canonical rows, which removes most of the hoped-for saving.
- Do not widen the coalescing window beyond the freshness budget to manufacture
  a better benchmark.
- Do not acknowledge queue admission as data durability at the source, weaken
  exact base fencing, publish SSE before commit, reconstruct raw FULL content
  from normalized rows, or skip MinIO streamed verification.
- Do not add more ingest concurrency to hide the ORM cost. It would increase
  simultaneous PostgreSQL bursts and contention while leaving per-event work
  unchanged.

## Acceptance criteria

The architecture is complete when production can sustain at least 150 admitted
conversation frames/minute and the target message rate while meeting all of the
following:

- p95 admitted-frame-to-user-visible SSE refresh below 2.5 seconds;
- steady API plus ingest-writer CPU below one core, measured over active load
  rather than only between bursts;
- total CPU cost at most 0.20 of the current baseline per sync-equivalent at
  target load, with CPU/message and CPU/source-record reported separately;
- no ORM frames on the raw writer's profile and no mapped instances created or
  hydrated in its transaction;
- bounded database round trips per coalesced DELTA, independent of message
  count apart from the COPY payload;
- normalized/projection golden diff empty for every supported tool;
- idempotent retry, stale delivery, base mismatch, lost response, writer crash,
  and ambiguous commit tests converge without duplicated or missing messages;
- every FULL pointer passes the existing immutable MinIO SHA/size verification
  sequence before commit;
- SSE events remain post-commit and owner-scoped; and
- collector committed bases advance only from committed/idempotent receipts.
