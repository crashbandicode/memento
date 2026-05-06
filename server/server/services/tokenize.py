"""Chinese-aware tokenization for Postgres full-text search.

Postgres's built-in tokenizers don't segment Chinese (no whitespace between
characters), so we pre-tokenize on the Python side with jieba and hand the
database a whitespace-joined token string. The server stores it under the
``simple`` text-search config, which is just a case-folder at that point.

Contract:
- ``tokenize_for_index(text)`` → space-joined tokens to feed
  ``to_tsvector('simple', ...)`` when writing ``documents.content_tsv``.
- ``tokenize_for_query(text)`` → ``"tok1 & tok2 & tok3"`` for
  ``to_tsquery('simple', ...)``. Empty / single-char tokens dropped.

Both functions also lower-case and strip ASCII punctuation so
``"Docker/PVC"`` becomes ``"docker pvc"``.
"""

from __future__ import annotations

import re
from typing import Iterable

_PUNCT_RE = re.compile(r"[\s　]+|[\.,;:!?\-_/\\()\[\]{}\"'`~@#$%^&*+=<>|]+")
# Huge JSONL conversations can push jieba + Postgres tsvector into OOM /
# cripplingly slow index build. 200 KB of prose is ~60 K CJK chars,
# empirically covers the topic of a doc; ingest path still stores the full
# raw content, this cap only affects what we hand the tokenizer.
_MAX_INPUT_CHARS = 200_000
_MAX_TOKENS_INDEX = 50_000  # hard cap per doc to avoid massive tsvectors
_MAX_QUERY_TOKENS = 16


def _segment(text: str) -> Iterable[str]:
    """Yield tokens from ``text`` using jieba.cut_for_search for CJK and a
    punctuation-splitter fallback for pure-ASCII input. jieba is imported
    lazily so the module loads even if the optional dep is missing."""
    if not text:
        return []
    try:
        import jieba  # type: ignore
    except ImportError:
        # Fallback: whitespace / punctuation split. Works for English, loses
        # Chinese word boundaries but still better than nothing.
        return (t for t in _PUNCT_RE.split(text) if t)

    tokens: list[str] = []
    for piece in _PUNCT_RE.split(text):
        if not piece:
            continue
        # jieba.cut_for_search adds short sub-tokens (bigrams inside long
        # words), better for recall than `cut`. For non-CJK strings it
        # returns the original piece, which is fine.
        tokens.extend(jieba.cut_for_search(piece))
    return tokens


def _normalize(tok: str) -> str:
    t = tok.strip().lower()
    # Skip single-char ASCII tokens (too noisy), keep single-char CJK.
    if len(t) == 1 and ord(t) < 128:
        return ""
    return t


def tokenize_for_index(text: str) -> str:
    if text and len(text) > _MAX_INPUT_CHARS:
        text = text[:_MAX_INPUT_CHARS]
    out: list[str] = []
    for tok in _segment(text):
        n = _normalize(tok)
        if n:
            out.append(n)
        if len(out) >= _MAX_TOKENS_INDEX:
            break
    return " ".join(out)


def tokenize_for_query(text: str) -> str:
    seen: list[str] = []
    # dedup while preserving order — repeated terms add no info to the
    # boolean AND, and we cap total to avoid ridiculously long tsqueries.
    seen_set: set[str] = set()
    for tok in _segment(text):
        n = _normalize(tok)
        if not n or n in seen_set:
            continue
        seen.append(n)
        seen_set.add(n)
        if len(seen) >= _MAX_QUERY_TOKENS:
            break
    return " & ".join(seen)
