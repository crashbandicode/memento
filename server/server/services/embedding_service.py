"""Embedding generation pipeline — calls external embedding HTTP server(s).

Default/quality path: BGE-M3 on the host (or compose) embedding server.
Optional fast tier (MEMENTO_EMBEDDING_TIERING_ENABLED): a smaller model with
its own URL/dimension and a separate pgvector table. Dimensions are never
padded or mixed in one column/index.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (
    ConversationMessage,
    Document,
    DocumentEmbedding,
    DocumentEmbeddingFast,
)

logger = logging.getLogger("embedding_service")

TIER_QUALITY = "quality"
TIER_FAST = "fast"
QUALITY_CATEGORIES = frozenset(
    {"memory", "identity", "plan", "learning", "note"}
)
FAST_ELIGIBLE_CATEGORIES = frozenset({"conversation", "backlog"})


def _env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


EMBEDDING_TIERING_ENABLED = _env_flag("MEMENTO_EMBEDDING_TIERING_ENABLED", "false")

EMBEDDING_DIM = _env_int("MEMENTO_EMBEDDING_DIM", 1024)
EMBEDDING_MODEL_NAME = os.environ.get(
    "MEMENTO_EMBEDDING_MODEL_NAME",
    "BAAI/bge-m3",
).strip()
EMBEDDING_BACKEND = os.environ.get(
    "MEMENTO_EMBEDDING_BACKEND",
    "torch",
).strip().lower()
EMBEDDING_MODEL_REVISION = os.environ.get(
    "MEMENTO_EMBEDDING_MODEL_REVISION",
    "",
).strip()
EMBEDDING_QUERY_PREFIX = os.environ.get(
    "MEMENTO_EMBEDDING_QUERY_PREFIX",
    "",
)
EMBEDDING_DOCUMENT_PREFIX = os.environ.get(
    "MEMENTO_EMBEDDING_DOCUMENT_PREFIX",
    "",
)
EMBEDDING_MAX_SEQUENCE_LENGTH = _env_int(
    "MEMENTO_EMBEDDING_MAX_SEQUENCE_LENGTH",
    0,
)
EMBEDDING_ONNX_FILE = os.environ.get(
    "MEMENTO_EMBEDDING_ONNX_FILE",
    "",
).strip()
EMBEDDING_ARTIFACT_SHA256 = os.environ.get(
    "MEMENTO_EMBEDDING_ARTIFACT_SHA256",
    "",
).strip().lower()
LEGACY_EMBEDDING_MODEL_NAME = "BAAI/bge-m3"
EMBEDDING_SERVER_URL = os.environ.get(
    "MEMENTO_EMBEDDING_SERVER_URL",
    "http://host.docker.internal:8002",
).rstrip("/")

EMBEDDING_FAST_DIM = _env_int("MEMENTO_EMBEDDING_FAST_DIM", 384)
EMBEDDING_FAST_MODEL_NAME = os.environ.get(
    "MEMENTO_EMBEDDING_FAST_MODEL_NAME",
    "intfloat/multilingual-e5-small",
).strip()
EMBEDDING_FAST_BACKEND = os.environ.get(
    "MEMENTO_EMBEDDING_FAST_BACKEND",
    "torch",
).strip().lower()
EMBEDDING_FAST_MODEL_REVISION = os.environ.get(
    "MEMENTO_EMBEDDING_FAST_MODEL_REVISION",
    "",
).strip()
EMBEDDING_FAST_QUERY_PREFIX = os.environ.get(
    "MEMENTO_EMBEDDING_FAST_QUERY_PREFIX",
    "query: ",
)
EMBEDDING_FAST_DOCUMENT_PREFIX = os.environ.get(
    "MEMENTO_EMBEDDING_FAST_DOCUMENT_PREFIX",
    "passage: ",
)
EMBEDDING_FAST_MAX_SEQUENCE_LENGTH = _env_int(
    "MEMENTO_EMBEDDING_FAST_MAX_SEQUENCE_LENGTH",
    512,
)
EMBEDDING_FAST_ONNX_FILE = os.environ.get(
    "MEMENTO_EMBEDDING_FAST_ONNX_FILE",
    "",
).strip()
EMBEDDING_FAST_ARTIFACT_SHA256 = os.environ.get(
    "MEMENTO_EMBEDDING_FAST_ARTIFACT_SHA256",
    "",
).strip().lower()
EMBEDDING_FAST_SERVER_URL = os.environ.get(
    "MEMENTO_EMBEDDING_FAST_SERVER_URL",
    "http://host.docker.internal:8003",
).rstrip("/")

CHUNK_SIZE = 2000  # chars per chunk
CHUNK_OVERLAP = 200

# Per-URL admission/availability. Never share failure state across tiers.
_server_states: dict[str, dict[str, Any]] = {}
# Legacy module aliases retained for existing tests.
_server_available: bool | None = None
_last_check_time: float = 0
try:
    _configured_request_timeout = float(
        os.environ.get("MEMENTO_EMBEDDING_REQUEST_TIMEOUT_SECONDS", "1200")
    )
except (TypeError, ValueError):
    _configured_request_timeout = 1200.0
if not math.isfinite(_configured_request_timeout):
    _configured_request_timeout = 1200.0
EMBEDDING_REQUEST_TIMEOUT_SECONDS = min(
    1200.0,
    max(60.0, _configured_request_timeout),
)
# Keep abandoned-claim recovery comfortably beyond the capped 3-CPU request
# deadline and Celery's task margin. A legitimate 50-chunk BGE-M3 request must
# never be reclaimed while it is still making progress.
EMBEDDING_PROCESSING_STALE_AFTER = timedelta(minutes=35)
CONVERSATION_EMBEDDING_MESSAGE_LIMIT = 100
CONVERSATION_EMBEDDING_MESSAGE_CHARS = 4_000
CONVERSATION_EMBEDDING_TOTAL_CHARS = 100_000
SEMANTIC_RRF_K = 60
FAST_SCHEMA_DIM = 384
_PROFILE_VERSION = 1


class EmbeddingServerBusy(RuntimeError):
    """The healthy embedding server is already processing another request."""


@dataclass(frozen=True)
class EmbeddingProfile:
    """One embedding route: model, dimension, HTTP endpoint, and vector table."""

    tier: str
    server_url: str
    model_name: str
    backend: str
    dimension: int
    orm_model: type
    model_revision: str = ""
    query_prefix: str = ""
    document_prefix: str = ""
    max_sequence_length: int = 0
    onnx_file: str = ""
    artifact_sha256: str = ""

    @property
    def profile_signature(self) -> str:
        return embedding_profile_signature(
            model_name=self.model_name,
            model_revision=self.model_revision,
            backend=self.backend,
            dimension=self.dimension,
            query_prefix=self.query_prefix,
            document_prefix=self.document_prefix,
            max_sequence_length=self.max_sequence_length,
            onnx_file=self.onnx_file if self.backend == "onnx" else "",
            artifact_sha256=(
                self.artifact_sha256 if self.backend == "onnx" else ""
            ),
        )


def embedding_profile_signature(
    *,
    model_name: str,
    model_revision: str,
    backend: str,
    dimension: int,
    query_prefix: str,
    document_prefix: str,
    max_sequence_length: int,
    onnx_file: str,
    artifact_sha256: str,
) -> str:
    """Return the immutable identity of one compatible vector space."""
    payload = {
        "artifact_sha256": artifact_sha256,
        "backend": backend,
        "dimension": dimension,
        "document_prefix": document_prefix,
        "max_sequence_length": max_sequence_length,
        "model_name": model_name,
        "model_revision": model_revision,
        "normalization": "l2",
        "onnx_file": onnx_file,
        "pooling": "sentence-transformers-config",
        "profile_version": _PROFILE_VERSION,
        "query_prefix": query_prefix,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _sync_legacy_availability(url: str) -> None:
    """Mirror quality-URL state onto legacy module globals for older tests."""
    global _server_available, _last_check_time
    if url.rstrip("/") != EMBEDDING_SERVER_URL.rstrip("/"):
        return
    state = _server_states.get(url.rstrip("/"), {})
    _server_available = state.get("available")
    _last_check_time = float(state.get("last_check") or 0)


def reset_embedding_server_state() -> None:
    """Clear per-URL availability (tests / process boot)."""
    global _server_available, _last_check_time
    _server_states.clear()
    _server_available = None
    _last_check_time = 0


def embedding_profile(tier: str = TIER_QUALITY) -> EmbeddingProfile:
    """Return the profile for a tier name."""
    if tier == TIER_FAST:
        if EMBEDDING_FAST_DIM != FAST_SCHEMA_DIM:
            raise RuntimeError(
                f"MEMENTO_EMBEDDING_FAST_DIM={EMBEDDING_FAST_DIM} does not match "
                f"document_embeddings_fast vector({FAST_SCHEMA_DIM})"
            )
        profile = EmbeddingProfile(
            tier=TIER_FAST,
            server_url=EMBEDDING_FAST_SERVER_URL,
            model_name=EMBEDDING_FAST_MODEL_NAME,
            backend=EMBEDDING_FAST_BACKEND,
            dimension=EMBEDDING_FAST_DIM,
            orm_model=DocumentEmbeddingFast,
            model_revision=EMBEDDING_FAST_MODEL_REVISION,
            query_prefix=EMBEDDING_FAST_QUERY_PREFIX,
            document_prefix=EMBEDDING_FAST_DOCUMENT_PREFIX,
            max_sequence_length=EMBEDDING_FAST_MAX_SEQUENCE_LENGTH,
            onnx_file=EMBEDDING_FAST_ONNX_FILE,
            artifact_sha256=EMBEDDING_FAST_ARTIFACT_SHA256,
        )
    else:
        profile = EmbeddingProfile(
            tier=TIER_QUALITY,
            server_url=EMBEDDING_SERVER_URL,
            model_name=EMBEDDING_MODEL_NAME,
            backend=EMBEDDING_BACKEND,
            dimension=EMBEDDING_DIM,
            orm_model=DocumentEmbedding,
            model_revision=EMBEDDING_MODEL_REVISION,
            query_prefix=EMBEDDING_QUERY_PREFIX,
            document_prefix=EMBEDDING_DOCUMENT_PREFIX,
            max_sequence_length=EMBEDDING_MAX_SEQUENCE_LENGTH,
            onnx_file=EMBEDDING_ONNX_FILE,
            artifact_sha256=EMBEDDING_ARTIFACT_SHA256,
        )
    if profile.backend == "onnx" and (
        not profile.onnx_file or not profile.artifact_sha256
    ):
        raise RuntimeError(
            f"{profile.tier} ONNX profile requires both an explicit ONNX file "
            "and its SHA-256 checksum"
        )
    return profile


def embedding_profile_health_mismatch(
    profile: EmbeddingProfile,
    payload: dict[str, Any],
) -> str | None:
    """Validate that one reachable process serves the configured profile."""
    if payload.get("status") != "ok" or payload.get("model") is not True:
        return "model is not ready"
    expected = {
        "model_name": profile.model_name,
        "backend": profile.backend,
        "dimension": profile.dimension,
        "profile_signature": profile.profile_signature,
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            return (
                f"{field}={payload.get(field)!r}; expected {value!r}"
            )
    return None


async def validate_embedding_profile_server(
    profile: EmbeddingProfile,
    *,
    timeout: float = 5.0,
) -> None:
    """Raise with a useful error unless a profile endpoint is ready/correct."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{profile.server_url.rstrip('/')}/health")
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        raise RuntimeError(
            f"{profile.tier} embedding server at {profile.server_url} "
            f"is unavailable: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"{profile.tier} embedding server returned invalid health metadata"
        )
    mismatch = embedding_profile_health_mismatch(profile, payload)
    if mismatch is not None:
        raise RuntimeError(
            f"{profile.tier} embedding server profile mismatch: {mismatch}"
        )


