# Deferred raw-ingest projection kinds handoff

## Delivered scope

This follow-on implements C3+C4 on
`feat/raw-deferred-projection-kinds`, based on the C1+C2 canary already in
production. It adds two durable outbox kinds:

- `claude_lineage`
- `subagent_lifecycle`

The existing `ck_ingest_projection_candidate_kind` constraint now also admits
those values. The ORM model, startup DDL, and documented migration all include
the same expansion. Startup DDL is idempotent for an installation that already
has the Phase 4 outbox: it adds the compact `payload` column if absent, then
reads the named CHECK through `pg_constraint`. It drops and re-adds the CHECK only
when PostgreSQL's canonical definition differs, so a normal boot does not run
a table-validating `ALTER TABLE`.

`payload` is an implementation necessity, not a raw-body duplicate. A Claude
conversation DELTA retains the prior FULL object pointer, while normalized
message rows deliberately do not preserve its UUID-parent graph or all
lifecycle evidence. At ingest, each new kind receives an allow-listed,
bounded set of only identity/lifecycle fields (UUID, parent UUID, agent,
record type, timestamp, sidechain flags, and terminal markers). No message
text, tool input, or arbitrary raw JSON is persisted in the outbox.

## Apply semantics

`_apply_claude_lineage` feeds the queued compact records to
`refresh_claude_lineage(..., mode="delta")`. It therefore uses its existing
append/rewind machinery and avoids a full object-store transcript reread (R5).
The projector folds all still-pending source-ordered DELTAs only when their
**newest** revision matches the document's current delivery revision. A FULL
replacement can deliberately restore an older fence while a newer raw DELTA
is still queued; in that case the projector supersedes the **entire** Claude
candidate group and calls neither apply path. It must not treat an older
matching row as permission to replay later queued records.

`_apply_subagent_lifecycle` feeds the same bounded records to
`_reconcile_subagent_document_lifecycle`. The established
`reconcile_child_lifecycle_metadata` terminal-regression guard remains the
authority, so a late non-terminal observation does not regress a completed
Claude child.

Both kinds preserve the existing candidate lifecycle: on a current newest
fence, older rows are completed and superseded while the newest row is
completed but unsuperseded. On a stale newest fence, every row in the group is
completed and superseded. Unknown kinds still follow the existing supersede
branch.

## Retention and rollout

`RealtimeIngestProjector.run_once` prunes one lock-protected, batch-limited
set of rows with `completed_at` older than
`realtime_ingest_projection_candidate_retention_hours` (default: 24 hours).
`realtime_ingest_projection_candidate_prune_batch_size` defaults to 256. The
pruner has no kind filter, so it covers Canvas, search, Claude lineage, and
subagent lifecycle candidates, including superseded rows; pending rows have
no `completed_at` and are never eligible. Selection and deletion share the
projector transaction, so a crash rolls the deletion back; a committed row has
no replay value. Targeted `run_once(document_ids=...)` calls prune only that
scope, while the long-lived production loop prunes globally.

Only the FastAPI API lifespan invokes `_run_migrations`; the ingest worker,
realtime drain, and projector do not. Deployment therefore requires the API
schema migration to run first, or the established full-stack recreate from
one image where that API migration completes before new raw writers run. Do
not roll a worker-only image containing the new enqueue/projector code ahead
of the API schema expansion.

## Enqueue scope

The raw writer enqueues both new kinds for **all** `claude_code` conversation
documents when deferred projections are enabled. This includes main-session
documents deliberately: otherwise main-session lineage can remain stale after
raw DELTAs. The lifecycle reconciler cheaply no-ops when the document is not a
subagent. Canvas and search enqueue behavior is unchanged.

## Tests run

Commands used PowerShell 7, the task PostgreSQL database URL, and the local
Python environment:

```powershell
[System.Environment]::SetEnvironmentVariable('MEMENTO_TASK_TEST_DATABASE_URL', 'postgresql+asyncpg://postgres:test@localhost:55437/postgres', 'Process')
& '..\.venv\Scripts\python.exe' -m pytest tests/test_realtime_ingest_projector.py --basetemp ..\.pytest-basetemp\rawfollowons-projector-full -q
# 2 failed, 15 passed, 2 warnings in 417.93s (0:06:57)
# Known clean-main baseline failures only:
# test_deferred_projections_flag_defaults_off
# test_deferred_ingest_live_fields_match_golden[True-raw]

& '..\.venv\Scripts\python.exe' -m pytest tests/test_realtime_ingest_parity.py --basetemp ..\.pytest-basetemp\rawfollowons-parity-full -q
# 16 passed, 2 warnings in 541.60s (0:09:01)

& '..\.venv\Scripts\python.exe' -m pytest tests/test_realtime_ingest_drain.py --basetemp ..\.pytest-basetemp\rawfollowons-drain -q
# 7 passed in 1.56s

& '..\.venv\Scripts\python.exe' -m pytest tests/test_claude_lineage.py --basetemp ..\.pytest-basetemp\rawfollowons-claude-lineage -q
# 12 passed in 65.35s (0:01:05)

& '..\.venv\Scripts\python.exe' -m pytest tests/test_subagent_lifecycle_reconciliation.py --basetemp ..\.pytest-basetemp\rawfollowons-subagent-lifecycle -q
# 11 passed in 0.77s

# Review-fix focused coverage
& '..\.venv\Scripts\python.exe' -m pytest tests/test_realtime_ingest_projector.py::test_deferred_claude_kinds_do_not_replay_newer_delta_after_full_rewind tests/test_realtime_ingest_projector.py::test_projector_prunes_completed_candidates_without_touching_pending tests/test_realtime_ingest_projector.py::test_runtime_migrations_do_not_replace_current_projection_kind_constraint --basetemp ..\.pytest-basetemp\rawfollowons-review-focused-final -q
# 3 passed, 2 warnings in 90.20s (0:01:30)
```

The two projector failures were re-run in the clean-main control-plane
worktree before being accepted as baseline: the default-flag test failed with
`assert True is False`, and the raw deferred-golden node failed at
`tests\test_realtime_ingest_parity.py:924: AssertionError`.

## Explicit non-changes

- No C1/C2 flag, pairing-gate, parser, or legacy ingest-path change.
- No API or web change.
- No sidecar change; launch metadata remains the convergent legacy sidecar
  writer and has no projection kind.
- No Cargo checks (the task environment's collector-sidecar binary is absent,
  as pre-declared by the task).

## Reviewer scrutiny list

- Confirm the PostgreSQL canonical CHECK definition is stable for the deployed
  PostgreSQL version, and that an already-widened constraint has no startup
  drop/re-add or validation churn.
- Confirm the enforced API-first (or single-image full-stack recreate) rollout
  completes schema readiness before worker-only raw-writer/projector code runs.
- Inspect the source-field allow-list to ensure it stays sufficient for
  lineage/lifecycle while never retaining transcript content in the outbox.
- Confirm Claude records replay only when the newest pending fence equals the
  current revision; a restored older FULL must supersede every pending row and
  never replay a newer DELTA.
- Confirm completed/superseded retention remains batch bounded across all four
  kinds and never selects a pending candidate.
- Review the all-`claude_code` enqueue choice against production candidate
  volume; the payload is bounded to projection fields, not body size and is
  reclaimed after the configured retention interval.
- Verify the golden fixture is additive: the new named sequence isolates
  lifecycle metadata and `active`/`is_subagent` lineage rows, without
  re-baselining existing keys.
