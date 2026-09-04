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
