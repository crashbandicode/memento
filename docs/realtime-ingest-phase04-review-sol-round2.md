# Realtime ingest Phase 4 second review, round 2 (Sol)

Date: 2026-08-27

Scope: the uncommitted blocker-fix working-tree changes on top of
`33ebe3d3d3def80c5aa8c7db981f1437550a4e8d`. This review verifies the two
BLOCKERs and one SHOULD-FIX from
`docs/realtime-ingest-phase04-review-sol.md`. No implementation or test file
was changed. Per the review request, I relied on the independently reported
test results and did not rerun test suites.

## Verdict

**SHIP enabling `realtime_ingest_deferred_projections` in production, in the
rollout order documented in `docs/realtime-ingest-phase04-handoff.md`.**

Both prior BLOCKERs are cleared, the prior SHOULD-FIX is cleared, and I found
no new BLOCKER or SHOULD-FIX. The documented old-row lexicon limitation and
the one-time full scan are rollout NOTEs below.

## Prior findings

### CLEARED — raw updates now invalidate from both sides of a replacement

Files: `server/server/services/realtime_raw_writer.py:73`, `:652`, `:676`,
`:703`, `:737`, `:746`, `:761`, `:762`, `:1201`, and `:1207`;
`server/tests/test_realtime_raw_writer.py:97`.

The three `operation="update"` constructors in `reduce_writer_state` are the
complete set of update-producing branches:

1. Cursor source-identity replacement at `realtime_raw_writer.py:652-658`
   captures `existing_cursor.role` and `existing_cursor.metadata_`.
2. Claude queue-row canonicalization at `:676-682` captures
   `queue_row.role` and `queue_row.metadata_`.
3. Codex delta-tail mirror-pair replacement at `:703-709` captures
   `delta_tail.role` and `delta_tail.metadata_`.

All three values come from reducer state that was already loaded. The change
adds no database read.

Candidate derivation then covers both sides of every update. Search includes
an old `user`/`assistant` role at `:737-745`. Canvas enqueues when the new
message names a Canvas, or an update's old or new role/metadata is
Canvas-capable at `:746-759`. I found no remaining reducer update shape that
can replace Canvas-capable or indexed text without requesting the matching
projection.

The candidate-only search signal does not alter flag-off behavior.
`search_text` at `:761` is still based only on the pre-existing new-value
flags and remains the value passed to the SSE change calculation at `:1201`.
The old-role-aware `candidate_search_text` is separate at `:762-770`, and its
result is consumed only by candidate enqueue inside the deferred flag guard
at `:1207-1216`. The flag still defaults to `false` at
`server/server/config.py:39`.

The new reducer regressions cover the reported Cursor failure directly:
user-to-system directives enqueues Canvas and search
(`test_realtime_raw_writer.py:97-106`), system-to-system enqueues neither
(`:109-117`), and user-to-user content replacement enqueues search
(`:120-127`).

### CLEARED — first deferred search apply restores proven large-FULL lexicon parity

Files: `server/server/services/realtime_ingest_projector.py:254`, `:266`,
`:292`, `:301`, `:307`, `:411`, and `:423`;
`server/tests/test_realtime_ingest_projector.py:370`.

`_apply_search` defines the first search apply as the absence of a historical
completed or superseded **search** candidate for the document
(`realtime_ingest_projector.py:254-264`). On that first apply it scans
`left(content, 2048)` for all stored user/assistant messages in ascending line
order (`:292-305`) and retains the 10,000-term cap (`:307-311`, with the
constant at `server/server/services/message_search.py:31`). Later applies use
`latest_search_rows`, which remains the descending last-200 query at
`realtime_ingest_projector.py:266-275`. FTS also remains last-200 at
`:279-289` on every apply.

The new 240-row regression places a nonce before the last-200 window and a
second nonce in the late window, then compares the complete prefixed lexicon
set and exact FTS text between synchronous and deferred runs
(`test_realtime_ingest_projector.py:370-434`). Its assertions at `:431-434`
prove the original large-document omission is repaired without weakening the
lexicon comparison.

The first-apply predicate is correct relative to projector transaction order.
Search projection runs at `realtime_ingest_projector.py:411`; only afterward
are the claimed rows assigned `completed_at`/`superseded_at` at `:421-425`.
Thus the current pending row does not suppress its own full scan. After a
successful outer commit, that history prevents later full scans. If the outer
transaction rolls back, both projection writes and completion markers roll
back, so replay is again a first apply and repeats the full scan. The lexicon
write remains idempotent through `ON CONFLICT DO NOTHING` at
`server/server/services/message_search.py:156`.

### CLEARED — crash/replay regression exercises apply-before-outer-commit rollback

File: `server/tests/test_realtime_ingest_projector.py:555`.

The new test enters the requested failure window, rather than simulating an
ordinary restart. It commits the deferred ingest, calls
`process_pending_candidates` directly for the crash document at `:610-613`,
then rolls back that session's outer transaction at `:614`. A fresh session
confirms both Canvas and search candidates are still pending at `:616-618`.
Separate fresh projector instances then drain the clean control and replayed
crash documents at `:620-623`. The test compares final snapshots, asserts one
Canvas reference, and asserts no pending crash candidates at `:625-639`.

This is the exact window named in the first review: projection apply occurred,
candidate completion had not committed, the outer transaction rolled back,
and a fresh projector replayed to quiescence.

## Findings

### NOTE — bounded post-first-apply lexicon divergence remains

After a document has a completed/superseded search candidate, an in-place edit
of an old user/assistant row outside the latest 200 can introduce a term that
the synchronous path would append to `conversation_search_terms`, while the
deferred projector will not append it. The FTS projection is intentionally
last-200 on both paths, and the lexicon is an append-only correction vocabulary
rather than the document's search corpus.

This is an acceptable bounded-input tradeoff, not a production-enable
BLOCKER. The first deferred apply now establishes the full initial corpus, the
proven large-FULL parity case is fixed, and later projections still refresh
the authoritative bounded FTS value. Keep this limitation documented and
include it in canary observation.

### NOTE — the first search apply performs one O(document messages) read

The first search apply materializes up to 2,048 characters for every stored
user/assistant row before term extraction. For a 12,000-message document that
is at most about 24.6 million characters of selected content, plus query and
Python object overhead; the 10,000-term cap stops further term extraction but
does not reduce that initial query result.

The cost is acceptable for Phase 4 because it is asynchronous, occurs once per
document after the ingest commit, and subsequent applies return to the
last-200 window. Monitor projector latency and memory during the documented
12k+ canary before expanding the flag-on rollout.

## Production-enable decision

No BLOCKERs and no SHOULD-FIX findings remain. Ship code with the flag false,
start the projector against the migrated outbox, then enable the flag on every
conversation-ingest committer in the documented order.

**Final verdict: SHIP enabling `realtime_ingest_deferred_projections` in
production per `docs/realtime-ingest-phase04-handoff.md`.**
