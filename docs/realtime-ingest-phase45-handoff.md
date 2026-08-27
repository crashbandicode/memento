# Realtime ingest Phase 4.5 handoff

## Objective and scope

Make the Phase 5 raw-DELTA retirement gate measurable and reachable by removing
the safe, repeat-only Codex history fallback and porting existing-document title
selection.  No feature flags, deploy/WSL files, `.env` files, commits, or pushes
were changed.  The production deferred-projections flag was treated as enabled
for the focused candidate assertion only.

Worktree started on `main` at
`fd916032f74d6fe9542a0151194d4f0a0748e97c`.  Existing unrelated artifact,
`build-artifacts`, and Tauri icon changes remain untouched.

## Task 1 — Codex history short-circuit

`server/server/services/realtime_raw_writer.py` conditionally loads recovery
rows and ordinary user rows only for a frame carrying `user_history` or
`first_user_message`.  It reduces the frame first, then permits raw ingest only
when it proves that both legacy history partitions and its reconciliation would
be no-ops; otherwise it raises the existing
`authoritative rebuild/history needs legacy reducer` reason before any raw
write.  This leaves true recovery and reconciliation with the legacy reducer.

### Exact “unchanged” definition

The comparison intentionally follows the legacy history identity and
normalization rules, and is stricter where legacy currently tolerates a bad
collector rewrite:

- Run `_prepare_document_metadata` first, so only its bounded, Codex-normalized
  `user_history` entries participate.  The post-prepare list index is the
  legacy stable source identity: `codex-history:{index}`.
- For every usable incoming entry, run the legacy history branch's second
  Codex normalization, trim it, apply the same bounded-message transformation,
  and normalize `ts` with the same `datetime.fromtimestamp(..., UTC)`/invalid
  to `None` behavior.
- Build the ordinary user occurrence set from all committed non-history user
  rows, then apply the raw frame's prospective user inserts and updates in
  memory.  This is deliberately after reduction but before `_apply`, so an
  ordinary Codex user rollout from the current DELTA participates in the
  proof.
- An incoming entry with an existing `codex-history:{index}` row must match its
  exact normalized content and timestamp.  An entry without that recovered row
  is also accepted when `partition_recovered_occurrences` proves it is already
  represented one-to-one by the current/prospective ordinary user occurrence
  set—the same missing-history dedup the legacy reducer performs.
- Independently run `partition_recovered_occurrences` for every stored
  `history_user_message` against that same ordinary occurrence set.  Any
  match would make legacy delete a recovered row, so it is a legacy fallback.
  Any stored negative-line recovered row is also a fallback because legacy
  would place it in the positive timeline.  Thus raw accepts only when legacy
  would neither insert, delete, nor place a recovered row.
- A new source ID that is not one-to-one represented, changed content or
  timestamp, malformed-only history list, absent state, matching recovered
  row, or negative recovered row falls back.  The proof imports the shared
  `history_recovery.partition_recovered_occurrences`; it does not duplicate
  the matching algorithm.
- With no usable `user_history`, an incoming `first_user_message` is ignored
  only if a committed `first_user_message` row has the exact same content.
  This is intentionally narrower than the legacy branch's broad “any user
  row” guard, because it is the only unambiguous first-prompt proof.

Focused reducer tests cover an unchanged history repeat taking the raw path,
changed/new history falling back, a negative recovery row falling back, and an
ordinary-user-represented history repeat committing raw.  Parity tests compare
the raw-fallback result with legacy when a prospective Codex user row matches
a recovered row, and compare raw with legacy when a history entry is already
represented by an ordinary row.

## Task 2 — native existing-document title selection

The raw reducer imports and calls the legacy pure
`ingest_service._select_updated_document_title`; it does not copy precedence
logic.  It merges the same stored metadata before selection, persists a changed
title to `documents`, and marks the raw SSE title change.  Candidate derivation
now passes the actual previous and current titles to
`_conversation_search_index_needs_refresh`, so a title-only DELTA creates a
deferred search candidate when the flag is enabled.

The Phase 4.5 integration test runs equivalent Claude explicit-title updates
through raw and legacy paths, asserts both committed document titles are
`Selected Claude title`, and verifies the raw final revision has a `search`
projection candidate with deferred projections enabled.

## Task 3 — new-document project resolution assessment

Classification: **(b) careful design — not implemented.**

