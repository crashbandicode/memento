# DO-NOT-SHIP

Reviewed `main` at `da549600e62993832af9effd56a505eeb3a8a7f3` through branch HEAD `016914c237da677a9438c08ecb570931d94a1617`, including all six commit messages and `git diff main..HEAD`.

## Blockers

### 1. `SendFeedback` can still render through the pending/inline interaction surfaces

Locations:

- `server/server/services/conversation_read_model.py:281` accepts a direct persisted `metadata_.interaction` without applying the new meta-tool exclusion.
- `server/server/api/conversations.py:942` does the same in the legacy message aggregation path.
- `server/server/services/conversation_parser.py:4122` lets any already-normalized live interaction through `coerce_claude_live_interaction`; it does not reject an interaction whose `tool_name` is `SendFeedback`.
- `web/src/components/viewers/ConversationViewer.tsx:1574` and `web/src/components/viewers/ConversationViewer.tsx:1759` render inline and pending API interactions as `QuestionInteractionCard` without `isMetaConversationTool`. The two new guards at the message/tool-call render sites do not cover these surfaces.

Concrete failing scenario: an already-stored message has `metadata_.interaction = {tool_name: "SendFeedback", ...question-shaped fields...}` from an older ingest. A full or incremental read-model pass places it in `pending_interactions`; `/pending-interactions` returns it; the viewer renders it in the needs-attention interaction panel at line 1759 even though the message bubble itself is now inert. I reproduced the server half directly: `_Accumulator.observe(...)` retained such a row with `pending_count == 1`. I also passed the equivalent stored/live interaction to `coerce_claude_live_interaction`, which returned it unchanged (`accepted: True`, `tool_name: SendFeedback`). An old `_live_interaction_signals` entry can therefore reach the inline card at line 1574 as well.

This violates the operator-mandated invariant that `SendFeedback` must never render as interaction chrome for new ingests or already-stored state. Rebuilding the read model is insufficient because `_question_interactions` currently re-accepts the persisted interaction.

Concrete fix direction: apply one shared server-side meta-tool predicate when reading direct stored interactions, coercing live interactions, and assembling both projected and legacy pending state; also filter the pending and inline client collections before every `QuestionInteractionCard` surface. Add regression coverage that feeds a persisted `SendFeedback` interaction through `_Accumulator`/`/pending-interactions` and asserts that neither pending nor inline UI state contains it.

## Non-blocking notes

- Parser symmetry is sound for the two Claude block paths. Both `parse_conversation_object` and `_parse_claude_record_messages` use the same shell-name/background predicate and the same immediate-result task-ID extractor. The regex is broad enough to recognize generic `Task ID`, but a foreground result cannot keep or terminate a foreground shell incorrectly: the read model only stores the extracted ID on an activity already marked `is_background`, and all ID-based control/event completion paths filter to background activities.

- The background-shell state machine otherwise matches the requested lifecycle. The immediate launch result updates and preserves the background activity; task notifications correlate by launch/task/thread tokens; `KillShell`/`TaskStop` and `BashOutput`/`TaskOutput` target only background activities; foreground activities still retire on their ordinary result. I found no path that kills a foreground activity through the new correlation logic.

- Coverage does not directly exercise terminal `BashOutput`/`TaskOutput` content or an immediate failed background launch. `_background_result_is_terminal` is deliberately marker-based, so a recorded fixture for one running poll and one terminal poll would materially reduce leak risk. I did not elevate this without a supplied source transcript contradicting the accepted markers; the recorded notification-completion path is covered and consistent.

- `background_running_count` correctly counts running background shell cards and replays background-agent start/terminal token sets. Terminal events remove any active entry sharing an exact agent tool-use, task, or thread token. The count is derived after live-state bounding and is removed before persistence in both the ORM writer (`conversation_read_model.py`) and raw SQL writer (`realtime_raw_writer.py`); no writer-column drift remains. The stated three-writer parity result is consistent with the code.

- Both pending-interaction API aggregation paths expose `is_background` on canonical and transient live activities. The legacy path's `background_running_count(..., [])` omission is benign within the accepted asymmetry: it still counts background shells, while only background-agent events require the read-model projection.

