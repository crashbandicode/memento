# Raw-writer unsupported shapes — investigation report

Branch: `perf/collector-steady-state`. Investigation only; no code/config/data changed.
Live data captured 2026-08-27 ~11:00–11:20 UTC from the running WSL/Docker stack
(`memento-realtime-ingest-drain-1`, `memento_postgres`, spool at
`/data/ingest-spool`). All windows are relative `docker logs --since` / `find -mmin`
snapshots, so counts drift a few % between commands; proportions are stable.

---

## 1. Raise sites & conditions

All `raise RawWriterUnsupported` live in
`server/server/services/realtime_raw_writer.py`. They split across three functions.
The realtime drain always enters via `ingest_conversation_raw_chain`, which forces
`mode="delta"` and `authoritative_rebase=False` for every frame — that determines
which sites are actually *reachable* in production (noted per row).

### Chain entrypoint — `ingest_conversation_raw_chain` (guards run once per chain)
- **L1389** `not frames` → "raw chain requires at least one frame". Empty chain.
- **L1394** `machine is None or owner is None` → unauthenticated owner/machine.
- **L1402** `category != "conversation" or content_type != "jsonl"`.
- **L1425** any frame differs from the head in tool/category/content_type/
  relative_path/machine_id/user_id, or `frame["mode"] != "delta"` →
  "raw chain has mixed source identities".
- **L1428** `metadata[CURSOR_PROJECTION_ORDER_KEY] is not None` →
  "Cursor projection reordering needs the legacy reducer" (rare; Cursor row reorder).
- **L1432** `_claude_subagent_pair_transcript_path(relative_path, category) is not None`
  → "Claude transcript/sidecar pairing needs the legacy reducer". **Reachable, secondary.**
- **L1454** `conversation_session_id(...)` changes between frames →
  "raw chain changes stable source identity".

### Identity guard — `_ensure_supported_identity_path` (L1169, runs under the source lock)
- **L1209** more than one live document shares this `session_id`, or the single
  match's `relative_path` differs from the incoming path →
  "stable-identity relocation/alias selection needs the legacy reducer" (rare; file moved/aliased).

### Per-frame reducer — `reduce_writer_state` (L392, runs once per reducer frame)
- **L454** `category != "conversation" or content_type != "jsonl"`.
- **L456** `authoritative_rebase or metadata.get("user_history") or
  metadata.get("first_user_message")` → **"authoritative rebuild/history needs legacy
  reducer". DOMINANT.** In the chain path `authoritative_rebase` is always `False`, so
  in production this fires **purely on the two metadata flags** — not on mode, not on
  `base_offset==0`, not on a base-hash mismatch.
- **L458** `mode not in {"full","delta"}`.
- **L460** `mode == "delta" and (base_hash is None or base_offset is None)` →
  a raw DELTA needs an exact committed base. Reachable but uncommon.
- **L474** idempotent FULL whose MinIO content pointer is missing → "needs legacy repair"
  (FULL-only, not reachable from the delta chain).
- **L497** existing doc + `mode=="full"` + hash differs + not superseded →
  "replacement FULL needs legacy reducer" (FULL-only).
- **L499** `state.document is None and mode == "delta"` → a DELTA with no existing FULL.
- **L551** new document whose payload carries `project_hash`/`project_path`/`"cwd"` →
  "new-document project resolution needs the legacy reducer" (guarded by `doc_row is None`;
  a delta with no doc hits L499 first, so effectively FULL-only).
- **L563** existing doc + (`incoming_title is not None` **or**
  `source_title_kind == "claude_ai_title"`) → **"existing-document title selection needs
  the legacy reducer". SECONDARY.** `incoming_title`/`incoming_title_is_explicit` are
  popped from metadata by `_prepare_document_metadata`.
- **L1052** post-staging defensive assert for a replacement FULL (should never fire).

`ingest_conversation_raw` (single-frame, L1214) mirrors L1228/1232/1241/1245 for the
non-chain caller; the drain does not use it.

**Reachable-in-drain set:** L456 (metadata history flags), L563 (title), L1432 (Claude
pairing), plus the rarer L460 / L1428 / L1209 / L1454 and `DeltaBaseMismatch`
(a distinct exception, not `RawWriterUnsupported`).

---

## 2. Live attribution — which sources/tools, and why

Fallback-reason breakdown from the drain log (one ~60m snapshot; every "legacy path"
warning parsed):

| reason | count | share |
|---|---:|---:|
| authoritative rebuild/history | 173 | ~72% |
| Claude transcript/sidecar pairing | 40 | ~17% |
| existing-document title selection | 26 | ~11% |

No other reason strings appeared. The drain warning itself carries **no identity**, so
attribution was done from the collector source and confirmed in Postgres.

