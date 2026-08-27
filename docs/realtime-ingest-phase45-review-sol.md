# Realtime ingest Phase 4.5 review (Sol)

Date: 2026-08-27

Scope: the uncommitted Phase 4.5 fallback-reduction changes on top of
`fd916032f74d6fe9542a0151194d4f0a0748e97c`. This was a code-only review of
the raw history shortcut, title port, chain fallback behavior, and outcome
telemetry. Per instruction, I did not rerun any test suite and did not change
implementation or test files.

## Verdict

**DO-NOT-SHIP these changes to production as a bundle.**

The title port and transaction rollback structure are sound, but the Codex
history predicate is not a valid proof that the legacy history branch would be
a no-op. A normal Codex user frame can pass the predicate while legacy would
delete or reposition a recovered history row. The raw path instead retains the
row, producing a different normalized transcript and downstream projections.

## Findings

### BLOCKER — matching stored history fields does not prove legacy reconciliation is a no-op

Files: `server/server/services/realtime_raw_writer.py:425`, `:449`, `:469`,
`:572`, and `:749`; `server/server/services/ingest_service.py:1869`, `:1927`,
`:1948`, `:5404`, `:5419`, `:5502`, and `:5554`;
`server/server/services/history_recovery.py:24`.

`_history_metadata_is_already_committed` looks only for an exact stored
`history_user_message` with the expected source ID, normalized content, and
timestamp (`realtime_raw_writer.py:449-477`). It runs before the incoming
JSONL is reduced into message mutations (`:572` versus `:749`) and does not
consider ordinary user rows already in the document or ordinary user rows the
current frame will add.

The legacy branch has additional behavior even when every incoming history
source ID already exists. It stages the current frame's message batch first
(`ingest_service.py:5404-5410`), skips reinserting an existing history source
ID at `:5501-5503`, but still unconditionally calls
`_reconcile_recovered_history_rows` at `:5554`. That reconciler compares all
stored history rows with all ordinary user rows using the one-to-one,
five-minute transport matching semantics (`:1927` and
`history_recovery.py:24-35`), deletes matched recovered rows at
`ingest_service.py:1948-1950`, and places surviving negative rows into the
positive presentation timeline.

A concrete supported divergence is therefore:

1. The document contains an exact `history_user_message` for
   `codex-history:N`.
2. A DELTA repeats that unchanged history metadata and contains the ordinary
   Codex user rollout message with the same normalized content and a timestamp
   inside the recovery window.
3. The Phase 4.5 proof passes before parsing the DELTA. Raw ingest inserts the
   ordinary user row and leaves the recovered row in place.
4. Legacy ingest stages the ordinary user row, matches the recovered row to
   it, and deletes the recovered duplicate before refreshing read/prompt
   projections.

Raw and legacy then differ in committed messages, line ordering/counts, read
model, prompts, search input, and potentially activity. The same flaw applies
to a surviving negative history row that legacy would place: the new tests
manually seed such a row at
`server/tests/test_realtime_ingest_parity.py:939-944`, but the history half of
that test runs only raw ingest and therefore does not compare the legacy line
mutation.

This must be fixed at the proof boundary. Either port the legacy occurrence
partition/reconciliation semantics using current plus prospective message
state, or retain legacy fallback whenever the frame/chain could cause a
recovered row to match, be removed, or be placed. Add parity coverage where an
incoming ordinary Codex user message matches an exact stored history entry;
compare the full message/read/prompt state, not only the raw disposition.

### SHOULD-FIX — the all-recovered-row requirement can preserve the dominant fallback storm

Files: `server/server/services/realtime_raw_writer.py:449-479`;
`server/server/services/ingest_service.py:5482-5518` and `:5554`.

The shortcut returns false if any usable incoming history entry lacks a
persisted `history_user_message`. Missing rows are not necessarily new or
changed history. Legacy deliberately avoids inserting a history entry already
represented by an ordinary user occurrence (`ingest_service.py:5506-5518`),
and reconciliation later deletes recovered rows that acquire such a source
match (`:5554`, via `:1948-1950`).

Consequently, once even one entry in the collector's repeated full history
list is represented by the normal Codex transcript, every later repeat fails
the all-entry proof. Legacy takes the same dedup path again, no recovered row
is created for that source ID, and the next frame falls back again. The
investigation establishes that Codex attaches the complete history on every
session emission, so this is the common benign shape the Phase 4.5 change is
intended to remove, not merely malformed input.

This conservatism is safe in isolation, but it makes the claimed reduction of
the dominant approximately 72% reason unproven and can leave the Phase 5 gate
unreachable. Resolve it together with the BLOCKER using the legacy one-to-one
source/recovered occurrence semantics. The post-`_prepare_document_metadata`
ordinal itself is correct: both raw and legacy enumerate the same filtered
list, and a later second-normalization skip consumes the same index in both
loops.

### SHOULD-FIX — the outcome log lacks the denominator for the literal frame-percentage gate

