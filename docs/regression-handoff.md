# Regression handoff — conversation UX & live interactions

**Date:** 2026-07-31
**Covered versions:** v0.1.44 → v0.1.54
**HEAD:** see release tag `v0.1.54`
**Status:** v0.1.54 integrates secure Canvas previews, Cursor child/task projection fixes, and Claude live-prompt Unicode/mobile fixes.

This document is the canonical bug-fix / regression handoff for conversation attention, live prompts, Cursor/Claude subagent presentation, and related navigation hardening. It is based on the actual commit diffs listed below (not release notes alone).

Related architecture context: [project-architecture.md](./project-architecture.md), [collector-architecture.md](./collector-architecture.md).

## Browser E2E rerun

Node 24 is installed for `patrick` at `~/.local/node-current`, `@playwright/test` is a web dev dependency, and Chromium is stored in the user-owned `~/.cache/ms-playwright` cache. The WSL host also has Chromium's `libnspr4`/`libnss3` runtime packages. From the repository root, run:

```powershell
/usr/bin/pwsh -NoLogo -NoProfile -NonInteractive -File ./web/run-playwright.ps1
```

The integrated Chromium release-candidate count is recorded after the required full rerun.

---

## Bugs fixed

### 1. `8ac973e` — fix: reconcile conversation attention and navigation (v0.1.44)

- **Symptom:** Attention badges reappeared for questions that were already answered or replayed after a human turn; scheduled automation text looked like human prompts; scrolling a long thread did not reliably load earlier/later pages.
- **Root cause:** Pending-question reconciliation treated replayed interaction rows as still pending; meaningful-human clearing was incomplete for delta ingest; scheduled `/loop`/`AUTO`/`CRON` content was not classified as context; viewer only loaded newer pages near the bottom, not earlier pages when scrolling up.
- **Fix:** Track `_latest_meaningful_human_timestamp` and pending-question reconciliation version in ingest (`ingest_service.py`); ignore interactions at or before the latest human turn; treat scheduled automations as context in parser/markdown and `ConversationViewer.tsx`; load earlier pages when scrolling up (`PAGE_LOAD_SCROLL_THRESHOLD` + `loadEarlier()`).
- **Key files:** `server/server/services/ingest_service.py`, `server/server/services/conversation_parser.py`, `server/server/services/conversation_markdown.py`, `server/server/api/conversations.py`, `web/src/components/viewers/ConversationViewer.tsx`
- **Tests:** `server/tests/test_conversations_normalized_api.py` (`test_pending_interactions_ignore_question_replayed_after_human_turn`, `test_live_interaction_does_not_reopen_before_latest_human_turn`); `server/tests/test_reparse_conversations.py` (`test_delta_lookback_does_not_revive_question_after_human_reply`, stored reconciliation helpers); `server/tests/test_conversation_markdown.py` / `test_conversation_parser.py` (scheduled automations as non-human); `server/tests/test_cursor_structured_tools.py` (submitted Cursor answer status); `server/tests/test_thread_metadata_service.py`
- **Coverage gap:** No dedicated web/unit test for adjacent-page scroll loading (`loadEarlier` / `PAGE_LOAD_SCROLL_THRESHOLD`).

### 2. `99192ab` — fix: preserve live approval interactions

- **Symptom:** Claude AskUserQuestion prompts often never appeared live (only after transcript flush). Cursor Plan-mode switch requests were missing or lost lifecycle (pending → answered/skipped).
- **Root cause:** Collector only saw questions after JSONL flush; no Claude side-file hook path. Cursor Plan-mode (`SwitchMode`) was not modeled as a first-class interaction through projection/normalization.
- **Fix:** Add Claude pending hook + side-file reader (`collector/collector/claude_pending_hook.py`, `claude_pending_questions.py`); generalize interaction signals; normalize Cursor Plan-mode via `normalize_interaction` / `build_cursor_interaction_response` in `conversation_parser.py`.
- **Key files:** `collector/collector/claude_pending_hook.py`, `collector/collector/claude_pending_questions.py`, `collector/collector/interaction_signals.py`, `collector/collector/cursor_state_export.py`, `server/server/services/conversation_parser.py`, `tauri-collector/sidecar/entry.py`
- **Tests:** `collector/tests/test_claude_pending_questions.py` (side-file emit/answer/hook install); `collector/tests/test_cursor_state_export.py` / `test_interaction_signals.py` (Plan-mode projection); `server/tests/test_cursor_structured_tools.py` (Plan-mode pending/skipped/answered); `server/tests/test_thread_metadata_service.py` (`test_cursor_plan_mode_signal_updates_pending_inbox_state`)
- **Coverage gap:** None material for the core paths; no Playwright E2E for live badge UX.

