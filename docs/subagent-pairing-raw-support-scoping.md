# Subagent transcript/sidecar pairing — raw-writer support scoping

Branch `main` @ `a15eb21b87`. Investigation only; no code/config/data changed (single
deliverable is this file). Live data captured 2026-08-27 from the running WSL/Docker
stack (`memento_postgres`), read-only. Companion to
`docs/raw-writer-unsupported-shapes-report.md` (this is the deep-dive on that report's
Shape C) and `docs/phase5-gate-soak2-report.md` (the soak in which this shape was 87 of
114 fallback frames, all from one document).

**Verdict up front:** the refusal is over-broad, exactly like the `first_user_message`
refusal fixed yesterday. A subagent **transcript** DELTA is a plain claude_code JSONL
append. Every cross-document effect the legacy reducer performs for it is either (a)
already skipped by the raw path for *all* claude transcripts today (lineage), (b)
event-driven and fail-open with no synchronous consumer (subagent lifecycle status), or
(c) a one-time enrichment that the **sidecar's own ingest** independently guarantees
(launch metadata). There is **no per-frame cross-document write** and **no consumer with
a sub-second consistency requirement**. The narrow-refusal design is sound.

---

## 1. Raise-site analysis

The refusal has one predicate, evaluated at three reachable sites, all keyed on
`_claude_subagent_pair_transcript_path(relative_path, category)`
(`server/server/services/ingest_service.py:376-387`):

```python
def _claude_subagent_pair_transcript_path(relative_path, category):
    if category == "conversation":                       # the .jsonl transcript
        identity = _claude_subagent_file_identity(relative_path, sidecar=False)
        return identity[0] if identity is not None else None
    if category == "state":                               # the .meta.json sidecar
        identity = _claude_subagent_file_identity(relative_path, sidecar=True)
        ...
```

`_claude_subagent_file_identity` (`ingest_service.py:292-306`) matches
`.../<parent>/subagents/agent-<id>.jsonl` (transcript) or `agent-<id>.meta.json`
(sidecar).

- **Chain entry** `ingest_conversation_raw_chain` — `realtime_raw_writer.py:1739-1742`.
  This is the **production realtime path** (the drain calls it, `tasks/ingest_spool.py:204`).
- **Single-frame** `ingest_conversation_raw` — `realtime_raw_writer.py:1548-1551`.
- Both are preceded by the hard gate `category != "conversation" or content_type != "jsonl"`
  (`realtime_raw_writer.py:1709`, `1535`; reducer copy `665`).

**Key reachability fact.** Sidecars are re-classified `category="state"` at the trust
boundary by `normalize_ingest_category` (`ingest_service.py:260-274`: a claude_code
`/subagents/…​.meta.json` becomes `state`). The raw gate rejects anything that is not
`conversation`/`jsonl` *before* the pairing check, so **`.meta.json` sidecars never reach
the raw writer at all.** The only frames this refusal actually removes from the raw path
are subagent **transcript** DELTAs — plain JSONL appends. (FULLs also never reach raw:
the chain forces `mode=="delta"` for every frame, `realtime_raw_writer.py:1731`, so an
initial FULL is `RawWriterUnsupported("mixed source identities")` → legacy.)

Two adjacent refusals that do **not** apply to subagent transcripts, confirming this is
the sole blocker:
- **Authoritative history** (`reduce_writer_state:667`, dominant Codex shape): fires only
  on `metadata.user_history`/`first_user_message`. Subagent transcripts carry neither
  (live metadata keys, §6). Not reached.
- **Existing-document title** (old Shape B): already native — `reduce_writer_state` calls
  `_select_updated_document_title` directly (`realtime_raw_writer.py:788`), no raise. The
  launch-description title is handled at read time anyway (`conversation_display_title`,
  `conversation_hierarchy.py:146-172`).

---

## 2. Per-frame cross-document behavior (Q1)

Traced from `tasks/ingest_spool.py` (drain) → `ingest_service.ingest_file` (legacy
fallback, `writer="legacy"`) → `_extract_messages`. For a subagent transcript **DELTA**
the legacy reducer touches exactly three things beyond the transcript's own
messages/read-model/delivery/sync (which the raw writer already reproduces):

