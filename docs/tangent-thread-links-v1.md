# Tangent-thread linking v1

## Delivered scope

Implemented read-time-only tangent-thread links for Claude Code conversation
detail responses. Tangents remain independent primary threads and list/dashboard
surfaces are unchanged.

`GET /api/conversations/{conversation_ref}` now conditionally includes either
or both of these fields; list endpoints remain unchanged:

```json
{
  "tangent_parent": {
    "document_id": "uuid",
    "tool_id": "claude_code",
    "title": "Parent conversation title",
    "canonical_url": "/conversations/claude/<session-id>"
  },
  "tangent_branches": [
    { "document_id": "uuid", "tool_id": "claude_code", "title": "…", "canonical_url": "…" }
  ]
}
```

Absent links are omitted, including an empty `tangent_branches` list. A tangent
detail header renders `Branched from <title>` with a distinct fork glyph. A
parent with one tangent renders a `Tangent → <title>` chip; with multiple it
renders an accessible expandable `Tangents (N)` control listing every branch.
The tangent UI deliberately adds no end-of-transcript continuation banner:
that behavior remains handoff-successor-only.

## Read-time design

Detection is contained in `server/server/api/conversations.py`, after the
existing detail payload has been assembled. It does not write or backfill any
conversation data.

- Tangent to parent reads the earliest normalized user row restricted to the
  existing opening window (`line_number <= 3`), then validates
  `MEMENTO-TANGENT-FROM:` with the public `conversation_hierarchy` briefing
  helpers. A valid UUID resolves only to a visible `claude_code` conversation
  whose normalized `relative_path` ends in `<uuid>.jsonl` within the current
  machine scope.
- Parent to tangents derives the viewed thread UUID from its `.jsonl` path and
  performs one candidate query using
  `to_tsvector('simple', conversation_messages.content) @@
  plainto_tsquery('simple', :uuid)`, matching `idx_conv_msg_content_fts`.
  The query uses the existing user-role partial-index predicate, the opening
  `line_number <= 3` bound, exact tangent prefix, Claude Code/document scope,
  and machine scope.
- The reverse probe is capped at 64 candidates. Python strictly revalidates
  each marker, de-duplicates by document ID, and returns every valid tangent
  in `delivery_activity_expression().desc().nulls_last()`,
  `Document.created_at.desc()`, `Document.id.desc()` order. The cap bounds
  both marker validation and de-duplication work; it intentionally does not
  promise to discover a branch beyond the first 64 index candidates.
- Delivery projection activity, not `Document.activity_at`, determines branch
  ordering. Conversation DELTAs advance `document_delivery_state.activity_at`
  while the canonical raw-path document column can remain frozen.
- Handoff and tangent link families are assembled independently, allowing a
  thread to be both a handoff successor and a tangent parent.

## Tests run

Commands used PowerShell 7, the task PostgreSQL URL, and the worktree virtual
environment.

```powershell
$env:MEMENTO_TASK_TEST_DATABASE_URL = 'postgresql+asyncpg://postgres:test@localhost:55437/postgres'
& '..\.venv\Scripts\python.exe' -m pytest tests/test_tangent_thread_links_api.py --basetemp ..\.pytest-basetemp\tangent-api -q
# 5 passed in 111.19s

& '..\.venv\Scripts\python.exe' -m pytest tests/test_handoff_thread_links_api.py --basetemp ..\.pytest-basetemp\tangent-handoff-api -q
# 4 passed in 87.03s

& '..\.venv\Scripts\python.exe' -m pytest tests/test_conversation_hierarchy.py --basetemp ..\.pytest-basetemp\tangent-hierarchy -q
# 50 passed, 6 subtests passed in 1.78s

& '..\.venv\Scripts\python.exe' -m pytest tests/test_realtime_ingest_drain.py --basetemp ..\.pytest-basetemp\tangent-drain -q
# 7 passed in 1.78s

& '..\.venv\Scripts\python.exe' -m pytest tests/test_orchestration_events.py --basetemp ..\.pytest-basetemp\tangent-orchestration-captured -q
# 14 passed in 171.33s

& '..\.venv\Scripts\python.exe' -m pytest tests/test_realtime_ingest_parity.py::test_pending_question_reconciliation_refreshes_dashboard_projection --basetemp ..\.pytest-basetemp\tangent-parity-recheck -q
# 1 passed in 23.96s

npx tsc --noEmit
# passed

node --test tests/*.test.mjs
# 87 passed, 0 failed

npx playwright test e2e/tangent-thread-links.spec.mjs e2e/handoff-thread-links.spec.mjs
# 6 passed in 47.5s
```