### Dominant shape (authoritative rebuild/history) = 100% Codex
- The trigger is the metadata flag, not `authoritative_rebase`. Those flags are set
  **only** by the Codex collector: `collector/collector/tools/codex.py::_enrich_with_thread_info`
  adds `meta["first_user_message"]` and `meta["user_history"]` (from `history.jsonl`),
  and `_enrich_with_thread_info` runs on **every** Codex session emission (call sites
  L385/L410). So every Codex delta after the first prompt carries `user_history` and
  falls back unconditionally — even when history has not changed.
- Cursor and Claude Code collectors set **neither** flag (grep across `collector/` finds
  `user_history`/`first_user_message` only in `codex.py`, `codex_export.py`, and
  `hermes.py`; `hermes` is tool_id `hermes` and has no live traffic).
- DB confirmation: `history_user_message` rows — the exact output signature of the legacy
  history reducer — exist for **codex only** (7 documents, 498 rows, latest 11:04 UTC).
  No `history_user_message` rows exist for any other tool.
- It is not one giant hot document: 7 distinct Codex docs carry history, and only ~3–4
  Codex sources were active in the window (`sync_state` last-60m: cursor 19, claude_code 12,
  codex 4, system 4). The 173 fallbacks over ~4 active Codex sources ≈ heavy per-source
  repeat — each Codex source re-sends `user_history` on essentially every delta.

### Secondary — Claude transcript/sidecar pairing (~17%)
Structural, path-based: `_claude_subagent_pair_transcript_path` returns non-None for
Claude Code subagent transcript files (`.jsonl`) and their `.meta.json` sidecars. Any
subagent transcript delta falls back regardless of content. Attribution: Claude Code
subagent sessions.

### Secondary — existing-document title selection (~11%)
Reached only when the frame has **no** `user_history`/`first_user_message` (L456 is
earlier), so this is **not** Codex history traffic. It fires when a delta to an existing
doc carries a title: predominantly Claude Code frames with
`source_title_kind=="claude_ai_title"` (set by `collector/collector/parsers/jsonl.py:183`),
plus occasional Codex title-only / Cursor title frames.

---

## 3. Semantic summary of each shape (legacy reducer, `ingest_service.py`)

### Authoritative rebuild / history  (`_ingest_conversation_messages`, L5354–5558)
Codex-specific reconstruction of the human side of the transcript from out-of-band
sources:
- **Reads:** all `role=='user'` `ConversationMessage` rows for the doc (excluding
  `history_user_message`), and all existing `history_user_message` rows (their
  `source_id`s and negative line slots).
- **Logic:** normalizes each `history.jsonl` entry (`codex-history:{i}` source_id),
  skips ones already stored, then `partition_recovered_occurrences` does a one-to-one
  content/timestamp dedup within a bounded transport-delay window so genuinely repeated
  prompts survive but rollout-vs-history duplicates do not.
- **Mutates:** inserts missing history rows at **negative** line numbers
  (`_history_line_number`, bounded by `MAX_USER_HISTORY_ENTRIES`), adds search text,
  then `_reconcile_recovered_history_rows` repositions/prunes previously-placed rows.
  The `first_user_message` branch (L5500) is a simpler fallback: if the doc has no user
  row yet, inject one anchored at the first non-system line.

Net: this is a stateful, order-sensitive merge into a reserved negative-line namespace,
deduped against existing rows — not a plain append.

### Existing-document title selection  (`_select_updated_document_title`, L2181)
A **pure** precedence function over already-loaded state:
- **Reads:** existing `doc.title`, incoming title, `tool_id`, `category`, and
  `stored_metadata["memento_title_source"]`.
- **Logic:** claude_code + `memento_title_source=="claude_ai_title"` + existing title →
  keep existing unless a new explicit claude.ai title arrived; codex + existing title
  that does not need derivation → keep existing; otherwise take the incoming title.
- **Mutates:** just `doc.title`. (The heavier `_apply_friendly_conversation_title`
  derivation is a separate call, not the thing L563 guards.)

### Claude transcript/sidecar pairing (for completeness)
Cross-file coordination: a Claude subagent `.jsonl` transcript and its `.meta.json`
state sidecar (different `category`) are linked into one logical conversation via the
hierarchy resolver. Requires reading/writing a second file/document — genuinely outside
the single-source, single-transaction model of the raw writer.

---

## 4. Support-cost assessment per shape

### Shape A — authoritative rebuild/history  → **(b) needs careful design**, but with a cheap (a) mitigation that matters more
- Native raw support means porting the Codex history merge into the pure reducer +
  staging: negative-line-slot allocation, `partition_recovered_occurrences` dedup,
  `_reconcile_recovered_history_rows`, bounded by `MAX_USER_HISTORY_ENTRIES`. Data reads
  (all user rows + existing history rows) are already loadable in `_load_state`.
  Invariants: negative-slot namespace must not collide with the positive append stream;
  dedup window must match legacy exactly to avoid duplicate prompts. Risk: medium
  (search text + read-model counts derive from these rows).
