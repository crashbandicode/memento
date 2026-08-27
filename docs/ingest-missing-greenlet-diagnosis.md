# Diagnosis: `MissingGreenlet` in `reconcile_message_canvases` during authoritative-rebase ingest

- **Symptom**: `POST /api/ingest/file/upload` 500s ~4–7×/10 min with
  `sqlalchemy.exc.MissingGreenlet` at `server/services/canvas_artifact_store.py:127`
  (`int(message.id)` inside the `message_by_id` dict-comprehension of
  `reconcile_message_canvases`). App frames: `api/ingest.py:979 → 481` →
  `ingest_service.py:3729 ingest_file → 5405 _extract_messages →
  canvas_artifact_store.py:127`; SQLAlchemy tail `_load_expired →
  load_scalar_attributes → load_on_pk_identity` (a **fully-expired** instance).
- **Status**: Root cause found, reproduced deterministically, and **two fixes
  empirically validated**. Not yet applied (per task).
- **Verdict**: **PRE-EXISTING bug, not a Phase 4 regression.** Reproduces
  identically on `0bef7c3461` (previous commit) and `89f5f35f36` (HEAD).

Repository HEAD when diagnosed: `89f5f35f36` (Phase 4, flag-off). SQLAlchemy 2.0.52,
asyncpg 0.31.0.

---

## 1. Root cause (one sentence)

The full/authoritative-rebase suffix-comparison path loads existing
`ConversationMessage` rows with a `load_only(...)` set that **omits
`document_id`**, then issues an ORM-enabled
`delete(ConversationMessage).where(document_id == doc.id, line_number >= line_num)`
with the **default `synchronize_session="auto"` → "evaluate"**. Because the
DELETE's WHERE references the *deferred* `document_id`, SQLAlchemy's in-Python
evaluator cannot decide match/no-match and returns `_EXPIRED_OBJECT` for **every**
loaded `ConversationMessage`; `_do_post_synchronize_evaluate` then calls
`state._expire(...)` on each — **fully expiring the mutated row that was already
appended to `canvas_reconcile_rows`**. When `reconcile_message_canvases` later
reads `message.id` on that expired row (a plain attribute access, not awaited),
the identity refresh is emitted outside the async greenlet → `MissingGreenlet`.

The expiry is caused by the **in-transaction DELETE**, not by any commit —
`expire_on_commit` is irrelevant (see §5).

## 2. The precise mechanism (code walk, HEAD line numbers)

`_extract_messages` (`server/server/services/ingest_service.py`), `full_prefix_intact`
path (reached when `preserve_full_rebase` is true — see §6):

1. **5244–5268** — existing rows fetched in ≤256-row blocks with
   `load_only(id, line_number, message_type, role, content, metadata_, timestamp)`.
   **`document_id` is NOT in this list**, so on every returned instance
   `document_id` is a deferred/unloaded attribute.
2. **5291–5308** — a row whose stored `source_id` equals the incoming `source_id`
   but whose content differs is **mutated in place** (`existing_message.content = …`,
   5299–5303, this succeeds → the instance is live here) and **appended to
   `canvas_reconcile_rows`** (5306), then `continue`.
3. **5309–5319** — a *later* differing row without a `source_id` match executes
   `await db.execute(delete(ConversationMessage).where(document_id == doc.id,
   line_number >= line_num))` (default `synchronize_session`), then
   `full_prefix_intact = False`. (An equivalent DELETE also runs at **5366–5372**.)
4. Inside SQLAlchemy that DELETE runs
   `bulk_persistence._do_post_synchronize_evaluate` →
   `_get_matched_objects_on_criteria` (bulk_persistence.py:928). For each
   non-expired mapped instance it calls `eval_condition(obj)`; because
   `document_id` is unloaded, the evaluator returns `evaluator._EXPIRED_OBJECT`,
   which is treated as a match with `is_partially_expired=True`, so
   `state._expire(dict_, session.identity_map._modified)` (bulk_persistence.py:2107)
   **fully expires the instance** — including the mutated row from step 2 (line 1),
   which does **not** even satisfy `line_number >= line_num`.
5. **5405** — `await reconcile_message_canvases(db, doc, canvas_reconcile_rows)`.
6. **canvas_artifact_store.py:124–128** — `int(message.id)` on the fully-expired
   row triggers `state._load_expired → load_on_pk_identity` synchronously, outside
   the greenlet → `MissingGreenlet`.