`node --test tests/` was also run as requested, but Node 24 treats the
directory argument as a module rather than expanding its contained test files.
It fails before discovering any tests with `Cannot find module ...\\web\\tests`;
the explicit repository test-file glob above is the successful equivalent.

`tests/test_realtime_ingest_parity.py` initially reported one failure after
14 passes: `test_pending_question_reconciliation_refreshes_dashboard_projection`
observed `updated == 4`, rather than its expected `1`. The same task database
contained four stale pending-question conversations, which that test's
reconciliation repaired. Clean main then completed the full suite at `15
passed, 1 warning in 496.84s`; re-running only the failed worktree test against
the now-clean database also passed. This is an order-dependent shared-test-DB
residue effect, not a tangent change.

`tests/test_realtime_ingest_projector.py` remains a known unrelated baseline
failure: this worktree reported `2 failed, 11 passed in 284.86s`, and clean
main at `b7ac030c4170bd72bc728f20a4d7cadf8bf29bac` reproduced the same two IDs:
`test_deferred_projections_flag_defaults_off` and
`test_deferred_ingest_live_fields_match_golden[True-raw]`. Both fail at
`assert mismatch.value.expected_hash == expected_hash` in the raw-writer
legacy-reducer path. No projector, parity, raw-writer, parser, or ingest source
was changed by this feature.

The workspace-required Cargo checks were attempted separately but cannot build
this fresh worktree because a collector packaging prerequisite is absent:

```powershell
cargo clippy --all-targets
cargo build --no-default-features
```

Exact raw failure from `cargo clippy --all-targets`:

```text
error: failed to run custom build command for `memento-app v0.1.73 (C:\Users\intpa\OneDrive\Documents\test\memento-tangent-rendering\tauri-collector\src-tauri)`

Caused by:
  process didn't exit successfully: `C:\Users\intpa\OneDrive\Documents\test\memento-tangent-rendering\tauri-collector\src-tauri\target\debug\build\memento-app-3c4e91fa35097220\build-script-build` (exit code: 1)
resource path `binaries\memento-collector-sidecar-x86_64-pc-windows-msvc.exe` doesn't exist
```

`cargo build --no-default-features` failed the same way, with its distinct
build-script hash `memento-app-33aa8f69ffc3b3d5` and the identical missing
resource-path line.

This missing sidecar binary is an environmental prerequisite, not a tangent
rendering failure; no binary was fabricated or copied into the worktree.

Focused API coverage proves parent resolution, all-branch delivery-state
ordering, absent/malformed-marker omission, and simultaneous handoff/tangent
families. The orchestration-event regression adds the tangent twin of the
handoff no-delegate-stamp case. Browser coverage exercises parent, one-child,
and multi-child rendering across desktop and mobile, including measured header
geometry and the intentional absence of a continuation banner.

## Explicit non-changes

No conversation parser, ingest service, raw writer, drain, projector,
normalized message output, realtime-ingest golden, migration, schema, index,
or dashboard/list behavior changed. In particular, this feature does not alter
marker persistence or delegate stamping; it only consumes existing normalized
message rows at detail-response read time.

## Reviewer scrutiny list

- Confirm production planner use of `idx_conv_msg_content_fts` with a
  representative large corpus (`EXPLAIN ANALYZE`), since that GIN lookup is
  the reverse-probe cost guardrail.
- Review the documented 64-candidate cap for workflows that may create an
  unusually large number of tangents from one parent.
- Confirm all Claude path variants still terminate in UUID `.jsonl` filenames;
  other paths intentionally resolve no reverse links.
- Check machine-scope authorization on both directions before linked document
  metadata is exposed.
- Smoke-test very long branch titles and narrow mobile widths in deployment;
  automated browser tests cover the current no-overlap geometry.
