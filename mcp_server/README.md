# Memento MCP Server

Personal AI memory powered by your Memento data.

## Usage

```bash
pip install memento-brain-memory
memento-memory --db-url postgresql+asyncpg://user:pass@host:port/memento
```

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
