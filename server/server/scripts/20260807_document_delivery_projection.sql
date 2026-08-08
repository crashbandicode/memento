-- Online migration for existing PostgreSQL installations.
--
-- Run with psql autocommit enabled. There is intentionally no documents
-- UPDATE/backfill: legacy rows are read through COALESCE fallbacks and acquire
-- a projection lazily on their next changed ingest.

CREATE TABLE IF NOT EXISTS document_delivery_state (
    document_id UUID PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    project_id UUID REFERENCES projects(id) ON DELETE SET NULL,
    revision_hash VARCHAR(64) NOT NULL,
    file_size_bytes BIGINT NOT NULL,
    delivery_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_modified_at TIMESTAMPTZ,
    activity_at TIMESTAMPTZ,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Reserve heap space so repeated delivery-only updates remain HOT. These
-- storage parameter changes are metadata-only and do not rewrite the table.
ALTER TABLE document_delivery_state SET (
    fillfactor = 70,
    autovacuum_vacuum_scale_factor = 0.02,
    autovacuum_vacuum_threshold = 100
);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_document_delivery_activity
    ON document_delivery_state (activity_at DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_document_delivery_project_activity
    ON document_delivery_state (project_id, activity_at DESC);

-- DELTA writes no longer touch these legacy columns. Drop their indexes online
-- after all application instances understand document_delivery_state.
DROP INDEX CONCURRENTLY IF EXISTS idx_documents_synced_at;
DROP INDEX CONCURRENTLY IF EXISTS idx_documents_tool_synced;
DROP INDEX CONCURRENTLY IF EXISTS idx_documents_project_synced;
DROP INDEX CONCURRENTLY IF EXISTS idx_documents_activity_at;
DROP INDEX CONCURRENTLY IF EXISTS idx_documents_project_activity;
