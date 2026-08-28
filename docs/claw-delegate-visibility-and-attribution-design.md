# Claw-delegate dashboard visibility + operator-vs-agent message attribution — design

Status: APPROVED by operator 2026-08-28 ("I agree on 1-4 and adopt it now"):
D1 Recent = collapse-group (one expandable "Claw · N delegated agents" row);
D2 chain-successor exemption + one-time run-2 un-stamp repair AUTHORIZED
   (repair runs only AFTER the exemption code deploys, or the reconciler
   would re-stamp it);
D3 historical backfill of message_origin for claude_code threads APPROVED
   (extend scripts/backfill_conversation_presentation.py);
D4 MEMENTO-DELEGATE-FROM briefing marker ADOPTED NOW (recorded in
   DELEGATION_ROUTING.md, both copies; Memento resolves it read-time).
Implementation target per operator: Grok xhigh delegate. Sequencing: branch
from main AFTER feat/background-task-visibility merges (overlapping files:
conversation_parser.py, conversation_read_model.py, api/conversations.py,
ConversationViewer.tsx).

## Problems (operator-reported, all verified against production)

P1. The dashboard "Recent" section floods with claw-delegated coding sessions
    (terra/sol/grok/luna/composer), drowning the main threads.
P2. In claw-spawned threads the operator's own typed messages render as
    "Parent agent" dispatches instead of "You".
P3. (Found during verification) Handoff-chain successor threads (spawn-prime-
    stop-resume pattern) get stamped `orchestration=claw`/`is_subagent=true`
    once the reconciler links them — the operator's LIVE MAIN THREAD then (a)
    folds out of Recent under its predecessor and (b) blanket-mislabels every
    operator message as parent-agent. Verified: run-2 doc 050540a9 carries
    orchestration=claw, orchestration_parent_document_id=<run-1 doc>,
    is_subagent=true, agent_path=claw/session/memento-run-successor. The
    current thread (run-3) is unstamped only because reconciliation hasn't
    linked it yet.

## Root causes (file:line refs against main @ da549600e6)

- Attribution is THREAD-level, not message-level:
  `conversation_hierarchy.conversation_user_role_origin` (:175-197) returns
  "parent_agent" for any orchestration-child thread; the messages endpoint
  stamps it on EVERY role=user message (`api/conversations.py:1595-1604`,
  legacy :1662, search :2597/:2672, prompts :2702); the viewer's
  `isParentAgentMessage` (`ConversationViewer.tsx:350-356`) additionally
  falls back to the same thread-level value.
- Flooding: claw delegates are folded from Recent only AFTER
  `orchestration_events._apply_agent_projection` (:254-325) stamps the child
  document, and that requires parent linkage from scanning the PARENT
  transcript for the orchestrationRunId string (:229-251, guard :260 skips
  unlinked runs). Unlinked or late-linked delegates appear as ordinary
  threads. Transport losses (e.g. the 1800s MCP idle-timeout killing a
  session_send whose result JSON never lands in the parent transcript) can
  orphan them permanently. Verified live today: terra rollout marked
  is_subagent=t, sol rollout still f while both were claw-spawned.

## Objective origin signals (verified in real data, 2026-08-28)

- Claude Code, PER-RECORD: claw/SDK-injected user records carry
  `entrypoint:"sdk-cli"` (content array); operator-typed records carry
  `entrypoint:"cli"` (content string). Verified in one and the same session
  file (035914ae): priming message sdk-cli vs typed follow-up cli. The parser
  currently reads neither field (`conversation_parser.py` reads only
  isMeta/isCompactSummary/isVisibleInTranscriptOnly at :323-328).
- Codex, PER-ROLLOUT: claw sends run `codex exec`; the rollout `session_meta`
  records `originator:"codex_exec"`, `source:"exec"` (verified on terra
  rollout 01a04887). Interactive sessions record a different originator. The
  parser currently skips session_meta records (:2202).
- Cursor: no known envelope marker (parser sees content/session_context only,
  :2413-2508). Fallback = thread-level classification + optional protocol
  prefix (below).
- Claw roster: `~/.openclaw/claude-sessions.json` maps session name → engine +
  native session/thread id + timestamps (12 rows today). Machine-local,
  live-ish (may prune); good for enrichment, not durable truth.
- OrchestrationRun/OrchestrationAgent tables already persist
  `native_session_id`, engine, run_kind, agent_key per delegate
  (`orchestration_events.py:355-475`) — independent of parent linkage.

