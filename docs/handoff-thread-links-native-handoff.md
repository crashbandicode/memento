# Native handoff-thread links checkpoint

Date: 2026-09-04

## Objective and scope

Generalize the existing read-time handoff navigation from Claude-only
conversations to Codex, Cursor, and cross-engine handoffs. The web already
renders the generic `handoff_predecessor` / `handoff_successor` response shape,
so this checkpoint changes only API resolution and regression coverage.

## Working state

- Worktree: `C:\Users\intpa\OneDrive\Documents\test\memento-context-governor`
- Branch: `feat/thread-handoff-health`
- Base HEAD before these uncommitted changes:
  `4dfdf1c283be282b68cc5d362b42a8283ed06206`
- The branch was already one commit ahead of
  `fork/feat/thread-handoff-health`.
- Modified source/tests are uncommitted. Nothing was pushed or deployed.
- The live API/web and the protected `memento-release` worktree were not
  restarted or changed.

## Decisions and implementation

- Handoff link resolution now applies to all foldable native tools:
  `claude_code`, `codex`, and `cursor`.
- Successor-to-predecessor lookup prefers persisted ingest metadata
  (`briefing_kind=handoff`, `briefing_session_id=<UUID>`), then resolves the
  parent by native `metadata.session_id` across visible native tools.
- Legacy Claude rows keep the prior first-user-marker and `.jsonl` path
  fallback.
- Codex/Cursor rows without handoff metadata skip that legacy first-message
  read; ordinary detail views therefore add only the existing bounded,
  indexed reverse FTS probe.
- Duplicate revisions are reduced with the existing per-tool canonical
  conversation selector. A native UUID collision across engine families
  fails closed instead of choosing an arbitrary parent.
- Tangent resolution retains the original Claude path helper name; this fixed
  the compatibility regression caught during the first targeted run.

## Evidence

- `ruff check` passed for all three modified Python files.
- `python -m compileall -q` passed for the modified Python files.
- `git diff --check` passed (only Git's expected LF-to-CRLF notices).
- PostgreSQL-backed focused regression command passed:
  `121 passed, 18 subtests passed` across handoff, tangent, native URL,
  hierarchy, and normalized-conversation API tests.
- New integration cases cover Codex-to-Codex links and a Cursor-to-Codex
  cross-engine handoff in both directions.
- Repository-mandated `cargo clippy --all-targets` and
  `cargo build --no-default-features` were attempted separately. Both stop in
  the unchanged Tauri build script because
  `tauri-collector/src-tauri/icons/icon.ico` is absent. Cargo's generated,
  untracked `Cargo.lock` was removed afterward; no Rust files remain changed.

## Exact next commands

Run from the worktree with PowerShell 7:

```powershell
git -c safe.directory='C:/Users/intpa/OneDrive/Documents/test/memento-context-governor' diff --check
git -c safe.directory='C:/Users/intpa/OneDrive/Documents/test/memento-context-governor' status --short --branch
```

After review, commit locally if requested. A refresh of the reported live
thread will not show the new navigation until these changes go through the
approved release/deployment path; do not mutate `memento-release` or restart
live services from this worktree.

## Release authorization — 2026-09-04

The operator explicitly authorized commit, push, and deployment everywhere.
The authoritative chain policy still limits the push target to `fork` and
forbids `origin`. Current topology inspection found one live Memento application
stack in the local `desktop-linux` Docker context; the linked Kubernetes context
is an unrelated, currently unreachable corporate cluster, and the fork's Fleet
workflow has not run since 2026-08-08. This server-only change therefore deploys
to the live Compose `api` service. Web, workers, ingestion, projection, storage,
database, Redis, collectors, and AI/MCP processes are excluded because none
consume the changed code path.

## Pre-recreate release checkpoint — 2026-09-04

- Source commit `ff10533e4904a7fa20d0d9832eb497f8c7ac9fbe` is pushed
  exactly to `fork/feat/thread-handoff-health`; `origin` is untouched.
- The clean source commit built successfully as idle image
  `memento-api:latest`, image ID
  `sha256:130927f8ebb4f1aac8428081d465e1171b2711939f9f80966dcff6ecc8d7ead5`.
- The running API is still container `18ca50fcf7b2`, image
  `sha256:3c4f9362b116435dfdfa3b20bb4c621935c9809b57ed775763765790e436d62f`,
  started `2026-09-02T15:21:05.179Z`, restart count 0.
- Immediate rollback tag
  `memento-api:rollback-pre-native-handoff-ff10533e` resolves exactly to that
  running pre-release image.
- The complete pre-recreate excluded-service inventory is captured in this
  session. All ten excluded Memento web/worker/ingest/projection/data services
  were running with restart count 0. Next mutation is exactly:
  `docker compose up -d --no-deps --no-build --force-recreate api`.

## Deployment and acceptance — 2026-09-04

- The first recreation command was run without the production env file because
  this worktree has no `.env`. Production validation correctly rejected the
  insecure Compose defaults; the API returned 502 and its restart policy made
  eight failed startup attempts. The public web application remained HTTP 200.
  No request reached application startup, no data migration ran, and no other
  service changed.
- Root cause was deployment-path configuration, not application code. The
  existing production env was found at the canonical WSL source worktree and
  passed directly to Compose without printing or persisting any value. The API
  was recreated again with that env and recovered normally.
- Accepted API container is `34680a200225`, running image
  `sha256:130927f8ebb4f1aac8428081d465e1171b2711939f9f80966dcff6ecc8d7ead5`,
  started `2026-09-04T20:16:20.490Z`, restart count 0. Logs show application
  startup complete and live ingest requests returning 200.
- Host and deployed `/app/server/api/conversations.py` hashes match exactly:
  `D558322F189B61001D325AEEF51B6850C811BA8E7CF00627F12D23FE60D6599E`.
- Public `/app` returns HTTP 200. Authenticated read-only acceptance against the
  reported Codex thread `01a06c8c-5020-7090-a891-81c734f8216e` returns HTTP 200
  and its predecessor `01a05cf3-4217-7260-8352-9cb4207976c4`. The live page
  visibly renders `Continued from Remove tiers 1 and 2` with that exact parent
  link. The owner token stayed in browser memory and was never printed or
  persisted.
- Final isolation audit matched every excluded web/worker/ingest/projection/
  embedding/MinIO/PostgreSQL/Redis container ID, image, start time, running
  state, and restart count 0 exactly to preflight.
