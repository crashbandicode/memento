# Raw Codex history port

## Delivered scope

`realtime_ingest_raw_codex_history` is a default-off canary flag for Codex
conversation **DELTAs** whose bounded `user_history` or `first_user_message`
would otherwise require the legacy reducer.  With the flag off, the existing
no-op proof and `RawWriterUnsupported("authoritative rebuild/history needs
legacy reducer")` path are unchanged.  With it on, the raw reducer plans and
stages the same final recovery-row state as the legacy Codex history branch.

Authoritative FULL rebuilds still take the legacy path.  The existing 16 MiB
coalesced-delta bound and every enumerated legacy-forever guard are unchanged.

## Reducer and staging design

The raw state load now includes the complete committed conversation timeline
only for a metadata-bearing history frame.  The planner first applies the
current frame's prospective source mutations in memory, then mirrors the
legacy sequence:

1. Normalize and bound history through `_prepare_document_metadata`, reserve
   `codex-history:{index}` source identities, and allocate the same bounded
   `_history_line_number` negative slots.  The namespace is the legacy range
   `[-MAX_USER_HISTORY_ENTRIES, -1]`, disjoint from normal positive appends.
2. Call the shared `partition_recovered_occurrences` helper for initial
   history-vs-source dedup, then again for the complete recovered set during
   reconciliation.  No local matching rule or tolerance was copied: the
   helper retains its exact one-to-one, normalized-content, five-minute
   transport-delay window.
3. Call the shared `recovered_occurrence_anchors` function for surviving
   negative rows.  Process anchors in descending order and shift every
   positive row at or after the anchor before placing each sorted recovery
   group, matching `_reconcile_recovered_history_rows`.
4. Translate the simulated final state into raw inserts, deletes, and line
   updates.  The stage deletes first, temporarily moves all update rows below
   the current minimum line, then writes their final lines and inserts.  Thus
   a recovery insertion cannot collide with the positive append stream or a
   shifted pre-existing row under the document/line unique index.

When any recovery row is inserted, removed, or relocated, the raw writer
rebuilds the read model and prompt projections from the post-stage database
rows.  That matches legacy's `force_full=recovered_history_changed` behavior,
including `user_message_count`, dashboard inputs, and derived prompt rows.
The legacy `first_user_message` fallback is also staged when bounded history
is absent and no post-frame user row exists.

## Flag and canary plan

Set `MEMENTO_REALTIME_INGEST_RAW_CODEX_HISTORY=true` only for the existing
raw-writer owner/device/tool canary scope.  The compose pass-through is
present for API, Celery ingest worker, realtime drain, and projector; its
default is `false` everywhere.  Watch drain
`legacy_fallback_frames_by_reason` for the authoritative-history bucket,
raw-to-legacy fallback counts, transaction failures, message/read-model
parity, prompt/search refreshes, and dashboard count changes.  Disable the
single flag to restore today's legacy fallback immediately.

## Tests

Focused reducer coverage currently includes flag-off fallback, recovery-row
insertion and positive-line shifting, resend dedup, and the 2,000-entry bound.
The recorded parity fixtures add four named Codex sequences:

- `codex_history_recovered_prompt`
- `codex_history_resent_dedup`
- `codex_history_interleaved_positive_append`
- `codex_history_entry_bound`

The corresponding golden additions are new top-level keys; existing golden
keys were not edited.  Final command tallies are recorded in
`HISTORY_PORT_PROGRESS.md`.

## Explicit non-changes

- No Claude paths, sidecar handling, parser behavior, API behavior, web code,
  or deferred projection kinds changed.
- No raw support was added for authoritative FULL replacement/rebase.
- No change was made to the shared history-recovery matching algorithm or its
  tolerance.

## Reviewer scrutiny list

1. Compare the raw planner's two helper invocations and ordering against the
   legacy initial merge plus `_reconcile_recovered_history_rows`.
2. Verify all affected positive rows (including already-placed recovery rows)
   receive a collision-safe shift, while normal append rows remain positive.
3. Check the stage delete/temp-move/final-update ordering under the unique
   `(document_id, line_number)` constraint.
4. Check a recovery mutation always forces a full read/prompt projection
   rebuild and a search invalidation, but a resend no-op does not.
5. Confirm the default-off flag retains the exact prior fallback reason in
   both singleton and coalesced-chain writers.

## Follow-up notes

### F1 — proof-sized history state load

History metadata now first loads only the rows consumed by
`_history_metadata_is_already_committed`: all recovered rows and all ordinary
user rows. That preserves the default-off no-op proof without materializing
the complete conversation timeline. With the raw Codex-history flag on, a
frame whose proof is not already committed retries the pure reducer after a
second, complete timeline read; that is the only path that invokes the
collision-safe history merge. Raw-on resends that the proof accepts retain the
proof-sized read, while changed history has the same full merge input as before.
