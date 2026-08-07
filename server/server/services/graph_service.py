"""Knowledge graph auto-extraction — extracts entities and relations from conversations using LLM."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (
    Document,
    KnowledgeEntity,
    KnowledgeObservation,
    KnowledgeRelation,
    Machine,
)

logger = logging.getLogger("graph_service")

_GRAPH_SCHEMA_VERSION = "knowledge-json-v1"
_JSON_ONLY_SUFFIX = "\n\nRespond with JSON only."
_RETRY_BASE_SECONDS = 15 * 60
_RETRY_MAX_SECONDS = 24 * 60 * 60

_EXTRACTION_TEMPLATE = (
    "分析以下 AI 编程对话，提取结构化知识。用中文回复。\n\n"
    "返回 JSON 对象：\n"
    '{"entities": [{"name": "实体名", "type": "project|tool|technology|concept|person|file", "summary": "简要描述"}],\n'
    ' "relations": [{"source": "实体1", "target": "实体2", "type": "uses|creates|depends_on|fixes|discussed"}],\n'
    ' "observations": [{"entity": "实体名", "content": "学到了什么、做了什么决定"}]}\n\n'
    "规则：\n"
    "- 提取具体的、可复用的知识（不要泛泛而谈）\n"
    "- 实体名用标准名称（如 PostgreSQL 而非 postgres 数据库）\n"
    "- summary 和 observations 的 content 用中文\n"
    "- 最多 10 个实体、10 个关系、10 个观察\n"
    "- 重点关注：使用的技术、解决的问题、做出的决定\n\n"
    "对话内容：\n"
)


@dataclass(frozen=True, slots=True)
class _ProviderConfig:
    kind: str
    api_key: str
    model: str
    base_url: str | None = None


class _ProviderFailure(Exception):
    def __init__(
        self,
        kind: str,
        *,
        permanent: bool,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(kind)
        self.kind = kind
        self.permanent = permanent
        self.retry_after_seconds = retry_after_seconds


def _provider_config() -> _ProviderConfig | None:
    api_key = os.environ.get("MEMENTO_AI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if api_key:
        return _ProviderConfig(
            kind="openai_compatible",
            api_key=api_key,
            model=os.environ.get("MEMENTO_AI_MODEL", "kimi-k2.5"),
            base_url=os.environ.get(
                "MEMENTO_AI_BASE_URL",
                "https://coding.dashscope.aliyuncs.com/v1",
            ),
        )
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get(
        "MEMENTO_ANTHROPIC_API_KEY"
    )
    if anthropic_key:
        return _ProviderConfig(
            kind="anthropic",
            api_key=anthropic_key,
            model=os.environ.get(
                "MEMENTO_ANTHROPIC_MODEL",
                "claude-sonnet-4-20250514",
            ),
        )
    return None


def knowledge_provider_configured() -> bool:
    """Return whether graph extraction has a usable LLM credential."""
    return _provider_config() is not None


def _request_message(prompt: str) -> str:
    return prompt + _JSON_ONLY_SUFFIX


def _graph_input_hash(prompt: str, config: _ProviderConfig) -> str:
    """Fingerprint the exact provider input and its interpretation contract."""
    payload = json.dumps(
        {
            "schema_version": _GRAPH_SCHEMA_VERSION,
            "provider": config.kind,
            "model": config.model,
            "base_url": config.base_url,
            "message": _request_message(prompt),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_json_object(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end])
    except json.JSONDecodeError as exc:
        logger.info("LLM returned invalid JSON: %s", str(exc)[:100])
        return None
    return parsed if isinstance(parsed, dict) else None


def _retry_after_seconds(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    value = headers.get("retry-after") if headers else None
    try:
        return max(1, min(int(value), _RETRY_MAX_SECONDS)) if value else None
    except (TypeError, ValueError):
        return None


def _classify_provider_failure(exc: Exception) -> _ProviderFailure:
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code in (401, 403):
        kind = "authentication"
    elif status_code in (400, 404, 405, 410, 422):
        kind = "invalid_request"
    elif status_code == 429:
        kind = "rate_limit"
    elif status_code is not None and status_code >= 500:
        kind = "provider_unavailable"
    else:
        kind = "transport"
    return _ProviderFailure(
        kind,
        permanent=status_code in (400, 401, 403, 404, 405, 410, 422),
        retry_after_seconds=_retry_after_seconds(exc),
    )


async def _call_llm(prompt: str) -> dict | None:
    """Call LLM for entity extraction via OpenAI-compatible API. Returns parsed JSON or None."""
    config = _provider_config()
    if config is None:
        logger.debug("No AI API key set, skipping graph extraction")
        return None
    if config.kind == "anthropic":
        return await _call_anthropic(prompt, config)

    try:
        import openai

        client = openai.AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
        )
        response = await client.chat.completions.create(
            model=config.model,
            messages=[{"role": "user", "content": _request_message(prompt)}],
            max_tokens=2000,
        )
        text = response.choices[0].message.content or "{}"
        return _parse_json_object(text)
    except Exception as exc:
        failure = _classify_provider_failure(exc)
        logger.warning("LLM extraction failed (%s): %s", failure.kind, exc)
        raise failure from exc


async def _call_anthropic(
    prompt: str,
    config: _ProviderConfig,
) -> dict | None:
    """Call Anthropic Claude for entity extraction."""
    try:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=config.api_key)
        response = await client.messages.create(
            model=config.model,
            max_tokens=2000,
            messages=[{"role": "user", "content": _request_message(prompt)}],
        )
        text = response.content[0].text
        return _parse_json_object(text)
    except Exception as exc:
        failure = _classify_provider_failure(exc)
        logger.warning("Anthropic extraction failed (%s): %s", failure.kind, exc)
        raise failure from exc


def _retry_at(
    attempts: int,
    *,
    retry_after_seconds: int | None = None,
) -> datetime:
    delay = retry_after_seconds or min(
        _RETRY_BASE_SECONDS * (2 ** max(0, attempts - 1)),
        _RETRY_MAX_SECONDS,
    )
    return datetime.now(timezone.utc) + timedelta(seconds=delay)


def _mark_failure(
    doc: Document,
    *,
    kind: str,
    permanent: bool,
    retry_after_seconds: int | None = None,
) -> None:
    doc.knowledge_failure_kind = kind
    if permanent:
        doc.knowledge_status = "permanent_failed"
        doc.knowledge_retry_at = None
    else:
        doc.knowledge_status = "failed"
        doc.knowledge_retry_at = _retry_at(
            doc.knowledge_attempts or 1,
            retry_after_seconds=retry_after_seconds,
        )


async def extract_knowledge_from_document(
    db: AsyncSession,
    doc: Document,
    user_id: uuid.UUID | None = None,
) -> int:
    """Extract entities, relations, and observations from a document. Returns count of items created.

    Side-effects on ``doc.knowledge_status`` / ``doc.knowledge_attempts``:
      * 'skipped' — content too short or wrong category; never tried.
      * 'failed'  — transient LLM failure. The durable retry timestamp controls
        when knowledge_retry may claim it again.
      * 'permanent_failed' — request/auth/model errors that retries cannot fix.
      * 'ok'      — extraction completed (zero entities is still 'ok' —
        the LLM saw the doc and decided there's nothing graph-worthy).
    """
    # Do this before any document, message, or graph query. A deployment that
    # intentionally has no LLM credential should not turn every live transcript
    # append into no-op graph bookkeeping and retry traffic.
    if not knowledge_provider_configured():
        return 0

    if doc.category not in ("conversation", "memory", "learning", "plan"):
        doc.knowledge_status = "skipped"
        return 0

    if doc.category != "conversation" and (not doc.content or len(doc.content) < 200):
        doc.knowledge_status = "skipped"
        return 0

    # Content prep — the LLM only sees ~4000 chars, so what those 4000
    # chars ARE matters a lot. For conversation docs the raw .jsonl
    # head is mostly tool_use / tool_result JSON metadata — opaque to
    # the LLM, no entities to be found. That's why the initial sweep
    # of large conversations all got marked 'failed' even on glm-5.2:
    # the model literally never saw the user / assistant text.
    #
    # For conversations, pull role='user'/'assistant' rows from the
    # already-parsed conversation_messages table and concat into a
    # role-tagged transcript. For other categories (memory / plan /
    # learning / identity) the raw content IS the text — keep the
    # original head-of-content path.
    content = ""
    if doc.category == "conversation":
        from ..db.models import ConversationMessage

        rows = (
            await db.execute(
                select(ConversationMessage.role, ConversationMessage.content)
                .where(
                    ConversationMessage.document_id == doc.id,
                    ConversationMessage.role.in_(("user", "assistant")),
                )
                .order_by(ConversationMessage.line_number)
                .limit(200)
            )
        ).all()
        chunks: list[str] = []
        used = 0
        for role, c in rows:
            text = (c or "").strip()
            if not text:
                continue
            # Drop tool-result noise that often pads user-role messages
            if (
                text.startswith("[Result]")
                or text.startswith("[Tool:")
                or text.startswith('{"tool_use_id"')
            ):
                continue
            chunk = f"[{role}] {text}\n"
            if used + len(chunk) > 4000:
                # Truncate the last chunk to fit, then stop.
                chunks.append(chunk[: 4000 - used])
                break
            chunks.append(chunk)
            used += len(chunk)
        content = "".join(chunks)
        # Conversation has no parsed messages yet (very fresh ingest /
        # parse miss) — fall back to raw head so we still try.
        if not content:
            content = (doc.content or "")[:4000]
    else:
        content = (doc.content or "")[:4000]

    if len(content) < 200:
        doc.knowledge_status = "skipped"
        return 0

    prompt = _EXTRACTION_TEMPLATE + content
    config = _provider_config()
    if config is None:
        return 0
    input_hash = _graph_input_hash(prompt, config)
    meta = dict(doc.metadata_ or {})
    if meta.get("_graph_hash") == input_hash:
        # The hash itself is the completion marker. Requiring an observation
        # here made valid zero-entity results run forever.
        doc.knowledge_status = "ok"
        doc.knowledge_retry_at = None
        doc.knowledge_failure_kind = None
        return 0

    # Attempts are scoped to the exact request. A schema/model/prompt change is
    # a new input and gets a fresh retry budget.
    if meta.get("_graph_attempt_hash") != input_hash:
        doc.knowledge_attempts = 0
    meta["_graph_attempt_hash"] = input_hash
    doc.metadata_ = meta

    # Charge an attempt up front so a hung LLM still counts toward the
    # cap (otherwise a stuck call would never block subsequent retries). Commit
    # the read/preparation transaction before the network call so Postgres does
    # not kill an idle-in-transaction connection while the model is working.
    doc.knowledge_attempts = (doc.knowledge_attempts or 0) + 1
    _mark_failure(doc, kind="interrupted", permanent=False)
    await db.commit()
    try:
        result = await _call_llm(prompt)
    except _ProviderFailure as failure:
        _mark_failure(
            doc,
            kind=failure.kind,
            permanent=failure.permanent,
            retry_after_seconds=failure.retry_after_seconds,
        )
        return 0
    if result is None:
        _mark_failure(doc, kind="invalid_response", permanent=False)
        return 0

    # Keep the last good graph across transient LLM failures. Replace it only
    # after a valid extraction result is available in this same transaction.
    from sqlalchemy import delete

    await db.execute(
        delete(KnowledgeObservation).where(
            KnowledgeObservation.source_document_id == doc.id
        )
    )

    count = 0

    # Get or determine user_id from document's machine
    if not user_id and doc.machine_id:
        machine = (
            await db.execute(
                select(Machine.user_id).where(Machine.id == doc.machine_id)
            )
        ).scalar_one_or_none()
        user_id = machine

    # Process entities
    entity_map: dict[str, KnowledgeEntity] = {}
    for e in result.get("entities", []):
        name = e.get("name", "").strip()
        etype = e.get("type", "concept").strip()
        if not name:
            continue

        # Upsert entity — scoped to the document's owner so user B's ingest
        # can't attach observations to user A's entity just because the LLM
        # pulled out the same name. Schema already has
        # UniqueConstraint(user_id, name, entity_type); this query was missing
        # the user_id predicate, silently cross-pollinating knowledge graphs.
        existing = (
            await db.execute(
                select(KnowledgeEntity)
                .where(
                    KnowledgeEntity.name == name,
                    KnowledgeEntity.entity_type == etype,
                    KnowledgeEntity.user_id == user_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()

        if existing:
            if e.get("summary") and (
                not existing.summary or len(e["summary"]) > len(existing.summary)
            ):
                existing.summary = e["summary"]
            entity_map[name] = existing
        else:
            entity = KnowledgeEntity(
                user_id=user_id,
                name=name,
                entity_type=etype,
                summary=e.get("summary"),
            )
            db.add(entity)
            await db.flush()
            entity_map[name] = entity
            count += 1

    # Process relations
    for r in result.get("relations", []):
        source = entity_map.get(r.get("source", ""))
        target = entity_map.get(r.get("target", ""))
        if source and target and source.id != target.id:
            # Check if relation exists
            existing_rel = (
                await db.execute(
                    select(KnowledgeRelation)
                    .where(
                        KnowledgeRelation.source_id == source.id,
                        KnowledgeRelation.target_id == target.id,
                        KnowledgeRelation.relation_type == r.get("type", "related"),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()

            if existing_rel:
                existing_rel.strength = (existing_rel.strength or 1.0) + 1.0
            else:
                db.add(
                    KnowledgeRelation(
                        source_id=source.id,
                        target_id=target.id,
                        relation_type=r.get("type", "related"),
                    )
                )
                count += 1

    # Process observations
    for o in result.get("observations", []):
        entity_name = o.get("entity", "")
        entity = entity_map.get(entity_name)
        content_text = o.get("content", "").strip()
        if entity and content_text:
            db.add(
                KnowledgeObservation(
                    entity_id=entity.id,
                    content=content_text,
                    source_document_id=doc.id,
                )
            )
            count += 1

    # Mark this content version as extracted
    meta = dict(doc.metadata_ or {})
    meta["_graph_hash"] = input_hash
    meta["_graph_attempt_hash"] = input_hash
    doc.metadata_ = meta
    doc.knowledge_status = "ok"
    doc.knowledge_retry_at = None
    doc.knowledge_failure_kind = None

    await db.flush()
    logger.info(
        "Extracted %d knowledge items from %s/%s", count, doc.tool_id, doc.relative_path
    )
    return count
