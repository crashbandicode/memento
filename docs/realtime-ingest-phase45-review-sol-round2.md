# Realtime ingest Phase 4.5 review, round 2 (Sol)

Date: 2026-08-27

Scope: Revision 2 of the uncommitted Phase 4.5 changes on top of
`fd916032f74d6fe9542a0151194d4f0a0748e97c`, reviewed against the three
findings in `docs/realtime-ingest-phase45-review-sol.md`. This was a code-only
review. Per instruction, I did not rerun test suites and did not change
implementation or test files.

## Verdict

**SHIP the Phase 4.5 changes to production.**

Revision 2 clears the prior BLOCKER and both SHOULD-FIX findings. I found no
new BLOCKER or SHOULD-FIX. The documented flag-off rollback constraint remains
a NOTE and does not block the stated flag-on production deployment.

## Prior findings

### CLEARED — the history proof now rejects every legacy insert/delete/place case

Files: `server/server/services/realtime_raw_writer.py:352`, `:447`, `:475`,
`:536`, `:557`, `:561`, and `:927`;
`server/server/services/ingest_service.py:1927`, `:1948`, `:5506`, and
`:5554`; `server/server/services/history_recovery.py:24`.

The proof now runs after `reduce_writer_state` has built the prospective
message mutations (`realtime_raw_writer.py:927-934`), while still running
before `_apply` performs database writes. Its committed ordinary-user input is
loaded with the same legacy role/message-type scope and line order at
`:345-358`: `role = 'user'` and message type distinct from
`history_user_message`.

Both legacy occurrence comparisons use the shared
`partition_recovered_occurrences` helper, including its same-content,
one-to-one, five-minute matching behavior; no matching logic was copied:

- For incoming source-ID-missing history entries, raw calls the helper at
  `:536-539` and falls back if even one entry remains unmatched at `:540-541`.
  Therefore raw accepts exactly the no-insert side of the legacy missing-gap
  partition at `ingest_service.py:5506-5518`.
- Raw independently partitions every stored recovered row against the same
  ordinary-user occurrence set at `realtime_raw_writer.py:547-560`. Any
  `matched_recovered` makes the proof fail at `:561`, matching the legacy
  deletion performed through `_reconcile_recovered_history_rows`
  (`ingest_service.py:1927`, `:1948-1950`, and call at `:5554`).
- Any stored recovered row below line 1 also makes the proof fail at
  `realtime_raw_writer.py:561-566`, covering the legacy placement mutation.
- Existing source IDs still require exact normalized content and timestamp at
  `:503-530`; this is safely stricter than legacy's source-ID-only skip.

The earlier false-accept sequence is therefore closed. If the current DELTA
adds an ordinary user occurrence that would make legacy delete a stored
recovered row, `matched_recovered` is non-empty and raw falls back. If no
stored recovered row exists because an ordinary user already represents the
history entry, the missing-entry partition accepts it and raw can proceed.

### CLEARED — prospective updates model legacy post-staging state

File: `server/server/services/realtime_raw_writer.py:466`.

The prospective occurrence map begins with all committed ordinary user rows
at `:466-474`. For every update mutation, it removes the pre-update row by
`existing_id` at `:475-477`. It then adds the mutation's post-update occurrence
only when the replacement role is `user` and the replacement message type is
not `history_user_message` (`:478-497`). Inserts are added under distinct
ordinal keys by the same block.

That ordering matches legacy staging: after an update from user to non-user,
the old occurrence is absent; after non-user to user or user to changed user,
the replacement content/timestamp is present. The proof therefore observes
the same ordinary-user state that legacy queries after staging and before its
history partitions. I found no remaining pre/post-update inversion.

### CLEARED — occurrence-represented repeats no longer cause the fallback storm

Files: `server/server/services/realtime_raw_writer.py:509` and `:536`;
`server/tests/test_realtime_raw_writer.py:279`;
`server/tests/test_realtime_ingest_parity.py:1238`.

