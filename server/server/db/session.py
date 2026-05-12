"""Database session factory."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..config import settings

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    # docker-compose.yml bumps Postgres max_connections to 200, so we have
    # ~50 connection budget per service across api / celery-worker /
    # celery-beat + admin/migration headroom. Pool 30+30=60 gives the
    # ingest semaphore (24 concurrent) breathing room when each ingest
    # holds a connection 1-3s while waiting on post-ingest queueing,
    # without starving the every-10s collector heartbeats + command polls.
    pool_size=30,
    max_overflow=30,
    pool_recycle=3600,
    pool_timeout=10,  # fail fast instead of stalling user requests 30s
)

# Separate engine for post-ingest (embedding + knowledge graph) so a re-sync
# storm can't starve the user-facing request pool. Smaller pool because
# post-ingest is already capped at 8 concurrent tasks via Semaphore.
post_ingest_engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=12,
    max_overflow=8,
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
