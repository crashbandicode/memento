from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.message_search import (  # noqa: E402
    MessageSearchCursor,
    build_message_search_expressions,
    decode_search_cursor,
    encode_search_cursor,
    extract_search_terms,
    make_search_snippet,
    normalize_search_query,
    suggest_corrected_query,
)


class MessageSearchTests(unittest.TestCase):
    def compile_expression(self, expression):
        compiled = expression.compile(dialect=postgresql.dialect())
        return str(compiled), compiled.params

    def test_query_expression_keeps_partial_index_roles_and_index_operators(self) -> None:
        expressions = build_message_search_expressions("stale clean lokup")
        sql, params = self.compile_expression(expressions.predicate)

        self.assertIn("conversation_messages.role IN", sql)
        self.assertIn("to_tsvector(", sql)
        self.assertIn("conversation_messages.content", sql)
        self.assertIn("websearch_to_tsquery(", sql)
        self.assertNotIn("conversation_messages.content %%>", sql)
        self.assertIn("ILIKE", sql)
        self.assertIn("stale clean lokup", params.values())
        self.assertIn(["user", "assistant"], params.values())

    def test_body_fuzzy_operator_requires_an_explicit_opt_in(self) -> None:
        expressions = build_message_search_expressions(
            "stale clean lokup",
            include_body_fuzzy=True,
        )
        sql, _params = self.compile_expression(expressions.predicate)
        self.assertIn("conversation_messages.content %%>", sql)

    def test_short_global_query_avoids_unselective_trigram_branches(self) -> None:
        expressions = build_message_search_expressions("AI")
        sql, params = self.compile_expression(expressions.predicate)

        self.assertIn("websearch_to_tsquery(", sql)
        self.assertIn("AI", params.values())
        self.assertNotIn(" %> ", sql)
        self.assertNotIn("ILIKE", sql)

    def test_thread_search_allows_short_exact_substrings(self) -> None:
        expressions = build_message_search_expressions(
            "AI",
            allow_short_substring=True,
        )
        sql, _params = self.compile_expression(expressions.predicate)
        self.assertIn("ILIKE", sql)

    def test_literal_wildcards_are_escaped(self) -> None:
        expressions = build_message_search_expressions("95%_done")
        _sql, params = self.compile_expression(expressions.predicate)
        self.assertIn(r"%95\%\_done%", params.values())

    def test_cursor_round_trip_and_rejects_invalid_data(self) -> None:
        expected = MessageSearchCursor(
            score=4.125,
            timestamp=datetime(2026, 7, 14, 12, 30, tzinfo=timezone.utc),
            message_id=42,
            seen_document_ids=("root", "child"),
        )
        self.assertEqual(decode_search_cursor(encode_search_cursor(expected)), expected)
        with self.assertRaises(HTTPException):
            decode_search_cursor("not-a-valid-cursor")

    def test_snippet_centers_exact_hit_and_stays_bounded(self) -> None:
        content = "before " * 100 + "needle phrase" + " after" * 100
        snippet = make_search_snippet(content, "needle phrase")
        self.assertIn("needle phrase", snippet)
        self.assertLessEqual(len(snippet), 422)
        self.assertTrue(snippet.startswith("…"))
        self.assertTrue(snippet.endswith("…"))

    def test_query_normalization_is_bounded(self) -> None:
        self.assertEqual(normalize_search_query("  stale   lookup  "), "stale lookup")
        self.assertEqual(len(normalize_search_query("x" * 700)), 500)

    def test_lexicon_terms_are_normalized_bounded_and_ascii(self) -> None:
        terms = extract_search_terms(
            "Lookup LOOKUP foo_bar isn't x ab café 12345 " + "z" * 80
        )
        self.assertIn("lookup", terms)
        self.assertIn("foo_bar", terms)
        self.assertIn("isn't", terms)
        self.assertNotIn("x", terms)
        self.assertNotIn("ab", terms)
        self.assertNotIn("café", terms)
        self.assertFalse(any(len(term) > 64 for term in terms))


class _LexiconResult:
    def __init__(self, row) -> None:
        self.row = row

    def first(self):
        return self.row


class _LexiconDb:
    def __init__(self, rows) -> None:
        self.rows = list(rows)

    async def execute(self, _statement):
        return _LexiconResult(self.rows.pop(0))


class MessageCorrectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_correction_preserves_known_tokens_and_repairs_typo(self) -> None:
        db = _LexiconDb([
            None,
            None,
            ("lookup", 0.625, 2599, 1),
        ])
        corrected = await suggest_corrected_query(db, "stale clean lokup")
        self.assertEqual(corrected, "stale clean lookup")

    async def test_correction_returns_none_when_every_token_is_known(self) -> None:
        db = _LexiconDb([None, None])
        corrected = await suggest_corrected_query(db, "stale lookup")
        self.assertIsNone(corrected)

    async def test_common_alternative_does_not_override_established_term(self) -> None:
        db = _LexiconDb([("color", 0.6, 1000, 100)])
        corrected = await suggest_corrected_query(db, "colour")
        self.assertIsNone(corrected)


if __name__ == "__main__":
    unittest.main()
