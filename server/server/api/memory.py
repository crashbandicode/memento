"""Memory API — knowledge graph visualization and embedding stats."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (
    Document, DocumentEmbedding, KnowledgeEntity, KnowledgeObservation,
    KnowledgeRelation, Machine, User,
)
from ..db.session import get_db
from ..middleware.auth import get_current_user
from ..services.conversation_hierarchy import (
    ConversationRef,
    build_subagent_summaries,
    current_thread_id,
    fold_codex_subagents,
)
from ..services.user_filter import user_machine_ids

router = APIRouter(prefix="/api/memory", tags=["memory"])


def _is_admin(user: User) -> bool:
    return user.role in ("admin", "owner")


def _user_entity_ids_subq(user: User):
    """Subquery: IDs of KnowledgeEntity rows owned by this user."""
    return select(KnowledgeEntity.id).where(KnowledgeEntity.user_id == user.id)


def _user_doc_ids_subq(user: User):
    """Subquery: IDs of Documents belonging to this user's machines."""
    return select(Document.id).where(
        Document.machine_id.in_(
            select(Machine.id).where(Machine.user_id == user.id)
        )
    )


@router.get("/stats")
async def get_memory_stats(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Overall memory statistics — scoped to current user unless admin/owner."""
    admin = _is_admin(_user)

    ent_q = select(func.count()).select_from(KnowledgeEntity)
    if not admin:
        ent_q = ent_q.where(KnowledgeEntity.user_id == _user.id)
    entities = (await db.execute(ent_q)).scalar() or 0

    rel_q = select(func.count()).select_from(KnowledgeRelation)
    if not admin:
        rel_q = rel_q.where(KnowledgeRelation.source_id.in_(_user_entity_ids_subq(_user)))
    relations = (await db.execute(rel_q)).scalar() or 0

    obs_q = select(func.count()).select_from(KnowledgeObservation)
    if not admin:
        obs_q = obs_q.where(KnowledgeObservation.entity_id.in_(_user_entity_ids_subq(_user)))
    observations = (await db.execute(obs_q)).scalar() or 0

    emb_q = select(func.count()).select_from(DocumentEmbedding)
    if not admin:
        emb_q = emb_q.where(DocumentEmbedding.document_id.in_(_user_doc_ids_subq(_user)))
    embeddings = (await db.execute(emb_q)).scalar() or 0

    # Entity type breakdown
    type_q = select(KnowledgeEntity.entity_type, func.count()).group_by(KnowledgeEntity.entity_type)
    if not admin:
        type_q = type_q.where(KnowledgeEntity.user_id == _user.id)
    type_result = await db.execute(type_q)
    entity_types = {r[0]: r[1] for r in type_result.all()}

    return {
        "entities": entities,
        "relations": relations,
        "observations": observations,
        "embeddings": embeddings,
        "entity_types": entity_types,
    }


@router.get("/graph")
async def get_knowledge_graph(
    limit: int = Query(100, ge=1, le=500),
    entity_type: str | None = None,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Get knowledge graph data (nodes + edges) for visualization."""
    admin = _is_admin(_user)
    # Nodes: entities
    entity_q = select(KnowledgeEntity).order_by(KnowledgeEntity.updated_at.desc()).limit(limit)
    if entity_type:
        entity_q = entity_q.where(KnowledgeEntity.entity_type == entity_type)
    if not admin:
        entity_q = entity_q.where(KnowledgeEntity.user_id == _user.id)
    entities = (await db.execute(entity_q)).scalars().all()

    entity_ids = {e.id for e in entities}
    nodes = [
        {
            "id": str(e.id),
            "name": e.name,
            "type": e.entity_type,
            "summary": e.summary,
        }
        for e in entities
    ]

    # Edges: relations between visible entities
    if entity_ids:
        rel_result = await db.execute(
            select(KnowledgeRelation).where(
                KnowledgeRelation.source_id.in_(entity_ids),
                KnowledgeRelation.target_id.in_(entity_ids),
            )
        )
        edges = [
            {
                "source": str(r.source_id),
                "target": str(r.target_id),
                "type": r.relation_type,
                "strength": r.strength,
            }
            for r in rel_result.scalars().all()
        ]
    else:
        edges = []

    return {"nodes": nodes, "edges": edges}


@router.get("/entities/{entity_id}")
async def get_entity_detail(
    entity_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Get entity detail with observations and relations."""
    entity = (await db.execute(
        select(KnowledgeEntity).where(KnowledgeEntity.id == entity_id)
    )).scalar_one_or_none()
    if not entity:
        return {"error": "not found"}
    # Isolation: non-admin can only view their own entities. Mask as "not found"
    # rather than 403 to avoid leaking the existence of other users' entities.
    if not _is_admin(_user) and entity.user_id != _user.id:
        return {"error": "not found"}

    # Observations
    obs_result = await db.execute(
        select(KnowledgeObservation)
        .where(KnowledgeObservation.entity_id == entity_id)
        .order_by(KnowledgeObservation.observed_at.desc())
        .limit(20)
    )
    observations = [
        {
            "content": o.content,
            "observed_at": o.observed_at.isoformat() if o.observed_at else None,
            "source_document_id": str(o.source_document_id) if o.source_document_id else None,
        }
        for o in obs_result.scalars().all()
    ]

    # Outgoing relations
    out_result = await db.execute(
        select(KnowledgeRelation, KnowledgeEntity)
        .join(KnowledgeEntity, KnowledgeRelation.target_id == KnowledgeEntity.id)
        .where(KnowledgeRelation.source_id == entity_id)
    )
    outgoing = [
        {"target_name": target.name, "target_type": target.entity_type, "relation": rel.relation_type}
        for rel, target in out_result.all()
    ]

    # Incoming relations
    in_result = await db.execute(
        select(KnowledgeRelation, KnowledgeEntity)
        .join(KnowledgeEntity, KnowledgeRelation.source_id == KnowledgeEntity.id)
        .where(KnowledgeRelation.target_id == entity_id)
    )
    incoming = [
        {"source_name": source.name, "source_type": source.entity_type, "relation": rel.relation_type}
        for rel, source in in_result.all()
    ]

    return {
        "id": str(entity.id),
        "name": entity.name,
        "type": entity.entity_type,
        "summary": entity.summary,
        "observations": observations,
        "outgoing_relations": outgoing,
        "incoming_relations": incoming,
    }


@router.get("/search")
async def search_memory(
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[dict]:
    """Search entities and observations."""
    admin = _is_admin(_user)
    pattern = f"%{q}%"

    # Search entities
    ent_search_q = (
        select(KnowledgeEntity)
        .where(KnowledgeEntity.name.ilike(pattern) | KnowledgeEntity.summary.ilike(pattern))
        .limit(limit)
    )
    if not admin:
        ent_search_q = ent_search_q.where(KnowledgeEntity.user_id == _user.id)
    entity_result = await db.execute(ent_search_q)
    results = [
        {
            "type": "entity",
            "id": str(e.id),
            "name": e.name,
            "entity_type": e.entity_type,
            "summary": e.summary,
        }
        for e in entity_result.scalars().all()
    ]

    # Search observations
    if len(results) < limit:
        obs_search_q = (
            select(KnowledgeObservation, KnowledgeEntity.name)
            .join(KnowledgeEntity, KnowledgeObservation.entity_id == KnowledgeEntity.id)
            .where(KnowledgeObservation.content.ilike(pattern))
            .limit(limit - len(results))
        )
        if not admin:
            obs_search_q = obs_search_q.where(KnowledgeEntity.user_id == _user.id)
        obs_result = await db.execute(obs_search_q)
        for o, entity_name in obs_result.all():
            results.append({
                "type": "observation",
                "id": str(o.id),
                "name": entity_name,
                "content": o.content,
                "observed_at": o.observed_at.isoformat() if o.observed_at else None,
            })

    return results


@router.post("/compact")
async def compact_memory(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Run memory compaction — merge old observations into summaries."""
    from ..services.memory_compaction import run_compaction
    return await run_compaction(db)


@router.post("/reset")
async def reset_memory(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Clear this user's knowledge graph + embeddings. Admin/owner clears everything.

    Memory will regenerate from next ingest. A non-admin calling this MUST NOT
    be able to wipe other users' data — without scoping this was a catastrophic
    multi-tenant bug (any logged-in user could nuke everyone's graph).
    """
    from sqlalchemy import delete, text

    admin = _is_admin(_user)

    if admin:
        obs = (await db.execute(delete(KnowledgeObservation))).rowcount
        rels = (await db.execute(delete(KnowledgeRelation))).rowcount
        ents = (await db.execute(delete(KnowledgeEntity))).rowcount
        embs = (await db.execute(delete(DocumentEmbedding))).rowcount
        await db.execute(text(
            "UPDATE documents SET metadata = metadata - '_graph_hash' "
            "WHERE metadata ? '_graph_hash'"
        ))
    else:
        obs = (await db.execute(
            delete(KnowledgeObservation).where(
                KnowledgeObservation.entity_id.in_(_user_entity_ids_subq(_user))
            )
        )).rowcount
        rels = (await db.execute(
            delete(KnowledgeRelation).where(
                KnowledgeRelation.source_id.in_(_user_entity_ids_subq(_user))
            )
        )).rowcount
        ents = (await db.execute(
            delete(KnowledgeEntity).where(KnowledgeEntity.user_id == _user.id)
        )).rowcount
        embs = (await db.execute(
            delete(DocumentEmbedding).where(
                DocumentEmbedding.document_id.in_(_user_doc_ids_subq(_user))
            )
        )).rowcount
        await db.execute(text(
            "UPDATE documents SET metadata = metadata - '_graph_hash' "
            "WHERE metadata ? '_graph_hash' "
            "AND machine_id IN (SELECT id FROM machines WHERE user_id = :uid)"
        ), {"uid": _user.id})

    await db.commit()
    return {
        "status": "reset",
        "deleted": {
            "entities": ents,
            "relations": rels,
            "observations": obs,
            "embeddings": embs,
        },
    }


# ---------------------------------------------------------------------------
# Direct memory writes — MCP memory_store tool calls this
# ---------------------------------------------------------------------------
class ObservationCreate(BaseModel):
    content: str
    entity_name: str | None = None
    entity_type: str = "concept"


@router.post("/observations")
async def create_observation(
    body: ObservationCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Store a free-form memory observation attached to a (possibly new) entity.

    Closes the previous remote-mode stub in mcp_server — memory_store now
    actually persists. Always scoped to the calling user via user_id; the
    unique constraint (user_id, name, entity_type) upserts entities across
    repeated stores with the same name.
    """
    name = (body.entity_name or "").strip() or "Note"
    etype = (body.entity_type or "concept").strip() or "concept"

    existing = (await db.execute(
        select(KnowledgeEntity).where(
            KnowledgeEntity.user_id == _user.id,
            KnowledgeEntity.name == name,
            KnowledgeEntity.entity_type == etype,
        ).limit(1)
    )).scalar_one_or_none()

    if existing is None:
        entity = KnowledgeEntity(user_id=_user.id, name=name, entity_type=etype)
        db.add(entity)
        await db.flush()
    else:
        entity = existing

    obs = KnowledgeObservation(entity_id=entity.id, content=body.content)
    db.add(obs)
    await db.commit()
    return {
        "status": "stored",
        "entity_id": str(entity.id),
        "entity_name": entity.name,
        "observation_id": str(obs.id),
    }


# ---------------------------------------------------------------------------
# Vector-backed semantic search over DocumentEmbedding
# ---------------------------------------------------------------------------
@router.get("/semantic")
async def semantic_search(
    q: str = Query(..., min_length=1, max_length=1000),
    limit: int = Query(5, ge=1, le=20),
    tool_filter: str | None = None,
    days: int | None = Query(None, ge=1, le=3650),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Semantic search over document chunks via BGE-M3 embeddings.

    Embeds the query against the host-side embedding server, ranks
    DocumentEmbedding rows by pgvector cosine distance, deduplicates by
    document keeping the best-scoring chunk's text as snippet. Returns empty
    list if the embedding server is unavailable — caller should fall back to
    substring search.
    """
    from ..services.embedding_service import _call_embedding_server  # noqa: F401

    mids = await user_machine_ids(db, _user)

    # 30s timeout: the embedding server is CPU-only on Apple Silicon
    # (MPS deliberately avoided — see embedding_server.py:33-43 for the
    # macOS kernel deadlock). A cold-cached query + 4kB Chinese tokenize
    # can easily push 5-12s on M-series CPU. 8s was tripping false
    # "embedding-server-unavailable" returns on a perfectly healthy
    # server, silently degrading semantic search to a trigram fallback.
    # The MCP client's own timeout is well above this.
    embeds = await _call_embedding_server([q], timeout=30.0)
    if not embeds or not embeds[0]:
        return {"results": [], "note": "embedding-server-unavailable"}

    qvec = embeds[0]
    dist_col = DocumentEmbedding.embedding.cosine_distance(qvec).label("dist")

    ranked_chunks_q = (
        select(
            DocumentEmbedding.chunk_text.label("chunk_text"),
            Document.id.label("document_id"),
            Document.tool_id.label("tool_id"),
            Document.title.label("title"),
            Document.relative_path.label("relative_path"),
            Document.category.label("category"),
            Document.synced_at.label("synced_at"),
            Document.source_modified_at.label("source_modified_at"),
            Document.metadata_.label("metadata"),
            Document.file_size_bytes.label("file_size_bytes"),
            dist_col,
            func.row_number().over(
                partition_by=Document.id,
                order_by=(dist_col.asc(), DocumentEmbedding.id),
            ).label("chunk_rank"),
        )
        .join(Document, DocumentEmbedding.document_id == Document.id)
        .where(Document.embedding_status == "ok")
    )
    if tool_filter:
        ranked_chunks_q = ranked_chunks_q.where(Document.tool_id == tool_filter)
    if days:
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        ranked_chunks_q = ranked_chunks_q.where(Document.synced_at >= cutoff)
    if mids is not None:
        ranked_chunks_q = ranked_chunks_q.where(Document.machine_id.in_(mids))

    ranked_chunks = ranked_chunks_q.subquery()
    stmt = (
        select(
            ranked_chunks.c.chunk_text,
            ranked_chunks.c.document_id,
            ranked_chunks.c.tool_id,
            ranked_chunks.c.title,
            ranked_chunks.c.relative_path,
            ranked_chunks.c.category,
            ranked_chunks.c.synced_at,
            ranked_chunks.c.source_modified_at,
            ranked_chunks.c.metadata,
            ranked_chunks.c.file_size_bytes,
            ranked_chunks.c.dist,
        )
        .where(ranked_chunks.c.chunk_rank == 1)
        .order_by(ranked_chunks.c.dist.asc(), ranked_chunks.c.document_id)
    )

    # Rank one best chunk per document in SQL, then page document candidates
    # until enough *logical* groups exist. A root with hundreds of subagents no
    # longer consumes 50 chunks per child or defeats a fixed overfetch window.
    batch_size = max(100, limit * 10)
    rows: list = []
    logical_groups: set[tuple[str, str]] = set()
    scanned = 0
    while len(logical_groups) < limit:
        batch = (
            await db.execute(
                stmt.offset(scanned).limit(batch_size)
            )
        ).all()
        if not batch:
            break
        rows.extend(batch)
        scanned += len(batch)
        for row in batch:
            _chunk, did, tid, _title, _path, category, _synced, \
                _source_modified, metadata, _file_size, _dist = row
            values = metadata or {}
            if category == "conversation" and tid == "codex":
                if (
                    str(values.get("thread_source") or "").strip().lower()
                    == "subagent"
                    and values.get("root_session_id")
                ):
                    logical_id = str(values["root_session_id"])
                else:
                    logical_id = current_thread_id(values) or str(did)
                logical_groups.add(("codex", logical_id))
            else:
                logical_groups.add(("document", str(did)))
        if len(batch) < batch_size:
            break

    best_by_document: dict = {}
    ranked_documents: list = []
    for (
        chunk, did, tid, title, rpath, cat, synced, source_modified,
        metadata, file_size, dist,
    ) in rows:
        if did in best_by_document:
            continue
        item = {
            "id": str(did),
            "_document_id": did,
            "tool_id": tid,
            "title": title or (rpath.split("/")[-1] if rpath else ""),
            "relative_path": rpath,
            "category": cat,
            "snippet": (chunk or "")[:400],
            "synced_at": synced.isoformat() if synced else None,
            "score": round(1.0 - float(dist), 4),
            "_metadata": metadata,
            "_source_modified_at": source_modified,
            "_synced_at_value": synced,
            "_file_size_bytes": file_size,
        }
        best_by_document[did] = item
        ranked_documents.append(item)

    # Pull lightweight root/sibling metadata for every Codex group represented
    # by the ranked candidates. This lets a matching child navigate through its
    # canonical root even when the root's own embedding scored outside the
    # vector window.
    root_thread_ids = {
        group_id
        for group_kind, group_id in logical_groups
        if group_kind == "codex"
    }
    companion_cards: dict = {}
    if root_thread_ids:
        root_ids = list(root_thread_ids)
        companions_q = select(
            Document.id,
            Document.tool_id,
            Document.title,
            Document.relative_path,
            Document.category,
            Document.synced_at,
            Document.source_modified_at,
            Document.metadata_,
            Document.file_size_bytes,
        ).where(
            Document.tool_id == "codex",
            Document.category == "conversation",
            or_(
                Document.metadata_["session_id"].astext.in_(root_ids),
                Document.metadata_["thread_id"].astext.in_(root_ids),
                Document.metadata_["root_session_id"].astext.in_(root_ids),
            ),
        )
        if mids is not None:
            companions_q = companions_q.where(Document.machine_id.in_(mids))
        companion_rows = (await db.execute(companions_q)).all()
        for (
            did, tid, title, rpath, category, synced, source_modified,
            metadata, file_size,
        ) in companion_rows:
            companion_cards[did] = {
                "id": str(did),
                "_document_id": did,
                "tool_id": tid,
                "title": title or (rpath.split("/")[-1] if rpath else ""),
                "relative_path": rpath,
                "category": category,
                "synced_at": synced.isoformat() if synced else None,
                "_metadata": metadata,
                "_source_modified_at": source_modified,
                "_synced_at_value": synced,
                "_file_size_bytes": file_size,
            }

    hierarchy_cards = {
        item["_document_id"]: item
        for item in ranked_documents
        if item["category"] == "conversation"
    }
    hierarchy_cards.update(companion_cards)
    conversation_refs = [
        ConversationRef(
            document_id=item["_document_id"],
            tool_id=item["tool_id"],
            relative_path=item["relative_path"],
            metadata=item["_metadata"],
            title=item["title"],
            source_modified_at=item["_source_modified_at"],
            synced_at=item["_synced_at_value"],
            file_size_bytes=item["_file_size_bytes"],
        )
        for item in hierarchy_cards.values()
    ]
    hierarchy = fold_codex_subagents(conversation_refs)
    subagents_by_document = build_subagent_summaries(
        hierarchy,
        conversation_refs,
    )

    results: list[dict] = []
    emitted: set = set()
    for match in ranked_documents:
        document_id = match["_document_id"]
        canonical_id = hierarchy.canonical_document_ids.get(
            document_id,
            document_id,
        )
        if canonical_id in emitted:
            continue
        canonical = (
            best_by_document.get(canonical_id)
            or companion_cards.get(canonical_id)
            or match
        )
        result = {
            key: value
            for key, value in canonical.items()
            if not key.startswith("_")
        }
        # Preserve the best-scoring matching chunk in the folded group, even
        # when its navigable card is the canonical root document.
        result["snippet"] = match["snippet"]
        result["score"] = match["score"]
        match_metadata = match.get("_metadata") or {}
        is_subagent_match = (
            str(match_metadata.get("thread_source") or "").strip().lower()
            == "subagent"
        )
        result["matched_subagent_id"] = (
            str(document_id)
            if is_subagent_match and document_id != canonical_id
            else None
        )
        result["subagent_count"] = hierarchy.subagent_counts.get(canonical_id, 0)
        result["is_subagent_orphan"] = (
            canonical_id in hierarchy.orphan_document_ids
        )
        result["subagents"] = subagents_by_document.get(canonical_id, [])
        results.append(result)
        emitted.add(canonical_id)
        if len(results) >= limit:
            break

    return {"results": results}


# ---------------------------------------------------------------------------
# Vacuum — drop entities that ended up with zero observations
# ---------------------------------------------------------------------------
@router.post("/vacuum")
async def vacuum_memory(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    """Remove 'zombie' knowledge entities that no longer have any observations.

    Why this exists: `_purge_device_data` already drops orphan entities inline
    when it deletes a device, but that cleanup was added after the product
    shipped — older installations still carry zero-observation entities from
    pre-cleanup device deletes. This endpoint is a one-shot/on-demand sweep
    so admin can nuke them without shelling into psql.

    Scope: non-admin hits only their own entities (user_id = _user.id).
    admin/owner cleans globally.
    """
    from sqlalchemy import delete

    admin = _is_admin(_user)

    orphan_q = select(KnowledgeEntity.id).where(
        ~KnowledgeEntity.id.in_(
            select(KnowledgeObservation.entity_id).where(
                KnowledgeObservation.entity_id.isnot(None)
            )
        )
    )
    if not admin:
        orphan_q = orphan_q.where(KnowledgeEntity.user_id == _user.id)

    orphan_ids = [r[0] for r in (await db.execute(orphan_q)).all()]
    rels_deleted = 0
    ents_deleted = 0
    if orphan_ids:
        r1 = await db.execute(
            delete(KnowledgeRelation).where(
                KnowledgeRelation.source_id.in_(orphan_ids)
                | KnowledgeRelation.target_id.in_(orphan_ids)
            )
        )
        rels_deleted = r1.rowcount or 0
        r2 = await db.execute(
            delete(KnowledgeEntity).where(KnowledgeEntity.id.in_(orphan_ids))
        )
        ents_deleted = r2.rowcount or 0

    await db.commit()
    return {
        "status": "vacuumed",
        "scope": "all" if admin else "self",
        "entities_deleted": ents_deleted,
        "relations_deleted": rels_deleted,
    }
