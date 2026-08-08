"""Database session factory."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..config import settings


class TransactionalAsyncSession(AsyncSession):
    """Async session with rollback-safe, post-commit side-effect delivery."""

    async def commit(self) -> None:
        await super().commit()
        try:
            from ..services.cache import publish_staged_cache_invalidations

            await publish_staged_cache_invalidations(self)
        except Exception:
            # A committed transaction must not look failed because an optional
            # cache backend is unavailable.
            pass

    async def rollback(self) -> None:
        await super().rollback()
        from ..services.cache import discard_staged_cache_invalidations

        discard_staged_cache_invalidations(self)


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

# Search is optionally isolated from the write-heavy primary. Keep the default
# as the existing engine so self-hosted installs do not create a second pool or
# require a replica. A configured search URL receives a deliberately smaller,
# read-only pool.
_search_database_url = settings.search_database_url.strip()
if _search_database_url and _search_database_url != settings.database_url:
    search_engine = create_async_engine(
        _search_database_url,
        echo=False,
        pool_size=10,
        max_overflow=5,
        pool_recycle=3600,
        pool_timeout=10,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "default_transaction_read_only": "on",
                "application_name": "memento-search",
            }
        },
    )
else:
    search_engine = engine

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
    class_=TransactionalAsyncSession,
    expire_on_commit=False,
)

search_session_factory = (
    async_session_factory
    if search_engine is engine
    else async_sessionmaker(
        search_engine,
        class_=TransactionalAsyncSession,
        expire_on_commit=False,
    )
)

post_ingest_session_factory = async_sessionmaker(
    post_ingest_engine,
    class_=TransactionalAsyncSession,
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


async def get_search_db() -> AsyncGenerator[AsyncSession, None]:
    """Provide a read-only search session, optionally backed by a replica."""
    async with search_session_factory() as session:
        try:
            yield session
        finally:
            # End the read transaction promptly so a primary fallback does not
            # retain snapshots that delay vacuum.
            await session.rollback()