## Proposed design

### A. Per-message origin (fixes P2; parser/read-model change)

A1. Parser: at the Claude user-record branch
    (`conversation_parser.py:1855-1858`) read `obj["entrypoint"]`:
    `sdk-cli` → NormalizedMessage.message_origin="parent_agent";
    `cli` → message_origin="human"; absent/unknown → unset.
A2. Persist as `meta["message_origin"]` in
    `ingest_service._conversation_message_metadata` (:860-943) — flows through
    both writers automatically (parity golden re-baseline required, pure
    addition).
A3. API: per-message `origin` = metadata message_origin when present, else the
    existing thread-level fallback (`api/conversations.py:1600/:1662/:2672`).
    Native Claude subagents keep today's behavior (their prompts genuinely are
    parent dispatches, and their records won't carry `cli`).
A4. Viewer: `isParentAgentMessage` prefers per-message origin; an explicit
    "human" origin renders as "You" even inside a delegated/successor thread
    (`ConversationViewer.tsx:350-356`, plus markdown/copy label :4505-4529).
A5. Codex (phase 2, optional): capture rollout session_meta originator and
    scope records under it; `codex_exec` → parent_agent, interactive → human.
A6. Cursor: keep thread-level only (accept limitation) unless the prefix
    convention (C2) is adopted.
A7. Backfill: `scripts/backfill_conversation_presentation.py` already touches
    origin; extend it to recompute message_origin for claude_code docs from
    stored raw lines, or accept forward-only + full-resync healing.
    OPERATOR DECISION.

### B. Recent-section behavior for delegates (fixes P1)

B1. Stamp delegates WITHOUT waiting for parent linkage: split
    `_apply_agent_projection` so that when `run.parent_document_id` is None
    but `native_session_id` resolves to a document, it still stamps
    `orchestration="claw"` + run_kind/agent_key (+ a parentless-delegate
    state), leaving parent fields null; the existing retry loop
    (`reconcile_orchestration_for_document`, :478-551) fills the parent in
    later. This makes classification independent of transport luck.
B2. Dashboard: collapse rows whose `orchestration == "claw"` into ONE
    aggregate "Claw · N delegated agents" row in Recent (reuse the
    SubagentBadge idiom/labeling), expandable; parent-linked ones keep
    today's folding under their parent. Requires surfacing `orchestration`
    on the recent-row payload (`api/dashboard.py:574-606`,
    `app/page.tsx:194-205, 577-647`).
    ALTERNATIVE (simpler): filter them out entirely behind a client toggle
    ("Show delegated"). OPERATOR DECISION: collapse-group (recommended) vs
    hide+toggle.

### C. Chain-successor exemption + protocol markers (fixes P3, covers cursor)

C1. EXEMPT handoff/tangent threads from delegate classification: a thread
    whose first user message begins `MEMENTO-HANDOFF-FROM:` or
    `MEMENTO-TANGENT-FROM:` is a PRIMARY thread (successor/tangent), not a
    delegate. The orchestration reconciler must not set
    is_subagent/orchestration on it (keep the handoff/tangent LINK features
    as the relationship display). Also un-stamp run-2 (050540a9) and any
    previously mis-stamped chain threads (one-time repair, archive-safe
    metadata edit — needs operator go since it writes server data).
C2. Protocol marker for engines without objective signals (cursor; also
    belt-and-braces elsewhere): adopt a first-line convention in every claw
    delegate briefing — `MEMENTO-DELEGATE-FROM: <parent session id>` — added
    by the orchestrator (routing-doc rule, no claw code change), parsed
    read-time exactly like the handoff/tangent markers. Gives both delegate
    classification AND parent linkage without orchestrationRunId scanning.
    OPERATOR DECISION: adopt now or defer.

## Verification plan (whoever implements)

- Unit: parser entrypoint mapping (sdk-cli/cli/absent), metadata persistence,
  API per-message override + fallback, viewer label preference.
- Parity: new golden case with one sdk-cli and one cli user record (pure
  addition; three writers identical).
- Live: (1) this thread (035914ae) — operator messages must render "You"
  post-deploy even after orchestration linkage lands; (2) terra/sol rollouts
  collapse into the Recent aggregate row; (3) run-2 renders correctly after
  the C1 repair.

## Out of scope

- Rewriting claw internals; cross-machine roster sync; codex/cursor per-message
  origin beyond A5/A6; historical re-ingest beyond the A7/C1 targeted repairs.
