"""Hybrid search engine — semantic (pgvector) + full-text (tsvector) + knowledge graph."""

from __future__ import annotations

import logging
import os
import hashlib
import json
import math
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select, text, or_
from sqlalchemy.ext.asyncio import AsyncSession

from .db import (
    Document,
    DocumentEmbedding,
    DocumentEmbeddingFast,
    KnowledgeEntity,
    KnowledgeObservation,
    Machine,
)

logger = logging.getLogger("mcp_memory.search")


_local_model = None
_model_lock = None
_PROFILE_VERSION = 1


def _embedding_profile(tier: str) -> dict:
    if tier == "fast":
        prefix = "MEMENTO_EMBEDDING_FAST"
        defaults = {
            "model_name": "intfloat/multilingual-e5-small",
            "dimension": 384,
            "query_prefix": "query: ",
            "document_prefix": "passage: ",
            "max_sequence_length": 512,
        }
    else:
        prefix = "MEMENTO_EMBEDDING"
        defaults = {
            "model_name": "BAAI/bge-m3",
            "dimension": 1024,
            "query_prefix": "",
            "document_prefix": "",
            "max_sequence_length": 0,
        }
    try:
        dimension = int(os.environ.get(f"{prefix}_DIM", defaults["dimension"]))
        max_sequence_length = int(
            os.environ.get(
                f"{prefix}_MAX_SEQUENCE_LENGTH",
                defaults["max_sequence_length"],
            )
        )
    except (TypeError, ValueError):
        dimension = int(defaults["dimension"])
        max_sequence_length = int(defaults["max_sequence_length"])
    profile = {
        "tier": tier,
        "server_url": os.environ.get(f"{prefix}_SERVER_URL", "").strip().rstrip("/"),
        "model_name": os.environ.get(
            f"{prefix}_MODEL_NAME",
            str(defaults["model_name"]),
        ).strip(),
        "model_revision": os.environ.get(f"{prefix}_MODEL_REVISION", "").strip(),
        "backend": os.environ.get(f"{prefix}_BACKEND", "torch").strip().lower(),
        "dimension": dimension,
        "query_prefix": os.environ.get(
            f"{prefix}_QUERY_PREFIX",
            str(defaults["query_prefix"]),
        ),
        "document_prefix": os.environ.get(
            f"{prefix}_DOCUMENT_PREFIX",
            str(defaults["document_prefix"]),
        ),
        "max_sequence_length": max_sequence_length,
        "onnx_file": os.environ.get(f"{prefix}_ONNX_FILE", "").strip(),
        "artifact_sha256": os.environ.get(
            f"{prefix}_ARTIFACT_SHA256",
            "",
        ).strip().lower(),
    }
    if profile["backend"] != "onnx":
        profile["onnx_file"] = ""
        profile["artifact_sha256"] = ""
    identity = {
        "artifact_sha256": profile["artifact_sha256"],
        "backend": profile["backend"],
        "dimension": profile["dimension"],
        "document_prefix": profile["document_prefix"],
        "max_sequence_length": profile["max_sequence_length"],
        "model_name": profile["model_name"],
        "model_revision": profile["model_revision"],
        "normalization": "l2",
        "onnx_file": profile["onnx_file"],
        "pooling": "sentence-transformers-config",
        "profile_version": _PROFILE_VERSION,
        "query_prefix": profile["query_prefix"],
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    profile["profile_signature"] = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    return profile


def _profile_row_filter(embedding_table, profile: dict):
    current = embedding_table.profile_signature == profile["profile_signature"]
    legacy = _embedding_profile("quality")
    legacy.update(
        model_name="BAAI/bge-m3",
        model_revision="",
        backend="torch",
        dimension=1024,
        query_prefix="",
        document_prefix="",
        max_sequence_length=0,
        onnx_file="",
        artifact_sha256="",
    )
    identity = {
        key: legacy[key]
        for key in (
            "artifact_sha256",
            "backend",
            "dimension",
            "document_prefix",
            "max_sequence_length",
            "model_name",
            "model_revision",
            "onnx_file",
            "query_prefix",
        )
    }
    identity.update(
        normalization="l2",
        pooling="sentence-transformers-config",
        profile_version=_PROFILE_VERSION,
    )
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    legacy_signature = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    if profile["profile_signature"] != legacy_signature:
        return current
    return or_(
        current,
        and_(
            embedding_table.profile_signature.is_(None),
            or_(
                embedding_table.model_name.is_(None),
                embedding_table.model_name == "BAAI/bge-m3",
            ),
            or_(
                embedding_table.backend.is_(None),
                embedding_table.backend == "torch",
            ),
        ),
    )


def _get_local_model():
    """Load BGE-M3 model lazily."""
    global _local_model, _model_lock
    import threading
    if _model_lock is None:
        _model_lock = threading.Lock()
    if _local_model is not None:
        return _local_model
    with _model_lock:
        if _local_model is not None:
            return _local_model
        try:
            from sentence_transformers import SentenceTransformer
            _local_model = SentenceTransformer("BAAI/bge-m3")
            return _local_model
        except Exception:
            return None


async def _get_internal_embedding(
    query: str,
    *,
    tier: str,
) -> list[float] | None:
    """Use Memento's configured profile server when direct MCP can reach it."""
    profile = _embedding_profile(tier)
    server_url = profile["server_url"]
    if not server_url:
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{server_url}/embed",
                json={
                    "texts": [query],
                    "model": profile["model_name"],
                    "dimensions": profile["dimension"],
                    "backend": profile["backend"],
                    "profile_signature": profile["profile_signature"],
                    "purpose": "query",
                },
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("profile_signature") != profile["profile_signature"]:
                return None
            embeddings = payload.get("embeddings", [])
        if (
            len(embeddings) == 1
            and len(embeddings[0]) == profile["dimension"]
            and all(math.isfinite(float(value)) for value in embeddings[0])
        ):
            return embeddings[0]
    except Exception as exc:
        logger.debug("%s embedding server failed: %s", tier, exc)
    return None


async def _get_embedding(
    query: str,
    *,
    tier: str = "quality",
) -> list[float] | None:
    """Generate a profile-compatible query vector."""
    internal = await _get_internal_embedding(query, tier=tier)
    if internal is not None:
        return internal
    # Avoid loading a second model into direct MCP merely to support the
    # optional fast tier. Its dedicated service is the authoritative runtime.
    if tier == "fast":
        return None

    import asyncio
    profile = _embedding_profile(tier)
    legacy = (
        profile["model_name"] == "BAAI/bge-m3"
        and profile["model_revision"] == ""
        and profile["backend"] == "torch"
        and profile["query_prefix"] == ""
        and profile["max_sequence_length"] == 0
    )
    if not legacy:
        return None
    # Try local model
    model = _get_local_model()
    if model is not None:
        try:
            embedding = await asyncio.to_thread(
                lambda: model.encode(query, normalize_embeddings=True).tolist()
            )
            return embedding
        except Exception:
            pass

    # A third-party embedding API is not a compatible fallback for a BGE-M3
    # index even when dimensions happen to match.
    return None


async def _tier_has_searchable_rows(
    db: AsyncSession,
    *,
    tier: str,
) -> bool:
    embedding_table = (
        DocumentEmbeddingFast if tier == "fast" else DocumentEmbedding
    )
    profile = _embedding_profile(tier)
    result = await db.execute(
        select(embedding_table.id)
        .join(Document, embedding_table.document_id == Document.id)
        .where(
            Document.embedding_status == "ok",
            Document.embedding_tier == tier,
            _profile_row_filter(embedding_table, profile),
        )
        .limit(1)
    )
    return result.first() is not None


async def _semantic_search_tier(
    db: AsyncSession,
    query: str,
    limit: int,
    user_machine_ids: list[uuid.UUID] | None,
    tool_filter: str | None,
    cutoff: datetime | None,
    *,
    tier: str,
) -> list[dict]:
    """Search one dimension-safe pgvector table."""
    embedding = await _get_embedding(query, tier=tier)
    if embedding is None:
        return []

    try:
        from pgvector.sqlalchemy import Vector
    except ImportError:
        return []

    embedding_table = (
        DocumentEmbeddingFast if tier == "fast" else DocumentEmbedding
    )
    profile = _embedding_profile(tier)
    q = (
        select(
            embedding_table.chunk_text,
            embedding_table.document_id,
            Document.title,
            Document.tool_id,
            Document.relative_path,
            Document.synced_at,
            embedding_table.embedding.cosine_distance(embedding).label("distance"),
        )
        .join(Document, embedding_table.document_id == Document.id)
        # Invalidation retains old vectors as a delta-reuse cache. They are not
        # search-current until the claim-fenced finalizer marks the document ok.
        .where(
            Document.embedding_status == "ok",
            Document.embedding_tier == tier,
            _profile_row_filter(embedding_table, profile),
        )
        .order_by("distance")
        .limit(limit)
    )
    if user_machine_ids is not None:
        q = q.where(Document.machine_id.in_(user_machine_ids))
    if tool_filter:
        q = q.where(Document.tool_id == tool_filter)
    if cutoff:
        q = q.where(Document.synced_at >= cutoff)

    result = await db.execute(q)
    return [
        {
            "content": row.chunk_text,
            "title": row.title or row.relative_path,
            "tool_id": row.tool_id,
            "relative_path": row.relative_path,
            "date": row.synced_at.strftime("%Y-%m-%d") if row.synced_at else "",
            "score": 1.0 - row.distance,  # Convert distance to similarity
            "source": "semantic",
        }
        for row in result.all()
    ]


def _fuse_semantic_rankings(
    rankings: list[list[dict]],
    limit: int,
) -> list[dict]:
    nonempty = [ranking for ranking in rankings if ranking]
    if len(nonempty) <= 1:
        return nonempty[0][:limit] if nonempty else []
    scores: dict[tuple[str, str], float] = {}
    items: dict[tuple[str, str], dict] = {}
    for ranking in nonempty:
        for rank, item in enumerate(ranking, start=1):
            key = (item["relative_path"], item["content"])
            scores[key] = scores.get(key, 0.0) + (1.0 / (60 + rank))
            items[key] = item
    ordered = sorted(scores, key=scores.get, reverse=True)
    fused = []
    for key in ordered[:limit]:
        item = dict(items[key])
        item["score"] = min(1.0, scores[key] * 61)
        fused.append(item)
    return fused


async def _semantic_search(
    db: AsyncSession, query: str, limit: int, user_machine_ids: list[uuid.UUID] | None,
    tool_filter: str | None, cutoff: datetime | None,
) -> list[dict]:
    """Search all persisted active tiers and combine them with RRF."""
    rankings = [
        await _semantic_search_tier(
            db,
            query,
            limit,
            user_machine_ids,
            tool_filter,
            cutoff,
            tier="quality",
        )
    ]
    if await _tier_has_searchable_rows(db, tier="fast"):
        rankings.append(
            await _semantic_search_tier(
                db,
                query,
                limit,
                user_machine_ids,
                tool_filter,
                cutoff,
                tier="fast",
            )
        )
    return _fuse_semantic_rankings(rankings, limit)


async def _fulltext_search(
    db: AsyncSession, query: str, limit: int, user_machine_ids: list[uuid.UUID] | None,
    tool_filter: str | None, cutoff: datetime | None,
) -> list[dict]:
    """Search lightweight candidates, then hydrate verified text for snippets."""
    pattern = f"%{query}%"
    q = (
        select(Document)
        .where(
            or_(
                Document.content_tsv.op("@@")(func.plainto_tsquery("simple", query)),
                Document.title.ilike(pattern),
            )
        )
        .order_by(Document.synced_at.desc())
        .limit(limit)
    )
    if user_machine_ids is not None:
        q = q.where(Document.machine_id.in_(user_machine_ids))
    if tool_filter:
        q = q.where(Document.tool_id == tool_filter)
    if cutoff:
        q = q.where(Document.synced_at >= cutoff)

    result = await db.execute(q)
    results = []
    from .content_store import document_content

    for document in result.scalars().all():
        # Extract relevant snippet around the match
        content = (await document_content(db, document)) or ""
        idx = content.lower().find(query.lower())
        if idx >= 0:
            start = max(0, idx - 200)
            end = min(len(content), idx + len(query) + 300)
            snippet = ("..." if start > 0 else "") + content[start:end] + ("..." if end < len(content) else "")
        else:
            snippet = content[:500]

        results.append({
            "content": snippet,
            "title": document.title or document.relative_path,
            "tool_id": document.tool_id,
            "relative_path": document.relative_path,
            "date": document.synced_at.strftime("%Y-%m-%d") if document.synced_at else "",
            "score": 0.5,  # Fixed score for full-text matches
            "source": "fulltext",
        })
    return results


async def _graph_search(
    db: AsyncSession, query: str, limit: int, user_id: uuid.UUID | None,
) -> list[dict]:
    """Search via knowledge graph entity matching."""
    # Find matching entities
    q = (
        select(KnowledgeEntity.name, KnowledgeEntity.entity_type, KnowledgeEntity.summary)
        .where(KnowledgeEntity.name.ilike(f"%{query}%"))
        .limit(5)
    )
    if user_id:
        q = q.where(or_(KnowledgeEntity.user_id == user_id, KnowledgeEntity.user_id.is_(None)))

    entities = await db.execute(q)
    entity_rows = entities.all()
    if not entity_rows:
        return []

    results = []
    for name, etype, summary in entity_rows:
        # Get recent observations
        obs_q = (
            select(KnowledgeObservation.content, KnowledgeObservation.observed_at)
            .join(KnowledgeEntity, KnowledgeObservation.entity_id == KnowledgeEntity.id)
            .where(KnowledgeEntity.name == name)
            .order_by(KnowledgeObservation.observed_at.desc())
            .limit(3)
        )
        obs_result = await db.execute(obs_q)
        observations = [f"- {r.content}" for r in obs_result.all()]

        content = f"**{name}** ({etype})\n"
        if summary:
            content += f"{summary}\n"
        if observations:
            content += "\nRecent observations:\n" + "\n".join(observations)

        results.append({
            "content": content,
            "title": name,
            "tool_id": "knowledge_graph",
            "relative_path": f"entity/{etype}/{name}",
            "date": "",
            "score": 0.7,
            "source": "graph",
        })
    return results


async def hybrid_search(
    db: AsyncSession,
    query: str,
    limit: int = 5,
    tool_filter: str | None = None,
    days: int | None = None,
    user_id: uuid.UUID | None = None,
) -> list[dict]:
    """Combined semantic + full-text + graph search with deduplication."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days else None

    # Get user's machine IDs for data isolation
    user_machine_ids = None
    if user_id:
        result = await db.execute(select(Machine.id).where(Machine.user_id == user_id))
        user_machine_ids = [r[0] for r in result.all()]

    # Run all search strategies
    semantic_results = await _semantic_search(db, query, limit * 2, user_machine_ids, tool_filter, cutoff)
    fulltext_results = await _fulltext_search(db, query, limit * 2, user_machine_ids, tool_filter, cutoff)
    graph_results = await _graph_search(db, query, limit, user_id)

    # Merge and deduplicate by relative_path
    seen = set()
    merged = []
    for r in sorted(semantic_results + fulltext_results + graph_results, key=lambda x: -x["score"]):
        key = r["relative_path"]
        if key in seen:
            continue
        seen.add(key)
        merged.append(r)
        if len(merged) >= limit:
            break

    return merged