- The header callback is safe in the sole caller: React's state setter has stable identity, navigation resets both viewer and page count to zero, stale fetches are cancelled by the effect cleanup, and older servers missing `background_running_count` fall back to zero. An older activity missing `is_background` is also falsy at runtime despite the newer required TypeScript field.

- The live-card word `Background` remains hardcoded English at `web/src/components/viewers/ConversationViewer.tsx:5319`, while the header chip is localized. Also, the English header template renders `1 background tasks running`. These are presentation notes, not correctness blockers.

- The golden fixture is internally consistent: the launch result assigns `task-shell-1`; the completion event carries both `task-shell-1` and `toolu-background-shell`; the resulting `live_activities` is empty; and `SendFeedback` persists only as an inert tool row. The ingest endpoint, durable inbox replay, collector records, and thread metadata service preserve `is_background`, including across later updates.

## Validation performed

- `git diff --check main..HEAD` — clean.
- Focused parser/read-model cases — `6 passed, 125 deselected` with cache and bytecode writes disabled.
- Web meta-tool classification test — `1 passed` (Node emitted only its existing module-type warning).
- Independent reproductions of the blocker described above — both succeeded.

I did not rerun the full server suite or Cargo, per the review instructions and the supplied base-comparison evidence.

## Round 2

### Verdict: DO-NOT-SHIP

Reviewed fix commit `c4e12286482aa5a2142b628cf7363a178f271217` against its parent `016914c237da677a9438c08ecb570931d94a1617`.

#### Resolved from Round 1

- The original reproductions now pass: `coerce_claude_live_interaction` rejects the stored/live `SendFeedback` interaction, and `_Accumulator.observe(...)` leaves `pending_interactions` empty.
- The parser-owned predicate is applied to direct and normalized tool-call interactions in both read-model/legacy aggregators, to stale projected pending items, and before and after live AskUserQuestion-wrapper recovery.
- The client filters both pending and inline collections at their only state-ingress point, covering the former `QuestionInteractionCard` escapes. Answered inline items are removed with their interaction. `inferred_responses` does not independently create chrome; it only supplies a pairing map consumed by the already-guarded message interaction sites, so it does not require the same collection filter.
- The background tag and count text presentation notes from Round 1 are fixed in both locales.

#### Remaining blocker

##### 1. Historic meta interactions still persist false pending-question header/dashboard state

Locations:

- `server/server/services/ingest_service.py:1552` (`_pending_question_interactions`) and `server/server/services/ingest_service.py:1614` (`_advance_stored_pending_questions`) still read direct stored `metadata_.interaction` and raw `tool_calls[].interaction` without `is_meta_tool_interaction`.
- The core/legacy delta path seeds those IDs at `server/server/services/ingest_service.py:4929` and persists them through `_store_pending_question_ids` at `server/server/services/ingest_service.py:5435`. The raw writer uses the same unsafe helper/store sequence at `server/server/services/realtime_raw_writer.py:834` and `server/server/services/realtime_raw_writer.py:955`.
- `web/src/app/conversations/[...ref]/page.tsx:199` renders `pending_question_count > 0` as the `Awaiting response` header chip; the same persisted count also drives dashboard needs-attention state.

Concrete failing scenario: a historic row contains the same persisted `SendFeedback` interaction used in the Round 1 reproduction. On a later delta containing only a non-human assistant/tool row, `_pending_question_interactions` returns that meta interaction, seeds `pending_question_ids`, and `_store_pending_question_ids` writes `pending_question_count = 1`. The API's pending/inline collections are now correctly empty, but the conversation still displays `Awaiting response` and the dashboard can still classify it as needing attention. The full reconciliation twin has the same problem: `_advance_stored_pending_questions` adds the meta ID, and `PENDING_QUESTION_RECONCILIATION_VERSION` remains `3`, so already-persisted false counts are neither invalidated nor repaired.

I reproduced this at HEAD: the two Round 1 outputs were fixed (`coerce_accepted: False`, `read_model_pending: 0`), while the ingest readers returned `delta_tail_pending: 1` and `reconciled_pending_ids: ['feedback-1']` for that exact stored interaction.