### 3. `d77e480` — fix: surface Cursor task completions (v0.1.45)

- **Symptom:** Native Cursor “task finished” system notifications were invisible or looked like human/context noise; project location was hard to see on conversation pages.
- **Root cause:** Task-finished envelopes lived in session-context / follow-up shapes and were not normalized into deduplicated agent lifecycle events; UI lacked a dedicated location header.
- **Fix:** Parse `system_notification` / task body into `normalize_cursor_task_completion_event`; dedupe same-task completions; expand follow-up allowlist; add `ConversationLocation` + API location fields.
- **Key files:** `server/server/services/conversation_parser.py`, `server/server/services/conversation_markdown.py`, `server/server/api/conversations.py`, `web/src/components/conversations/ConversationLocation.tsx`, `web/src/components/viewers/ConversationViewer.tsx`, `collector/tests` fixtures via state export
- **Tests:** `server/tests/test_conversation_parser.py` (visible event, status variants, dedupe, malformed stays context); `server/tests/test_conversations_normalized_api.py` (`test_messages_expose_task_completion_as_agent_event`); `server/tests/test_cursor_structured_tools.py`; `collector/tests/test_cursor_state_export.py` (`test_state_export_captures_task_notification_without_compat_transcript`); location helpers in normalized API tests
- **Coverage gap:** Location UI itself has no front-end component test (API path covered).

### 4. `cb7e648` — fix: classify Cursor directives and surface subagent metadata

- **Symptom:** Product-injected `ADDITIONAL DIRECTIVE` / `<additional_directives>` text appeared as human prompts; Cursor subagent cards lacked model and start/complete times.
- **Root cause:** Directive payloads were parsed as user turns; spawn/lifecycle events did not carry requested model or observed timestamps into hierarchy merge.
- **Fix:** `normalize_cursor_additional_directives` → `role=system` / `raw_type=cursor_directives` with body dedupe; propagate model + `started_at`/`completed_at` through parser and `merge_subagent_event_summaries`; SubagentBadge shows model/times.
- **Key files:** `server/server/services/conversation_parser.py`, `server/server/services/conversation_hierarchy.py`, `server/server/services/conversation_markdown.py`, `web/src/components/conversations/SubagentBadge.tsx`, `web/src/components/viewers/ConversationViewer.tsx`
- **Tests:** `server/tests/test_conversation_parser.py` (directives as context, transport dedupe); `server/tests/test_conversation_markdown.py`; `server/tests/test_conversation_hierarchy.py` (model/times merge); `collector/tests/test_cursor_state_export.py` (`test_subagent_task_projects_requested_model_and_exact_start_time`); `server/tests/test_conversations_normalized_api.py`
- **Coverage gap:** SubagentBadge presentation CSS/UI not covered by front-end tests.

### 5. `8a5349d` — fix: clean Cursor directive conversation titles

- **Symptom:** Thread titles showed `ADDITIONAL DIRECTIVE: …` instead of the useful directive body.
- **Root cause:** Title derivation stripped session-context prefixes but not additional-directive labels/envelopes.
- **Fix:** Use `normalize_cursor_additional_directives` in `_conversation_title_needs_derivation`, `_friendly_conversation_title`, and `_apply_friendly_conversation_title` (`ingest_service.py`) so titles clean without querying prompt rows when the raw title is itself a directive.
- **Key files:** `server/server/services/ingest_service.py`
- **Tests:** `server/tests/test_ingest_titles.py` (`test_cursor_directive_title_is_cleaned_without_querying_prompt_rows`)
- **Coverage gap:** None for this path.

### 6. `a7422d3` — fix: preserve live prompts and message metadata

