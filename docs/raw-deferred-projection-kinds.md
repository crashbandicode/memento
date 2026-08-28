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
replaces the named CHECK constraint.

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
newest revision matches the document's current delivery revision.

`_apply_subagent_lifecycle` feeds the same bounded records to
`_reconcile_subagent_document_lifecycle`. The established
`reconcile_child_lifecycle_metadata` terminal-regression guard remains the
authority, so a late non-terminal observation does not regress a completed
Claude child.

Both kinds preserve the existing candidate lifecycle: completed rows whose
revision is no longer current are marked superseded; a current candidate
applies once and leaves its newest row completed but unsuperseded. Unknown
kinds still follow the existing supersede branch.

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
# 2 failed, 12 passed, 2 warnings in 332.43s (0:05:32)
# Known clean-main baseline failures only:
# test_deferred_projections_flag_defaults_off
# test_deferred_ingest_live_fields_match_golden[True-raw]

& '..\.venv\Scripts\python.exe' -m pytest tests/test_realtime_ingest_parity.py --basetemp ..\.pytest-basetemp\rawfollowons-parity-full -q
# 16 passed, 2 warnings in 542.00s (0:09:01)

& '..\.venv\Scripts\python.exe' -m pytest tests/test_realtime_ingest_drain.py --basetemp ..\.pytest-basetemp\rawfollowons-drain -q
# 7 passed in 2.60s

& '..\.venv\Scripts\python.exe' -m pytest tests/test_claude_lineage.py --basetemp ..\.pytest-basetemp\rawfollowons-claude-lineage -q
# 12 passed in 65.35s (0:01:05)

& '..\.venv\Scripts\python.exe' -m pytest tests/test_subagent_lifecycle_reconciliation.py --basetemp ..\.pytest-basetemp\rawfollowons-subagent-lifecycle -q
# 11 passed in 0.77s
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

- Confirm startup migration ordering is acceptable for the production API boot
  sequence and older Phase 4 tables acquire `payload` before raw writers run.
- Inspect the source-field allow-list to ensure it stays sufficient for
  lineage/lifecycle while never retaining transcript content in the outbox.
- Confirm the grouped current-revision fence only replays a contiguous pending
  raw chain, then supersedes older candidate rows exactly once.
- Review the all-`claude_code` enqueue choice against production candidate
  volume; the payload is bounded to projection fields, not body size.
- Verify the golden fixture is additive: the new named sequence isolates
  lifecycle metadata and `active`/`is_subagent` lineage rows, without
  re-baselining existing keys.
