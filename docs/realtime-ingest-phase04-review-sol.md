# Realtime ingest Phase 4 second review (Sol)

Date: 2026-08-27

Scope: uncommitted Phase 4 working-tree changes implementing the durable,
revision-fenced Canvas/search projector from `docs/REALTIME_INGEST_DESIGN.md`.
This was a read-only implementation review; no implementation or test files
were changed.

## Verdict

**DO-NOT-SHIP enabling `realtime_ingest_deferred_projections` in production.**

The code can ship in the documented first rollout step with the flag left at
its default `false`: I found no flag-off behavior regression. Do not proceed to
the rollout step that enables the flag until the two BLOCKERs below are fixed
and covered by regression tests.

## Findings

### BLOCKER — raw Cursor updates can change both projections without enqueueing either candidate

Files: `server/server/services/realtime_raw_writer.py:608`,
`server/server/services/realtime_raw_writer.py:646`,
`server/server/services/realtime_raw_writer.py:729`,
`server/server/services/realtime_raw_writer.py:740`, and supporting parser
evidence at `server/server/services/conversation_parser.py:2443`.

The Cursor state reducer resolves updates by `source_id` and then replaces the
stored row's role, content, and metadata. Candidate detection, however, checks
only the **new** mutation values:

- search is requested only when the new normalized role is `user` or
  `assistant`;
- the update half of the Canvas predicate calls
  `canvas_message_can_have_reference()` on the new role/metadata.

A supported mutation can therefore replace an existing indexed and
Canvas-capable user row with a non-indexed/non-capable system row under the
same `source_id`. This is not hypothetical parser input: Cursor user records
containing additional directives normalize to `role="system"` and
`raw_type="cursor_directives"` in `conversation_parser.py:2443-2452`. The raw
writer applies that replacement at `realtime_raw_writer.py:650-655`, but
`has_search_text` remains false and the Canvas update predicate also returns
false. With the flag enabled, neither outbox row is inserted. Existing
`content_tsv` text and any Canvas reference for the old row can then remain
stale indefinitely.

Candidate derivation for an update must include the pre-update row's indexed
role and Canvas capability, not just the replacement values. The existing
row is already available as `existing_cursor`, so this does not require a new
database scan.

### BLOCKER — deferred lexicon output is not equivalent to synchronous FULL ingest

Files: `server/server/services/realtime_ingest_projector.py:245`,
`server/server/services/realtime_ingest_projector.py:269`,
`server/server/services/ingest_service.py:4622`,
`server/server/services/ingest_service.py:5635`, and
`server/tests/test_realtime_ingest_projector.py:142`.

The projector extracts lexicon terms only from the latest 200 user/assistant
rows. The synchronous writer extracts terms while parsing the incoming
transcript and admits terms across the parsed FULL up to
`MAX_LEXICON_TERMS_PER_INGEST` (10,000). On a new 12k-message conversation,
unique terms present only before the last-200 window are inserted by the
synchronous path but are never inserted by the deferred path. The lexicon is
append-only (`ON CONFLICT DO NOTHING`), so a later replay of the same
last-200 window cannot repair those omissions.

The dedicated eventual-golden test does not catch this mismatch. It compares
the complete selected Canvas snapshot and exact `content_tsv::text`, but for
the lexicon it queries and asserts only one nonce (`test_realtime_ingest_projector.py:173-176`
and `:325-326`) in a two-message fixture. Thus Canvas and FTS equality are
meaningful for the fixture, while lexicon equality is a weakened presence
check. This violates the Phase 4 search/lexicon parity gate for proven large
documents.

### SHOULD-FIX — the restart/replay test does not inject a crash after apply and before commit

File: `server/tests/test_realtime_ingest_projector.py:330`.

`test_projector_restart_replay_is_idempotent` calls `run_once(limit=1)`, which
commits one group normally, then drains the untouched groups and finally
verifies that another quiescent run is a no-op. That proves resume across
remaining outbox rows and no-op replay after completion, but it does not
exercise the named crash window: projection writes performed, outer
transaction rolled back before candidate completion commits, then replayed
after restart.

The implementation's transaction structure is correct for that window, and
the apply operations are idempotent, so this is a test-coverage defect rather
than a third implementation blocker. Add failure injection immediately before
the session commit (or call `process_pending_candidates` and roll the outer
transaction back), then rerun with a fresh projector and compare the final
snapshot.

### NOTE — Canvas discovery is document-bounded but not row-bounded

Files: `server/server/services/realtime_ingest_projector.py:205` and
`server/server/db/models.py:373`.

The Canvas query is scoped by `document_id` and can use the leading column of
`uq_conv_msg_doc_line`; existing references are also indexed by document.
However, the leading-wildcard `ILIKE '%.canvas.tsx%'` cannot use the ordinary
message-content indexes and may scan every message in that document. For a
12k-message document this is a finite, asynchronous sparse-candidate scan and
is reasonable to canary rather than a ship blocker. The search projection is
better bounded: it returns at most 200 matching rows and can walk
`(document_id, line_number)` backward, although it may filter intervening
non-user/assistant rows.

### NOTE — the handoff's compose statement is inaccurate

File: `docs/realtime-ingest-phase04-handoff.md:15` and
`docs/realtime-ingest-phase04-handoff.md:221`.

The handoff says `docker-compose.yml` was untouched and that no compose file
is in the diff. It is modified to back-fill the Phase 3
`realtime-ingest-drain` service and related environment entries. That compose
change is expected and should remain; only the handoff statement is wrong.

## Requested scrutiny, cleared items