- **Symptom:** Claude permission requests, elicitations, and agent-need-input notifications still missed the live window; Cursor thinking/message bubbles lost native timestamps after compat projection.
- **Root cause:** Pending hook only matched AskUserQuestion Pre/PostToolUse; Cursor state export preferred timestamp-free compatibility transcripts over authoritative bubble times.
- **Fix:** Expand hook specs to PermissionRequest / Elicitation / Notification; normalize those live prompt types in `conversation_parser.py`; prefer subagent/state timestamps over timestamp-free compat rows in `cursor_state_export.py`.
- **Key files:** `collector/collector/claude_pending_hook.py`, `collector/collector/claude_pending_questions.py`, `collector/collector/cursor_state_export.py`, `server/server/services/conversation_parser.py`, `web/src/components/viewers/ConversationViewer.tsx` (elicitation/permission labels)
- **Tests:** `collector/tests/test_claude_pending_questions.py` (permission side-file pending/close, stable id); `collector/tests/test_cursor_state_export.py` (state supersedes timestamp-free compat; malformed timestamps rejected); `server/tests/test_conversation_parser.py` (permission + MCP elicitation normalization, Cursor native bubble time); `server/tests/test_conversations_normalized_api.py` (`test_messages_return_exact_cursor_native_timestamp`); `server/tests/test_thread_metadata_service.py` (`test_live_claude_permission_request_is_stored`)
- **Coverage gap:** None material for collector/server paths.

### 7. `8e88ba4` — fix: preserve Claude always-allow permission choices

- **Symptom:** Live Claude permission prompts showed only Allow/Deny; the source “always allow” / rule suggestion was missing (or unlabeled), so users could not see session/project scope choices Memento had ingested.
- **Root cause:** `normalize_claude_live_prompt_interaction` ignored `permission_suggestions` from the hook payload.
- **Fix:** Add `_claude_permission_suggestion_option` to render source-backed allow-always labels (session / project / user settings); default empty destinations to session-scoped wording; keep suggestions on the pending side-file.
- **Key files:** `server/server/services/conversation_parser.py`, `collector/tests/test_claude_pending_questions.py` (fixture assertion)
- **Tests:** `server/tests/test_conversation_parser.py` (`test_claude_permission_request_preserves_always_allow_rule`); collector hook test asserts `permission_suggestions` round-trip
- **Coverage gap:** No API/UI test that the option appears in pending-interactions JSON for a live client.

### 8. `a2d6484` — fix: refresh parents for path-linked subagents

- **Symptom:** Viewing a Claude/Cursor parent conversation did not refresh when a path-linked `/subagents/` child synced (subagent list / prompts stayed stale until a same-document event).
- **Root cause:** `useConversationPrompts` only reacted to SSE events whose `document_id` matched the open thread.
- **Fix:** Pass `toolId`/`relativePath` scope into `useConversationPrompts`; treat companion conversation SSE events under the same path-linked root (`pathLinkedRootId` / `isCompanionConversationEvent`) as refresh triggers.
- **Key files:** `web/src/lib/use-conversation-prompts.ts`, `web/src/app/conversations/[id]/page.tsx`
- **Tests:** None added in this commit.
- **Coverage gap:** **GAP** — no unit test for `pathLinkedRootId` / `isCompanionConversationEvent`; no web test for companion SSE refresh.

### 9. `790072f` — fix: reconcile Claude subagent launch metadata (v0.1.46)

- **Symptom:** Claude child cards showed wrong/missing launch descriptions depending on whether the `.meta.json` sidecar or `.jsonl` transcript arrived first; labels could cross-bind across siblings.
- **Root cause:** Sidecar launch metadata was not paired to an exact sibling transcript under a shared ingest lock; enrichment was order-sensitive.
- **Fix:** Exact path-safe sibling pairing (`_claude_subagent_sidecar_evidence`, `_reconcile_claude_subagent_launch_metadata`) with pair lock; protect launch fields in essential/protected metadata; prefer launch description on summaries.
- **Key files:** `server/server/services/ingest_service.py`, `server/server/services/conversation_hierarchy.py`
- **Tests:** `server/tests/test_claude_subagent_sidecars.py` (sidecar↔transcript either order, reject mismatch, bounded fields, event publish dedupe); `server/tests/test_conversation_hierarchy.py` (`test_subagent_summary_prefers_claude_launch_description`)
- **Coverage gap:** None for ingest pairing.

### 10. `d7d89df` — feat: expose user-scoped hierarchical tasks (v0.1.48)

