from __future__ import annotations

import inspect

from server.api import conversations, memory, search
from server.db.session import get_search_db


def _dependency(function, parameter: str = "db"):
    return inspect.signature(function).parameters[parameter].default.dependency


def test_corpus_search_routes_use_search_session() -> None:
    assert _dependency(search.search) is get_search_db
    assert _dependency(search.search_messages) is get_search_db


def test_semantic_and_conversation_search_routes_use_search_session() -> None:
    assert _dependency(memory.search_memory) is get_search_db
    assert _dependency(memory.semantic_search) is get_search_db
    assert _dependency(conversations.search_conversation_messages) is get_search_db
