# Realtime ingest Phase 0/1 handoff

- **Date / branch:** 2026-08-26 / `main` (no commit created).
- **Objective / scope:** Implement only Phases 0 and 1 of
  `docs/REALTIME_INGEST_DESIGN.md`: a semantic/performance gate and Core
  staging for new conversation DELTA messages. Do not modify `web/` or later
  raw-writer/spool/coalescing phases.
- **Authoritative design read:** complete on 2026-08-26.
- **Initial worktree:** dirty only in unrelated `web/`, `artifacts/`,
  `tauri-collector/`, `build-artifacts/`, and `docs/regression-handoff.md`
  paths. Preserve all of these changes.
- **Current-path finding:** `_extract_messages` constructs
  `ConversationMessage` instances, calls `add_all`, and flushes in 100-row /
  4 MiB batches. DELTA tail promotion, Claude queue reconciliation, and Cursor
  stable-source updates operate on existing ORM rows and remain ORM in Phase 1.
- **Phase 0 plan:** add recorded Claude queued-row, Codex mirror-pair, and
  Cursor state-delta sequences; snapshot message rows (without generated IDs),
  usage, delivery/sync fences, read/prompt/task/dashboard projections, and
  staged realtime event changes. Compare the legacy staging path with a
  checked-in golden and then compare Core staging to that same golden.
- **Phase 1 plan:** default DELTA append staging to plain dictionaries plus
  `sqlalchemy.insert(...).returning(...)`; make small unmapped adapters only
  where the unchanged Canvas compatibility projector needs generated message
  IDs. Keep the surrounding session transaction, locks, projection refreshes,
  and event staging unchanged.
- **Verification requested:** use
  `C:\Users\intpa\OneDrive\Documents\test\memento-canvas-fix\.venv\Scripts\python.exe`
  with `MEMENTO_TASK_TEST_DATABASE_URL` pointing at the fresh local PostgreSQL
  database on `localhost:55437`; run parity, efficiency/ordering/streaming
  suites excluding fcntl spool tests and the known `DeltaBaseMismatch` case;
  report baseline and Phase 1 fixture-DB CPU/time metrics.

## Completed

- **Phase 0 gate:** `server/tests/test_realtime_ingest_parity.py` records
  representative existing parser shapes: a Claude queued row reconciled by its
  canonical DELTA, a Codex transport mirror pair split across the DELTA
  boundary, and a Cursor `cursor_state_v1` stable-row update plus a task
  DELTA. `server/tests/fixtures/realtime_ingest_parity_golden.json` is the
  current-path golden. It compares generated-ID-free message rows, usage
  events, delivery/sync fences, read/prompt/task/dashboard projections, and
  owner-scoped staged SSE payload/change sets; a failure names the first
  differing field path. It also exercises full idempotent retry, delayed stale
  DELTA, guarded base mismatch (including the current last-FULL fence),
  Cursor's idempotent state DELTA, and a Codex authoritative FULL rebase. It
  runs both the retained
  current ORM staging branch and default Phase 1 Core branch against the same
  file.
- **Phase 0 performance harness:**
  `server/server/scripts/benchmark_realtime_ingest.py` seeds an exact-base
  Codex fixture FULL outside the measurement, then measures one 1,000-message
  DELTA transaction on a fresh document. It reports median process CPU and
  wall time, plus bounded separate tracemalloc allocation samples. Run from
  `server/` with `python -m server.scripts.benchmark_realtime_ingest --writer
  legacy|core` and `MEMENTO_TASK_TEST_DATABASE_URL` set.
- **Phase 1 writer:** ordinary new conversation DELTA rows now remain plain
  dictionaries through extraction and use SQLAlchemy Core executemany against
  `conversation_messages`. Existing tail promotion, Claude queue matching,
  and Cursor stable-source reconciliation continue to mutate their loaded ORM
  rows. The current Canvas projector is still called for actual Canvas
  candidates via a small unmapped generated-ID adapter; non-candidates do not
  pay a no-op RETURNING/reconciliation cost. FULLs retain ORM staging.
- **Benchmark evidence (2026-08-26):** fixture DB
  `postgresql+asyncpg://postgres:test@localhost:55437/postgres`, 1 warmup, 5
  timed samples, 3 allocation samples, 1,000 normalized Codex DELTA messages
  / 388,892 input bytes. Legacy median: CPU **328.125 ms**, wall **433.673
  ms**, net allocations **145,387 bytes / 1,943 blocks**, traced peak
  **2,545,206 bytes**. Core median: CPU **156.250 ms**, wall **254.692 ms**,
  net allocations **126,020 bytes / 1,698 blocks**, traced peak **2,494,435
  bytes**. That is a 52.4% CPU reduction and 41.3% wall-time reduction on this
  large append fixture; allocation comparison is diagnostic only because it is
  separately traced.
- **Verification completed:** parity 2 passed; Canvas candidate Core smoke 1
  passed; ingest efficiency 9 passed; ingest ordering 23 passed / 1 requested
  known base-mismatch test deselected; multipart ingest streaming 1 passed.
  fcntl spool streaming tests were not run. `cargo clippy --all-targets` and
  `cargo build --no-default-features` both passed in `tauri-collector/src-tauri`.
- **No commit / push.**
