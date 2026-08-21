import json

from server.services.ingest_service import _remove_replaced_usage
from server.services.conversation_parser import (
    AssistantIdentityState,
    AssistantUsageObservation,
    iter_conversation_messages,
)
from server.services.conversation_usage import (
    add_token_usage,
    claude_message_token_usage,
    codex_total_token_usage,
    usage_observation_values,
)
from server.scripts.backfill_conversation_token_usage import (
    extract_assistant_identity_from_lines,
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
        "cache_read_tokens": 19_712,
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
        "input_uncached": 100,
        "cache_read_tokens": 2_000,
        "cache_write_tokens": 300,
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
        "input_uncached": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "source": "claude",
    }


def test_claude_delta_replacement_removes_the_prior_event_value() -> None:
    identity = AssistantIdentityState(
        token_usage={
            "input_tokens": 30,
            "uncached_input_tokens": 30,
            "output_tokens": 7,
            "total_tokens": 37,
            "source": "claude",
        }
    )

    _remove_replaced_usage(
        identity,
        [
            {
                "input_tokens": 10,
                "uncached_input_tokens": 10,
                "output_tokens": 2,
                "total_tokens": 12,
                "source": "claude",
            }
        ],
    )

    assert identity.token_usage == {
        "input_tokens": 20,
        "uncached_input_tokens": 20,
        "input_uncached": 20,
        "output_tokens": 5,
        "total_tokens": 25,
        "source": "claude",
    }


def test_usage_observation_rejects_timezone_ambiguous_timestamp() -> None:
    values = usage_observation_values(
        AssistantUsageObservation(
            source_id="codex:event-1",
            timestamp="2026-08-21T12:34:56",
            source="codex",
            model="gpt-5.6-sol",
            reasoning_effort="xhigh",
            service_tier="priority",
            attribution_status="attributed",
            token_usage={"input_tokens": 10, "output_tokens": 2},
        )
    )

    assert values["occurred_at"] is None
    assert values["attribution_status"] == "missing_timestamp"


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
        "input_uncached": 8,
        "output_tokens": 2,
        "total_tokens": 10,
        "source": "claude",
    }
    identity = extract_assistant_identity_from_lines(rows, "claude_code")
    assert len(identity.usage_observations) == 1
    assert identity.usage_observations[0].source_id == "claude_code:message-1"


def test_codex_observations_are_deltas_at_exact_model_and_effort() -> None:
    identity = AssistantIdentityState()
    rows = [
        {
            "timestamp": "2026-08-01T12:00:00Z",
            "type": "turn_context",
            "payload": {"model": "gpt-first", "effort": "medium"},
        },
        {
            "timestamp": "2026-08-01T12:01:00Z",
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
            "timestamp": "2026-08-01T12:02:00Z",
            "type": "turn_context",
            "payload": {"model": "gpt-second", "effort": "xhigh"},
        },
        {
            "timestamp": "2026-08-01T12:03:00Z",
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

    list(
        iter_conversation_messages(
            "\n".join(json.dumps(row) for row in rows),
            "codex",
            assistant_identity=identity,
        )
    )

    assert [item.model for item in identity.usage_observations] == [
        "gpt-first",
        "gpt-second",
    ]
    assert [item.reasoning_effort for item in identity.usage_observations] == [
        "medium",
        "xhigh",
    ]
    assert [item.token_usage["total_tokens"] for item in identity.usage_observations] == [
        12,
        25,
    ]
    assert all(
        item.attribution_status == "attributed"
        for item in identity.usage_observations
    )
    assert identity.started_at == "2026-08-01T12:00:00+00:00"
    assert identity.last_activity_at == "2026-08-01T12:03:00+00:00"


def test_claude_observation_exposes_exact_cache_split() -> None:
    identity = AssistantIdentityState()
    row = {
        "type": "assistant",
        "uuid": "row-1",
        "timestamp": "2026-08-01T12:00:00Z",
        "message": {
            "id": "message-1",
            "model": "claude-opus-4-1",
            "usage": {
                "input_tokens": 100,
                "cache_read_input_tokens": 200,
                "cache_creation_input_tokens": 300,
                "output_tokens": 40,
            },
        },
    }

    list(
        iter_conversation_messages(
            json.dumps(row),
            "claude_code",
            assistant_identity=identity,
        )
    )

    assert len(identity.usage_observations) == 1
    observation = identity.usage_observations[0]
    assert observation.model == "claude-opus-4-1"
    assert observation.token_usage["input_uncached"] == 100
    assert observation.token_usage["cache_read_tokens"] == 200
    assert observation.token_usage["cache_write_tokens"] == 300