| # | Effect | Reads | Writes | Other-document? | Classification |
|---|---|---|---|---|---|
| A | Claude lineage refresh `refresh_claude_lineage` (`ingest_service.py:4766`, impl `claude_lineage.py:445-577`) | this doc's own lineage rows + the appended records | `claude_conversation_lineage_records` **keyed to the transcript's own `document_id`** | **No** — same document, separate table | (c)-ish per-frame *but already skipped by raw for every claude transcript today* |
| B | Subagent lifecycle status `_reconcile_subagent_document_lifecycle` (`ingest_service.py:2666-2705`, called `3726`) | this doc's metadata + delta content | `subagent_lifecycle_status/_source/_at/_evidence` in **this doc's own `metadata`** (`store_document_metadata`, same doc) | **No** — same document | **(b) event-driven** — only when a terminal record type appears |
| C | Launch-metadata enrich `_reconcile_claude_subagent_launch_metadata` (`ingest_service.py:2522-2637`, called `3719`) | **the sibling `.meta.json` sidecar document** | `agent_launch_description/agent_tool_use_id/agent_type/agent_id/agent_launch_metadata_source` into **this transcript's own `metadata`** | **Read only** of the sidecar; write is same-document | **(a) creation/one-shot** — no-op once present (`:2620-2625`); also produced from the sidecar side |

Two general effects the legacy conversation path also runs, which the raw path **already
skips for all conversation docs** (so they are not subagent-specific and not new
divergences):
- `apply_deferred_conversation_metadata` (`ingest_service.py:3714`) — drains the
  conversation metadata inbox into this doc.
- `reconcile_orchestration_for_document` (`ingest_service.py:3829-3834`) — sets
  orchestration **link** columns and projects orchestrator metadata *into* the child; it
  never writes agent status from the transcript (subagent-trace §2). For native
  path-linked subagents (no orchestrator, the 87-frame case) it is a no-op.

**Critical structural finding:** there is **no write to any document other than the
transcript itself** on the transcript-delta path. Effect C's only other-document access
is a *read* of the sidecar. The parent/root session document is **never mutated** when a
child delta lands — subagent counts, folding, titles and per-child status are all
computed at **read time** by `fold_conversation_subagents` / `build_subagent_summaries`
(`conversation_hierarchy.py:272-537`), not persisted onto the parent. So "does the PARENT
read model get refreshed on child deltas?" → **No.**

There is also no cross-document *identity* work: `conversation_session_id` returns `None`
for `claude_code` (`conversation_identity.py:68`), so `_ensure_supported_identity_path`
and the chain's stable-identity guard are inert for claude, and
`path_linked_subagent_identity` (the `is_subagent/parent_thread_id/root_session_id/
agent_depth` fields) is **already applied inside the raw chain** for claude_code
(`realtime_raw_writer.py:1753-1757`; impl `conversation_hierarchy.py:94-115`).

---

## 3. Sidecar path (Q2)

- **Category/content_type:** `state` / `json` (re-classified at `ingest_service.py:260-274`).
- **Ingest path:** synchronous legacy `ingest_file`; it can never enter the raw chain
  (§1). It acquires the normal source lock **plus** a shared
  `sidecar-pair:<transcript_path>` advisory lock (`ingest_service.py:3144-3157`).
- **What the sidecar writes into the pair:** via `_reconcile_claude_subagent_launch_metadata`
  (state branch, `ingest_service.py:2540-2637`) it `SELECT … FOR UPDATE` on the sibling
  **transcript** document and merges the validated launch metadata into the
  **transcript's** `metadata`, then re-runs lifecycle for the transcript
  (`3737-3741`), refreshes the transcript's dashboard projection, and publishes a
  `file_synced` SSE for the transcript with `changes ⊇ {conversation.metadata, project,
  dashboard}` (`_reconcile_idempotent_claude_ingest:2833-2876`).