def embedding_tiering_enabled() -> bool:
    """Feature flag — false keeps quality-only behavior."""
    return EMBEDDING_TIERING_ENABLED


def embedding_row_profile_filter(table: type, profile: EmbeddingProfile):
    """SQL predicate that excludes vectors from incompatible model spaces."""
    current = table.profile_signature == profile.profile_signature
    legacy_signature = embedding_profile_signature(
        model_name=LEGACY_EMBEDDING_MODEL_NAME,
        model_revision="",
        backend="torch",
        dimension=1024,
        query_prefix="",
        document_prefix="",
        max_sequence_length=0,
        onnx_file="",
        artifact_sha256="",
    )
    if profile.profile_signature != legacy_signature:
        return current
    return or_(
        current,
        and_(
            table.profile_signature.is_(None),
            or_(
                table.model_name.is_(None),
                table.model_name == LEGACY_EMBEDDING_MODEL_NAME,
            ),
            or_(table.backend.is_(None), table.backend == "torch"),
        ),
    )


def desired_embedding_tier(category: str | None) -> str:
    """Category → preferred tier when tiering is enabled (ignores stickiness)."""
    if not embedding_tiering_enabled():
        return TIER_QUALITY
    cat = (category or "").strip().lower()
    if cat in QUALITY_CATEGORIES:
        return TIER_QUALITY
    if cat in FAST_ELIGIBLE_CATEGORIES:
        return TIER_FAST
    # Unknown categories stay on the quality path (conservative).
    return TIER_QUALITY


