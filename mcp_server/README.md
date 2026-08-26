# Memento MCP Server

Personal AI memory powered by your Memento data.

## Usage

```bash
pip install memento-brain-memory
memento-memory --db-url postgresql+asyncpg://user:pass@host:port/memento
```

### Direct-mode MinIO content reads

Direct mode keeps reading `documents.content` from PostgreSQL unless all of
these variables are set on the machine running the MCP sidecar:

```text
MEMENTO_S3_ENDPOINT=https://minio.example.internal
MEMENTO_S3_ACCESS_KEY=<read-only key>
MEMENTO_S3_SECRET_KEY=<read-only secret>
MEMENTO_S3_BUCKET=memento
```

With that complete configuration, the sidecar streams and SHA-256-verifies a
document's verified object before using it. Any missing configuration,
transport failure, or proof mismatch falls back to PostgreSQL during the
dual-read rollout. Remote mode does not need these variables.

## Claude Code Configuration

```json
{
  "mcpServers": {
    "memento-memory": {
      "command": "memento-memory",
      "args": ["--db-url", "postgresql+asyncpg://postgres:postgres@localhost:5433/memento"]
    }
  }
}
```

## Usage telemetry

`memory_usage_cycle(since, until, tool="all", include_threads=false)` returns
raw native token counts for a half-open ISO-8601 time range. Results are grouped
by model and reasoning effort and keep Claude cache reads and writes separate.
Set `include_threads=true` to include document/native IDs, titles, activity
bounds, per-thread model selections, and their token totals. Cursor usage stays
explicitly unattributed because Cursor does not currently expose exact native
token accounting.

`memory_conversation_info(document_id)` returns the same lifetime token/model
metadata for one thread. These tools deliberately do not calculate prices or
billing; consumers should apply their own pricing source and reporting logic.