Concrete fix direction: apply the parser-owned predicate to every interaction consumed by `_pending_question_interactions`, `_advance_stored_pending_questions`, `_update_pending_question_ids`, and `_normalized_interaction_ids`; then increment the pending-question reconciliation version so existing `_pending_question_ids`/`pending_question_count` metadata and dashboard projections are rebuilt without meta tools. Add core/raw reconciliation coverage asserting both the collections and persisted count remain empty for a historic `SendFeedback` interaction.

#### Round 2 validation

- `git diff --check HEAD^..HEAD` — clean.
- Focused server meta-tool regressions — `4 passed, 176 deselected` with cache and bytecode writes disabled.
- Web meta-tool tests — `2 passed`; only the existing Node module-type warning was emitted.
- Independent Round 1 and pending-count reproductions — results recorded above.

I did not rerun the full suite, Cargo, or the already-reported three-writer parity suite.

## Round 3

### Verdict: DO-NOT-SHIP

Reviewed fix commit `b752ca4cc8909dc9e0951fbe4af0d801e30578b8` against its parent `c4e12286482aa5a2142b628cf7363a178f271217`.

#### Blockers

##### 1. The version-4 reconciliation repairs document metadata but leaves the durable dashboard badge stale

Locations:

- `server/server/services/ingest_service.py:1803` stores the rebuilt document metadata and commits, but `reconcile_pending_question_metadata` never refreshes `DashboardDocumentProjection`.
- `server/server/api/dashboard.py:145` reads `pending_question_count` from that durable projection; its value is only re-derived from document metadata when `refresh_dashboard_document_projection` runs (`server/server/services/dashboard_projection.py:152`).

Concrete failing scenario: an existing v3 conversation has a historic `SendFeedback` row, document `pending_question_count = 1`, and dashboard projection `pending_question_count = 1`, then the server starts without another collector delta. The v4 startup reconciliation does re-scan the document and clears its document-level count, but the dashboard projection remains at 1, so the dashboard still classifies the conversation as needing attention.

I reproduced this against the real test PostgreSQL database: before reconciliation, both counts were 1; afterward, document metadata had no count and carried reconciliation version 4, while `DashboardDocumentProjection.pending_question_count` was still 1. The startup repair reported the document as updated. The new writer-parametrized regression does not cover this path because its assistant delta refreshes the projection through ordinary ingest.

Concrete fix direction: refresh the dashboard projection for every document whose reconciliation changes pending-question metadata, and add a direct `reconcile_pending_question_metadata` regression that starts with a stale projection.

##### 2. The version mismatch can erase a legitimate live-only `AskUserQuestion` badge

Locations:

- `server/server/services/ingest_service.py:1302` discards all carried pending IDs when the metadata version is not 4.
- `server/server/services/ingest_service.py:1418` writes the rebuilt IDs/count but does not stamp reconciliation version 4 and does not include retained live signals in the count.
- The legacy/Core sequence at `server/server/services/ingest_service.py:5466` and the raw sequence at `server/server/services/realtime_raw_writer.py:953` retain legitimate `_live_interaction_signals` before storing the now-empty pending-ID set.
- `server/server/services/ingest_service.py:1714` limits the later startup reconciliation to documents still carrying `_pending_question_ids`; once the delta removes that key, the repair no longer selects the document.

Concrete failing scenario: a v3 document has a real unanswered `AskUserQuestion` represented by `_pending_question_ids = ["ask-live-1"]`, `pending_question_count = 1`, and a legitimate `_live_interaction_signals` entry, but its canonical transcript row has not landed yet. An unrelated assistant-only delta arrives first. The version mismatch seeds an empty ID set; the live-signal reconciliation correctly retains `ask-live-1`; `_store_pending_question_ids` nevertheless removes the ID and count and leaves the version at 3. The header/dashboard badge disappears even though `/pending-interactions` can still expose the live question, and the startup repair can no longer find the document by `_pending_question_ids`.

I reproduced that exact helper sequence at HEAD: the live signal remained, while `_pending_question_ids` and `pending_question_count` became absent, the version remained 3, and the reconciliation query predicate no longer matched. The same risk applies to documents created after the one-time startup repair because the only assignment of version 4 is inside `reconcile_pending_question_metadata`. A canonical `AskUserQuestion` already present in the last 32 stored rows is retained; I verified that case separately. The uncovered case is the valid transient/live path before canonical persistence (or a legitimate interaction outside that bounded tail).