- **Invariant to respect:** the sidecar is the *authoritative and convergent* source of
  launch metadata — it enriches the transcript whether it arrives before or after the
  transcript, independent of the transcript delta stream. The only thing a raw transcript
  commit must not do is **lose** that launch metadata. It does not:
  `_merge_delta_metadata(existing, incoming) = {**existing, **incoming}`
  (`ingest_service.py:634-655`) preserves existing metadata keys, and the raw reducer
  builds `document_values["metadata"]` from that same merge
  (`realtime_raw_writer.py:785`). Both the sidecar enrich and the raw delta take
  `FOR UPDATE` on the **same transcript `documents` row** (raw `_load_state` uses
  `FOR UPDATE OF d`, `realtime_raw_writer.py:266`; sidecar uses `with_for_update(of=Document)`,
  `ingest_service.py:2581/2604`), so the read-modify-write is serialized and a lost update
  is not possible even though the raw path does **not** take the `sidecar-pair` advisory
  lock. (Parity nuance to test, §7: delivery-metadata copy ordering.)

---

## 4. Preload requirements (Q3)

For a **plain append** subagent transcript DELTA (no terminal record; launch metadata
already present or sidecar absent), the raw writer needs **no additional preload** to be
byte-identical to legacy on messages / read model / prompts / tasks / dashboard /
delivery / sync:

- Subagent identity fields (`is_subagent`, `parent_thread_id`, `root_session_id`,
  `agent_depth`) — already injected pre-reduce (`realtime_raw_writer.py:1753-1757`).
- Read-model `lifecycle` column — already computed by the raw path: `_refresh_projections`
  → `_identity_values(document)["lifecycle"] = persisted_child_lifecycle(metadata)`
  (`conversation_read_model.py:430`), the same derivation legacy uses.
- Launch/lifecycle metadata already on the document — preserved through the delta merge
  (§3), and `WriterState` already carries `document.document_metadata`.
- Claude cache invalidation — the reducer already forces
  `interactions_changed = … or tool_id == "claude_code"` (`realtime_raw_writer.py:1027`),
  standing in for legacy's `interactions_changed = mode=="full" or lineage_changed`
  (`ingest_service.py:4869`).

The **only** artifacts a raw delta would not reproduce are the three same-document extras
in §2:
- **A (lineage rows):** not preloadable-away; it is a genuine per-frame write. But the
  raw path **already omits it for every claude transcript** (see §6 live proof: main
  session `050540a9` has 780 messages but only 7 lineage rows), and its consumers
  fail-open (§5). So this is an *already-accepted* divergence, not a new requirement.
- **B (lifecycle status):** only differs on frames whose delta content contains a
  terminal record (`assistant.stop_reason=end_turn` / `isApiErrorMessage` /
  `system.subtype ∈ {turn_aborted, api_error, …}`, `subagent_lifecycle.py:124-170`).
  Detectable by a cheap bounded scan of the delta bytes; no DB preload needed.
- **C (launch metadata):** requires reading the sidecar document. Avoidable — the sidecar
  side already writes it (§3).

Net: **no new `_load_state` read is required** for parity on the append case. The design
question is purely *how to handle A/B/C's fidelity*, not what to preload.

---

## 5. Deferred-projection feasibility & consumer consistency (Q4)

The Phase 4 outbox (`ingest_projection_candidates`, `realtime_ingest_projector.py`) is
the natural home. Today its `kind` is constrained to `('canvas','search')`
(`CheckConstraint ck_ingest_projection_candidate_kind`, `db/models.py:287-290`;
migration `db/migrations/versions/20260827_01_add_ingest_projection_candidates.py`;
runtime DDL `main.py:194`), and the projector `else`-branch supersedes unknown kinds
(`realtime_ingest_projector.py:412-413`). Widening it is mechanical; the raw writer
already enqueues into this outbox in-transaction (`enqueue_projection_candidates_raw`,
`realtime_ingest_projector.py:104-139`; called `realtime_raw_writer.py:1445-1454`).

Feasibility of each event-driven effect as a revision-fenced candidate:

- **A lineage → feasible.** A `claude_lineage` kind whose apply reruns
  `refresh_claude_lineage` against the current revision. Cost note: a naive FULL rebuild
  re-reads the whole transcript from MinIO; prefer the existing DELTA mode
  (`claude_lineage.py:491-577`) or a bounded rebuild so a hot large transcript does not
  pay a full-content read per apply.
- **B subagent lifecycle → feasible and cheap.** A `subagent_lifecycle` kind whose apply
  reruns `_reconcile_subagent_document_lifecycle` over the bounded tail. Terminal-regression
  guard already lives in `reconcile_child_lifecycle_metadata` (`subagent_lifecycle.py:344-392`),
  so out-of-order application is safe.