def current_embedding_tier(doc: Document | Any) -> str:
    """Persisted tier, defaulting historical rows to quality."""
    tier = getattr(doc, "embedding_tier", None) or TIER_QUALITY
    return TIER_FAST if tier == TIER_FAST else TIER_QUALITY


def resolve_embedding_tier(doc: Document | Any) -> str:
    """Sticky, no-demotion tier resolution for one document.

    Quality is sticky: once a document is on the quality path it never
    automatically moves to the fast table. Fast documents promote to quality
    when their category requires it. New documents must be assigned an initial
    ``embedding_tier`` at creation (see ingest); unset/legacy rows default to
    quality so enabling the feature never mass-re-embeds the corpus.
    """
    current = current_embedding_tier(doc)
    if not embedding_tiering_enabled():
        # Disabling assignment must not orphan already-indexed fast documents.
        # New documents still start on quality via desired_embedding_tier();
        # existing fast rows remain active/searchable until explicitly promoted.
        return current
    if current == TIER_QUALITY:
        return TIER_QUALITY
    return desired_embedding_tier(getattr(doc, "category", None))


def apply_embedding_tier_policy(doc: Document | Any) -> bool:
    """Apply sticky tier policy; invalidate safely on promotion.

    Returns True when the document must be re-embedded (promotion). Fast rows
    are retained as a non-searchable cache while status is pending; search
    only reads the active tier for status=ok documents.
    """
    target = resolve_embedding_tier(doc)
    current = current_embedding_tier(doc)
    doc.embedding_tier = target
    if current == TIER_FAST and target == TIER_QUALITY:
        doc.embedding_status = "pending"
        doc.embedding_attempts = 0
        doc.embedding_claim_token = None
        doc.embedding_claimed_at = None
        return True
    return False


