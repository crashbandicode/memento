import json

from server.services.conversation_parser import (
    AssistantIdentityState,
    iter_conversation_messages,
)
from server.services.conversation_usage import (
    add_token_usage,
    claude_message_token_usage,
    codex_total_token_usage,
)
from server.scripts.backfill_conversation_token_usage import (
    extract_token_usage_from_lines,
)


def test_codex_cumulative_snapshot_preserves_provider_breakdown() -> None:
    usage = codex_total_token_usage(
        {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": 31_051,
                    "cached_input_tokens": 19_712,
                    "output_tokens": 421,
                    "reasoning_output_tokens": 247,
                    "total_tokens": 31_472,
                }
            },
        }
    )

    assert usage == {
        "input_tokens": 31_051,
        "cached_input_tokens": 19_712,
        "output_tokens": 421,
        "reasoning_output_tokens": 247,
        "total_tokens": 31_472,
        "source": "codex",
    }


def test_claude_usage_combines_all_prompt_token_classes() -> None:
    usage = claude_message_token_usage(
        {
            "usage": {
                "input_tokens": 100,
                "cache_read_input_tokens": 2_000,
                "cache_creation_input_tokens": 300,
                "output_tokens": 50,
                "output_tokens_details": {"thinking_tokens": 20},
            }
        }
    )

    assert usage == {
        "input_tokens": 2_400,
        "uncached_input_tokens": 100,
        "cached_input_tokens": 2_000,
        "cache_write_input_tokens": 300,
        "output_tokens": 50,
        "reasoning_output_tokens": 20,
        "total_tokens": 2_450,
        "source": "claude",
    }


def test_claude_parser_counts_each_api_message_id_once() -> None:
    assistant = {
        "type": "assistant",
        "uuid": "transcript-row-1",
        "message": {
            "id": "api-message-1",
            "role": "assistant",
            "model": "claude-opus-4-1",
            "content": [{"type": "text", "text": "Done"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    }
    identity = AssistantIdentityState()

    list(
        iter_conversation_messages(
            "\n".join((json.dumps(assistant), json.dumps(assistant))),
            "claude_code",
            assistant_identity=identity,
        )
    )

    assert identity.token_usage == {
        "input_tokens": 10,
        "uncached_input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "source": "claude",
    }


def test_codex_latest_snapshot_replaces_older_cumulative_total() -> None:
    identity = AssistantIdentityState()
    rows = [
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 10,
                        "output_tokens": 2,
                    }
                },
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 30,
                        "output_tokens": 7,
                    }
                },
            },
        },
    ]

    assert list(
        iter_conversation_messages(
            "\n".join(json.dumps(row) for row in rows),
            "codex",
            assistant_identity=identity,
        )
    ) == []
    assert identity.token_usage == {
        "input_tokens": 30,
        "output_tokens": 7,
        "total_tokens": 37,
        "source": "codex",
    }


def test_add_token_usage_keeps_claude_delta_totals_additive() -> None:
    assert add_token_usage(
        {"input_tokens": 20, "output_tokens": 2, "source": "claude"},
        {"input_tokens": 10, "output_tokens": 3, "source": "claude"},
    ) == {
        "input_tokens": 30,
        "output_tokens": 5,
        "total_tokens": 35,
        "source": "claude",
    }


def test_backfill_uses_the_same_cross_provider_rules_as_live_ingest() -> None:
    rows = [
        json.dumps(
            {
                "type": "assistant",
                "uuid": "row-1",
                "message": {
                    "id": "message-1",
                    "usage": {"input_tokens": 8, "output_tokens": 2},
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "uuid": "row-2",
                "message": {
                    "id": "message-1",
                    "usage": {"input_tokens": 8, "output_tokens": 2},
                },
            }
        ),
    ]

    assert extract_token_usage_from_lines(rows, "claude_code") == {
        "input_tokens": 8,
        "uncached_input_tokens": 8,
        "output_tokens": 2,
        "total_tokens": 10,
        "source": "claude",
    }