Note: rows in `canvas_reconcile_rows` are always at `line_number < line_num`
(they were appended before the DELETE and line numbers are processed in
increasing order), so they are collateral casualties of the evaluate-sync, never
the intended DELETE targets — i.e. they are expired, not expunged, which is
exactly why the failure is `MissingGreenlet` (lazy refresh) and not
`DetachedInstanceError`.

## 3. Expiry culprit stack (captured by instrumentation)

Patching `InstanceState._expire` and filtering to `ConversationMessage` shows the
mutated line-1 row is expired here (identical on both commits):

```
sqlalchemy/orm/session.py:2373  execute
sqlalchemy/orm/session.py:2271  _execute_internal
sqlalchemy/orm/bulk_persistence.py:2049  orm_execute_statement
sqlalchemy/orm/context.py:309   orm_setup_cursor_result
sqlalchemy/orm/bulk_persistence.py:832  orm_setup_cursor_result
sqlalchemy/orm/bulk_persistence.py:2107 _do_post_synchronize_evaluate → state._expire(...)
```

Reconcile-entry snapshot of the offending row: `{expired: True, id_loaded: False}`.

## 4. Reproduction

New test (does not modify production code):
`server/tests/test_ingest_expired_reconcile_repro.py`.

Run:

```
$env:MEMENTO_TASK_TEST_DATABASE_URL='postgresql+asyncpg://postgres:test@localhost:55437/postgres'
& 'C:\Users\intpa\OneDrive\Documents\test\memento-canvas-fix\.venv\Scripts\python.exe' -m pytest `
  server/tests/test_ingest_expired_reconcile_repro.py -q -s `
  --basetemp='C:\Users\intpa\OneDrive\Documents\test\memento-control-plane\.pytest-basetemp\repro'
```

Scenario (`tool_id="claude_code"`, modeling one request per session, the real
`get_db` value `expire_on_commit=False`):

1. Session A — FULL ingest of 3 rows (`user-1` carries a `.canvas.tsx` link, plus
   `asst-2`, `asst-3`); commit; close.
2. Session B (fresh identity map) — FULL ingest with `authoritative_rebase=True`
   where line 1 keeps `source_id=user-1` with **changed** content (→ suffix-compare
   update + append to `canvas_reconcile_rows`), line 2 has a **different**
   `source_id` + changed content (→ fires the DELETE), lines 3–4 append.

Result: `MissingGreenlet` at `reconcile_message_canvases` — exact production
symptom. Test asserts `reproduced is True`, that a row is expired at reconcile
entry, and that `_do_post_synchronize_evaluate` is in the expiry stack.

## 5. `expire_on_commit` — the premise in the task brief is wrong

The brief states production `get_db` uses `expire_on_commit=True` and that the
`expire_on_commit=False` test fixtures "mask" the bug. Both claims are incorrect
at this codebase:

- `server/server/db/session.py:150-154` — `async_session_factory` (the factory
  `get_db` uses) has been `expire_on_commit=False` **since the initial commit**
  (`git log -L` on those lines confirms it was never `True`).
- The reproducer parametrizes `expire_on_commit ∈ {False, True}` and reproduces in
  **both** cases, because the expiry comes from the in-transaction DELETE, not a
  commit (no commit occurs inside `ingest_file` before line 5405). `expire_on_commit`
  is a red herring for this bug.

What actually kept this out of the existing test suite is that integration tests
reuse **one** session across the initial ingest and the rebase, so the mutated row
is served from the live identity map and re-hydrated by the block SELECT; the bug
needs a **fresh per-request session** (production behavior) so the rebase's DELETE
is the first thing to touch those instances. The reproducer models that faithfully.

## 6. Tool clarification (`codex` vs `claude_code`/`cursor`)

The `full_prefix_intact` path is gated by
`preserve_full_rebase = mode != "delta" and tool_id in {"claude_code","cursor"} and
not user_history` (`ingest_service.py:4765-4773`), and `canvas_reconcile_rows` is
only populated for **claude_code/cursor** (append sites 5306 and, for
`cursor_state_v1` projection deltas, 5119). **Codex cannot reach the crashing
5405 path** (it never appends to `canvas_reconcile_rows`). So the 500ing requests
are **claude_code or cursor FULL authoritative rebases**, even though the
triggering traffic was described as "codex". Also note `mode` is `"full"` for this
path (an authoritative rebase is uploaded as a FULL — `api/ingest.py:960/990`), not
a literal `mode="delta"`. The reproducer uses `claude_code` + `mode="full"` +
`authoritative_rebase=True`, matching the code path exactly.