- **Symptom / need:** No user-scoped API/MCP surface for hierarchical TodoWrite / plan task trees across conversations and agents.
- **Root cause:** Task state lived only inside transcripts; no projection table or selectors.
- **Fix:** Add `conversation_tasks` projection service, tasks API, DB model, ingest hooks, MCP `memory_tasks`, and backfill script.
- **Key files:** `server/server/services/conversation_tasks.py`, `server/server/api/tasks.py`, `server/server/db/models.py`, `mcp_server/mcp_server/server.py`, `mcp_server/mcp_server/remote_client.py`
- **Tests:** `server/tests/test_conversation_tasks.py`, `server/tests/test_tasks_api.py`, `server/tests/test_task_projection_integration.py`, `server/tests/test_mcp_tasks.py`
- **Coverage gap:** No Playwright/UI coverage (API/MCP covered). Included here because it shipped in the same release train and is a regression-sensitive surface.

### 11. `83951fb` — fix: preserve conversation navigation and agent context (v0.1.49)

- **Symptom:** Large-thread prompt jumps / reparses scrambled message order; Claude child user turns looked like human prompts; subagent lifecycle cards cross-correlated or flipped completed children back to running on replayed launches.
- **Root cause:** Client merge keyed only by DB id; prompt-jump windows could place the target beyond the server limit; Claude Agent tool launches lacked exact tool-use correlation; terminal status was not sticky.
- **Fix:** Extract `conversation-message-order.ts` (`mergeMessagesChronologically` by server line, `placeTargetWindow`, `contextBeforeIncludingTarget`); mark Claude child user turns `origin=parent_agent`; correlate lifecycle by `agent_tool_use_id` then thread id; sticky terminal status; launch-description display titles for children.
- **Key files:** `web/src/lib/conversation-message-order.ts`, `web/src/components/viewers/ConversationViewer.tsx`, `server/server/services/conversation_parser.py` (`normalize_claude_agent_launch_event`), `server/server/services/conversation_hierarchy.py`, `server/server/api/conversations.py`
- **Tests:** `web/tests/conversation-message-order.test.mjs` (line-order, backwards timestamps, around-window limits, detached windows); `server/tests/test_conversation_hierarchy.py` / `test_conversation_parser.py` / `test_conversation_markdown.py` / `test_conversations_normalized_api.py` (parent-agent origin, tool-use correlation, sticky terminal, launch titles)
- **Coverage gap:** ConversationViewer integration of detached tails not covered beyond the pure order helper tests.

### 12. `7cb05b0` — fix: preserve live Claude question prompts (v0.1.50)

- **Symptom:** Live AskUserQuestion prompts disappeared or duplicated when Claude also emitted a PermissionRequest wrapper for the same question; clients did not refresh on metadata-only interaction updates.
- **Root cause:** PermissionRequest wrappers replaced or raced the canonical AskUserQuestion side-file/signal; pending API did not fingerprint-dedupe wrappers; interaction updates did not publish SSE when only metadata changed.
- **Fix:** Treat wrapped AskUserQuestion as the canonical question in the collector hook (aliases, richer input, ignore malformed nests); `coerce_claude_live_interaction` / `interaction_question_fingerprint` on server; prune duplicate wrappers in `apply_conversation_interaction_update`; publish `_publish_file_synced_event` on metadata-only updates; pending API fingerprint dedupe.
- **Key files:** `collector/collector/claude_pending_hook.py`, `server/server/services/conversation_parser.py`, `server/server/services/thread_metadata_service.py`, `server/server/api/conversations.py`, `server/server/services/conversation_hierarchy.py`
- **Tests:** `collector/tests/test_claude_pending_questions.py` (wrapper does not replace question; stable id through answer; malformed ignored); `server/tests/test_conversation_parser.py` (wrapper → question, coerce from Yes dump, historical reparse); `server/tests/test_thread_metadata_service.py` (live wrapper is question/closes, duplicate prune, publish); `server/tests/test_conversations_normalized_api.py` (historical answered + pending preview); `server/tests/test_conversation_hierarchy.py` (explicit subagent boolean)
- **Coverage gap:** No Playwright coverage for the live question card refresh path.

### 13. (post-v0.1.50) — fix: Claude `async_launched` false-complete status