- **C launch metadata → does not need a projection kind** — the sidecar side is the
  convergent writer (§3).

**Consumer consistency — every consumer tolerates sub-second async** (verified by two
independent read-path traces):

| Consumer | Reads | Behavior under async lag | Sync required? |
|---|---|---|---|
| Permission-history visibility `history_entry_is_visible` (`claude_lineage.py:805-861`), called `api/conversations.py:1813/1867/2305/2371` | lineage `active`/scope | **Fail-open**: `lineage_state is None → return True` (`:831`); malformed/disagreeing origin → visible (`:829,847-851`). Missing/stale lineage over-shows a card briefly; never wrongly hides. | No |
| Subagent status badge `build_subagent_summaries` (`conversation_hierarchy.py:477,523`) | this child's `subagent_lifecycle_*` metadata | Absent → `status="unknown"` (`:523`), never a wrong terminal; parent-recorded lifecycle events keep the card visible; terminal stickiness `:695-704`. Self-heals. | No |
| Orchestration status resolver `orchestration_agent_summary` / `enrich_lifecycle_status` (`orchestration_events.py:137-182`, `subagent_lifecycle.py:507-537`) | orchestrator's **own** `OrchestrationAgent.status` row, not child metadata | Independent of transcript ingest; `if not incoming: return enriched` (`:525`) → keeps orchestrator's own status; no age/silence guessing. | No |
| Handoff link resolvers `_handoff_predecessor/successor_reference` (`api/conversations.py:802,857`) | first user message content + `document_delivery_state.activity_at` | Read none of A/B/C. Successor tie-break self-heals via delivery `activity_at`. | No |
| Dashboard rollups (`dashboard_category_rollup.py`, `activity_rollup_task.py`, `dashboard.py`) | `tool_id/category` counts; `subagent_count` is a structural fold | No numeric count depends on `subagent_lifecycle_status`/lineage; only the embedded per-card badge degrades to "unknown". | No |

---

## 6. Live verification (Q5)

Target: the soak's dominant doc `068761ac-f93b-44b8-bb7d-5c7d88dd3ee4` (the Phase-5 Plan
agent subagent transcript) and its sibling sidecar.

**Transcript doc `068761ac`** (`documents`): `tool_id=claude_code`, `category=conversation`,
`content_type=jsonl`, path `…/fe4bdc0b-…/subagents/agent-a8219f353e7676f9c.jsonl`,
`session_id=agent-a8219f353e7676f9c`, `is_subagent=true`, `parent_thread_id=root_session_id=
fe4bdc0b-…`, `agent_depth=1`. **Full metadata key set:** `agent_depth, first_timestamp,
is_subagent, last_timestamp, message_types, parent_thread_id, project_hash, project_path,
root_session_id, session_id, _stored_source_hash, _stored_source_revision_hash,
_stored_source_size, total_lines`.

- **No `user_history`/`first_user_message`** → the dominant history refusal is
  structurally unreachable for this shape (confirms §1).
- **No `agent_launch_*` and no `subagent_lifecycle_*` keys at all** — i.e. effects B and C
  left **nothing** on the document, despite all 87 frames going legacy and the sidecar
  existing. These fields are already best-effort in the *current* all-legacy pipeline.

**Message profile** (`conversation_messages`): 56 `tool_use` + 56 `tool_result` + 23
`assistant` + 2 `user` = 137. **Read model** (`conversation_read_models`):
`message_count=137`, `projected_through_line=137`, `generation=1`,
`lifecycle={"status":"completed","source":"claude_child_transcript",
"evidence":"assistant.stop_reason=end_turn",…}`.

> **Divergence, already present under legacy:** the read-model `lifecycle` says
> `completed` while the document's `metadata.subagent_lifecycle_*` is absent — even though
> `_identity_values` derives the former from the latter (`conversation_read_model.py:430`).
> This proves the badge-source metadata and the read-model lifecycle are **already
> eventually-consistent / divergent today**, so deferring them cannot introduce a new
> class of inconsistency. Today's conversation-detail subagent badge for this doc already
> resolves to "unknown" (metadata absent), while the dashboard card shows "completed" (read
> model). The narrow raw design does not regress this.

