# Handoff-thread linking v1 handoff

## Delivered scope

Implemented read-time-only handoff thread links for Claude Code conversation
detail responses. The working tree is on `main`; unrelated pre-existing
artifact/generated-file changes were preserved.

`GET /api/conversations/{conversation_ref}` now conditionally includes either
or both of these fields, and list endpoints remain unchanged:

```json
{
  "handoff_predecessor": {
    "document_id": "uuid",
    "tool_id": "claude_code",
    "title": "Conversation title",
    "canonical_url": "/conversations/claude/<session-id>"
  },
  "handoff_successor": { "...same shape...": "..." }
}
```

Absent links are omitted rather than emitted as null. The web detail header
renders `Continued from <title>` for a predecessor and a `Handed off → <title>`
terminal chip for a successor. At the actual end of a loaded transcript
(`hasMore` is false), a subtle `Continue reading →` link appears for the
successor. All three use Next `Link` navigation and therefore retain normal
client-side app state.

## Read-time design

Detection is contained in `server/server/api/conversations.py`, after the
existing detail data has been assembled. It does not write or backfill any
stored conversation data.

- Successor to predecessor: read exactly the earliest normalized user row,
  ordered by `(line_number, id)` with `LIMIT 1`. Its content must begin with
  `MEMENTO-HANDOFF-FROM:` and contain a complete UUID on that marker line.
  A valid ID resolves only to a visible `claude_code` conversation whose
  `relative_path` ends in `<uuid>.jsonl` (both slash styles are normalized).
- Predecessor to successor: derive the viewed thread UUID from its
  `<uuid>.jsonl` path. Use one candidate query over
  `to_tsvector('simple', conversation_messages.content) @@
  plainto_tsquery('simple', :uuid)`, which matches the existing
  `idx_conv_msg_content_fts` expression. The query satisfies the existing
  partial-index predicate with `role = 'user'`; it also limits candidates to
  Claude Code conversation rows, `line_number <= 3`, an exact marker prefix,
  the current user's machine scope, and `LIMIT 64`. Python then strictly
  validates the full marker before returning a successor.

This makes the reverse probe a GIN-index lookup of UUID terms followed by a
small bounded candidate check—no unbounded content scan, new index, table,
migration, schema change, or persistent cache. The `line_number <= 3` window
is intentional: it accommodates Claude JSONL opening records while keeping the
probe bounded. Caching is only the natural request-local ORM/session state.

## Tests run

Commands below used PowerShell 7, the task database URL, the requested Python
environment, and `--basetemp` under the repository where applicable.

```powershell
$env:MEMENTO_TASK_TEST_DATABASE_URL = 'postgresql+asyncpg://postgres:test@localhost:55437/postgres'
& 'C:\Users\intpa\OneDrive\Documents\test\memento-canvas-fix\.venv\Scripts\python.exe' -m pytest tests/test_handoff_thread_links_api.py --basetemp ..\.pytest-basetemp\handoff-thread-links-api-report -q
# 4 passed (JUnit: tests=4, failures=0, errors=0, skipped=0)

& 'C:\Users\intpa\OneDrive\Documents\test\memento-canvas-fix\.venv\Scripts\python.exe' -m pytest tests/test_realtime_ingest_projector.py --basetemp ..\.pytest-basetemp\handoff-thread-links-projector-report -q
# 13 passed (JUnit: tests=13, failures=0, errors=0, skipped=0)

& 'C:\Users\intpa\OneDrive\Documents\test\memento-canvas-fix\.venv\Scripts\python.exe' -m pytest tests/test_realtime_ingest_parity.py --basetemp ..\.pytest-basetemp\handoff-thread-links-parity-report -q
# 8 passed, 1 pytest-cache warning (302.80s)

& 'C:\Users\intpa\OneDrive\Documents\test\memento-canvas-fix\.venv\Scripts\python.exe' -m pytest tests/test_realtime_ingest_drain.py --basetemp ..\.pytest-basetemp\handoff-thread-links-drain-report -q
# 6 passed, 1 pytest-cache warning (1.31s)

npx tsc --noEmit
# passed

npx playwright test e2e/handoff-thread-links.spec.mjs
# 2 passed: desktop and mobile

cargo clippy --all-targets
# passed, zero warnings

cargo build --no-default-features
# passed
```

The focused API coverage exercises predecessor-to-successor, successor-to-
predecessor, marker-absent, and malformed-UUID cases against PostgreSQL. The
hermetic Playwright mock-router coverage asserts both header affordances and
the continue-reading banner, follows the banner with client-side navigation,
and measures the desktop/mobile header geometry so the links do not overlap
the export control or exceed the viewport.

## Explicit non-changes

No conversation parser, ingest service, raw writer, drain, projector,
normalized message output, realtime parity golden, migration, database schema,
or index was changed. In particular,
`server/tests/fixtures/realtime_ingest_parity_golden.json` and all
`tests/test_realtime_ingest_*.py` files remain untouched; the three existing
realtime-ingest suites above passed.

## Reviewer scrutiny list

- Confirm the production planner chooses `idx_conv_msg_content_fts` for the
  reverse UUID probe on a representative large corpus (`EXPLAIN ANALYZE`), as
  that is the intended cost guardrail.
- Check any Claude path variants still terminate in the UUID `.jsonl` form;
  non-matching paths deliberately produce no reverse link.
- Confirm the v1 deterministic first returned successor is acceptable if a
  malformed workflow creates multiple successor documents for one predecessor.
- Verify authorization expectations: both resolution paths apply the existing
  visible-machine scope before exposing a linked document.
- Smoke-test long titles and narrow mobile widths in the deployed browser;
  the automated geometry checks cover the current layout, including the prior
  pin/export overlap lesson.