- **Symptom:** On the Compare-bjobs Claude thread (`conversation` `3f30d6db-90e4-43cc-948d-14a99eda3e1c`, session `c2badf82-0183-4c85-9191-d76222f66ede`), a launched background subagent showed **complete** in Memento while still **running** in Claude.
- **Root cause:** Genuine local bug. `_claude_agent_result_kind` in `conversation_parser.py` did not recognize Claude’s explicit `async_launched` enqueue status, so the launch tool_result fell through to terminal `completed`. Hierarchy projection then mapped that event to UI `completed`. DB evidence: launch row projected `running`, then 36 ms later the result row projected `completed` despite “working in the background”; the child stayed active for ~7 more minutes.
- **Fix:** Treat `async_launched` as a nonterminal status (alongside `running` / `started` / `pending` / `in_progress` / `background`), projecting it as `started`/`running`. Genuine foreground completions remain terminal.
- **Key files:** `server/server/services/conversation_parser.py`
- **Tests:** `server/tests/test_claude_async_agent_lifecycle.py` (async launch stays running with no completion timestamp; hierarchy projects `running`; foreground completion stays terminal)
- **Coverage gap:** Existing normalized rows for already-ingested launches remain incorrectly terminal until reparse/backfill. Sidecar reconciliation that requires `agentId` still misses title enrichment when sidecars omit the field (use filename ID). Confirm eventual background-completion notifications still project so corrected agents do not stay `running` forever. UI/Playwright coverage of the running badge is tracked under Workstream D.

### 14. v0.1.51 — conversation smart links across Claude, Cursor, and Codex

- **Symptom:** Conversation links looked like raw Markdown: file paths and SHAs were plain monospace, repository compares had no provider/ref structure, generic URLs lacked a domain cue, and long-message expansion was a weak text-only action.
- **Fix:** Normalize links in the shared `MarkdownViewer` render layer. File links and inline paths now use document chips (including `+N -M` stats); GitHub/GitLab compare and commit URLs show provider icons plus ref pills; generic web links show a domain cue; inline SHAs use compact pills; the expand control is an accessible icon button.
- **Why all three tools are covered:** Claude, Cursor, and Codex conversation prose all reaches the same `MarkdownViewer` component. No collector-specific markup is required.
- **Key files:** `web/src/components/viewers/SmartLink.tsx`, `SmartLink.module.css`, `MarkdownViewer.tsx`, `ConversationViewer.tsx`, `web/src/lib/smart-link-classifier.mjs`
- **Tests:** `web/tests/smart-link-classifier.test.mjs`; fixture invariants in `web/e2e/fixtures/mock-router.test.mjs`; real Chromium assertions for all three tool ids in `web/e2e/smart-links.spec.mjs`.

### 15. v0.1.53 — Slack-like inline code across conversation tools

- **Symptom:** Plain Markdown inline code was only monospace because the typography utility styles were not reliably emitted; it had no background, padding, radius, or accent color.
- **Fix:** Give plain `<code>` nodes an explicit shared CSS-module class with a token-based neutral chip, restrained tan text, compact spacing, and dark-mode colors. Fenced code is explicitly excluded/reset and remains a block.
- **Why all three tools are covered:** Claude, Cursor, and Codex message prose uses the shared `MarkdownViewer` / `SmartCode` renderer.
- **Tests:** `web/e2e/smart-links.spec.mjs` checks computed light/dark inline-chip styling for all three tools and separately verifies fenced code keeps its syntax-highlighted block layout.

### 16. `feat/canvas-viewer` — secure Canvas artifact preview