**Sidecar doc `f452b31c`** (`documents`): `category=state`, `content_type=json`,
`file_size_bytes=144`, `synced_at=2026-08-27 21:44:40Z`. It carries no `agent_*` metadata
on itself (its payload lives in content, not document metadata) — consistent with the
enrich reading sidecar *content*, and with the transcript never having been enriched here.

**Lineage rows** (`claude_conversation_lineage_records`):
- Transcript `068761ac`: 166 rows, 166 `is_subagent`, 164 `is_eligible`, 162 `active` —
  written by the legacy fallback (87 frames + initial FULL).
- **Main session `050540a9`** (root `fe4bdc0b.jsonl`, raw-committed 100% per the soak):
  **780 messages / read-model 780**, but **only 7 lineage rows, max `source_order`=9**,
  `last_offset=3,985,431`. This is the concrete precedent: **raw-committed claude deltas
  already do not maintain lineage**, and the product functions (permission cards fail
  open). A subagent transcript routed raw would behave identically.

---

## 7. Design recommendation (Q6)

**Per-frame coupling is not real.** The narrow-refusal design is recommended.

### 7.1 Exact predicate that must still go legacy

Under the new flag, keep refusing **only**:
- Any non-`conversation`/non-`jsonl` frame — i.e. **the `.meta.json` sidecar** (already
  excluded by the `category`/`content_type` gate; leave it exactly as is — the sidecar is
  the convergent launch-metadata writer and must stay legacy).
- Any **FULL** (already legacy via the `mode=="delta"` chain gate) — this is where the
  creation-time lineage FULL-build, first launch enrich, and first lifecycle happen.
- The existing enumerated legacy-forever guards unchanged (Cursor reorder,
  stable-identity relocation, delta-without-base).

Everything else — subagent transcript **DELTAs** — flows through the *same* raw path
claude main transcripts already use. Concretely: gate the two `_claude_subagent_pair_
transcript_path(...) is not None` raises (`realtime_raw_writer.py:1548-1551`,
`1739-1742`) on `not settings.realtime_ingest_raw_subagent_transcripts`. Because those
sites only see `category=="conversation"` frames (the sidecar was filtered earlier), the
flag cleanly enables transcript-only support.

### 7.2 Raw preloads / reducer changes

- **None required** for append parity (§4). `path_linked_subagent_identity`, title
  selection, read-model `lifecycle`, and metadata preservation are already in place.
- Optional (fidelity): in `_apply`, enqueue the new projection kinds for claude_code
  documents (or only subagent docs) alongside the existing canvas/search enqueue
  (`realtime_raw_writer.py:1445-1454`).

### 7.3 Effects that defer via the outbox

Two options, cheapest first:

- **Minimal (recommended to ship first): defer nothing new; accept main-session-equivalent
  behavior.** Lineage is built at the legacy FULL and goes stale on raw deltas (exactly as
  `050540a9` today); lifecycle status/launch metadata land from the FULL and the sidecar.
  All consumers fail-open (§5). Smallest, lowest-risk, and provably no worse than the
  status quo for main claude transcripts.
- **Enhanced (fidelity follow-on): add `claude_lineage` + `subagent_lifecycle` projection
  kinds.** Widen `ck_ingest_projection_candidate_kind`, add
  `_apply_lineage`/`_apply_subagent_lifecycle` to `process_pending_candidates`, enqueue
  from `_apply`. Restores lineage freshness and terminal-status updates post-commit within
  sub-second lag. Do this only if the "unknown" badge window or stale permission cards are
  judged user-visible enough to matter.

Launch metadata (C) needs no deferral — the sidecar is the convergent writer.

### 7.4 Flag / canary plan

- Add `realtime_ingest_raw_subagent_transcripts: bool = False`
  (`server/server/config.py`, alongside `realtime_ingest_raw_writer_*`).
- Canary via the existing per-owner/device/tool allowlists; watch the drain
  `legacy_fallback_frames_by_reason` counter — the "Claude transcript/sidecar pairing"
  bucket should collapse to sidecar-only (which never counted, since sidecars are legacy
  by category) and near-zero for transcripts.
- One-way-door note: Phase 5 already hard-requires
  `MEMENTO_REALTIME_INGEST_DEFERRED_PROJECTIONS=true`; the enhanced option's new kinds
  ride the same projector, so they inherit that constraint.

