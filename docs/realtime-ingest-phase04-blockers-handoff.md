# Realtime ingest Phase 4 blocker fixes handoff

## Objective and scope

Implement the two BLOCKERs and SHOULD-FIX from
`docs/realtime-ingest-phase04-review-sol.md`, without enabling
`realtime_ingest_deferred_projections`, committing, pushing, changing deploy
files, or regenerating parity goldens.

Started on `main` at `33ebe3d3d3def80c5aa8c7db981f1437550a4e8d`.  Existing
unrelated artifact/build/Tauri-icon changes were left alone.

## Task 1 — raw writer pre-update candidates

`MessageMutation` now preserves `previous_role` and `previous_metadata` for
every raw update shape.  Candidate derivation uses both the replacement and
the pre-update values:

- Canvas enqueues when either side is Canvas-capable, in addition to the
  existing new-body `.canvas.tsx` test.  A user/assistant/tool replacement
  that becomes a non-capable system row therefore still reconciles stale
  references.
- Search treats an update as text-changing when either the old or new role is
  `user`/`assistant`.
- The old-role signal is deliberately candidate-only.  `search_text`, which
  drives the pre-existing raw writer SSE event, retains its prior new-value
  semantics so flag-off parity is unchanged.  The full parity gate caught
  this distinction during implementation; the final result below confirms
  it.

New pure reducer coverage in `server/tests/test_realtime_raw_writer.py` proves:

1. Cursor user-to-system directives replacement enqueues Canvas and search.
2. System-to-system directives replacement enqueues neither.
3. User-to-user content update enqueues search.

## Task 2 — full-ingest lexicon parity for large documents

Design selected: **one full ascending lexicon scan on the first deferred
search apply for a document**.  `_apply_search` tests whether the document has
any completed or superseded search candidate.  If it has none, it reads
`left(content, 2048)` from all stored user/assistant rows in line-number order
and feeds terms to the existing capped (`MAX_LEXICON_TERMS_PER_INGEST =
10,000`) upsert.  The regular FTS value remains the intentionally bounded
last-200 corpus.  After the first apply, lexicon extraction also remains
last-200, so a hot 12k-message transcript never receives an unbounded scan per
DELTA.

This aligns a new document's first deferred FULL projection with the
synchronous full-ingest lexicon corpus while requiring no schema migration or
candidate-row scope expansion.  The large-transcript test creates 240 rows,
places one unique term only before the last-200 window and another in the late
window, captures the synchronous flag-off FULL result, clears only the
UUID-prefixed test terms from the shared task lexicon, then runs deferred
ingestion plus the projector on the same transcript.  It compares both the
prefixed lexicon set and FTS text with the captured synchronous result.

Residual gap: after the first search apply, an in-place edit of an old
user/assistant row that introduces a brand-new term outside the latest 200
rows does not add that term to the append-only lexicon.  It does still enqueue
and refresh the bounded FTS projection.  This is the bounded-cost tradeoff of
the selected design and should remain explicit in future rollout review.

## Task 3 — apply-before-commit crash replay

Added a projector integration test that ingests a deferred crash fixture and a
clean control fixture, calls `process_pending_candidates` directly for the
crash document, then rolls back the outer session.  It asserts both candidate
kinds remain pending, uses a fresh projector to quiescence, and compares the
final Canvas/search snapshot with the clean run.  It also asserts exactly one
Canvas reference and no remaining pending crash-document candidates.

## Validation

All Python commands used
`C:\Users\intpa\OneDrive\Documents\test\memento-canvas-fix\.venv\Scripts\python.exe`,
with cwd `server/`,
`MEMENTO_TASK_TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:55437/postgres`,
and a `--basetemp` directory under this repository.  Pytest emitted only the
pre-existing Windows `.pytest_cache` permission warning.

| Command | Final result |
| --- | --- |
| `python -m py_compile server/services/realtime_raw_writer.py server/services/realtime_ingest_projector.py tests/test_realtime_raw_writer.py tests/test_realtime_ingest_projector.py` | `OK` |
| `python -m pytest tests/test_realtime_raw_writer.py -q --basetemp ..\\build-artifacts\\phase4-blockers-gate-raw-recheck` | `3 passed, 1 warning in 1.31s` |
| `python -m pytest tests/test_realtime_ingest_projector.py -q --basetemp ..\\build-artifacts\\phase4-blockers-gate-projector-final-verified` | `13 passed, 1 warning in 283.49s` |
| `python -m pytest tests/test_realtime_ingest_parity.py -q --basetemp ..\\build-artifacts\\phase4-blockers-gate-parity-rerun` | `6 passed, 1 warning in 216.06s` |
| `python -m pytest tests/test_realtime_ingest_drain.py -q --basetemp ..\\build-artifacts\\phase4-blockers-gate-drain` | `5 passed, 1 warning in 1.85s` |
| `python -m pytest tests/test_ingest_spool.py -q --basetemp ..\\build-artifacts\\phase4-blockers-gate-spool` | `42 passed, 1 warning, 16 subtests passed in 3.51s` |
| `python -m pytest tests/test_ingest_expired_reconcile_repro.py -q --basetemp ..\\build-artifacts\\phase4-blockers-gate-expired-reconcile` | `5 passed, 1 warning in 108.72s` |
| `cargo clippy --all-targets` (cwd `tauri-collector/src-tauri`) | `Finished dev profile` — zero warnings |
| `cargo build --no-default-features` (same cwd) | `Finished dev profile` |

`server/tests/fixtures/realtime_ingest_parity_golden.json` was not regenerated
or modified.

## Reviewer scrutiny list

- Verify raw candidate data is captured from the already-loaded reducer row;
  no database read was added to derive pre-update state.
- Verify candidate-only old-role search invalidation cannot alter flag-off
  event behavior; the raw Phase 0 parity case covers this.
- Verify the first-apply predicate checks historical completed/superseded
  **search** candidates only, before current candidates are marked complete.
- Verify full-scan lexicon order is ascending line order and extraction stays
  capped at 10,000 terms; later applies use the last-200 query.
- Keep the residual old-row-edit lexicon gap documented during flag-on rollout
  review.
- Verify the crash test rolls back the outer transaction after direct apply,
  then creates a fresh projector and passes `document_ids=` on every drain.
- Confirm `realtime_ingest_deferred_projections` remains default-off and has
  not been enabled in any environment or deploy configuration.

No commit or push was performed.