- **Need:** Conversation transcripts can reference `*.canvas.tsx` artifacts, but Memento previously rendered them as ordinary file links with no artifact-specific presentation.
- **Feasibility contract:** Self-contained HTML exports render in a locked-down iframe; validated `http(s)` artifact URLs use the same opaque-origin sandbox; transcript-carried TSX is inert read-only text; link-only artifacts show an honest unsupported fallback. Memento does not compile TSX and does not claim access to Cursor's Canvas runtime.
- **Security:** Server descriptors are bounded by path, scheme, type, source/HTML size, and per-message count checks and never read files from disk. Inline HTML gets a fresh security head before all untrusted markup, `default-src 'none'` plus explicit no-connect/frame/object/worker directives, and an iframe sandbox containing only `allow-scripts` (no same-origin or popup permission). A remote URL necessarily performs its requested network navigation; because cross-origin HTML cannot accept an injected CSP, URL mode is separately protected by strict `http(s)` validation, opaque origin, no referrer, and denied browser capabilities.
- **Key files:** `server/server/services/canvas_artifacts.py`, `server/server/api/conversations.py`, `web/src/lib/canvas-artifact.mjs`, `web/src/lib/canvas-context.tsx`, `web/src/components/viewers/CanvasViewer.tsx`, `web/src/components/viewers/SmartLink.tsx`
- **Tests:** `server/tests/test_canvas_artifacts.py`; `web/tests/canvas-artifact.test.mjs`; classifier/router fixture tests; `web/e2e/canvas-viewer.spec.mjs` covers Claude, Cursor, and Codex plus desktop modal, mobile sheet, source, HTML embed, unsupported fallback, focus restoration, sandbox, capability policy, and CSP placement.
- **Screenshots:** `artifacts/canvas-viewer/cursor-source-desktop.png`, `artifacts/canvas-viewer/cursor-source-mobile.png`, `artifacts/canvas-viewer/codex-embed-desktop.png`, `artifacts/canvas-viewer/claude-unsupported-desktop.png`

### 17. Pending — Cursor child origin and current-task carrier projection

- **Symptom:** In production Cursor thread `32034817-ddef-4d00-a871-6d041f8b94bc`, child dispatches were labeled/countable as human prompts and the mutable current-task state appeared twice: once as the pinned **Active task list** and again as a historical **Task update** before the first real prompt. This made an otherwise complete thread look tangled.
- **Data-loss audit:** No root messages were missing. At the 17:09 UTC snapshot, the raw export, normalized rows, and API each had 182 records/messages with contiguous lines 1–182 and 182 unique normalized source IDs; 170 source IDs were preserved byte-for-byte and 12 were intentionally transformed into stable task-completion IDs. The root later grew normally while the Cursor session remained active (243 messages in the desktop/mobile browser snapshot, 257 in a later API capture, and 272 at final handoff). At 272, source records, normalized rows, unique lines, unique source IDs, and API total still agreed exactly; 260 source IDs were preserved byte-for-byte and the same 12 task-completion IDs were intentionally stabilized. All seven child documents had complete API totals matching their stored normalized rows. Four raw-vs-normalized child count differences were repeated shell-finished notifications correctly deduplicated to four stable completion events, not lost user/assistant turns.
- **Root cause:** `conversation_user_role_origin` recognized only Claude children even though Cursor `/subagents/` documents use the same parent-dispatch semantics. Their API `origin` remained null and `/prompts` returned one or two parent directives per child. Separately, Cursor state export intentionally transports one mutable `cursor_state_task` record at stable line 1 so task projection can update incrementally; `ConversationViewer` rendered that carrier row after already rendering the same projected state in the pinned task card.
- **Fix:** Classify path/metadata-linked Cursor children as `parent_agent`, which also removes their parent directives from child prompt navigation. Suppress only a `task_state.is_current` message whose ordered task payload exactly matches the pinned active state; historical task transitions remain visible. Source/export and stored normalized rows stay unchanged.
- **Repair:** No targeted reparse/re-export was run because stored projection was complete and correctly ordered. Both fixes are request/render-time and take effect for historical documents without mutating production rows.
- **Key files:** `server/server/services/conversation_hierarchy.py`, `web/src/lib/conversation-message-order.ts`, `web/src/components/viewers/ConversationViewer.tsx`
- **Tests:** 47 focused server tests + 6 subtests; 15 focused Cursor collector tests; 41 browser-free web tests (including 7 message-order tests); six focused real-Chromium scenarios (desktop 1440×1000 and mobile 472×1024 included); full hermetic Playwright suite 15/15. The deterministic `cursorThreadProjection` fixture preserves the line-1 task carrier and interleaved child completion order from the production shape.

### 18. Claude live prompt Unicode and mobile clearance

