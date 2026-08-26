# MinIO content externalization handoff

**Date:** 2026-08-26  
**Worktree:** `C:\Users\intpa\OneDrive\Documents\test\memento-control-plane`  
**Branch:** `main` (working tree changes only; no commit or push)

## Current objective

Complete the safe pre-nulling work after the verified document-content
backfill: direct MCP reads, Canvas notification-driven collection, and the
dry-run-first nulling utility described in [MINIO_CONTENT_DESIGN.md](./MINIO_CONTENT_DESIGN.md).

## Implemented

- Direct MCP mode has a package-local verified MinIO reader. It is disabled
  unless the client running the sidecar provides all of `MEMENTO_S3_ENDPOINT`,
  `MEMENTO_S3_ACCESS_KEY`, `MEMENTO_S3_SECRET_KEY`, and `MEMENTO_S3_BUCKET`.
  Missing configuration and all object GET/proof failures fall back to the
  existing PostgreSQL compatibility content.
- New eligible Canvas references enqueue `canvas.sync` through the existing
  durable control poll channel. Collector `0.0.50` consumes that command to
  schedule a bounded pending-artifact poll; ordinary conversation uploads no
  longer reset the Canvas poll backoff.
- `server.scripts.null_document_content` is dry-run by default and only
  nulls an inline value after streaming its immutable object and proving both
  the stored object pointer and the current PostgreSQL UTF-8 bytes agree.

## Verification evidence

- `mcp_server/tests/test_content_store.py`: **1 passed** (no S3 configuration
  reads the PostgreSQL compatibility value).
- `server/tests/test_canvas_backfill_integration.py::test_new_messages_project_canvas_references_without_inventory_scan` and
  `server/tests/test_null_document_content.py::test_apply_refuses_to_null_when_pg_bytes_do_not_match_pointer` against
  `postgresql+asyncpg://postgres:test@localhost:55437/postgres`: **2 passed**.
- `collector/tests/test_canvas_sync.py` and `collector/tests/test_control_channel.py`:
  **15 passed**. `collector/tests/test_main.py` could not collect in the
  supplied Canvas venv because it lacks the declared `concurrent-log-handler`
  and `watchdog` packages; no repository dependency was changed to work around
  that environment issue.
- `cargo clippy --all-targets` and `cargo build --no-default-features` in
  `tauri-collector/src-tauri`: **passed**.

Do not execute the nulling script with `--apply` against production.