Concrete fix direction: after a version-mismatch rebuild, persist version 4 and derive active IDs from both the bounded canonical tail and retained non-meta live signals. Add a three-writer regression for a legitimate live-only AskUserQuestion before an assistant delta, not only the stale-meta case that is expected to clear.

#### Non-blocking notes

- The Round 2 reproduction is fixed: `coerce_claude_live_interaction` rejects `SendFeedback`; `_Accumulator`, `_pending_question_interactions`, and `_advance_stored_pending_questions` all produce no pending interaction/ID for the historic row.
- The v4 startup selection is correctly bounded to conversation documents carrying `_pending_question_ids`, and v3 documents re-trigger because only version 4 is skipped. It is not a full-table conversation-message scan.
- The shared predicate is present in all six named ingest readers. I found no additional stored interaction path that can independently create interaction cards after the Round 2 server and client guards; the remaining failures above are persisted badge/projection state, not card rendering.
- `git diff --check HEAD^..HEAD` is clean. The new writer-parametrized historic-meta regression passes (`3 passed`). Direct reproductions above used bytecode/cache writes disabled where applicable.

I did not run the full suite or Cargo, per the review instructions.

## Round 4

### Verdict: SHIP-WITH-NOTES

Reviewed fix commit `0ea775f11e57b3c0229774b2ada5b79efd480d40` against its parent `b752ca4cc8909dc9e0951fbe4af0d801e30578b8`.

#### Blockers

None.

#### Resolved from Round 3

- `reconcile_pending_question_metadata` now flushes repaired document metadata and calls `refresh_dashboard_document_projection` at `server/server/services/ingest_service.py:1854`. The direct startup-repair regression is green: the historic meta-tool document count and its durable dashboard count both become zero.
- On a version mismatch, `_pending_question_ids_for_ingest` now seeds from `_active_live_interaction_ids` at `server/server/services/ingest_service.py:1305`. The exact legitimate live-only AskUserQuestion scenario from Round 3 remains pending, keeps its signal and count, and is stamped version 4 in legacy, Core, and raw writers.
- `_store_pending_question_ids` computes whether the document has or had relevant state before removing empty keys (`server/server/services/ingest_service.py:1457`). A stale/meta v3 state is cleared and stamped v4; an active live question is retained and stamped; an unrelated unversioned document receives no stamp; an already-v4 document stays v4. I found no normal main-to-HEAD ingest state that is stamped before its pending set is rebuilt or that needs repair while remaining permanently outside the repair selection.
- The at-or-before-human rule in `_active_live_interaction_ids` matches the existing server-wide interaction semantics: a meaningful human turn supersedes an older prompt. Missing or malformed timestamps fail open and retain the live interaction. I found no evidence that the new helper drops a genuinely pending interaction under the supported timestamp model.

#### Non-blocking notes

- The claimed one-field golden update is not present. The committed `realtime_ingest_parity_golden.json` blob is byte-for-byte identical at `HEAD^` and `HEAD` (`39c5d33c7faba21f416279202ad0a1f7600abb8e`), and it contains no `_pending_question_reconciliation_version` field. The three-writer golden gate nevertheless passes. This is consistent with the implementation: the background/meta sequence has no pending-question state after `SendFeedback` exclusion, so conditional stamping deliberately leaves it untouched. The validation/presentation claim should be corrected, but there is no runtime or parity defect.
- A document already stranded by the interim Round 3 bug with only a legitimate `_live_interaction_signals` entry and no `_pending_question_ids` would not match the startup repair query; its next delta would reconstruct and stamp it. That state is not produced by main or the Round 4 code path, so I do not treat compatibility with the unshipped intermediate review commit as a blocker.
- The version-transition state matrix also leaves an empty v3 marker unchanged when the document has no pending IDs, count, or active live signal. Nothing requires healing in that state; a later real interaction makes the pending set non-empty and stamps v4 in the same ingest.

#### Round 4 validation

- `git diff --check HEAD^..HEAD` — clean.
- Round 3 reproductions plus historic-meta writer coverage — `7 passed`.
- Three-writer recorded golden parity gate — `3 passed`, no skips.
- Direct state-matrix reproduction confirmed live-v3 retention, meta-v3 clearing, conditional no-churn, and human-supersession behavior.

I did not run the full suite or Cargo, per the review instructions.
