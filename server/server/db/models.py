"""SQLAlchemy ORM models for the Memento database."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer,
    LargeBinary, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    reconstructor,
    relationship,
)

try:
    from pgvector.sqlalchemy import Vector
except ImportError:
    Vector = None  # pgvector not installed — models still loadable


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Machines (registered collector instances)
# ---------------------------------------------------------------------------
class Machine(Base):
    __tablename__ = "machines"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Despite the legacy column name, this stores the collector's persistent
    # device ID.  A device must have exactly one row: initial collector scans
    # upload several files concurrently, so application-level lookup alone is
    # not enough to prevent duplicate registrations.
    collector_token_hash: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True,
    )
    collector_version: Mapped[str | None] = mapped_column(String(50))
    # Bounded collector capability snapshot (schema-versioned JSON) reported on
    # the control poll so the server can route only supported command kinds.
    capabilities: Mapped[dict | None] = mapped_column(JSONB)
    capabilities_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User | None"] = relationship()
    documents: Mapped[list[Document]] = relationship(back_populates="machine")


# ---------------------------------------------------------------------------
# Tools (AI tools known to the system)
# ---------------------------------------------------------------------------
class Tool(Base):
    __tablename__ = "tools"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)  # e.g. "claude_code"
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    icon: Mapped[str | None] = mapped_column(String(50))
    description: Mapped[str | None] = mapped_column(Text)
    total_files: Mapped[int] = mapped_column(Integer, default=0)
    total_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    documents: Mapped[list[Document]] = relationship(back_populates="tool")
    projects: Mapped[list[Project]] = relationship(back_populates="tool")


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------
class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    tool_id: Mapped[str | None] = mapped_column(ForeignKey("tools.id"))
    source_path: Mapped[str | None] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(20), default="private")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    tool: Mapped[Tool | None] = relationship(back_populates="projects")
    documents: Mapped[list[Document]] = relationship(back_populates="project")
    permissions: Mapped[list[Permission]] = relationship(back_populates="project")


# ---------------------------------------------------------------------------
# Documents (every synced file)
# ---------------------------------------------------------------------------
class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tool_id: Mapped[str] = mapped_column(ForeignKey("tools.id"), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"))
    machine_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("machines.id"))

    # File identity
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    content_type: Mapped[str] = mapped_column(String(50), nullable=False)

    # Content
    title: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)              # for files < 1MB
    content_s3_key: Mapped[str | None] = mapped_column(String(500))  # for files > 1MB
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Full-text search index: space-joined jieba tokens of title + content,
    # fed through to_tsvector('simple', ...). Populated in ingest_service.
    content_tsv: Mapped[object | None] = mapped_column(TSVECTOR, nullable=True)

    # Parsed metadata (tool-specific)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)

    # Rendering
    rendered_html: Mapped[str | None] = mapped_column(Text)

    # AI summary
    ai_summary: Mapped[str | None] = mapped_column(Text)
    ai_summary_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # State
    visibility: Mapped[str] = mapped_column(String(20), default="private")
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    # Tracks the outcome of the embedding pipeline so failures don't silently
    # drop documents. Values: pending (just ingested), processing (claimed),
    # ok (embedded), failed (call errored — retry candidate), skipped (too
    # short / binary — intentional). The token/timestamp fence stale workers.
    embedding_status: Mapped[str] = mapped_column(String(20), default="pending")
    embedding_attempts: Mapped[int] = mapped_column(Integer, default=0)
    embedding_claim_token: Mapped[str | None] = mapped_column(String(36))
    embedding_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # SHA-256 of the exact bounded chunk list sent to the embedding model.
    # This is intentionally separate from content_hash: append-only transcripts
    # often change on disk without changing the first 100 messages / 50 chunks
    # that Memento embeds.
    embedding_content_hash: Mapped[str | None] = mapped_column(String(64))
    # Active embedding route: "quality" (1024-d BGE-M3 table) or "fast"
    # (384-d small-model table). Historical rows default to quality; the
    # sticky policy never auto-demotes quality → fast.
    embedding_tier: Mapped[str] = mapped_column(String(20), default="quality")
    # Knowledge-graph extraction pipeline status. Values: pending (just
    # ingested), ok (including successful zero-entity results), failed
    # (transient provider error), permanent_failed (bad auth/request/model),
    # and skipped (content too short / wrong category).
    knowledge_status: Mapped[str] = mapped_column(String(20), default="pending")
    knowledge_attempts: Mapped[int] = mapped_column(Integer, default=0)
    knowledge_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    knowledge_failure_kind: Mapped[str | None] = mapped_column(String(50))

    # Timestamps
    source_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Latest timestamp carried by a normalized user/assistant message.  This
    # represents when a conversation actually happened; synced_at remains the
    # independent collector-delivery timestamp.
    activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    tool: Mapped[Tool] = relationship(back_populates="documents")
    project: Mapped[Project | None] = relationship(back_populates="documents")
    machine: Mapped[Machine | None] = relationship(back_populates="documents")
    messages: Mapped[list[ConversationMessage]] = relationship(back_populates="document", cascade="all, delete-orphan")
    usage_events: Mapped[list[ConversationUsageEvent]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )
    versions: Mapped[list[DocumentVersion]] = relationship(back_populates="document", cascade="all, delete-orphan")
    delivery_state: Mapped[DocumentDeliveryState | None] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        uselist=False,
        lazy="joined",
        innerjoin=False,
    )

    __table_args__ = (
        Index("idx_documents_tool", "tool_id"),
        Index("idx_documents_project", "project_id"),
        Index("idx_documents_category", "category"),
        Index("idx_documents_machine", "machine_id"),
        Index("idx_documents_machine_tool", "machine_id", "tool_id"),
        Index("idx_documents_project_category", "project_id", "category"),
        # Unique per machine+tool+path
        Index("uq_documents_machine_tool_path", "machine_id", "tool_id", "relative_path", unique=True),
    )

    @reconstructor
    def _apply_delivery_projection(self) -> None:
        """Expose latest delivery values to legacy instance-level readers.

        Query predicates use explicit projection expressions (see
        ``services.document_delivery``). Instance consumers historically read
        these attributes directly, so hydrate them without marking the
        canonical row dirty.
        """
        state = self.delivery_state
        if state is None:
            return
        from sqlalchemy.orm.attributes import set_committed_value

        set_committed_value(self, "content_hash", state.revision_hash)
        set_committed_value(self, "file_size_bytes", state.file_size_bytes)
        set_committed_value(self, "metadata_", state.delivery_metadata)
        set_committed_value(self, "source_modified_at", state.source_modified_at)
        set_committed_value(self, "activity_at", state.activity_at)
        set_committed_value(self, "synced_at", state.synced_at)


# ---------------------------------------------------------------------------
# Hot conversation delivery state
# ---------------------------------------------------------------------------
class DocumentDeliveryState(Base):
    """Narrow latest-delivery projection for append-heavy documents.

    The canonical ``documents`` row remains the last durable full snapshot.
    Conversation DELTAs update this row instead. Only meaningful activity is
    indexed, so revision/sync/metadata-only updates remain HOT-eligible.
    """

    __tablename__ = "document_delivery_state"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL")
    )
    revision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    delivery_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    source_modified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    document: Mapped[Document] = relationship(back_populates="delivery_state")

    __table_args__ = (
        Index("idx_document_delivery_activity", activity_at.desc()),
        Index(
            "idx_document_delivery_project_activity",
            "project_id",
            activity_at.desc(),
        ),
    )


# ---------------------------------------------------------------------------
# Conversation messages (extracted from JSONL)
# ---------------------------------------------------------------------------
class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    message_type: Mapped[str | None] = mapped_column(String(50))
    role: Mapped[str | None] = mapped_column(String(50))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped[Document] = relationship(back_populates="messages")

    __table_args__ = (
        Index("uq_conv_msg_doc_line", "document_id", "line_number", unique=True),
        Index("idx_conv_msg_timestamp", "timestamp"),
        Index("idx_conv_msg_doc_ts", "document_id", "timestamp"),
        Index(
            "idx_conv_msg_doc_source_id",
            "document_id",
            text("(metadata ->> 'source_id')"),
        ),
    )


# ---------------------------------------------------------------------------
# Personal conversation message bookmarks
# ---------------------------------------------------------------------------
class PinnedMessage(Base):
    """A user's personal bookmark for one conversation message.

    Anchored to the message's stable native id (``source_id`` from the message
    metadata), with the document-local ``line_number`` as a fallback, so a full
    re-ingest of a conversation — which recreates its message rows with new
    autoincrement ids — does not drop the pin. ``message_id`` is only a nullable
    last-known row pointer (``ON DELETE SET NULL``), not the pin identity.
    """

    __tablename__ = "pinned_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[str | None] = mapped_column(Text)
    line_number: Mapped[int | None] = mapped_column(Integer)
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversation_messages.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "uq_pinned_messages_user_source",
            "user_id",
            "document_id",
            "source_id",
            unique=True,
            postgresql_where=text("source_id IS NOT NULL"),
        ),
        Index(
            "uq_pinned_messages_user_line",
            "user_id",
            "document_id",
            "line_number",
            unique=True,
            postgresql_where=text("source_id IS NULL"),
        ),
        Index("idx_pinned_messages_user_created", "user_id", created_at.desc()),
        Index("idx_pinned_messages_user_document", "user_id", "document_id"),
    )


class ConversationUsageEvent(Base):
    """One exact native usage observation attributed to a model selection."""

    __tablename__ = "conversation_usage_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    machine_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("machines.id", ondelete="CASCADE")
    )
    tool_id: Mapped[str] = mapped_column(String(50), nullable=False)
    source_id: Mapped[str] = mapped_column(String(512), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    model: Mapped[str | None] = mapped_column(String(200))
    reasoning_effort: Mapped[str | None] = mapped_column(String(50))
    service_tier: Mapped[str | None] = mapped_column(String(50))
    attribution_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="attributed"
    )
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    uncached_input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    cached_input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    cache_write_input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reasoning_output_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    total_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    document: Mapped[Document] = relationship(back_populates="usage_events")

    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "source_id",
            name="uq_conversation_usage_document_source",
        ),
        Index("idx_conversation_usage_occurred", "occurred_at"),
        Index(
            "idx_conversation_usage_tool_occurred",
            "tool_id",
            "occurred_at",
        ),
        Index(
            "idx_conversation_usage_machine_occurred",
            "machine_id",
            "occurred_at",
        ),
        Index(
            "idx_conversation_usage_document_occurred",
            "document_id",
            "occurred_at",
        ),
    )


# ---------------------------------------------------------------------------
# Durable metadata that can arrive before its conversation content
# ---------------------------------------------------------------------------
class ConversationMetadataInbox(Base):
    """Latest unapplied collector signal for one logical conversation item."""

    __tablename__ = "conversation_metadata_inbox"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    machine_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("machines.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tool_id: Mapped[str] = mapped_column(
        ForeignKey("tools.id", ondelete="CASCADE"), nullable=False
    )
    route_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64))
    metadata_type: Mapped[str] = mapped_column(String(50), nullable=False)
    signal_id: Mapped[str] = mapped_column(String(512), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source_timestamp: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "machine_id",
            "tool_id",
            "route_hash",
            "metadata_type",
            "signal_id",
            name="uq_conversation_metadata_inbox_signal",
        ),
        Index(
            "idx_conversation_metadata_inbox_session",
            "machine_id",
            "tool_id",
            "session_id",
        ),
        Index("idx_conversation_metadata_inbox_expiry", "expires_at"),
    )


# ---------------------------------------------------------------------------
# Cross-tool orchestrator identity and lifecycle
# ---------------------------------------------------------------------------
class OrchestrationRun(Base):
    """One durable external-orchestrator run scoped to a collector machine."""

    __tablename__ = "orchestration_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    machine_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("machines.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    installation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    external_run_id: Mapped[str] = mapped_column(String(256), nullable=False)
    orchestrator: Mapped[str] = mapped_column(String(64), nullable=False)
    orchestrator_version: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    run_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    parent_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    agents: Mapped[list["OrchestrationAgent"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "machine_id", "installation_id", "external_run_id",
            name="uq_orchestration_run_external",
        ),
        Index("idx_orchestration_run_parent", "parent_document_id"),
        Index("idx_orchestration_run_status", "machine_id", "status"),
    )


class OrchestrationAgent(Base):
    """One named Claw child and its exact native conversation binding."""

    __tablename__ = "orchestration_agents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orchestration_runs.id", ondelete="CASCADE"), nullable=False
    )
    agent_key: Mapped[str] = mapped_column(String(256), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(256), nullable=False)
    codename: Mapped[str | None] = mapped_column(String(256))
    engine: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(256))
    effort: Mapped[str | None] = mapped_column(String(64))
    cwd: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="declared")
    native_session_id: Mapped[str | None] = mapped_column(String(512))
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )
    last_event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    run: Mapped[OrchestrationRun] = relationship(back_populates="agents")

    __table_args__ = (
        UniqueConstraint("run_id", "agent_key", name="uq_orchestration_agent_key"),
        Index("idx_orchestration_agent_native", "engine", "native_session_id"),
        Index("idx_orchestration_agent_document", "document_id"),
    )


class OrchestrationEventReceipt(Base):
    """Idempotency fence for collector outbox retries."""

    __tablename__ = "orchestration_event_receipts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    machine_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("machines.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("machine_id", "event_id", name="uq_orchestration_event_receipt"),
        Index("idx_orchestration_receipt_created", "created_at"),
    )


# ---------------------------------------------------------------------------
# Agent control plane (bidirectional server ⇄ collector command channel)
# ---------------------------------------------------------------------------
class AgentControlSession(Base):
    """One managed agent session started or resumed through Memento.

    Sessions discovered only from transcript files never get a row here —
    file observation is not an input channel, so those remain view-only.
    """

    __tablename__ = "agent_control_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    machine_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("machines.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tool_id: Mapped[str] = mapped_column(String(50), nullable=False)
    adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    adapter_version: Mapped[str | None] = mapped_column(String(64))
    capabilities: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    native_session_id: Mapped[str | None] = mapped_column(String(512))
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="starting")
    state_reason: Mapped[str | None] = mapped_column(String(128))
    active_native_turn_id: Mapped[str | None] = mapped_column(String(256))
    # Bounded list of unanswered adapter escalations (questions/approvals),
    # maintained from collector lifecycle events.
    pending_interactions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    collector_revision: Mapped[str | None] = mapped_column(String(64))
    server_revision: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_agent_control_session_machine", "machine_id", "state"),
        Index("idx_agent_control_session_document", "document_id"),
        Index("idx_agent_control_session_native", "tool_id", "native_session_id"),
    )


class AgentControlCommand(Base):
    """Authoritative current state of one control command.

    A command is admitted durably before any side effect is acknowledged,
    delivered under an expiring lease, and closed only by an explicit terminal
    transition — delivery alone never deletes or completes it.
    """

    __tablename__ = "agent_control_commands"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    trace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    machine_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("machines.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    control_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_control_sessions.id", ondelete="SET NULL")
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )
    native_session_id: Mapped[str | None] = mapped_column(String(512))
    native_turn_id: Mapped[str | None] = mapped_column(String(256))
    interaction_id: Mapped[str | None] = mapped_column(String(256))
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    # "retry" re-queues after a lost lease; "fail_once_delivered" refuses to
    # repeat a delivered-but-unreported command (destructive legacy semantics).
    redelivery_policy: Mapped[str] = mapped_column(
        String(24), nullable=False, default="retry"
    )
    lease_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_delivery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[dict | None] = mapped_column(JSONB)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "machine_id", "idempotency_key",
            name="uq_agent_control_command_idempotency",
        ),
        Index("idx_agent_control_command_pending", "machine_id", "state", "created_at"),
        Index("idx_agent_control_command_session", "control_session_id"),
        Index("idx_agent_control_command_trace", "trace_id"),
    )


class AgentControlEvent(Base):
    """Append-only structural lifecycle trace for control commands/sessions.

    ``received_at_server`` is the authoritative ordering time; device wall
    time and monotonic elapsed time are retained for latency analysis only.
    Rows reference content by digest — never by payload body.
    """

    __tablename__ = "agent_control_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    machine_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("machines.id", ondelete="CASCADE"), nullable=False
    )
    command_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_control_commands.id", ondelete="CASCADE")
    )
    control_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_control_sessions.id", ondelete="SET NULL")
    )
    trace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    parent_event_id: Mapped[str | None] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(96), nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="server")
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    native_session_id: Mapped[str | None] = mapped_column(String(512))
    native_turn_id: Mapped[str | None] = mapped_column(String(256))
    interaction_id: Mapped[str | None] = mapped_column(String(256))
    adapter: Mapped[str | None] = mapped_column(String(64))
    adapter_version: Mapped[str | None] = mapped_column(String(64))
    collector_revision: Mapped[str | None] = mapped_column(String(64))
    server_revision: Mapped[str | None] = mapped_column(String(64))
    occurred_at_device: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    received_at_server: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    outcome: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    payload_digest: Mapped[str | None] = mapped_column(String(80))
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("machine_id", "event_id", name="uq_agent_control_event_id"),
        Index("idx_agent_control_event_command", "command_id", "id"),
        Index("idx_agent_control_event_trace", "trace_id"),
        Index("idx_agent_control_event_received", "received_at_server"),
    )


# ---------------------------------------------------------------------------
# Claude transcript branch lineage
# ---------------------------------------------------------------------------
class ClaudeConversationLineageRecord(Base):
    """One UUID-bearing Claude raw transcript record.

    Normalized conversation rows intentionally omit non-visual records. The
    branch tree therefore lives separately, keyed by raw record UUID, so a
    resumed parent can invalidate an abandoned suffix without storing an
    unbounded UUID chain in document metadata.
    """

    __tablename__ = "claude_conversation_lineage_records"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    record_uuid: Mapped[str] = mapped_column(String(512), primary_key=True)
    parent_uuid: Mapped[str | None] = mapped_column(String(512))
    source_order: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_sidechain: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # ``is_sidechain`` preserves Claude's literal record flag. ``is_subagent``
    # is the effective branch scope (explicit sidechain OR agentId OR child
    # transcript), used for active-leaf eligibility.
    is_subagent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    agent_id: Mapped[str | None] = mapped_column(String(512))
    is_eligible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index(
            "idx_claude_lineage_document_active",
            "document_id",
            "active",
        ),
        Index(
            "idx_claude_lineage_document_order",
            "document_id",
            "source_order",
        ),
    )


# ---------------------------------------------------------------------------
# Captured Cursor Canvas artifacts
# ---------------------------------------------------------------------------
class CanvasArtifactBlob(Base):
    """Immutable, hash-addressed artifact bytes shared by authorized links."""

    __tablename__ = "canvas_artifact_blobs"

    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CanvasArtifact(Base):
    """One validated Canvas source and its optional renderable representation."""

    __tablename__ = "canvas_artifacts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    origin_machine_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("machines.id", ondelete="CASCADE"), nullable=False
    )
    source_hash: Mapped[str] = mapped_column(
        ForeignKey("canvas_artifact_blobs.content_hash"), nullable=False
    )
    compiled_hash: Mapped[str | None] = mapped_column(
        ForeignKey("canvas_artifact_blobs.content_hash")
    )
    runtime_hash: Mapped[str | None] = mapped_column(
        ForeignKey("canvas_artifact_blobs.content_hash")
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    render_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    compiler_version: Mapped[str | None] = mapped_column(String(128))
    runtime_sdk_version: Mapped[str | None] = mapped_column(String(128))
    origin: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("user_id", "source_hash", name="uq_canvas_artifact_user_source"),
        Index("idx_canvas_artifact_machine", "origin_machine_id"),
    )


class CanvasArtifactReference(Base):
    """A device-owned conversation reference and its explicit backfill outcome."""

    __tablename__ = "canvas_artifact_references"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[int] = mapped_column(
        ForeignKey("conversation_messages.id", ondelete="CASCADE"), nullable=False
    )
    machine_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("machines.id", ondelete="CASCADE"), nullable=False
    )
    recorded_path: Mapped[str] = mapped_column(Text, nullable=False)
    path_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("canvas_artifacts.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="discovered"
    )
    reason: Mapped[str | None] = mapped_column(String(128))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "message_id",
            "path_hash",
            name="uq_canvas_reference_message_path",
        ),
        Index("idx_canvas_reference_machine_status", "machine_id", "status"),
        Index("idx_canvas_reference_document", "document_id"),
        Index("idx_canvas_reference_artifact", "artifact_id"),
    )


class CanvasArtifactInventoryState(Base):
    """Per-device high-water mark for bounded historical reference discovery."""

    __tablename__ = "canvas_artifact_inventory_states"

    machine_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("machines.id", ondelete="CASCADE"), primary_key=True
    )
    last_message_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ---------------------------------------------------------------------------
# Authoritative current conversation task state
# ---------------------------------------------------------------------------
class ConversationTaskState(Base):
    """One canonical current task-list projection per conversation document."""

    __tablename__ = "conversation_task_states"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    machine_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("machines.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    tool_id: Mapped[str] = mapped_column(String(50), nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(512))
    root_thread_id: Mapped[str | None] = mapped_column(String(512))
    parent_thread_id: Mapped[str | None] = mapped_column(String(512))
    agent_id: Mapped[str | None] = mapped_column(String(512))
    agent_path: Mapped[str | None] = mapped_column(Text)
    agent_depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    source_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversation_messages.id", ondelete="SET NULL")
    )
    source_line_number: Mapped[int | None] = mapped_column(Integer)
    source_ids: Mapped[list] = mapped_column(JSONB, default=list)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    explicit_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    quality: Mapped[str] = mapped_column(
        String(32), nullable=False, default="partial"
    )
    projection_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )

    pending_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    in_progress_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    blocked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    cancelled_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    outstanding_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("idx_task_state_machine", "machine_id"),
        Index("idx_task_state_user", "user_id"),
        Index("idx_task_state_thread", "machine_id", "tool_id", "thread_id"),
        Index("idx_task_state_root", "machine_id", "tool_id", "root_thread_id"),
        Index("idx_task_state_parent", "machine_id", "tool_id", "parent_thread_id"),
        Index("idx_task_state_agent", "machine_id", "tool_id", "agent_id"),
        Index(
            "idx_task_state_outstanding",
            "machine_id",
            "outstanding_count",
            observed_at.desc(),
        ),
        Index("idx_task_state_status_counts", "tool_id", "outstanding_count"),
        Index("idx_task_state_pending", "machine_id", "pending_count"),
        Index("idx_task_state_in_progress", "machine_id", "in_progress_count"),
        Index("idx_task_state_blocked", "machine_id", "blocked_count"),
        Index("idx_task_state_completed", "machine_id", "completed_count"),
        Index("idx_task_state_cancelled", "machine_id", "cancelled_count"),
    )


# ---------------------------------------------------------------------------
# Materialized conversation read state
# ---------------------------------------------------------------------------
class ConversationReadModel(Base):
    """One ingest-owned read projection per normalized conversation."""

    __tablename__ = "conversation_read_models"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    machine_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("machines.id", ondelete="CASCADE")
    )
    tool_id: Mapped[str] = mapped_column(String(50), nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(512))
    root_thread_id: Mapped[str | None] = mapped_column(String(512))
    parent_thread_id: Mapped[str | None] = mapped_column(String(512))
    agent_id: Mapped[str | None] = mapped_column(String(512))
    agent_tool_use_id: Mapped[str | None] = mapped_column(String(512))
    agent_depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_subagent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    user_message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    assistant_message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    human_character_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    projected_through_line: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    latest_assistant_line: Mapped[int | None] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    projection_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )

    pending_interactions: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list
    )
    inferred_responses: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list
    )
    live_activities: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list
    )
    agent_events: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    runtime: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    lifecycle: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    latest_human_at: Mapped[str] = mapped_column(
        String(128), nullable=False, default=""
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index(
            "idx_conversation_read_root",
            "machine_id",
            "tool_id",
            "root_thread_id",
        ),
        Index(
            "idx_conversation_read_thread",
            "machine_id",
            "tool_id",
            "thread_id",
        ),
        Index(
            "idx_conversation_read_agent",
            "machine_id",
            "tool_id",
            "agent_id",
        ),
        Index(
            "idx_conversation_read_tool_use",
            "machine_id",
            "tool_id",
            "agent_tool_use_id",
        ),
    )


# ---------------------------------------------------------------------------
# Activity rollup (daily calendar)
# ---------------------------------------------------------------------------
class ConversationActivityHourly(Base):
    """Precomputed hourly (UTC) countable-message counts per machine + tool.

    Refreshed in the background so the daily-calendar endpoint reads a few
    thousand rows instead of aggregating the multi-million-row messages table
    on every cold cache miss. Hourly grain keeps the user's timezone-adjusted
    day boundaries exact. See services/activity_rollup.py.
    """

    __tablename__ = "conversation_activity_hourly"

    hour: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    # Documents with no machine_id are folded into the all-zero sentinel so
    # this column can be part of the primary key (PK columns are NOT NULL).
    # A machine-scoped read filters `machine_id = ANY(mids)`, which correctly
    # excludes the sentinel; an unscoped (admin) read includes it.
    machine_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    tool_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("idx_activity_hourly_machine_hour", "machine_id", "hour"),
    )


# ---------------------------------------------------------------------------
# Dashboard category rollup
# ---------------------------------------------------------------------------
class DashboardDocumentCategoryRollup(Base):
    """Precomputed document counts per machine, tool, and category.

    ``machine_id`` uses the same non-null sentinel as the daily activity
    rollup when a legacy document has no machine. That keeps the composite
    primary key compact while preserving owner/admin visibility of those rows.
    """

    __tablename__ = "dashboard_document_category_rollups"

    machine_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    tool_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    category: Mapped[str] = mapped_column(String(50), primary_key=True)
    document_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )

    __table_args__ = (
        Index(
            "idx_dashboard_category_rollup_tool_category",
            "tool_id",
            "category",
        ),
    )


# ---------------------------------------------------------------------------
# Dashboard document projection
# ---------------------------------------------------------------------------
class DashboardDocumentProjection(Base):
    """One narrow, ingest-owned dashboard row per document."""

    __tablename__ = "dashboard_document_projections"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    machine_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("machines.id", ondelete="CASCADE")
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL")
    )
    tool_id: Mapped[str] = mapped_column(String(50), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, default="private"
    )

    title: Mapped[str | None] = mapped_column(Text)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source_modified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    session_id: Mapped[str | None] = mapped_column(String(512))
    root_thread_id: Mapped[str | None] = mapped_column(String(512))
    parent_thread_id: Mapped[str | None] = mapped_column(String(512))
    is_subagent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    is_archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    hierarchy_metadata: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict
    )

    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    user_message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    assistant_message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    human_character_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    pending_question_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    agent_mode: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    projection_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("idx_dashboard_projection_machine", "machine_id"),
        Index(
            "idx_dashboard_projection_machine_tool_category",
            "machine_id",
            "tool_id",
            "category",
        ),
        Index(
            "idx_dashboard_projection_project_session",
            "project_id",
            "session_id",
        ),
        Index(
            "idx_dashboard_projection_root",
            "machine_id",
            "tool_id",
            "root_thread_id",
        ),
        Index(
            "idx_dashboard_projection_activity",
            activity_at.desc(),
            "document_id",
        ),
        Index(
            "idx_dashboard_projection_synced",
            synced_at.desc(),
            "document_id",
        ),
        Index(
            "idx_dashboard_projection_effective_activity",
            func.coalesce(activity_at, source_modified_at, synced_at).desc(),
            document_id.desc(),
            postgresql_where=category == "conversation",
        ),
    )


class DashboardProjectionState(Base):
    """Marks completion of the one-time legacy dashboard backfill."""

    __tablename__ = "dashboard_projection_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    projection_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    backfill_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# Prompt rows stay normalized so an append updates one bounded row instead of
# rewriting an ever-growing JSONB outline on every conversation ingest.
class ConversationPromptProjection(Base):
    """One materialized human prompt per normalized conversation message."""

    __tablename__ = "conversation_prompt_projections"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    message_id: Mapped[int] = mapped_column(
        ForeignKey("conversation_messages.id", ondelete="CASCADE"),
        primary_key=True,
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "idx_conversation_prompt_line",
            "document_id",
            "line_number",
            "message_id",
        ),
    )


# ---------------------------------------------------------------------------
# Conversation search spelling lexicon
# ---------------------------------------------------------------------------
class ConversationSearchTerm(Base):
    """Compact vocabulary used to correct misspelled message-search tokens.

    Fuzzy matching the 800K+ message bodies directly creates a huge lossy GIN
    candidate set. Searching unique, bounded terms first and then executing a
    corrected FTS query keeps the expensive path small and deterministic.
    """

    __tablename__ = "conversation_search_terms"

    term: Mapped[str] = mapped_column(String(64), primary_key=True)
    frequency: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index(
            "idx_conversation_search_terms_trgm",
            "term",
            postgresql_using="gin",
            postgresql_ops={"term": "gin_trgm_ops"},
        ),
    )


# ---------------------------------------------------------------------------
# Daily summaries (AI-generated)
# ---------------------------------------------------------------------------
class DailySummary(Base):
    __tablename__ = "daily_summaries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # user_id scopes the summary to its owner. Historically this table was
    # global (single AI digest per date per tool across the whole instance),
    # which leaked one user's aggregated activity to every other user.
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    summary_date: Mapped[date] = mapped_column(Date, nullable=False)
    tool_id: Mapped[str | None] = mapped_column(ForeignKey("tools.id"))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    highlights: Mapped[dict | None] = mapped_column(JSONB)
    source_document_ids: Mapped[list | None] = mapped_column(ARRAY(UUID(as_uuid=True)))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        # Uniqueness now includes user_id so each user gets their own summary.
        Index("uq_daily_summary_user_date_tool", "user_id", "summary_date", "tool_id", unique=True),
        Index("idx_daily_summary_user", "user_id"),
    )


# ---------------------------------------------------------------------------
# Document version history
# ---------------------------------------------------------------------------
class DocumentVersion(Base):
    __tablename__ = "document_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_delta: Mapped[str | None] = mapped_column(Text)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped[Document] = relationship(back_populates="versions")


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    hashed_password: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="pending")   # pending | viewer | admin | owner
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | active | disabled
    collector_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    github_id: Mapped[str | None] = mapped_column(String(50))
    totp_secret: Mapped[str | None] = mapped_column(Text)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    permissions: Mapped[list[Permission]] = relationship(
        back_populates="user", foreign_keys="[Permission.user_id]",
    )


# ---------------------------------------------------------------------------
# Invite codes — enable invite-only registration
# ---------------------------------------------------------------------------
class InviteCode(Base):
    __tablename__ = "invite_codes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    max_uses: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    use_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    role_on_accept: Mapped[str] = mapped_column(String(20), default="viewer", nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Permissions (project-level access control)
# ---------------------------------------------------------------------------
class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    tool_id: Mapped[str | None] = mapped_column(ForeignKey("tools.id"))
    permission: Mapped[str] = mapped_column(String(20), default="read")
    granted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="permissions", foreign_keys=[user_id])
    project: Mapped[Project | None] = relationship(back_populates="permissions")

    __table_args__ = (
        Index("uq_permission_user_project_tool", "user_id", "project_id", "tool_id", unique=True),
    )


# ---------------------------------------------------------------------------
# Access audit log
# ---------------------------------------------------------------------------
class AccessLog(Base):
    __tablename__ = "access_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id"))
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_access_logs_user", "user_id", created_at.desc()),
        Index("idx_access_logs_document", "document_id", created_at.desc()),
    )


# ---------------------------------------------------------------------------
# Sync state tracking (server-side)
# ---------------------------------------------------------------------------
class SyncState(Base):
    __tablename__ = "sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    machine_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("machines.id"))
    tool_id: Mapped[str | None] = mapped_column(ForeignKey("tools.id"))
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    last_hash: Mapped[str | None] = mapped_column(String(64))
    last_offset: Mapped[int] = mapped_column(BigInteger, default=0)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("uq_sync_state", "machine_id", "tool_id", "relative_path", unique=True),
    )


# ---------------------------------------------------------------------------
# Document Embeddings (pgvector semantic search)
# ---------------------------------------------------------------------------
class DocumentEmbedding(Base):
    __tablename__ = "document_embeddings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Chunk-local identity lets append/small-edit re-embeds update only changed
    # vectors instead of rewriting every row and its HNSW entry.
    chunk_hash: Mapped[str | None] = mapped_column(String(64))
    model_name: Mapped[str | None] = mapped_column(String(200))
    backend: Mapped[str | None] = mapped_column(String(32))
    # Immutable fingerprint of model revision, backend/artifact, dimensions,
    # pooling/normalization, prefixes, and sequence-length policy.
    profile_signature: Mapped[str | None] = mapped_column(String(80))
    embedding = mapped_column(Vector(1024) if Vector else Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped[Document] = relationship()

    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_doc_embedding_chunk"),
        Index("idx_doc_embedding_doc", "document_id"),
    )


class DocumentEmbeddingFast(Base):
    """Fast-tier vectors — separate 384-d table/index from quality 1024-d rows.

    Never pad or mix dimensions with ``document_embeddings``. Used only when
    ``MEMENTO_EMBEDDING_TIERING_ENABLED`` routes ordinary conversation/backlog
    documents to the smaller model.
    """

    __tablename__ = "document_embeddings_fast"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_hash: Mapped[str | None] = mapped_column(String(64))
    model_name: Mapped[str | None] = mapped_column(String(200))
    backend: Mapped[str | None] = mapped_column(String(32))
    profile_signature: Mapped[str | None] = mapped_column(String(80))
    embedding = mapped_column(Vector(384) if Vector else Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped[Document] = relationship()

    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_doc_embedding_fast_chunk"),
        Index("idx_doc_embedding_fast_doc", "document_id"),
    )


# ---------------------------------------------------------------------------
# Knowledge Graph
# ---------------------------------------------------------------------------
class KnowledgeEntity(Base):
    __tablename__ = "knowledge_entities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)  # project/tool/concept/person/technology
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    observations: Mapped[list[KnowledgeObservation]] = relationship(back_populates="entity", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("user_id", "name", "entity_type", name="uq_entity_user_name_type"),
        Index("idx_entity_user", "user_id"),
        Index("idx_entity_type", "entity_type"),
    )


class KnowledgeRelation(Base):
    __tablename__ = "knowledge_relations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_entities.id", ondelete="CASCADE"), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_entities.id", ondelete="CASCADE"), nullable=False)
    relation_type: Mapped[str] = mapped_column(String(50), nullable=False)  # uses/creates/depends_on/discussed_in
    strength: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    source: Mapped[KnowledgeEntity] = relationship(foreign_keys=[source_id])
    target: Mapped[KnowledgeEntity] = relationship(foreign_keys=[target_id])

    __table_args__ = (
        Index("idx_relation_source", "source_id"),
        Index("idx_relation_target", "target_id"),
    )


class KnowledgeObservation(Base):
    __tablename__ = "knowledge_observations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_entities.id", ondelete="CASCADE"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    entity: Mapped[KnowledgeEntity] = relationship(back_populates="observations")

    __table_args__ = (
        Index("idx_observation_entity", "entity_id"),
    )


# ---------------------------------------------------------------------------
# Public share links (timeline / daily report)
# ---------------------------------------------------------------------------
class ShareLink(Base):
    __tablename__ = "share_links"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Opaque token exposed in the URL. 24 bytes base32 ≈ 40 chars — plenty of
    # entropy so enumeration attacks aren't useful, short enough to be
    # copy-pasteable.
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # "timeline" → target_id is a project uuid; "daily" → target_id is a date
    # string YYYY-MM-DD. Keeps the table a single type; discriminator logic
    # lives in the API.
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # When set, only this user (after login) may view the share; the public
    # /s/<token> page still works as the URL but the API requires auth and
    # checks the user matches. NULL = anonymous public link (legacy default).
    target_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_share_owner", "owner_user_id"),
        Index("idx_share_kind_target", "kind", "target_id"),
        Index("idx_share_target_user", "target_user_id"),
    )


class ShareView(Base):
    __tablename__ = "share_views"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    share_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("share_links.id", ondelete="CASCADE"), nullable=False)
    ip: Mapped[str | None] = mapped_column(INET)
    country: Mapped[str | None] = mapped_column(String(80))
    region: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(120))
    user_agent: Mapped[str | None] = mapped_column(Text)
    referer: Mapped[str | None] = mapped_column(Text)
    viewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_share_view_share", "share_id", "viewed_at"),
    )