An incoming history entry without a stored recovery row is collected as a
missing occurrence at `realtime_raw_writer.py:509-521`, then accepted when the
shared partition proves it one-to-one represented by an ordinary user row at
`:536-541`. The all-persisted-recovery-row requirement from Revision 1 is
gone.

Focused reducer coverage confirms that shape commits raw
(`test_realtime_raw_writer.py:279-304`). The integration parity case seeds the
ordinary-user representation, applies the history repeat through raw and
legacy, and compares full message/read-model/prompt snapshots
(`test_realtime_ingest_parity.py:1238-1290`). This covers the benign repeated
full-history shape that previously could fall back indefinitely.

### CLEARED — history-bearing chains remain frame-sequential

File: `server/server/services/realtime_raw_writer.py:1744`.

`load_recovered_history` is the `any(...)` of history metadata across every
prepared frame at `:1744-1748`. `can_combine` now explicitly requires that
value to be false at `:1779-1781`, so even identical history metadata cannot
enter same-metadata content combining.

The existing sequential loop retains prepared order at `:1846`, rereads state
inside the open transaction for each frame at `:1850-1862`, reduces exactly
that frame at `:1864-1882`, and applies it once at `:1884`. Consequently:

- a later ordinary user cannot justify an earlier history proof;
- an earlier ordinary user is visible when a later history-bearing frame is
  proved;
- failure in any later proof still rolls back all earlier in-transaction raw
  writes before legacy replay.

I found no reorder or double-apply path introduced by the sequentialization.

### CLEARED — the drain log now has the exact frame denominator

File: `server/server/tasks/ingest_spool.py:92`, `:107`, `:110`, `:121`,
`:137`, `:216`, and `:259`.

Every recorded raw terminal disposition calls `_record_handled` first at
`ingest_spool.py:107-115`; only `committed` additionally increments the raw
commit numerator. Thus raw `idempotent`, `stale_delta`, and `superseded`
chains/frames are included in `total_handled_*` without being mislabeled as
raw commits.

The mutually exclusive legacy branch records one handled fallback outcome at
`:216-219` and returns at `:234` before the raw terminal call at `:259-262`.
The normal raw branch calls `record_raw_handled` once. The log emits
`total_handled_chains` and `total_handled_frames` beside the existing raw and
per-reason fallback numerators at `:135-150`. Per-reason fallback frames can
therefore be divided by the literal handled/drained frame total, while the
per-reason chain count divided by logged elapsed time supplies the rate.

The focused counter test includes an idempotent raw outcome and asserts the
full denominator (`server/tests/test_realtime_ingest_drain.py:195-227`).

## Regression coverage

The new parity test exercises the exact prior BLOCKER at
`server/tests/test_realtime_ingest_parity.py:1061-1226`: a prospective ordinary
Codex user matches a stored recovered row, direct raw correctly raises
`RawWriterUnsupported`, raw fallback and legacy converge, and full
message/read-model/prompt state is equal with the recovered duplicate removed.

The converse case at `:1228-1290` proves an already ordinary-user-represented
history entry commits raw and remains equal to legacy. The reducer regression
at `server/tests/test_realtime_raw_writer.py:259-277` proves a negative stored
recovery row falls back for legacy placement. Together these cover insert,
delete, and place decisions rather than only checking disposition.

The reported Revision 2 runs were green (raw 9, drain 6, spool 42 plus 16
subtests, parity 8, projector 13, expired-reconcile 5), and the parity golden
was untouched. I did not rerun them.

## Remaining notes

- The flag-off synchronous-search limitation is now explicitly documented in
  `docs/realtime-ingest-phase45-handoff.md` under **Rollback constraint**.
  Production is already using deferred projections, so it is an operational
  rollback constraint rather than a defect in this deployment.
- New-document project resolution remains correctly deferred as careful-design
  work; Revision 2 does not broaden that path.

## Production decision

No BLOCKER or SHOULD-FIX finding remains from the prior review, and no proven
new defect was introduced by Revision 2.

**Final verdict: SHIP the Phase 4.5 changes to production.**
