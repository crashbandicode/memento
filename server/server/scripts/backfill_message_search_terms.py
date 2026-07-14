"""Rebuild the compact typo-correction vocabulary from normalized messages.

Exact and full-text message indexes are live immediately. This derived table
contains only unique, bounded words and lets pg_trgm correct misspelled query
tokens without fuzzy-scanning every transcript body.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from ..db.session import engine


REBUILD_STATEMENTS = (
    r"""CREATE TEMP TABLE rebuilt_conversation_search_terms (
    term VARCHAR(64) PRIMARY KEY,
    frequency BIGINT NOT NULL
) ON COMMIT DROP""",
    r"""INSERT INTO rebuilt_conversation_search_terms (term, frequency)
SELECT lower(word)::varchar(64), SUM(ndoc)::bigint
FROM ts_stat($query$
    SELECT to_tsvector('simple', content)
    FROM conversation_messages
    WHERE role IN ('user', 'assistant')
$query$)
WHERE char_length(word) BETWEEN 3 AND 64
  AND word ~ '^[a-z][a-z0-9_''-]{2,63}$'
GROUP BY lower(word)""",
    r"""INSERT INTO conversation_search_terms (term, frequency, updated_at)
SELECT term, frequency, now()
FROM rebuilt_conversation_search_terms
ON CONFLICT (term) DO UPDATE
SET frequency = EXCLUDED.frequency,
    updated_at = EXCLUDED.updated_at""",
    r"""DELETE FROM conversation_search_terms AS current
WHERE NOT EXISTS (
    SELECT 1
    FROM rebuilt_conversation_search_terms AS rebuilt
    WHERE rebuilt.term = current.term
)""",
)


async def rebuild_message_search_terms() -> int:
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL statement_timeout = 0"))
        for statement in REBUILD_STATEMENTS:
            await connection.execute(text(statement))
        count = (
            await connection.execute(
                text("SELECT count(*) FROM conversation_search_terms")
            )
        ).scalar_one()
    async with engine.begin() as connection:
        await connection.execute(text("ANALYZE conversation_search_terms"))
    return int(count)


async def _main() -> None:
    count = await rebuild_message_search_terms()
    print(f"conversation search lexicon ready: {count:,} terms")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