### 7.5 Parity-golden fixture plan

Golden-diff raw+projector vs legacy over interleaved sequences (the sidecar side must
stay legacy in the harness):
1. transcript FULL (legacy) → transcript DELTA append (raw) → assert messages / read model
   / delivery / sync byte-identical.
2. sidecar arrives *after* the transcript FULL, then a raw transcript DELTA lands
   concurrently → assert launch metadata present and not clobbered; assert no lost update
   on `documents.metadata` and `document_delivery_state.delivery_metadata`.
3. a DELTA carrying `assistant.stop_reason=end_turn` → assert lifecycle status
   (via the deferred kind, enhanced option) or documented "unknown-until-FULL/next-legacy"
   (minimal option).
4. lineage: assert enhanced-option projector reproduces legacy `active`/`is_subagent`
   sets; assert minimal-option matches main-session behavior.
5. permission-card visibility unchanged across both options (fail-open).

### 7.6 Risks

- **R1 — stale lineage over-shows a permission card.** Fail-open bias
  (`claude_lineage.py:831`); already true for main sessions. Mitigate with the enhanced
  `claude_lineage` kind if needed. *Low.*
- **R2 — subagent badge "unknown" / stale terminal window.** Self-heals; already observed
  under legacy for the target doc (§6). Mitigate with the `subagent_lifecycle` kind.
  *Low.*
- **R3 — sidecar/transcript metadata race.** Serialized by the shared transcript
  `documents`-row `FOR UPDATE` (§3); verify the `delivery_metadata` copy in the golden
  fixture #2. *Low-medium.*
- **R4 — orchestrated (non-native) subagents.** `reconcile_orchestration_for_document` is
  skipped by raw for all conversation docs already and self-heals from the orchestrator
  side; native path-linked subagents (the 87-frame case) have no orchestration row. *Low.*
- **R5 — projector full-content read cost for lineage (enhanced only).** Use DELTA-mode /
  bounded refresh, not a per-apply FULL rebuild of a large transcript. *Medium, enhanced
  option only.*

### 7.7 Size estimate & commit breakdown

- **C1 — flag + gate (minimal, shippable):** `config.py` flag; gate the two pairing raises
  on the flag. ~30 LOC + a couple of unit tests. **This alone closes the ~10% pairing
  fallback share** measured in soak-2.
- **C2 — parity golden fixtures:** interleaved transcript/sidecar/terminal sequences,
  minimal-option assertions. ~150–250 LOC test-only.
- **C3 (enhanced, optional) — deferred projection kinds:** migration widening the CHECK
  constraint + `models.py`/`main.py` DDL; `_apply_lineage`/`_apply_subagent_lifecycle` in
  the projector; enqueue in `_apply`. ~200–300 LOC + tests.
- **C4 (enhanced, optional) — extend golden fixtures** to assert projector parity for
  lineage/lifecycle.

Total for the shippable core (C1+C2): ~200–300 LOC, **materially smaller and lower-risk
than the Shape A history port**. The enhanced fidelity (C3+C4) is a clean, independent
follow-on that does not block the fallback-elimination win.

---

## 8. Verdict summary

- **Refusal is over-broad.** It blocks plain claude_code JSONL append DELTAs; the sidecar
  (the actual cross-document writer) is already excluded by category and stays legacy.
- **No per-frame cross-document write** exists on the transcript-delta path — every legacy
  effect writes the transcript's own document/rows; the only other-document access is a
  *read* of the sidecar, which the sidecar's own ingest makes redundant.
- **No consumer needs synchronous consistency** — permission visibility fails open,
  subagent status degrades to "unknown"/self-heals, orchestration uses its own event
  stream, handoff/dashboard read neither field.
- **The raw path already skips lineage for all claude transcripts** (live: 780-message
  main session with 7 lineage rows), so subagent transcripts routed raw are no worse than
  today's supported main sessions.
- **Recommendation:** ship the flag-gated narrow refusal (C1+C2, ~200–300 LOC) to
  eliminate the pairing fallback; add deferred `claude_lineage`/`subagent_lifecycle`
  projection kinds (C3+C4) only if the transient "unknown" badge / stale permission card
  fidelity is judged worth it.
