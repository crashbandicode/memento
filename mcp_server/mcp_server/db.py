"""Database connection for MCP server — connects directly to PostgreSQL."""

from __future__ import annotations

import os

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
import uuid
from datetime import date, datetime

try:
    from pgvector.sqlalchemy import Vector
except ImportError:
    Vector = None


class Base(DeclarativeBase):
    pass


# Minimal model definitions (mirror of server models, read-only)

class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    email: Mapped[str] = mapped_column(String(320))
    name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))
    collector_token: Mapped[str | None] = mapped_column(String(64))


class Machine(Base):
    __tablename__ = "machines"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class Tool(Base):
    __tablename__ = "tools"
    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(100))


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    slug: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(500))
    tool_id: Mapped[str | None] = mapped_column(ForeignKey("tools.id"))
    source_path: Mapped[str | None] = mapped_column(Text)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tool_id: Mapped[str] = mapped_column(String(50))
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"))
    machine_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("machines.id"))
    relative_path: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(50))
    content_type: Mapped[str] = mapped_column(String(50))
    title: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    file_size_bytes: Mapped[int] = mapped_column(BigInteger)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    ai_summary: Mapped[str | None] = mapped_column(Text)
    embedding_status: Mapped[str] = mapped_column(String(20))
    embedding_tier: Mapped[str] = mapped_column(String(20))
    source_modified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"))
    line_number: Mapped[int] = mapped_column(Integer)
    message_type: Mapped[str | None] = mapped_column(String(50))
    role: Mapped[str | None] = mapped_column(String(20))
    content: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentDeliveryState(Base):
    __tablename__ = "document_delivery_state"
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id"), primary_key=True
    )
    activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_metadata: Mapped[dict] = mapped_column(JSONB)


class ConversationUsageEvent(Base):
    __tablename__ = "conversation_usage_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"))
    machine_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("machines.id"))
    tool_id: Mapped[str] = mapped_column(String(50))
    source_id: Mapped[str] = mapped_column(String(512))
    source: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    model: Mapped[str | None] = mapped_column(String(200))
    reasoning_effort: Mapped[str | None] = mapped_column(String(50))
    service_tier: Mapped[str | None] = mapped_column(String(50))
    attribution_status: Mapped[str] = mapped_column(String(32))
    input_tokens: Mapped[int] = mapped_column(BigInteger)
    uncached_input_tokens: Mapped[int] = mapped_column(BigInteger)
    cached_input_tokens: Mapped[int] = mapped_column(BigInteger)
    cache_write_input_tokens: Mapped[int] = mapped_column(BigInteger)
    output_tokens: Mapped[int] = mapped_column(BigInteger)
    reasoning_output_tokens: Mapped[int] = mapped_column(BigInteger)
    total_tokens: Mapped[int] = mapped_column(BigInteger)


class DailySummary(Base):
    __tablename__ = "daily_summaries"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    summary_date: Mapped[date] = mapped_column(Date)
    tool_id: Mapped[str | None] = mapped_column(String(50))
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    highlights: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DocumentEmbedding(Base):
    __tablename__ = "document_embeddings"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"))
    chunk_index: Mapped[int] = mapped_column(Integer)
    chunk_text: Mapped[str] = mapped_column(Text)
    model_name: Mapped[str | None] = mapped_column(String(200))
    backend: Mapped[str | None] = mapped_column(String(32))
    profile_signature: Mapped[str | None] = mapped_column(String(80))
    embedding = mapped_column(Vector(1024) if Vector else Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DocumentEmbeddingFast(Base):
    __tablename__ = "document_embeddings_fast"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"))
    chunk_index: Mapped[int] = mapped_column(Integer)
    chunk_text: Mapped[str] = mapped_column(Text)
    model_name: Mapped[str | None] = mapped_column(String(200))
    backend: Mapped[str | None] = mapped_column(String(32))
    profile_signature: Mapped[str | None] = mapped_column(String(80))
    embedding = mapped_column(Vector(384) if Vector else Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeEntity(Base):
    __tablename__ = "knowledge_entities"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(String(50))
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KnowledgeRelation(Base):
    __tablename__ = "knowledge_relations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_entities.id"))
    target_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_entities.id"))
    relation_type: Mapped[str] = mapped_column(String(50))
    strength: Mapped[float] = mapped_column(Float)


class KnowledgeObservation(Base):
    __tablename__ = "knowledge_observations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_entities.id"))
    content: Mapped[str] = mapped_column(Text)
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def get_db_url() -> str:
    return os.environ.get(
        "MEMENTO_DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5433/memento",
    )


def get_search_db_url(primary_url: str | None = None) -> str:
    """Return an optional direct-MCP search target, falling back to primary."""
    return (
        os.environ.get("MEMENTO_SEARCH_DATABASE_URL", "").strip()
        or primary_url
        or get_db_url()
    )


def create_engine_and_session(
    db_url: str | None = None,
    *,
    read_only: bool = False,
) -> async_sessionmaker[AsyncSession]:
    url = db_url or get_db_url()
    connect_args = (
        {
            "server_settings": {
                "default_transaction_read_only": "on",
                "application_name": "memento-mcp-search",
            }
        }
        if read_only
        else {}
    )
    engine = create_async_engine(
        url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=read_only,
        connect_args=connect_args,
    )
    return async_sessionmaker(engine, expire_on_commit=False)
