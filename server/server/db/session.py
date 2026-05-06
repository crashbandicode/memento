"""Database session factory."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..config import settings

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    # Postgres default max_connections=100. With api + celery_worker +
    # celery_beat each owning a pool, total has to leave room. 25+25 per
    # service * 3 services = 150 max < 100 would breach. Keep total per
    # service ≤ 32 so 3 services + admin/migration < 100.
    pool_size=20,
    max_overflow=12,
    pool_recycle=3600,
    pool_timeout=10,  # fail fast instead of stalling user requests 30s
)

# Separate engine for post-ingest (embedding + knowledge graph) so a re-sync
# storm can't starve the user-facing request pool. Smaller pool because
# post-ingest is already capped at 8 concurrent tasks via Semaphore.
post_ingest_engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=8,
    max_overflow=4,
    pool_recycle=3600,
    pool_timeout=15,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

post_ingest_session_factory = async_sessionmaker(
    post_ingest_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that provides a database session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
