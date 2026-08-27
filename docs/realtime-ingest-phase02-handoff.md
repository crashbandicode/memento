# Realtime ingest Phase 2 handoff

## Second-reviewer pass complete (2026-08-26)

- **Objective:** correctness-audit the uncommitted Phase 2 raw writer, close any
  unsafe fallback/commit windows, reduce the F=1 residual below 0.35x legacy,
  and rerun parity/regression/benchmark evidence. No commit or push.
- **Correctness fixes applied:** commit-attempted failures now use a distinct
  non-fallback exception and reread an exact machine/tool/path delivery+sync
  fence after releasing the original connection; stable UUID sources use the
  same identity advisory-lock domain as the legacy writer; relocations,
  Cursor ordering hints, and Claude transcript/sidecar pairs reject before
  mutation; raw DELTAs require an exact base and chain from delivery state;
  authenticated owner+machine scope is mandatory; server re-sanitization,
  Codex identity extraction, path hierarchy metadata, `needs_review`, cache
  invalidation, project activity, and post-ingest scheduling are preserved.
- **Performance changes applied:** connection JSON codecs and the reusable
  temp stage are initialized once per pooled connection; the common state is
  loaded in one scalar query plus the bounded tail; COPY consumes a generator;
  UPDATE+INSERT returns only ordinal/ID in one statement and projection rows
  reuse the reducer values; append-only prompt deletion and activity scans are
  skipped; tool/delivery/document-review/sync fences share one statement; SSE
  search signaling no longer builds a second 77 KiB text copy.
- **Final benchmark (same sequential run, 1 warmup / 9 timed / 3 allocation):**
  legacy **93.750 ms CPU / 180.123 ms wall / 873 blocks**; Core **93.750 /
  124.641 / 760**; raw **15.625 / 40.701 / 401**. Raw is **0.167x** legacy CPU,
  below the 0.35 ship gate; raw wall is 0.326x Core and allocations are 0.459x
  legacy. Windows process CPU samples are quantized at 15.625 ms, so retain the
  raw samples in benchmark output when comparing future runs.
- **Verification:** parity **5/5** (including chained DELTA, exact-source
  ambiguous convergence, non-fallback uncertain outcome, and failed MinIO
  PUT), ordering/efficiency **33/33**, read-amplification projections **22/22**,
  targeted Python compilation and `git diff --check` pass, `cargo clippy
  --all-targets` passes with zero warnings, and `cargo build
  --no-default-features` passes.
- **Resume:** review `git diff`/the untracked raw writer if desired. No commit or
  push was requested or performed.

- **Date / branch:** 2026-08-26 / `main` (no commit created).
- **Scope:** Phase 2 only from `docs/REALTIME_INGEST_DESIGN.md`: a synchronous,
  canary-gated raw `asyncpg` conversation writer. Phase 3 spool admission and
  coalescing are intentionally untouched.
- **Worktree:** preserve all pre-existing `artifacts/`, collector icon/lock,
  build-artifact, and handoff changes. This work adds the raw writer module and
  changes only server ingest/config/benchmark/parity files.

## Implementation

- `server/services/realtime_raw_writer.py` defines the plain-data
  `WriterState`, `IngestMutation`, and `MessageMutation` reducer contract.
  The current shared stored-message iterator remains the normalizer. The raw
  transaction takes the existing source advisory lock, COPYs rows into a
  connection-local stage table, applies direct message/delivery/sync/read
  model/prompt/task/dashboard SQL, and returns an owner-scoped staged SSE
  payload only after commit.
- `ingest_file(..., writer="raw")` tries that transaction and uses the old
  writer only for unsupported reductions or pre-commit errors. Normal FULL
  snapshots call the unchanged MinIO finalizer with its content advisory lock
  on the same asyncpg connection. Replacement/rebase and streamed source
  shapes remain safe fallback reductions.
- The endpoint is opt-in only through comma-separated
  `MEMENTO_REALTIME_INGEST_RAW_WRITER_OWNERS`, `_DEVICES`, or `_TOOLS`.
  Empty selectors leave the old writer active.
- The benchmark accepts `--writer raw`; its setup FULL is outside the F=1
  measurement.

## Verification

Run from `server/` with
`MEMENTO_TASK_TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:55437/postgres`
and `C:\Users\intpa\OneDrive\Documents\test\memento-canvas-fix\.venv\Scripts\python.exe`.

- `tests/test_realtime_ingest_parity.py -q`: **5 passed**. This includes all
  legacy/Core/raw golden paths, ambiguous-commit reread/idempotent retry, and
  a failed ambiguous MinIO PUT with no committed document pointer.
- `tests/test_ingest_ordering.py -q -k 'not guarded_delta_rejects_mismatched_committed_base'`:
  **23 passed, 1 deselected** (requested known DeltaBaseMismatch case).
- `tests/test_ingest_efficiency.py -q`: **9 passed**.
- `tests/test_read_amplification_projections.py -q`: **22 passed**.
- F=1 benchmark (200 normalized Codex DELTA messages, 1 warmup / 5 timed / 3
  allocation samples): legacy CPU median **109.375 ms**, Core **93.750 ms**,
  raw **31.250 ms**. Raw is **0.286x** legacy, below the 0.35 gate. Wall
  medians were 151.290 / 173.679 / 107.905 ms respectively. The first raw
  pool creation is intentionally outside timed samples via warmup.
- `cargo clippy --all-targets` and `cargo build --no-default-features` passed
  in `tauri-collector/src-tauri`.

## Next command

`git status --short`; no commit or push was requested. Keep Phase 3 admission,
spool, receipt, quiet-window, and deferred-projector work out of this change.