- **The higher-leverage fact:** this fires on every Codex delta *even when history is
  unchanged*, so ~72% of fallbacks are pure waste. A **mechanical (a)** fix removes most
  of them without porting anything: revision-gate the flag so the collector attaches
  `user_history`/`first_user_message` only when `history.jsonl` actually advanced (Codex
  already tracks a per-thread `revision`), **or** short-circuit in the raw writer to a
  no-op when the incoming history `source_id`s are already a subset of stored ones.
  Recommendation: do the (a) mitigation first; treat the full (b) port as optional.

### Shape B — existing-document title selection  → **(a) mechanical**
`_select_updated_document_title` is a pure function of state the reducer already holds
(`doc_row["title"]`, incoming title, tool_id, `memento_title_source`). Replace the L563
raise with a call to it and set `title` accordingly; carry `memento_title_source` into
the stored metadata when `claude_ai_title`. No new reads, no cross-row state, low risk
(title is not an identity/dedup key). Only nuance: if you also want the friendly
first-prompt derivation, that is a small follow-on, but it is not what L563 blocks.

### Shape C — Claude transcript/sidecar pairing  → **(c) intentionally legacy-forever** (or defer)
Needs two files across two categories linked in one logical write — structurally against
the raw writer's single-source/single-transaction design. It is ~17% today but rare per
source and complex; keeping it on the legacy reducer is the right answer. If its volume
grows it can be revisited as a (b) design, but it should be an *explicit exclusion*, not
a blocker.

### Rare tail — Cursor projection reordering (L1428), identity relocation/alias (L1209),
mid-chain identity change (L1454), delta-without-base (L460): all low-frequency
correctness/identity guards. Leave on legacy → **(c)**; they are the safety net the raw
path is allowed to defer to.

---

## 5. Recommended Phase 5 gate definition

The design's literal bar (Phase 5, `docs/REALTIME_INGEST_DESIGN.md:505`) — "a full
operational interval with **no fallback**" — is unreachable as written, because the
Codex collector forces the history fallback on every delta. Recommended redefinition:

1. **First close the two cheap gaps:** ship Shape B as native (a), and apply the Shape A
   (a) mitigation (revision-gate `user_history`/`first_user_message`, or subset
   short-circuit). Together these remove ~83% of current fallbacks (title + history)
   without touching the hard shapes.
2. **Redefine the gate** as: *one full operational interval (≥60 min steady-state soak +
   a restart/recovery drill) with **zero raw-writer fallbacks except an explicitly
   enumerated legacy-forever set** — {Claude subagent transcript/sidecar pairing, Cursor
   projection reordering, stable-identity relocation/alias, delta-without-committed-base}
   — and each excluded shape bounded to **< 1 fallback/min and < 2% of drained frames**,
   verified over the interval.*
3. **Make it measurable:** the drain currently logs fallbacks but **not** raw successes,
   so the ratio is only inferable by counting spool completion receipts (see §6). Add a
   per-shape fallback counter and a raw-commit counter to the drain so the gate can be
   asserted directly rather than reconstructed from logs. (Instrumentation only — no
   fallback logic change.)

---

## 6. Quantified raw : legacy ratio

Method: the drain does not log raw commits, so the denominator is taken from spool
**completion receipts** (`/data/ingest-spool/completed/*.json`, one per drained frame,
7-day retention, `mtime` = commit time). Legacy frames come from summing the
"applying N frame(s)" counts in the drain warnings. One temporally-consistent 60m
snapshot:

| metric (last 60m) | value |
|---|---:|
| total realtime frames drained (receipts) | 463 |
| legacy frames | 346 |
| raw frames | 117 |
| legacy chains (warnings) | 276 |
| **frame-level legacy share** | **~74.7%** |
| **frame-level raw share** | **~25.3%** |

All 463 receipts carried `admission_identity` (all realtime; no non-realtime pollution).
Chain-level is consistent: legacy averages 346/276 ≈ 1.25 frames/chain, so raw ≈ 94
chains, ~75% of ~370 total chains route legacy.

**Caveat — workload dependence:** this hour was a Codex-heavy burst (30m and 60m receipt
counts were nearly equal, ~449 vs ~463, i.e. most activity was in the last half hour),
so legacy share is inflated toward Codex. The task's cited baseline (~105 legacy chains /
10 min out of ~37 syncs/min ≈ 370/10 min) implies a calmer-period legacy share nearer
~28–30%. The ratio therefore tracks Codex's share of live traffic directly: because every
Codex delta falls back today, raw:legacy swings between roughly 1:2.5 (Codex-heavy, now)
and ~2.5:1 (Codex-light). Landing the Shape A mitigation collapses the Codex contribution
and makes the raw path dominant in every regime.