- **Symptom:** The dreamland-yoga live AskUserQuestion card for session `c2badf82-0183-4c85-9191-d76222f66ede` rendered em dashes as `â€”`, exposed the metadata-only placeholder `Line 0`, and let the fixed mobile Prompt navigator cover the second question.
- **Root cause:** On Windows, the Claude hook parsed `sys.stdin` through the process ANSI text codec even though Claude writes UTF-8 JSON bytes. The side file, collector signal, server normalization, and JSON response then faithfully preserved that already-corrupted string. Live metadata intentionally uses nonpositive message/line placeholders before transcript persistence, but the viewer rendered the placeholder as a jump target. The floating mobile navigator had safe-area positioning without corresponding pending-card/transcript scroll clearance.
- **Fix:** Decode the hook's binary stdin as UTF-8 (BOM-tolerant) before JSON parsing. Repair only safely detectable UTF-8-as-CP1252 prompt fields on server normalization/read paths so already-stored live metadata recovers without globally reinterpreting Unicode. Hide nonpositive live line locations, retain positive-line navigation, and dock the mobile navigator after pending interactions.
- **Key files:** `collector/collector/claude_pending_hook.py`, `server/server/services/conversation_parser.py`, `web/src/components/viewers/ConversationViewer.tsx`
- **Tests:** `collector/tests/test_claude_pending_questions.py` covers UTF-8 bytes through the Windows-codec simulation, side file, and collector signal; `server/tests/test_conversation_parser.py` and `test_conversations_normalized_api.py` cover correct/repaired em dashes plus legitimate-Unicode negative cases; `web/e2e/claude-live-prompt-mobile.spec.mjs` uses the two-question payload in real Chromium at desktop and 472×1024 mobile sizes, including navigator operation, overlap geometry, and zero console/page errors.

---

## Test coverage matrix

| Feature / Bug | Covering test(s) | Type | Status |
|---|---|---|---|
| Attention badges / replayed questions (`8ac973e`) | `test_conversations_normalized_api.py`, `test_reparse_conversations.py`, `test_thread_metadata_service.py` | API / unit | covered |
| Scheduled automations as context (`8ac973e`) | `test_conversation_parser.py`, `test_conversation_markdown.py` | unit | covered |
| Adjacent page scroll load (`8ac973e`) | — | — | **GAP** |
| Claude live AskUserQuestion hook (`99192ab`) | `collector/tests/test_claude_pending_questions.py` | unit | covered |
| Claude live prompt Unicode / mobile clearance | collector + parser/API tests + `claude-live-prompt-mobile.spec.mjs` | unit / API / Chromium | covered |
| Cursor Plan-mode approvals (`99192ab`) | `test_cursor_structured_tools.py`, `test_thread_metadata_service.py`, collector state/signal tests | unit / API | covered |
| Cursor task completions (`d77e480`) | `test_conversation_parser.py`, `test_conversations_normalized_api.py`, `test_cursor_state_export.py` | unit / API | covered |
| Conversation location header (`d77e480`) | normalized API location helpers | API | covered (UI **GAP**) |
| Cursor directives ≠ human prompts (`cb7e648`) | `test_conversation_parser.py`, `test_conversation_markdown.py` | unit | covered |
| Subagent model / lifecycle times (`cb7e648`) | `test_conversation_hierarchy.py`, `test_cursor_state_export.py` | unit | covered |
| Directive thread titles (`8a5349d`) | `test_ingest_titles.py` | unit | covered |
| Live permission / elicitation capture (`a7422d3`) | collector + `test_conversation_parser.py` + `test_thread_metadata_service.py` | unit / API | covered |
| Cursor native message timestamps (`a7422d3`) | `test_cursor_state_export.py`, `test_conversations_normalized_api.py` | unit / API | covered |
| Always-allow permission option (`8e88ba4`) | `test_claude_permission_request_preserves_always_allow_rule` (+ collector fixture) | unit | covered (pending-API **GAP**) |
| Parent refresh for path-linked children (`a2d6484`) | — | — | **GAP** |
| Claude sidecar launch pairing (`790072f`) | `test_claude_subagent_sidecars.py`, hierarchy summary test | API / unit | covered |
| Hierarchical tasks API/MCP (`d7d89df`) | `test_conversation_tasks.py`, `test_tasks_api.py`, `test_task_projection_integration.py`, `test_mcp_tasks.py` | unit / API | covered (UI **GAP**) |
| Message order / prompt-jump windows (`83951fb`) | `web/tests/conversation-message-order.test.mjs` | unit (web) | covered |
| Parent-agent origin / sticky lifecycle (`83951fb`) | hierarchy + parser + normalized API tests | unit / API | covered |
| Cursor child parent-agent origin / prompt suppression (pending) | hierarchy + normalized API tests; `parent-agent-labeling.spec.mjs` | unit / API / Playwright | covered |
| Cursor current-task carrier rendered once (pending) | `conversation-message-order.test.mjs`; `cursor-thread-projection.spec.mjs` | unit / Playwright | covered (desktop + mobile) |
| AskUserQuestion vs PermissionRequest wrapper (`7cb05b0`) | collector + parser + thread_metadata + normalized API tests | unit / API | covered |
| Metadata-only live prompt SSE publish (`7cb05b0`) | `test_thread_metadata_service.py` (publish assertions) | unit / API | covered |
| Claude `async_launched` false-complete (post-v0.1.50) | `test_claude_async_agent_lifecycle.py`; `web/e2e/subagent-status.spec.mjs` | unit / Playwright | covered (reparse/backfill still needed; browser run verified on Node 24) |
| Smart file/repo/web links across Claude, Cursor, and Codex (v0.1.51) | `smart-link-classifier.test.mjs`, `smart-links.spec.mjs` | unit / Playwright | covered (3/3 tool scenarios) |
| Inline code chips across Claude, Cursor, and Codex (v0.1.53) | `smart-links.spec.mjs` | Playwright | covered (computed style + fenced-block regression) |
| Secure Canvas artifact viewer (`feat/canvas-viewer`) | `test_canvas_artifacts.py`, `canvas-artifact.test.mjs`, `canvas-viewer.spec.mjs` | unit / Playwright | covered (source, HTML/URL policy, fallback, desktop/mobile, security attributes) |
| End-to-end live conversation UX | `web/e2e/*.spec.mjs` (+ `web/e2e/fixtures/*`) | Playwright (fixture/mock) | covered; integrated release count pending |