This shape is not a pure title-style reducer.  The legacy new-document path
combines metadata precedence with payload `cwd` extraction and cleanup,
creates or repairs a shared `projects` row through `ensure_project`, and falls
back to a machine/user-scoped cross-document `session_id` project lookup.  A
correct raw port needs explicit concurrent project upsert/repair semantics,
tool/project foreign-key ordering, source-path precedence, and exact ownership
scope before it can attach `project_id` to both document and delivery state.
Those cross-row rules merit a dedicated raw-transaction design and parity
fixture rather than a small local patch.

Also, the current durable realtime drain accepts DELTA chains only: a
new-document DELTA already takes the earlier “DELTA requires an existing FULL
document” guard.  This project-resolution raise is relevant to the synchronous
raw FULL/new-document path (and any future realtime FULL admission), not to a
normally routed drain chain.  It remains a documented fallback until that
design is completed.

## Task 4 — per-shape drain observability

`server/server/tasks/ingest_spool.py` now maintains bounded in-process outcome
counters in the actual drain-to-writer bridge:

- raw committed chains and frames;
- total handled chains and frames, including raw `idempotent`, `stale_delta`,
  and `superseded` dispositions as well as legacy fallbacks;
- legacy fallback chains and frames grouped by the exact
  `RawWriterUnsupported` reason string;
- up to 16 distinct reasons per reporting window, with additional reasons
  grouped as `other/raw-writer-unsupported`.

It emits a single `ingest_spool` info log record every 60 seconds of activity
or after 100 chains, then resets the process-local window.  The record carries
`total_handled_chains`, `total_handled_frames`, `raw_committed_chains`,
`raw_committed_frames`,
`legacy_fallback_chains_by_reason`, and
`legacy_fallback_frames_by_reason`, making both Phase 5's shape allowlist and
the literal per-drained-frame percentage calculable from logs alone.  No database writes or
dependencies were added.  The counters are intentionally per process; sum the
same log fields if drain sharding is introduced.

## Expected fallback impact

Against the investigated 60-minute chain distribution, unchanged Codex history
repeats (about 72%) and existing-document title selection (about 11%) no longer
need the legacy path.  The nominal removable share remains about **83%** for
steady-state rows that legacy would not otherwise reconcile; the intentionally
conservative proof routes the small reconciliation-active subset to legacy.
the principal expected residual is the explicitly legacy-forever Claude
transcript/sidecar pairing (about 17%), plus the rare identity/base/order safety
guards.  History changes/new recovery entries still deliberately fall back, so
the exact live reduction depends on how often a Codex history.jsonl actually
advances.

## Validation

All Python commands used cwd `server/`,
`C:\Users\intpa\OneDrive\Documents\test\memento-canvas-fix\.venv\Scripts\python.exe`,
`MEMENTO_TASK_TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:55437/postgres`,
and a repository-local `--basetemp`.  Pytest emitted only the existing Windows
`.pytest_cache` permission warning.  The parity golden was read-only and was
not regenerated or modified.

| Command | Final result |
| --- | --- |
| `python -m py_compile server/services/realtime_raw_writer.py server/tasks/ingest_spool.py tests/test_realtime_raw_writer.py tests/test_realtime_ingest_drain.py tests/test_realtime_ingest_parity.py` | `OK` |
| `python -m pytest tests/test_realtime_raw_writer.py -q --basetemp ..\\build-artifacts\\phase45-gate-raw-final` | `7 passed, 1 warning in 1.28s` |
| `python -m pytest tests/test_realtime_ingest_drain.py -q --basetemp ..\\build-artifacts\\phase45-gate-drain` | `6 passed, 1 warning in 1.59s` |
| `python -m pytest tests/test_ingest_spool.py -q --basetemp ..\\build-artifacts\\phase45-gate-spool` | `42 passed, 1 warning, 16 subtests passed in 3.87s` |
| `python -m pytest tests/test_realtime_ingest_projector.py -q --basetemp ..\\build-artifacts\\phase45-gate-projector-final` | `13 passed, 1 warning in 283.14s` |
| `python -m pytest tests/test_realtime_ingest_parity.py -q --basetemp ..\\build-artifacts\\phase45-gate-parity-final` | `7 passed, 1 warning in 258.56s` |
| `cargo clippy --all-targets` (cwd `tauri-collector/src-tauri`) | `Finished dev profile` — zero warnings |
| `cargo build --no-default-features` (same cwd) | `Finished dev profile` |

## Reviewer scrutiny list

- Confirm the raw-history proof requires stable-source exactness for stored
  recovery rows and uses the shared one-to-one content/timestamp helper only
  for source-ID-missing entries already represented by ordinary user rows.