def conversation_embedding_content(
    message_contents: list[str | None],
) -> str:
    """Build the exact normalized-message fallback used for conversations.

    Callers must supply user/assistant messages in transcript order. Keeping
    this transformation shared lets repair jobs determine whether changing
    stored presentation rows actually changes the model input.
    """
    parts: list[str] = []
    used = 0
    for message in message_contents[:CONVERSATION_EMBEDDING_MESSAGE_LIMIT]:
        fragment = (message or "")[:CONVERSATION_EMBEDDING_MESSAGE_CHARS].strip()
        if not fragment:
            continue
        remaining = CONVERSATION_EMBEDDING_TOTAL_CHARS - used
        if remaining <= 0:
            break
        fragment = fragment[:remaining]
        parts.append(fragment)
        used += len(fragment)
    return "\n\n".join(parts)


def _chunk_text(
    text: str,
    chunk_chars: int = CHUNK_SIZE,
    overlap_chars: int = CHUNK_OVERLAP,
    *,
    max_chunks: int | None = None,
) -> list[str]:
    """Split text into overlapping chunks with smart boundary detection."""
    if len(text) <= chunk_chars:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_chars
        if end < len(text):
            for sep in ("\n\n", "\n", ". ", "。", "；"):
                break_pos = text.rfind(sep, start + chunk_chars // 2, end)
                if break_pos != -1:
                    end = break_pos + len(sep)
                    break
        chunk = text[start:end].strip()
        if len(chunk) > 50:
            chunks.append(chunk)
        if max_chunks is not None and len(chunks) >= max_chunks:
            break
        start = end - overlap_chars
    return chunks


def embedding_input_hash(chunks: list[str]) -> str:
    """Hash the exact ordered JSON array submitted to the model.

    Hashing the final bounded chunks (rather than the raw file) makes the
    identity match the real model input, including chunk boundaries. JSON is
    used so different chunk partitions cannot collide through concatenation.
    """
    payload = json.dumps(
        chunks,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def chunk_content_hash(chunk: str) -> str:
    """Return the stable identity of one exact chunk."""
    return hashlib.sha256(chunk.encode("utf-8")).hexdigest()


def chunk_embedding_is_reusable(
    *,
    chunk: str,
    stored_text: str,
    stored_hash: str | None,
    stored_model: str | None,
    stored_backend: str | None = None,
    stored_profile_signature: str | None = None,
    model_name: str = EMBEDDING_MODEL_NAME,
    backend: str = EMBEDDING_BACKEND,
    profile_signature: str | None = None,
) -> bool:
    """Whether an existing vector is valid for one current chunk.

    Rows created before per-chunk metadata was added have NULL hash/model
    values. Exact text plus the historical BGE-M3 model identity lets those
    rows be reused without a corpus-wide backfill.
    """
    effective_model = stored_model or LEGACY_EMBEDDING_MODEL_NAME
    if effective_model != model_name:
        return False
    effective_backend = stored_backend or "torch"
    if effective_backend != backend:
        return False
    expected_signature = (
        profile_signature
        if profile_signature is not None
        else embedding_profile(TIER_QUALITY).profile_signature
    )
    if stored_profile_signature:
        if stored_profile_signature != expected_signature:
            return False
    else:
        legacy_signature = embedding_profile_signature(
            model_name=LEGACY_EMBEDDING_MODEL_NAME,
            model_revision="",
            backend="torch",
            dimension=1024,
            query_prefix="",
            document_prefix="",
            max_sequence_length=0,
            onnx_file="",
            artifact_sha256="",
        )
        if expected_signature != legacy_signature:
            return False
    if stored_hash:
        return stored_hash == chunk_content_hash(chunk)
    return stored_text == chunk


async def document_embedding_input(
    db: AsyncSession,
    doc: Document,
) -> tuple[list[str], str]:
    """Return the exact bounded model input and its stable identity hash."""
    if doc.content_type in ("sqlite", "sqlite_export", "binary"):
        chunks: list[str] = []
        return chunks, embedding_input_hash(chunks)

    embedding_content = doc.content or ""
    if not embedding_content and doc.category == "conversation":
        rows = (
            (
                await db.execute(
                    select(
                        func.left(
                            ConversationMessage.content,
                            CONVERSATION_EMBEDDING_MESSAGE_CHARS,
                        )
                    )
                    .where(
                        ConversationMessage.document_id == doc.id,
                        ConversationMessage.role.in_(("user", "assistant")),
                    )
                    .order_by(
                        ConversationMessage.line_number,
                        ConversationMessage.id,
                    )
                    .limit(CONVERSATION_EMBEDDING_MESSAGE_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        embedding_content = conversation_embedding_content(list(rows))

    chunks = []
    if len(embedding_content) >= 100:
        chunks = _chunk_text(embedding_content, max_chunks=50)
    return chunks, embedding_input_hash(chunks)


async def _call_embedding_server(
    texts: list[str],
    timeout: float = EMBEDDING_REQUEST_TIMEOUT_SECONDS,
    *,
    raise_on_busy: bool = False,
    profile: EmbeddingProfile | None = None,
    purpose: str = "document",
) -> list[list[float]] | None:
    """Call one external embedding HTTP server for a profile.

    timeout: request timeout. Default covers the complete background document
    (up to 50 chunks); interactive callers pass 30s instead. When
    ``raise_on_busy`` is true, an admission 503 is surfaced separately so
    durable background work can remain pending without consuming its finite
    failure budget. Availability is tracked per server URL so a down fast
    server never blocks the quality path (and vice versa).
    """
    import time

    if purpose not in {"query", "document"}:
        raise ValueError("purpose must be 'query' or 'document'")
    active = profile or embedding_profile(TIER_QUALITY)
    url = active.server_url.rstrip("/")
    # Tests still poke the legacy quality-URL globals; mirror them in.
    if (
        url == EMBEDDING_SERVER_URL.rstrip("/")
        and _server_available is None
        and _last_check_time == 0
        and url in _server_states
    ):
        _server_states.pop(url, None)
    state = _server_states.setdefault(
        url,
        {"available": None, "last_check": 0.0},
    )
    if (
        url == EMBEDDING_SERVER_URL.rstrip("/")
        and _server_available is False
        and state["available"] is not False
    ):
        state["available"] = False
        state["last_check"] = float(_last_check_time or time.time())

    if state["available"] is False and (time.time() - state["last_check"]) < 60:
        return None

    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as client:
            # One request reserves server admission for the whole document.
            # The server still encodes in bounded model batches, but another
            # caller cannot interleave and force us to discard partial work.
            resp = await client.post(
                f"{url}/embed",
                json={
                    "texts": texts,
                    "model": active.model_name,
                    "dimensions": active.dimension,
                    "backend": active.backend,
                    "profile_signature": active.profile_signature,
                    "purpose": purpose,
                },
            )
            if resp.status_code == 503:
                state["available"] = True
                _sync_legacy_availability(url)
                if raise_on_busy:
                    raise EmbeddingServerBusy("embedding server is busy")
                return None
            if resp.status_code != 200:
                logger.warning(
                    "Embedding server returned %d (%s)",
                    resp.status_code,
                    url,
                )
                return None
            data = resp.json()
        state["available"] = True
        _sync_legacy_availability(url)
        returned_signature = data.get("profile_signature")
        if returned_signature is None:
            # One rolling-upgrade bridge for the historical quality torch
            # server. New fast/ONNX spaces must always prove their identity.
            legacy_ok = (
                active.tier == TIER_QUALITY
                and active.model_name == LEGACY_EMBEDDING_MODEL_NAME
                and active.backend == "torch"
            )
            if not legacy_ok:
                logger.warning(
                    "Embedding server omitted profile_signature for %s",
                    active.tier,
                )
                return None
        elif returned_signature != active.profile_signature:
            logger.warning(
                "Embedding server profile changed during request on %s: "
                "got %r, expected %r",
                active.tier,
                returned_signature,
                active.profile_signature,
            )
            return None
        vectors = data.get("embeddings", [])
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            logger.warning(
                "Embedding count mismatch on %s: got %d, expected %d",
                active.tier,
                len(vectors) if isinstance(vectors, list) else -1,
                len(texts),
            )
            return None
        for index, vector in enumerate(vectors):
            if (
                not isinstance(vector, list)
                or len(vector) != active.dimension
                or any(not math.isfinite(float(value)) for value in vector)
            ):
                logger.warning(
                    "Invalid embedding vector %d returned for %s profile",
                    index,
                    active.tier,
                )
                return None
        return vectors
    except EmbeddingServerBusy:
        raise
    except Exception as e:
        state["last_check"] = time.time()
        if state["available"] is not True:
            logger.info("Embedding server not available at %s: %s", url, e)
        else:
            logger.warning("Embedding call failed (%s): %s", url, e)
        state["available"] = False
        _sync_legacy_availability(url)
        return None


async def generate_document_embeddings(db: AsyncSession, doc: Document) -> int:
    """Generate and store embeddings for a document. Returns count of chunks created.

    Writes ``doc.embedding_status`` via raw UPDATE statements (not ORM attribute
    assignment) so concurrent ingests of the same file don't trigger
    SQLAlchemy's stale-row detection — under load every collector resend used
    to roll back the whole transaction and lose the embeddings.

    When tiering is enabled, routes to the quality or fast profile/table. Never
    writes incompatible dimensions into the same vector column.
    """
    revision_hash = doc.content_hash
    claim_token = str(uuid4())
    apply_embedding_tier_policy(doc)
    target_tier = resolve_embedding_tier(doc)
    profile = embedding_profile(target_tier)
    EmbeddingTable = profile.orm_model
    other_table = (
        DocumentEmbeddingFast
        if target_tier == TIER_QUALITY
        else DocumentEmbedding
    )
    chunks, input_hash = await document_embedding_input(db, doc)

    async def _claim_revision() -> bool:
        """Claim one exact revision, including abandoned processing work."""
        stale_before = datetime.now(timezone.utc) - EMBEDDING_PROCESSING_STALE_AFTER
        result = await db.execute(
            update(Document)
            .where(
                Document.id == doc.id,
                Document.content_hash == revision_hash,
                or_(
                    Document.embedding_status.in_(("pending", "failed")),
                    and_(
                        Document.embedding_status == "processing",
                        or_(
                            Document.embedding_claimed_at.is_(None),
                            Document.embedding_claimed_at < stale_before,
                        ),
                    ),
                ),
            )
            .values(
                embedding_status="processing",
                embedding_content_hash=input_hash,
                embedding_claim_token=claim_token,
                embedding_claimed_at=func.now(),
                embedding_tier=target_tier,
                updated_at=func.now(),
            )
        )
        await db.commit()
        if result.rowcount == 1:
            doc.embedding_tier = target_tier
        return result.rowcount == 1

    async def _set_status(status: str, *, bump_attempts: bool = False) -> bool:
        """Update embedding_status in its own short transaction.

        Critical: commits IMMEDIATELY so the documents-row write lock is
        released before any long-running await (BGE-M3 call can take 10+s).
        Without this, the doc row stays locked the whole time, heartbeat /
        ingest contention piles up and the connection pool dies.
        """
        values: dict = {
            "embedding_status": status,
            "embedding_claim_token": None,
            "embedding_claimed_at": None,
            "embedding_tier": target_tier,
            "updated_at": func.now(),
        }
        if bump_attempts:
            values["embedding_attempts"] = (
                func.coalesce(Document.embedding_attempts, 0) + 1
            )
        result = await db.execute(
            update(Document)
            .where(
                Document.id == doc.id,
                Document.embedding_content_hash == input_hash,
                Document.embedding_status == "processing",
                Document.embedding_claim_token == claim_token,
            )
            .values(**values)
        )
        await db.commit()
        return result.rowcount == 1

    if not await _claim_revision():
        return 0

    if doc.content_type in ("sqlite", "sqlite_export", "binary"):
        await db.execute(
            delete(EmbeddingTable).where(EmbeddingTable.document_id == doc.id)
        )
        await db.execute(
            delete(other_table).where(other_table.document_id == doc.id)
        )
        await _set_status("skipped", bump_attempts=True)
        return 0

    if not chunks:
        await db.execute(
            delete(EmbeddingTable).where(EmbeddingTable.document_id == doc.id)
        )
        await db.execute(
            delete(other_table).where(other_table.document_id == doc.id)
        )
        await _set_status("skipped", bump_attempts=True)
        return 0

    existing_rows = (
        await db.execute(
            select(
                EmbeddingTable.chunk_index,
                EmbeddingTable.chunk_text,
                EmbeddingTable.chunk_hash,
                EmbeddingTable.model_name,
                EmbeddingTable.backend,
                EmbeddingTable.profile_signature,
            ).where(EmbeddingTable.document_id == doc.id)
        )
    ).all()
    existing_by_index = {row.chunk_index: row for row in existing_rows}
    changed_indices = [
        index
        for index, chunk in enumerate(chunks)
        if (
            (row := existing_by_index.get(index)) is None
            or not chunk_embedding_is_reusable(
                chunk=chunk,
                stored_text=row.chunk_text,
                stored_hash=row.chunk_hash,
                stored_model=row.model_name,
                stored_backend=row.backend,
                stored_profile_signature=row.profile_signature,
                model_name=profile.model_name,
                backend=profile.backend,
                profile_signature=profile.profile_signature,
            )
        )
    ]

    # The normalized-message SELECT above starts a transaction. Release it
    # before a multi-minute model call so Postgres does not terminate the
    # connection under idle_in_transaction_session_timeout.
    await db.commit()
    embeddings: list[list[float]] = []
    if changed_indices:
        changed_chunks = [chunks[index] for index in changed_indices]
        logger.info(
            "Embedding %d/%d changed chunks for %s via %s (%s)",
            len(changed_chunks),
            len(chunks),
            doc.relative_path,
            profile.tier,
            profile.model_name,
        )
        try:
            result = await _call_embedding_server(
                changed_chunks,
                raise_on_busy=True,
                profile=profile,
            )
        except EmbeddingServerBusy:
            # Healthy but occupied is admission control, not a failed attempt.
            # Keep the durable document retry-eligible for the next scanner pass.
            await _set_status("pending")
            return 0
        if not result:
            await _set_status("failed", bump_attempts=True)
            return 0
        embeddings = result

        if len(embeddings[0]) != profile.dimension:
            logger.warning(
                "Embedding dim mismatch on %s: got %d, expected %d",
                profile.tier,
                len(embeddings[0]),
                profile.dimension,
            )
            await _set_status("failed", bump_attempts=True)
            return 0

        if len(embeddings) != len(changed_chunks):
            logger.warning(
                "Embedding count mismatch on %s: got %d, expected %d",
                profile.tier,
                len(embeddings),
                len(changed_chunks),
            )
            await _set_status("failed", bump_attempts=True)
            return 0
    else:
        logger.info(
            "Reusing all %d unchanged %s chunks for %s",
            len(chunks),
            profile.tier,
            doc.relative_path,
        )

    # Lock and re-check the exact model input immediately before writing
    # vectors. A concurrent append outside the bounded input may change the raw
    # file hash but can safely let this worker finish. A change within the
    # input updates embedding_content_hash and makes this return no row.
    current_revision = await db.execute(
        select(Document.id)
        .where(
            Document.id == doc.id,
            Document.embedding_content_hash == input_hash,
            Document.embedding_status == "processing",
            Document.embedding_claim_token == claim_token,
        )
        .with_for_update()
    )
    if current_revision.scalar_one_or_none() is None:
        await db.rollback()
        return 0

    # Upsert each changed chunk into the profile-specific table. ON CONFLICT
    # keeps concurrent post-ingest writers idempotent.
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    rows = [
        {
            "document_id": doc.id,
            "chunk_index": index,
            "chunk_text": chunks[index],
            "chunk_hash": chunk_content_hash(chunks[index]),
            "model_name": profile.model_name,
            "backend": profile.backend,
            "profile_signature": profile.profile_signature,
            "embedding": embedding,
        }
        for index, embedding in zip(changed_indices, embeddings)
    ]
    if rows:
        stmt = pg_insert(EmbeddingTable).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["document_id", "chunk_index"],
            set_={
                "chunk_text": stmt.excluded.chunk_text,
                "chunk_hash": stmt.excluded.chunk_hash,
                "model_name": stmt.excluded.model_name,
                "backend": stmt.excluded.backend,
                "profile_signature": stmt.excluded.profile_signature,
                "embedding": stmt.excluded.embedding,
            },
        )
        await db.execute(stmt)

    # Trim stale tail chunks for this profile in the same transaction.
    await db.execute(
        delete(EmbeddingTable).where(
            EmbeddingTable.document_id == doc.id,
            EmbeddingTable.chunk_index >= len(chunks),
        )
    )
    # After a successful quality write (including promotion), drop any fast
    # rows so they cannot become searchable later. Fast writes leave quality
    # rows alone — sticky quality policy forbids demotion.
    if target_tier == TIER_QUALITY:
        await db.execute(
            delete(DocumentEmbeddingFast).where(
                DocumentEmbeddingFast.document_id == doc.id
            )
        )

    await db.flush()
    await _set_status("ok", bump_attempts=True)
    logger.info(
        "Generated %d %s embeddings for %s/%s",
        len(chunks),
        profile.tier,
        doc.tool_id,
        doc.relative_path,
    )
    return len(chunks)


def reciprocal_rank_fusion(
    ranked_lists: list[list[dict]],
    *,
    k: int = SEMANTIC_RRF_K,
) -> list[dict]:
    """Bounded multi-tier fusion that never mixes incompatible vector spaces.

    Each list is already ordered best-first within one tier/index. RRF combines
    ranks (not raw cosine scores), so 384-d and 1024-d distances are never
    treated as comparable inside one ANN index.
    """
    scores: dict[Any, float] = {}
    best_item: dict[Any, dict] = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):
            doc_id = item["_document_id"]
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
            previous = best_item.get(doc_id)
            if previous is None:
                best_item[doc_id] = item
                continue
            # Prefer quality on equal RRF contribution; otherwise keep the
            # earlier (better-ranked within its own list) item already stored
            # unless this item's within-tier rank score is higher via later pass.
            prev_tier = previous.get("_tier", TIER_QUALITY)
            cur_tier = item.get("_tier", TIER_QUALITY)
            if prev_tier != TIER_QUALITY and cur_tier == TIER_QUALITY:
                best_item[doc_id] = item
    fused = []
    for doc_id, item in best_item.items():
        merged = dict(item)
        merged["score"] = round(scores[doc_id], 6)
        fused.append(merged)
    fused.sort(
        key=lambda row: (
            -float(row["score"]),
            0 if row.get("_tier") == TIER_QUALITY else 1,
            str(row["_document_id"]),
        )
    )
    return fused


async def tiers_with_searchable_rows(
    db: AsyncSession,
    *,
    machine_ids: list | None,
    tool_filter: str | None = None,
    days: int | None = None,
) -> list[str]:
    """Return active tiers that currently have searchable embedding rows."""
    # Always discover both physical indexes. The feature flag controls routing
    # of new documents, not visibility of already-persisted fast vectors.
    tiers = [TIER_QUALITY, TIER_FAST]

    from datetime import datetime, timedelta, timezone

    eligible: list[str] = []
    for tier in tiers:
        profile = embedding_profile(tier)
        table = profile.orm_model
        q = (
            select(table.document_id)
            .join(Document, table.document_id == Document.id)
            .where(
                Document.embedding_status == "ok",
                Document.embedding_tier == tier,
                embedding_row_profile_filter(table, profile),
            )
            .limit(1)
        )
        if tool_filter:
            q = q.where(Document.tool_id == tool_filter)
        if days:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            q = q.where(Document.synced_at >= cutoff)
        if machine_ids is not None:
            q = q.where(Document.machine_id.in_(machine_ids))
        if (await db.execute(q)).first() is not None:
            eligible.append(tier)
    return eligible