---

## Coverage gaps / recommended follow-ups

1. **Reparse/backfill false-complete rows (post-v0.1.50)** — After deploying the `async_launched` parser fix, reparse the Compare-bjobs conversation (and any other background Agent launches ingested before the fix) so stored lifecycle events flip from sticky `completed` back to `running`/`completed` correctly.
2. **Path-linked parent SSE refresh (`a2d6484`)** — Add a focused unit test for `pathLinkedRootId` / `isCompanionConversationEvent` in `web/` (export helpers or thin test seam). Highest-priority web GAP in this range.
2. **Adjacent scroll page loading (`8ac973e`)** — Cover `loadEarlier` / downward `loadMore` threshold behavior (component test or extracted scroll policy helper). Currently UI-only.
3. **Always-allow pending API surface (`8e88ba4`)** — Assert normalized pending-interactions payload includes the `allow-always` option id/label for a live Claude permission signal.
4. **ConversationLocation / SubagentBadge UI** — Optional front-end smoke tests; server already returns the data.
5. **Playwright / E2E** — Hermetic suite under `web/e2e/` is fixture-driven with SSE aborted. The integrated browser-free and Chromium counts will be recorded after the release-candidate run. Remaining UI gaps: path-linked companion refresh and directive title-cleaning.
6. **Hierarchical tasks UI (`d7d89df`)** — API/MCP well covered; if a web tasks browser ships later, add matching UI tests.
7. **ConversationViewer detached-tail integration (`83951fb`)** — Pure order helpers are covered; consider one integration test that `placeTargetWindow` results drive `data-detached-*` attributes after a prompt jump.

---

## Commit index (verify)

| SHA | Version | Subject |
|---|---|---|
| `8ac973e` | v0.1.44 | fix: reconcile conversation attention and navigation |
| `99192ab` | (pre-v0.1.45) | fix: preserve live approval interactions |
| `d77e480` | v0.1.45 | fix: surface Cursor task completions |
| `cb7e648` | | fix: classify Cursor directives and surface subagent metadata |
| `8a5349d` | | fix: clean Cursor directive conversation titles |
| `a7422d3` | | fix: preserve live prompts and message metadata |
| `8e88ba4` | | fix: preserve Claude always-allow permission choices |
| `a2d6484` | | fix: refresh parents for path-linked subagents |
| `790072f` | v0.1.46 | fix: reconcile Claude subagent launch metadata |
| `d7d89df` | v0.1.48 | feat: expose user-scoped hierarchical tasks |
| `83951fb` | v0.1.49 | fix: preserve conversation navigation and agent context |
| `7cb05b0` | v0.1.50 | fix: preserve live Claude question prompts |
| `f8f4f16` | v0.1.50 | release: v0.1.50 |