1. **Transactional integrity — clear.** The legacy/Core enqueue occurs at
   `ingest_service.py:4007-4023`, before `ingest_file` returns, using the same
   `AsyncSession` that owns the message writes. Its callers commit afterward.
   Raw enqueue occurs at `realtime_raw_writer.py:1180-1189`, inside the
   asyncpg transaction opened at `:1322-1374`; the coalesced chain uses the
   same `_apply` inside its transaction at `:1547-1656`. Legacy idempotent,
   stale, and superseded returns (`ingest_service.py:3273`, `:3289`, `:3339`,
   `:3361`) occur before message mutation. Raw non-committed dispositions
   return at `realtime_raw_writer.py:1027-1035` without staging messages.
   I found no path that commits message mutations while skipping enqueue when
   the flag is on, apart from the candidate-classification BLOCKER above.

2. **Candidate completeness — not clear.** Inserted eligible Canvas bodies,
   ordinary user/assistant text changes, FULLs, new documents, and
   post-update rows that remain Canvas-capable are covered. Pre-update role or
   capability is not covered, producing the first BLOCKER. The documented raw
   friendly-title gap was treated as accepted scope.

3. **Flag-off purity — clear.** The flag defaults to false at
   `server/server/config.py:39`. With it false, legacy/Core still executes the
   same Canvas projection/reconciliation, bounded FTS refresh, and lexicon
   upsert. The `_stage_new_conversation_messages` restructure computes
   `canvas_values` earlier and sets a transient attribute, but takes the same
   persistence/projection branches; that attribute has no flag-off consumer.
   Raw still does not apply Canvas/search when the flag is off. Its added
   candidate calculations are not consumed and do not change persisted output
   or disposition.

4. **Revision fence and concurrent-ingest race — clear.** Each claimed group
   rereads current delivery/document revision at
   `realtime_ingest_projector.py:366` and applies against current database
   rows before completion. Older candidate hashes are marked superseded at
   `:384-388`. The locked `rows` list is fixed by the group SELECT; a candidate
   inserted after that SELECT is not marked and remains pending for the next
   cycle. If a newer ingest commits between the revision read and the apply
   queries, the apply sees data at that newer committed state (at or after the
   claimed fence), while any new candidate remains pending. I found no path
   marking a live candidate complete before its projection apply succeeds.

5. **SAVEPOINT semantics — clear, with acceptable spurious-wake behavior.**
   `begin_nested()` at `realtime_ingest_projector.py:347` encloses the apply
   and candidate completion, and `continue` still exits the async context.
   A failed group rolls back only its savepoint; the poison regression proves
   a healthy document commits in the same outer cycle. Realtime events are
   stored in `AsyncSession.info` and are not savepoint-aware
   (`server/server/db/session.py:15-24`), so a rare failure after an event is
   queued can leave a phantom `file_synced` or Canvas `control_command` wake
   to be published after the outer transaction commits. It cannot publish
   uncommitted database state: the event is emitted only after the outer
   commit, clients/collectors subsequently read or lease committed rows, and
   the rolled-back command row is absent. The effect is a spurious refetch or
   empty command poll, which is acceptable under the stated criterion.

6. **Giant-document reads — clear for rollout with the performance NOTE
   above.** Search has bounded output and a suitable document/line index.
   Canvas is document-scoped and sparse-candidate-driven, but scans the
   document because of the substring predicate; monitor projector latency and
   database reads during the 12k+ canary.

7. **Idempotency/replay and duplicate projector — implementation clear.**
   Projection writes and candidate completion share the `run_once` session
   transaction (`realtime_ingest_projector.py:469-473`). A crash before commit
   rolls both back. Canvas exactly reconciles by unique
   `(message_id, path_hash)` identity, FTS overwrites deterministically, and
   lexicon insertion uses `ON CONFLICT DO NOTHING`. A normal second projector
   exits when the session advisory lock is owned. If two workers nevertheless
   overlap, `FOR UPDATE SKIP LOCKED` prevents them from claiming the same
   existing candidate rows; a newly inserted row can cause duplicate work on
   the same document, but the idempotent/unique writes make the failure mode a
   retry or duplicate wake rather than incorrect committed projection state.
   The missing crash-window test is recorded above.

8. **Golden tests — partially clear.** The dedicated test performs exact
   equality for its selected Canvas rows and `content_tsv::text` against the
   synchronous legacy path for deferred legacy/Core/raw. It does not prove
   full lexicon equality, producing the second BLOCKER. The Phase 0 parity
   golden does not contain Canvas/search projections; its Phase 4 wrapper
   checks the other live fields and intentionally excludes staged SSE. Neither
   `server/tests/test_realtime_ingest_parity.py` nor
   `server/tests/fixtures/realtime_ingest_parity_golden.json` is modified in
   the working tree, so the parity goldens were not regenerated.

## First-review fix verification

The poison-group fix is present and structurally correct:

- per-group `db.begin_nested()` SAVEPOINT;
- an error result without aborting the remaining groups;
- in-memory exponential retry backoff via `_attempts` / `_retry_after`, capped
  at 60 seconds;
- regression coverage showing the healthy document drains, the poison group
  stays pending and backs off, and recovery drains it after the error clears.

This fixes the original all-groups-one-transaction starvation mechanism.

## Final production-enable decision

**DO-NOT-SHIP the flag-on rollout.** Ship the code flag-off first if desired,
then fix pre-update candidate detection and make deferred lexicon semantics
match the synchronous path (with large-transcript equality coverage). After
those fixes and the documented API-migration/projector/writer rollout order,
re-review before enabling the flag.