- Confirm history rows are loaded only for metadata-bearing frames and that
  a failed proof cannot perform any raw mutation before the legacy fallback.
- Review whether a future native history merge should additionally port
  `partition_recovered_occurrences` and `_reconcile_recovered_history_rows`;
  this phase intentionally does neither.
- Confirm title selection is imported from the legacy helper and that the raw
  document title update, SSE `title_changed`, dashboard projection, and search
  candidate all observe the same before/after values.
- Confirm the Phase 5 log parser treats counters as per-process windows, sums
  frame counts for percentage gates, and preserves the named legacy-forever
  reason allowlist.
- Do not broaden raw support for project resolution without a separate
  project-upsert/session-fallback design and a raw-vs-legacy parity fixture.

No commit or push was performed.

## Rollback constraint

Deferred projections are enabled in production.  Disabling them while keeping
these newly raw-supported history/title shapes does **not** restore the old
synchronous FTS/search behavior: those frames used to reach the legacy writer,
but now can remain raw and the flag-off raw path still lacks that synchronous
refresh.  Treat a flag-only disable as insufficient rollback for these shapes;
retain deferred projections or explicitly restore legacy routing/code.

## Revision 2

This revision fixes the rejected no-op proof and telemetry denominator without
changing the cleared title port, fallback transaction boundaries, counter
reason boundedness, or Task-3 classification.

- The history shortcut now uses the shared one-to-one
  `partition_recovered_occurrences` helper after reducer mutations are known.
  It models committed plus prospective ordinary user rows, accepts a missing
  recovery row when legacy would deduplicate it against an ordinary row, and
  falls back if legacy would insert, delete, or place a recovery row.  History
  metadata chains remain sequential rather than combining same-metadata frames.
- The outcome log now includes `total_handled_chains` and
  `total_handled_frames`, counting terminal raw dispositions and legacy
  fallbacks, so each reason's fallback-frame percentage has the exact drained
  denominator.
- Added parity coverage for a prospective ordinary Codex user that matches a
  stored recovery row (raw defers; raw-fallback and legacy message lines/counts
  and types, read model, and prompts are equal), and for a history entry already
  represented by an ordinary user row (raw commits and matches legacy).  Pure
  reducer coverage also asserts that a stored negative recovery row falls back.

### Revision 2 validation

All Python commands used cwd `server/`,
`C:\Users\intpa\OneDrive\Documents\test\memento-canvas-fix\.venv\Scripts\python.exe`,
`MEMENTO_TASK_TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:55437/postgres`,
and repository-local `--basetemp` directories.  The parity golden was not
regenerated or modified.  Pytest emitted only the existing Windows
`.pytest_cache` permission warning.

| Command | Final result |
| --- | --- |
| `python -m py_compile server/services/realtime_raw_writer.py server/tasks/ingest_spool.py tests/test_realtime_raw_writer.py tests/test_realtime_ingest_drain.py tests/test_realtime_ingest_parity.py` | `OK` |
| `python -m pytest tests/test_realtime_raw_writer.py -q --basetemp ..\\build-artifacts\\phase45-rev2-raw-first` | `9 passed, 1 warning in 1.18s` |
| `python -m pytest tests/test_realtime_ingest_drain.py -q --basetemp ..\\build-artifacts\\phase45-rev2-gate-drain` | `6 passed, 1 warning in 1.52s` |
| `python -m pytest tests/test_ingest_spool.py -q --basetemp ..\\build-artifacts\\phase45-rev2-gate-spool` | `42 passed, 1 warning, 16 subtests passed in 3.84s` |
| `python -m pytest tests/test_realtime_ingest_parity.py -q --basetemp ..\\build-artifacts\\phase45-rev2-gate-parity` | `8 passed, 1 warning in 300.71s` |
| `python -m pytest tests/test_realtime_ingest_projector.py -q --basetemp ..\\build-artifacts\\phase45-rev2-gate-projector` | `13 passed, 1 warning in 283.20s` |
| `python -m pytest tests/test_ingest_expired_reconcile_repro.py -q --basetemp ..\\build-artifacts\\phase45-rev2-gate-expired-reconcile` | `5 passed, 1 warning in 109.40s` |
| `cargo clippy --all-targets` (cwd `tauri-collector/src-tauri`) | `Finished dev profile` — zero warnings |
| `cargo build --no-default-features` (same cwd) | `Finished dev profile` |
