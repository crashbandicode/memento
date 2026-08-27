# Realtime ingest Phase 3 handoff

## Objective and scope

Implement the binding Phase 3 of `REALTIME_INGEST_DESIGN.md` without a commit
or push.  The worktree is on `main`; retain all unrelated pre-existing artifact,
collector-icon, and prior-handoff changes.

## Implementation

- Guarded, capability-negotiated conversation JSONL DELTAs are fsynced into the
  existing manifest/chunk/ready-marker spool even below the chunk threshold.
  `MEMENTO_REALTIME_INGEST_SPOOL_DELTAS=false` remains the rollout fallback:
  eligible sources use the existing synchronous raw writer instead.
- A receipt is now explicitly `accepted` (spooled only) or `committed`
  (PostgreSQL done).  Receipt identity binds the authenticated source/revision
  envelope and payload SHA.  Same revision metadata with different payload
  bytes is rejected.
- Collector 0.0.52 advertises `realtime_ingest_async_admission_v1`.  Its local
  queue retains accepted payloads, keeps `file_state.synced_*` unchanged, and
  uses a bounded accepted cursor solely for successor capture.  A terminal
  head receipt drops every pending/accepted successor and invokes the existing
  FULL repair callback.
- `server.services.realtime_ingest_drain` is a persistent, marker-authoritative
  service/container.  It scans on startup and continuously, coalesces by a
  1.25-second quiet window or 2.0-second hard deadline, and completes every
  constituent receipt after one raw asyncpg transaction.  Legacy chunk jobs
  remain on their existing Celery finalizer; Celery recovery explicitly skips
  Phase 3 markers.
- `ingest_conversation_raw_chain` preserves per-frame semantics.  Plain chains
  with identical metadata use one combined reducer/stage/projection application;
  metadata-varying chains stay ordered inside the same transaction.  The reusable
  temporary stage is truncated between sequential frame applications.
- The benchmark now supports `--frames-per-drain N --coalesced` for raw writer
  chain-vs-separate measurements.

## Verification evidence

All commands used
`C:\Users\intpa\OneDrive\Documents\test\memento-canvas-fix\.venv\Scripts\python.exe`
with `MEMENTO_TASK_TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:55437/postgres`.

- `tests/test_realtime_ingest_parity.py -q`: **6 passed**.  Includes raw
  coalesced multi-frame output equal to the same frames committed one at a
  time.
- `tests/test_ingest_ordering.py -q`: **24 passed**.
- `tests/test_ingest_efficiency.py -q`: **9 passed**.
- `tests/test_ingest_spool.py -q`: **40 passed**; includes accepted/committed
  receipt and payload-proof conflict coverage.
- `tests/test_ingest_chunk_api.py -q`: **13 passed**; includes capability-gated
  durable admission and receipt status coverage.
- `tests/test_realtime_ingest_drain.py -q`: **2 passed**.  The synthetic
  150-frame/minute fixture hits the 2.0-second hard drain deadline, under the
  2.5-second admission-to-SSE gate before normal raw transaction time.
- `collector/tests/test_queue.py -q`: **46 passed**; verifies accepted does not
  advance durable cursor and head failure discards the speculative chain.
- `collector/tests/test_sync_client.py -q`: **25 passed**, 166 subtests.
- Benchmark (3 Codex frames): separate raw syncs median **36.759 ms wall**;
  coalesced raw drain median **12.623 ms wall** (**0.343x**, below 0.5x).
  Windows process CPU samples are 15.625 ms quantized; coalesced median was
  0 ms and separate median 15.625 ms, so wall is the stable comparison.

Pytest emitted only pre-existing Windows `.pytest_cache` permission warnings;
using `--basetemp` kept the test data in the workspace.  The new spool lock has
a Windows CRT fallback; POSIX `fcntl` remains the production lock path.

## Operational lifecycle

Start `realtime-ingest-drain` with Compose alongside the API.  It requires the
same `ingest_spool` volume, database, and Redis/SSE configuration.  It recovers
solely by scanning ready markers after crash/restart; no broker body or wake-up
is authoritative.  SIGTERM/SIGINT stops after the current drain loop boundary.

No commit and no push were performed.

## Second-reviewer protocol pass (2026-08-27, complete)

The second-reviewer pass found and surgically fixed receipt-order cursor
regression, pause/resume receipt-poll loss, unsafe queued acknowledgements when
the Phase 3 flag changes, missing restart SSE invalidation, timestamp-based
DELTA reordering, absence of a process-wide drain lock, incomplete terminal
receipting of failed chains, non-durable Windows rename publication, and a
Redis-outage SSE fallback that consumed the freshness budget.  It also made
admission payload-conflict lookup O(1), verifies the admitted payload SHA
before ingest, handles a head committed before successor admission, and makes
FULL admission fence the entire accepted DELTA chain.

Final evidence against `phase3v`/`phase3v_review_bench`:

- parity + ordering + efficiency: **39 passed** (6 + 24 + 9);
- server admission/spool/drain: **60 passed**, plus 16 subtests;
- SSE service: **9 passed**;
- collector queue: **48 passed**;
- collector sync-client: **26 passed**, plus 166 subtests;
- `cargo clippy --all-targets` and `cargo build --no-default-features`: clean;
- Python compileall and `git diff --check`: clean (line-ending notices only).

Final 3-frame benchmark, 15 timed samples after one warmup: separate raw syncs
median **33.109 ms**, coalesced raw transaction median **13.367 ms**, ratio
**0.404x** (gate: at most 0.5x).  The integrated 150-frame/minute fixture used
six arrivals in the 2.0-second hard window, a real raw transaction, unavailable
Redis, and process-local SSE dispatch: p95 **2.440 s** over 20 cycles (gate:
2.5 s).  One first/cold sample was **23.483 s**; the other 19 were
2.414--2.440 s.  The publisher's failed-Redis p95 alone fell from **2,012.5 ms**
to **404.3 ms** after reducing its socket budget.

No commit and no push were performed.