## 7. Bisect

`git worktree add ../mcp-bisect 0bef7c3461`, copied the reproducer in, ran it:
**reproduces identically** (`reproduced=True`, same `_do_post_synchronize_evaluate`
expiry, both `expire_on_commit` values). Worktree removed afterward.

Confirmed structurally: on `0bef7c3461` the suffix-compare `load_only` (omitting
`document_id`) is at 5200-5203, the DELETEs at 5262/5317, and the reconcile call at
5353 (unconditional). Phase 4's diff to `ingest_service.py`, with the flag
`realtime_ingest_deferred_projections=False` (its default), leaves the reconcile
call site behaviorally identical (it only wraps it in `if not
deferred_projections_enabled():`) and does not touch the `load_only` or the
DELETEs. **Conclusion: pre-existing latent bug**, newly *exposed* by the
authoritative-rebase traffic that began ~11:45 UTC, not introduced by the 12:52
UTC Phase 4 deploy.

## 8. Proposed fix (do not apply yet)

Both options below are **validated** by the reproducer (each makes `reproduced` go
`False` with zero `ConversationMessage` expiries).

**Preferred — Option A: load `document_id` in the ConversationMessage `load_only`
sets.** Add `ConversationMessage.document_id` to the `load_only(...)` options at
`ingest_service.py:5250-5258` (the suffix-compare fetch) and, for consistency /
defense-in-depth, the sibling sets at 4855-4863, 4901-4907 and 4995-5003. This
fixes the actual defect: the evaluate-sync can now reason about the rows, so it
expunges **only** the rows that truly match (`line_number >= line_num`) and leaves
the reconcile row live. It is the smallest change that also hardens against any
future ORM UPDATE/DELETE on `ConversationMessage` filtered by `document_id`.
Validated by `test_load_document_id_prevents_missing_greenlet`.

**Alternative — Option B: `synchronize_session=False` on the two full-path
DELETEs** (`ingest_service.py:5311-5316` and `5366-5372`), e.g.
`db.execute(delete(...).execution_options(synchronize_session=False))`. These
DELETEs remove committed rows the function is replacing; the code already resets
its own view (`full_existing_rows.clear()`, `full_prefix_intact=False`) and never
touches the deleted instances again, and new rows are Core-inserted with fresh
identities — so ORM session synchronization here is both unnecessary and the source
of the collateral expiry. Validated by
`test_synchronize_session_false_prevents_missing_greenlet`.

Not recommended as the primary fix: "pass IDs not instances to reconcile" or
"re-`select` the rows before 5405" both work but only paper over the fact that a
live, in-`canvas_reconcile_rows` instance is being silently expired mid-transaction
— Option A removes that surprise at the source.

## 9. Tests that should gate the fix

- New: `server/tests/test_ingest_expired_reconcile_repro.py`
  (`test_authoritative_rebase_reconcile_missing_greenlet` must **fail** pre-fix and
  **pass** post-fix; the two `test_*_prevents_missing_greenlet` cases document the
  fix behavior — after the real fix lands, they can be simplified/removed).
- Existing regression coverage to keep green:
  `server/tests/test_streaming_ingest_integration.py`
  (`test_streamed_ingest_projects_transactionally_and_rebases` — full+rebase path,
  canvas references) and `server/tests/test_canvas_artifact_store.py`.

## 10. Residual notes / risks

- Option B leaves the expunged rows (lines ≥ `line_num`) as stale persistent
  instances in the identity map for the remainder of the transaction. This is safe
  here (nothing re-reads them; their PKs are being deleted; replacement rows get new
  PKs), but Option A avoids even that by keeping the ORM's bookkeeping correct.
- The same `load_only`-omits-`document_id` pattern exists on the delta-tail/queue
  fetches (4855, 4901) and cursor projection fetch (4995). They are not on the
  observed crashing path today, but any ORM UPDATE/DELETE added there that filters
  on `document_id` would hit the identical trap — Option A closes them all.