File: `server/server/tasks/ingest_spool.py:68`, `:104`, `:109`, `:125`, and
`:246`; supporting dispositions at
`server/server/services/realtime_raw_writer.py:597`, `:613`, `:639`, and
`:644`.

The counters correctly distinguish their named outcomes: the fallback counter
is recorded in the exception branch at `ingest_spool.py:203-206`, that branch
returns before the raw counter, and raw is counted only when the terminal
disposition is exactly `committed` at `:246-247`. The reason set is bounded to
16 named reasons plus `other/raw-writer-unsupported`, and the `elapsed` value
plus per-reason chain counts are enough to compute the `<1/min` rate.

They are not enough to compute each reason as an exact percentage of all
drained frames. Raw `idempotent`, `stale_delta`, and `superseded` outcomes are
intentionally not labeled as raw commits, but no total-handled or
non-committed-frame field is logged either. The only available denominator is
`raw_committed_frames + sum(legacy_fallback_frames_by_reason)`, which excludes
those successfully drained raw frames. This matters particularly because the
Phase 5 gate includes a restart/recovery drill, where idempotent replay is an
explicit supported outcome.

Add `total_chains`/`total_frames` (or disposition-specific raw handled counts)
to the same bounded window before treating this record as sufficient evidence
for the literal `<2% of frames` gate. Alternatively, redefine and document the
gate as a percentage of committing writer attempts, but that is not the
investigation's stated drained-frame denominator.

### NOTE — newly admitted shapes extend the known raw flag-off projection gap

Files: `server/server/services/realtime_raw_writer.py:884` and `:1332`;
`server/server/services/ingest_service.py:3758` and `:3765`.

With deferred projections enabled, the title port passes real previous/current
titles into search candidacy and enqueues correctly. With the flag disabled,
the raw path still does not synchronously refresh FTS. Before Phase 4.5,
title-bearing and history-bearing frames fell back to legacy, whose flag-off
path performs the synchronous refresh. Accepting those shapes natively thus
extends the previously documented raw flag-off search gap.

Production is already flag-on, so this is not the deployment BLOCKER above.
It should remain an explicit rollback constraint: disabling deferred
projections while retaining these raw fallbacks reductions no longer restores
the old projection behavior for the newly supported shapes.

### NOTE — new-document project resolution remains a careful-design fallback

Files: `server/server/services/realtime_raw_writer.py:617` and `:668`;
`server/server/services/ingest_service.py:3442` and `:3447`.

I agree with the Task 3 classification. The drain's DELTA path rejects a
missing existing FULL before reaching project resolution. The reachable raw
FULL/new-document shape would have to reproduce shared project upsert/repair,
payload and metadata path precedence, and machine/user-scoped session lookup.
That is cross-row identity work, not a local reducer selection, and should
remain legacy until it has a dedicated transaction design and parity fixture.

## Cleared scrutiny

### Fallback-before-mutation — clear

Single-frame ingest loads history read-only, evaluates the proof in
`reduce_writer_state`, and calls `_apply` only after the reducer returns. Any
exception rolls back the transaction at
`realtime_raw_writer.py:1519-1526`.

For chains, `load_recovered_history` is computed across all prepared frames at
`:1656-1660`, so a later metadata-bearing frame causes the recovery rows to be
available on each state load. Non-combined frames reread state at `:1765-1774`
inside the same transaction and therefore see earlier in-transaction writes.
If a later proof fails, the outer rollback at `:1809-1816` removes every raw
write from earlier frames before the drain enters legacy fallback. This part
is correct; it does not cure the proof's semantic omission.

### Title selection and propagation — clear for the named selector

Raw imports the legacy `_select_updated_document_title` rather than copying
its precedence rules (`realtime_raw_writer.py:546`, `:683-690`). It uses the
same prepared incoming title or basename fallback, carries explicit
`claude_ai_title` provenance through the stored-metadata merge, and compares
the real previous and selected titles for search candidacy at `:884-889`.

`title_changed` is derived from those values at `:915`, persistence is guarded
by `IS DISTINCT FROM` at `:1224-1229`, the current title feeds dashboard values
through the document view at `:1264-1274`, and SSE receives the flag at
`:1323-1328`. The explicit-Claude integration comparison at
`server/tests/test_realtime_ingest_parity.py:1049-1057` verifies final title
and deferred search-candidate parity. The separate friendly-title derivation
remains outside this mechanical selector port, as classified in the
investigation.

### Counter boundedness and branch attribution — clear apart from the denominator finding

The 60-second/100-chain trigger is an either-threshold window
(`ingest_spool.py:116-124`). At most 16 exact reasons are retained; later
unknown reasons enter one other bucket (`:97-102`). Raw committed and legacy
fallback chain/frame numerators are attributed on mutually exclusive control
paths. The missing total-frame count is the only telemetry defect found.

## Production decision

Do not deploy until the history shortcut accounts for legacy recovered/source
reconciliation and a parity test proves the matching-user-frame case. The
telemetry denominator should also be corrected before its percentage is used
as the Phase 5 retirement gate.

**Final verdict: DO-NOT-SHIP the Phase 4.5 fallback-reduction changes to
production.**
