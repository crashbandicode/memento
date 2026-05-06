"""Celery application configuration."""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_process_init

from ..config import settings


@worker_process_init.connect
def _reset_engines_on_fork(**_kwargs) -> None:
    """When celery prefork spawns a child worker, the parent's SQLAlchemy
    engines (with live asyncpg sockets) are inherited via fork(2). Two
    children then race on the same TCP socket → asyncpg raises
    ``InterfaceError: cannot perform operation: another operation is in
    progress`` and every post-ingest transaction rolls back, embeddings
    silently never land.

    Standard fix per SQLAlchemy docs: dispose the engine on fork with
    close=False (don't actually close the parent's sockets; just drop the
    child's pool entries so it lazily creates fresh connections of its own).
    """
    try:
        from ..db.session import engine, post_ingest_engine
        engine.sync_engine.dispose(close=False)  # type: ignore[attr-defined]
        post_ingest_engine.sync_engine.dispose(close=False)  # type: ignore[attr-defined]
    except Exception:
        # If imports fail at fork time (e.g. session module not yet loaded),
        # the next request will lazily build a fresh engine anyway.
        pass

celery_app = Celery(
    "memento",
    broker=settings.redis_url,
    backend=settings.redis_url,
    # Explicit module list so workers register every task (our CLI is
    # `celery -A server.tasks.celery_app worker`, which only imports this
    # module — beat_schedule entries would otherwise fail with
    # "Received unregistered task").
    include=[
        "server.tasks.daily_digest",
        "server.tasks.summary_tasks",
        "server.tasks.embedding_retry",
        "server.tasks.tsvector_backfill",
        "server.tasks.db_backup",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=600,  # 10 min max per task
    worker_max_tasks_per_child=100,
    # Reliability defaults so a crashed / SIGKILLed worker doesn't swallow
    # tasks silently. acks_late: ack only after success; reject_on_worker_lost:
    # if worker dies mid-task, Redis requeues it to another worker.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
)

# Scheduled tasks
celery_app.conf.beat_schedule = {
    "daily-digest": {
        "task": "server.tasks.daily_digest.generate_daily_digest",
        "schedule": crontab(hour=23, minute=30),  # Run at 23:30 every day
    },
    # Every 15 min: reattempt documents whose embedding pipeline errored
    # (e.g. the host-side BGE-M3 server was briefly unreachable).
    "embedding-retry": {
        "task": "server.tasks.embedding_retry.retry_failed_embeddings",
        "schedule": crontab(minute="*/15"),
    },
    # Daily 03:30 — pg_dump | gzip → s3://memento-backups/daily/<date>.sql.gz,
    # rolling 14-day retention. Defends against the kind of incident that
    # wiped pgdata (volume nuke, install --purge, etc.).
    "daily-db-backup": {
        "task": "server.tasks.db_backup.run_daily_backup",
        "schedule": crontab(hour=3, minute=30),
    },
}
