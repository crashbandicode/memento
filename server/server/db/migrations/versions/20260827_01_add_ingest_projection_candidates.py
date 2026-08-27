"""add ingest_projection_candidates outbox

Revision ID: 20260827_01
Revises:
Create Date: 2026-08-27

This repo applies additive DDL through ``server.main._run_migrations`` on API
boot (plus SQLAlchemy ``create_all`` for fresh installs/tests).  This Alembic
revision documents the same Phase 4 outbox table so a later alembic-first
cutover has a reviewable root migration.  The CREATE TABLE is idempotent.
"""

from __future__ import annotations

from alembic import op

revision = "20260827_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_projection_candidates (
            id BIGSERIAL PRIMARY KEY,
            document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            revision_hash VARCHAR(64) NOT NULL,
            kind VARCHAR(32) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            claimed_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            superseded_at TIMESTAMPTZ,
            CONSTRAINT uq_ingest_projection_candidate_fence
                UNIQUE (document_id, revision_hash, kind),
            CONSTRAINT ck_ingest_projection_candidate_kind
                CHECK (kind IN ('canvas', 'search'))
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ingest_projection_candidates_pending
            ON ingest_projection_candidates (document_id, kind, created_at)
            WHERE completed_at IS NULL AND superseded_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_ingest_projection_candidates_pending")
    op.execute("DROP TABLE IF EXISTS ingest_projection_candidates")
