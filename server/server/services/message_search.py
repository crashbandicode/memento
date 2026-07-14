"""Shared, index-backed search primitives for normalized conversation messages.

The client never downloads a transcript to search it.  Both global search and
the in-conversation navigator use these expressions so ranking, role scope,
and typo tolerance cannot drift between surfaces.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Float, and_, case, cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ConversationMessage, ConversationSearchTerm


SEARCHABLE_MESSAGE_ROLES = ("user", "assistant")
GLOBAL_FUZZY_MIN_CHARS = 3
MAX_SEARCH_QUERY_CHARS = 500
MAX_SEARCH_SNIPPET_CHARS = 420
MAX_SEARCH_CONTENT_CHARS = 4_096
MAX_CURSOR_SEEN_DOCUMENTS = 100
MAX_LEXICON_TERMS_PER_INGEST = 10_000
MAX_CORRECTED_QUERY_TOKENS = 8
MIN_TERM_SIMILARITY = 0.45
MIN_KNOWN_TERM_CORRECTION_SIMILARITY = 0.55
KNOWN_TERM_CORRECTION_FREQUENCY_RATIO = 20
MIN_KNOWN_TERM_ALTERNATIVE_FREQUENCY = 20
_ASCII_TERM_RE = re.compile(r"(?i)\b[a-z][a-z0-9_'-]{2,63}\b")


@dataclass(frozen=True, slots=True)
class MessageSearchExpressions:
    predicate: Any
    score: Any
    match_type: Any


@dataclass(frozen=True, slots=True)
class MessageSearchCursor:
    score: float
    timestamp: datetime
    message_id: int
    seen_document_ids: tuple[str, ...] = ()


def normalize_search_query(value: str) -> str:
    """Trim UI whitespace while retaining punctuation meaningful to search."""
    return " ".join((value or "").strip().split())[:MAX_SEARCH_QUERY_CHARS]


def _escaped_ilike_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def build_message_search_expressions(
    query: str,
    *,
    allow_short_substring: bool = False,
    include_body_fuzzy: bool = False,
) -> MessageSearchExpressions:
    """Return one index-compatible predicate and its deterministic ranking.

    PostgreSQL can BitmapOr the partial FTS and trigram GIN indexes.  Exact
    substring hits win, then lexical full-text hits, then word-similarity
    matches.  The partial-index role predicate is intentionally present in
    every caller through this shared expression.
    """
    normalized = normalize_search_query(query)
    if not normalized:
        raise ValueError("search query is empty")

    role_scope = ConversationMessage.role.in_(SEARCHABLE_MESSAGE_ROLES)
    tsquery = func.websearch_to_tsquery("simple", normalized)
    content_vector = func.to_tsvector("simple", ConversationMessage.content)
    full_text_match = content_vector.op("@@")(tsquery)

    can_use_substring = allow_short_substring or len(normalized) >= GLOBAL_FUZZY_MIN_CHARS
    exact_match = (
        ConversationMessage.content.ilike(
            _escaped_ilike_pattern(normalized),
            escape="\\",
        )
        if can_use_substring
        else literal(False)
    )
    # ``content %> query`` is the commutator of ``query <% content``.  It asks
    # pg_trgm for the best matching continuous extent, which is much more
    # useful than whole-message similarity for a short query in a long reply.
    fuzzy_match = (
        ConversationMessage.content.op("%>")(normalized)
        if include_body_fuzzy and len(normalized) >= GLOBAL_FUZZY_MIN_CHARS
        else literal(False)
    )
    lexical_rank = cast(func.ts_rank_cd(content_vector, tsquery), Float)
    fuzzy_rank = cast(
        func.word_similarity(normalized, ConversationMessage.content),
        Float,
    )
    score = case(
        (exact_match, 4.0 + lexical_rank),
        (full_text_match, 3.0 + lexical_rank),
        (fuzzy_match, 1.0 + fuzzy_rank),
        else_=0.0,
    )
    match_type = case(
        (exact_match, "exact"),
        (full_text_match, "full_text"),
        (fuzzy_match, "fuzzy"),
        else_="none",
    )
    return MessageSearchExpressions(
        predicate=and_(
            role_scope,
            or_(exact_match, full_text_match, fuzzy_match),
        ),
        score=score,
        match_type=match_type,
    )


def extract_search_terms(content: str) -> set[str]:
    """Extract bounded ASCII terms for the typo-correction lexicon."""
    terms: set[str] = set()
    for match in _ASCII_TERM_RE.finditer(content or ""):
        terms.add(match.group(0).lower())
        if len(terms) >= MAX_LEXICON_TERMS_PER_INGEST:
            break
    return terms


async def upsert_search_terms(db: AsyncSession, terms: set[str]) -> None:
    """Make newly ingested vocabulary available to fuzzy search immediately."""
    if not terms:
        return
    ordered = sorted(terms)
    for start in range(0, len(ordered), 2_000):
        rows = [{"term": term, "frequency": 1} for term in ordered[start:start + 2_000]]
        statement = pg_insert(ConversationSearchTerm).values(rows)
        await db.execute(
            statement.on_conflict_do_update(
                index_elements=[ConversationSearchTerm.term],
                set_={
                    "frequency": ConversationSearchTerm.frequency + 1,
                    "updated_at": func.now(),
                },
            )
        )


async def suggest_corrected_query(db: AsyncSession, query: str) -> str | None:
    """Correct likely misspellings against the compact indexed vocabulary.

    PostgreSQL's own pg_trgm guidance recommends fuzzy-matching a unique word
    table and then rerunning full-text search. This avoids rechecking tens of
    thousands of lossy trigram matches against large message bodies.
    """
    normalized = normalize_search_query(query)
    matches = list(_ASCII_TERM_RE.finditer(normalized))
    if not matches or len(matches) > MAX_CORRECTED_QUERY_TOKENS:
        return None

    replacements: dict[str, str] = {}
    for token in dict.fromkeys(match.group(0).lower() for match in matches):
        similarity = func.similarity(ConversationSearchTerm.term, token)
        exact_frequency = (
            select(ConversationSearchTerm.frequency)
            .where(ConversationSearchTerm.term == token)
            .correlate(None)
            .scalar_subquery()
        )
        row = (
            await db.execute(
                select(
                    ConversationSearchTerm.term,
                    similarity.label("similarity"),
                    ConversationSearchTerm.frequency,
                    func.coalesce(exact_frequency, 0).label("exact_frequency"),
                )
                .where(
                    ConversationSearchTerm.term.op("%")(token),
                    ConversationSearchTerm.term != token,
                    func.abs(func.length(ConversationSearchTerm.term) - len(token)) <= 2,
                )
                .order_by(
                    similarity.desc(),
                    ConversationSearchTerm.frequency.desc(),
                    ConversationSearchTerm.term,
                )
                .limit(1)
            )
        ).first()
        if not row:
            continue
        alternative_similarity = float(row[1])
        alternative_frequency = int(row[2])
        known_frequency = int(row[3])
        if known_frequency:
            # A typo can itself enter the live vocabulary from a newly synced
            # message. Keep the original exact hits, but still add a corrected
            # search when a close alternative is overwhelmingly more common.
            should_replace = (
                alternative_similarity >= MIN_KNOWN_TERM_CORRECTION_SIMILARITY
                and alternative_frequency >= MIN_KNOWN_TERM_ALTERNATIVE_FREQUENCY
                and alternative_frequency
                >= known_frequency * KNOWN_TERM_CORRECTION_FREQUENCY_RATIO
            )
        else:
            should_replace = alternative_similarity >= MIN_TERM_SIMILARITY
        if should_replace:
            replacements[token] = row[0]

    if not replacements:
        return None
    parts: list[str] = []
    cursor = 0
    for match in matches:
        parts.append(normalized[cursor:match.start()])
        original = match.group(0)
        replacement = replacements.get(original.lower(), original)
        parts.append(replacement)
        cursor = match.end()
    parts.append(normalized[cursor:])
    corrected = "".join(parts)
    return corrected if corrected != normalized else None


def cursor_after_predicate(
    cursor: MessageSearchCursor,
    score_expression: Any,
    timestamp_expression: Any,
) -> Any:
    """Build keyset pagination for score/timestamp/message-id ordering."""
    return or_(
        score_expression < cursor.score,
        and_(
            score_expression == cursor.score,
            timestamp_expression < cursor.timestamp,
        ),
        and_(
            score_expression == cursor.score,
            timestamp_expression == cursor.timestamp,
            ConversationMessage.id < cursor.message_id,
        ),
    )


def encode_search_cursor(cursor: MessageSearchCursor) -> str:
    payload = {
        "s": cursor.score,
        "t": cursor.timestamp.isoformat(),
        "i": cursor.message_id,
        "d": list(cursor.seen_document_ids[-MAX_CURSOR_SEEN_DOCUMENTS:]),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_search_cursor(value: str | None) -> MessageSearchCursor | None:
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        timestamp = datetime.fromisoformat(str(payload["t"]))
        if timestamp.tzinfo is None:
            raise ValueError("cursor timestamp must be timezone-aware")
        seen = tuple(str(item) for item in payload.get("d", []))
        return MessageSearchCursor(
            score=float(payload["s"]),
            timestamp=timestamp,
            message_id=int(payload["i"]),
            seen_document_ids=seen[-MAX_CURSOR_SEEN_DOCUMENTS:],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid search cursor") from exc


def make_search_snippet(content: str, query: str) -> str:
    """Return a bounded excerpt centered on an exact hit when possible."""
    clean = " ".join((content or "").split())
    if len(clean) <= MAX_SEARCH_SNIPPET_CHARS:
        return clean
    needle = normalize_search_query(query).lower()
    position = clean.lower().find(needle) if needle else -1
    if position < 0:
        return clean[: MAX_SEARCH_SNIPPET_CHARS - 1].rstrip() + "…"
    radius = MAX_SEARCH_SNIPPET_CHARS // 2
    start = max(0, position - radius)
    end = min(len(clean), start + MAX_SEARCH_SNIPPET_CHARS)
    start = max(0, end - MAX_SEARCH_SNIPPET_CHARS)
    excerpt = clean[start:end].strip()
    return ("…" if start else "") + excerpt + ("…" if end < len(clean) else "")
