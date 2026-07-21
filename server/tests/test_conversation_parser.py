from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.conversation_parser import (  # noqa: E402
    AssistantIdentityState,
    count_conversation_messages,
    extract_codex_session_metadata,
    iter_conversation_messages,
    normalize_codex_user_payload,
    normalize_cursor_user_payload,
    parse_conversation,
    parse_conversation_line,
    pop_matching_claude_queue_user,
    strip_terminal_sequences,
)


class ConversationParserTests(unittest.TestCase):
    def test_codex_assistant_identity_tracks_turn_context_switches(self) -> None:
        raw = "\n".join([
            json.dumps({
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-sol", "effort": "xhigh"},
            }),
            json.dumps({
                "type": "response_item",
                "timestamp": "2026-07-17T20:00:00Z",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "id": "assistant-1",
                    "content": [{"type": "output_text", "text": "First"}],
                },
            }),
            json.dumps({
                "type": "turn_context",
                "payload": {"model": "gpt-5.6", "effort": "medium"},
            }),
            json.dumps({
                "type": "response_item",
                "timestamp": "2026-07-17T20:01:00Z",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "id": "assistant-2",
                    "content": [{"type": "output_text", "text": "Second"}],
                },
            }),
        ])

        messages = parse_conversation(raw, "codex")

        self.assertEqual(
            [(message.model, message.reasoning_effort) for message in messages],
            [("gpt-5.6-sol", "xhigh"), ("gpt-5.6", "medium")],
        )

    def test_codex_assistant_identity_survives_delta_boundary(self) -> None:
        identity = AssistantIdentityState()
        context_delta = json.dumps({
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "effort": "high"},
        })
        assistant_delta = json.dumps({
            "type": "event_msg",
            "timestamp": "2026-07-17T20:02:00Z",
            "payload": {"type": "agent_message", "message": "Continued"},
        })

        self.assertEqual(
            list(iter_conversation_messages(
                context_delta,
                "codex",
                assistant_identity=identity,
            )),
            [],
        )
        messages = list(iter_conversation_messages(
            assistant_delta,
            "codex",
            assistant_identity=identity,
        ))

        self.assertEqual(identity.model, "gpt-5.6-sol")
        self.assertEqual(messages[0].model, "gpt-5.6-sol")
        self.assertEqual(messages[0].reasoning_effort, "high")

    def test_codex_thread_settings_preserve_fast_service_tier(self) -> None:
        raw = "\n".join([
            json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "thread_settings_applied",
                    "thread_settings": {
                        "model": "gpt-5.6-sol",
                        "reasoning_effort": "ultra",
                        "service_tier": "priority",
                    },
                },
            }),
            json.dumps({
                "type": "event_msg",
                "timestamp": "2026-07-18T14:00:00Z",
                "payload": {
                    "type": "agent_message",
                    "message": "Fast response",
                },
            }),
        ])

        messages = parse_conversation(raw, "codex")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].model, "gpt-5.6-sol")
        self.assertEqual(messages[0].reasoning_effort, "ultra")
        self.assertEqual(messages[0].service_tier, "priority")

    def test_codex_fast_service_tier_survives_delta_boundary(self) -> None:
        identity = AssistantIdentityState()
        settings_delta = json.dumps({
            "type": "event_msg",
            "payload": {
                "type": "thread_settings",
                "thread_settings": {
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "ultra",
                    "service_tier": "fast",
                },
            },
        })
        assistant_delta = json.dumps({
            "type": "event_msg",
            "timestamp": "2026-07-18T14:01:00Z",
            "payload": {"type": "agent_message", "message": "Continued"},
        })

        self.assertEqual(
            list(iter_conversation_messages(
                settings_delta,
                "codex",
                assistant_identity=identity,
            )),
            [],
        )
        messages = list(iter_conversation_messages(
            assistant_delta,
            "codex",
            assistant_identity=identity,
        ))

        self.assertEqual(identity.service_tier, "fast")
        self.assertEqual(messages[0].service_tier, "fast")

    def test_codex_reasoning_summary_keeps_latest_visible_snapshot(self) -> None:
        raw = "\n".join([
            json.dumps({
                "type": "response_item",
                "timestamp": "2026-07-18T23:26:48Z",
                "payload": {
                    "type": "reasoning",
                    "id": "reasoning-1",
                    "summary": [{
                        "type": "summary_text",
                        "text": "Planning the probe",
                    }],
                    "encrypted_content": "must-never-render",
                },
            }),
            json.dumps({
                "type": "response_item",
                "timestamp": "2026-07-18T23:26:59Z",
                "payload": {
                    "type": "reasoning",
                    "id": "reasoning-1",
                    "summary": [
                        {"type": "summary_text", "text": "Planning the probe"},
                        {"type": "summary_text", "text": "Verifying the result"},
                    ],
                    "encrypted_content": "must-never-render",
                },
            }),
            json.dumps({
                "type": "event_msg",
                "timestamp": "2026-07-18T23:27:00Z",
                "payload": {"type": "turn_aborted", "turn_id": "turn-1"},
            }),
        ])

        messages = parse_conversation(raw, "codex")

        self.assertEqual([message.raw_type for message in messages], ["reasoning", "turn_aborted"])
        self.assertEqual(
            messages[0].thinking,
            "Planning the probe\n\nVerifying the result",
        )
        self.assertNotIn("must-never-render", messages[0].thinking)

    def test_codex_subagent_activity_is_a_safe_semantic_event(self) -> None:
        raw = json.dumps({
            "type": "event_msg",
            "timestamp": "2026-07-18T23:14:53Z",
            "payload": {
                "type": "sub_agent_activity",
                "event_id": "agent-event-1",
                "agent_thread_id": "agent-thread-1",
                "agent_path": "/root/attr_slo_bounded_archive_fix",
                "kind": "interacted",
                "encrypted_content": "must-never-render",
            },
        })

        messages = parse_conversation(raw, "codex")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].raw_type, "agent_event")
        self.assertEqual(messages[0].agent_event["kind"], "updated")
        self.assertEqual(
            messages[0].agent_event["label"],
            "Attr SLO Bounded Archive Fix",
        )
        self.assertNotIn("must-never-render", messages[0].content)

    def test_cursor_task_v2_becomes_shared_agent_lifecycle_event(self) -> None:
        raw = json.dumps({
            "type": "cursor_state_tool",
            "role": "tool",
            "id": "task-bubble:tool",
            "timestamp": "2026-07-21T12:00:00Z",
            "tool_name": "task_v2",
            "tool_status": "completed",
            "tool_input": json.dumps({
                "description": "RNO API Mongo diagnosis",
                "prompt": "Investigate the site roster",
                "subagentType": "explore",
            }),
            "content": json.dumps({
                "agentId": "94d64099-e015-4fdb-848a-efaf7acc1695",
            }),
        })

        messages = parse_conversation(raw, "cursor")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].raw_type, "agent_event")
        self.assertEqual(messages[0].tool_name, "Agent activity")
        self.assertEqual(
            messages[0].agent_event,
            {
                "version": 1,
                "agent_path": "/root/rno_api_mongo_diagnosis",
                "agent_thread_id": "94d64099-e015-4fdb-848a-efaf7acc1695",
                "label": "RNO API Mongo diagnosis",
                "kind": "completed",
            },
        )

    def test_cursor_cancelled_task_without_agent_id_is_not_an_agent_event(self) -> None:
        raw = json.dumps({
            "type": "cursor_state_tool",
            "role": "tool",
            "id": "cancelled-task:tool",
            "timestamp": "2026-07-21T12:00:00Z",
            "tool_name": "task_v2",
            "tool_status": "cancelled",
            "tool_input": json.dumps({"name": "general-purpose"}),
            "content": "Status: cancelled",
        })

        messages = parse_conversation(raw, "cursor")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].raw_type, "cursor_state_tool")
        self.assertIsNone(messages[0].agent_event)

    def test_codex_list_agents_result_is_a_subagent_status_snapshot(self) -> None:
        raw = json.dumps({
            "type": "response_item",
            "timestamp": "2026-07-19T21:42:37Z",
            "payload": {
                "type": "function_call_output",
                "call_id": "list-agents-1",
                "output": json.dumps({
                    "agents": [
                        {"agent_name": "/root", "agent_status": "running"},
                        {
                            "agent_name": "/root/pdx_index_scope_rca",
                            "agent_status": "running",
                        },
                        {
                            "agent_name": "/root/dr_index_fix_review",
                            "agent_status": {"completed": "private completion text"},
                        },
                    ],
                }),
            },
        })

        messages = parse_conversation(raw, "codex")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].raw_type, "agent_event")
        self.assertEqual(messages[0].tool_name, "Subagent status")
        self.assertEqual(messages[0].agent_event["kind"], "snapshot")
        self.assertEqual(
            messages[0].agent_event["agents"],
            [
                {
                    "agent_path": "/root/pdx_index_scope_rca",
                    "label": "Pdx Index Scope RCA",
                    "status": "running",
                },
                {
                    "agent_path": "/root/dr_index_fix_review",
                    "label": "Dr Index Fix Review",
                    "status": "completed",
                },
            ],
        )
        self.assertNotIn("private completion text", messages[0].content)
        self.assertNotIn("private completion text", json.dumps(messages[0].agent_event))

    def test_codex_unrelated_agents_json_remains_a_tool_result(self) -> None:
        raw = json.dumps({
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "ordinary-tool-1",
                "output": json.dumps({
                    "agents": [{"name": "sales", "status": "active"}],
                }),
            },
        })

        messages = parse_conversation(raw, "codex")

        self.assertEqual(messages[0].raw_type, "tool_output")
        self.assertIsNone(messages[0].agent_event)

    def test_claude_assistant_model_is_preserved(self) -> None:
        raw = json.dumps({
            "type": "assistant",
            "uuid": "assistant-claude-1",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [{"type": "text", "text": "Done"}],
            },
        })

        messages = parse_conversation(raw, "claude_code")
        isolated = parse_conversation_line(raw, "claude_code")

        self.assertEqual(messages[0].model, "claude-opus-4-8")
        self.assertEqual(messages[0].reasoning_effort, "")
        assert isolated is not None
        self.assertEqual(isolated.model, "claude-opus-4-8")

    def test_claude_thinking_mode_follows_observed_turn_blocks(self) -> None:
        raw = "\n".join([
            json.dumps({
                "type": "user",
                "uuid": "user-1",
                "message": {"role": "user", "content": "First"},
            }),
            json.dumps({
                "type": "assistant",
                "uuid": "thinking-1",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-8",
                    "content": [{"type": "thinking", "thinking": ""}],
                },
            }),
            json.dumps({
                "type": "assistant",
                "uuid": "answer-1",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-8",
                    "content": [{"type": "text", "text": "First answer"}],
                },
            }),
            json.dumps({
                "type": "user",
                "uuid": "user-2",
                "message": {"role": "user", "content": "Second"},
            }),
            json.dumps({
                "type": "assistant",
                "uuid": "answer-2",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-8",
                    "content": [{"type": "text", "text": "Second answer"}],
                },
            }),
        ])

        assistants = [
            message for message in parse_conversation(raw, "claude_code")
            if message.role == "assistant"
        ]

        self.assertEqual(
            [message.reasoning_effort for message in assistants],
            ["extended", ""],
        )

    def test_claude_question_and_tool_response_are_structured(self) -> None:
        raw = "\n".join([
            json.dumps({
                "type": "assistant",
                "uuid": "assistant-1",
                "timestamp": "2026-07-14T10:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": "toolu-1",
                        "name": "AskUserQuestion",
                        "input": {
                            "questions": [{
                                "header": "Release",
                                "question": "How should I ship this?",
                                "multiSelect": False,
                                "options": [
                                    {"label": "Deploy now", "description": "Build and deploy immediately."},
                                    {"label": "Hold", "description": "Leave the change local."},
                                ],
                            }],
                        },
                    }],
                },
            }),
            json.dumps({
                "type": "user",
                "uuid": "user-1",
                "timestamp": "2026-07-14T10:00:02Z",
                "message": {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": "toolu-1",
                        "content": (
                            'Your questions have been answered: '
                            '"How should I ship this?"="Deploy now". '
                            "You can now continue with these answers in mind."
                        ),
                    }],
                },
            }),
        ])

        messages = parse_conversation(raw, "claude_code")

        self.assertEqual(len(messages), 2)
        interaction = messages[0].interaction
        assert interaction is not None
        self.assertEqual(interaction["id"], "toolu-1")
        self.assertEqual(interaction["questions"][0]["type"], "single_select")
        self.assertEqual(interaction["questions"][0]["options"][0]["label"], "Deploy now")
        response = messages[1].interaction_response
        assert response is not None
        self.assertEqual(response["interaction_id"], "toolu-1")
        self.assertEqual(response["answers"][0]["selected_option_ids"], ["Deploy now"])

    def test_cursor_question_response_supports_multiple_selection(self) -> None:
        raw = "\n".join([
            json.dumps({
                "role": "assistant",
                "timestamp": "2026-07-14T11:00:00Z",
                "message": {
                    "content": [{
                        "type": "tool_use",
                        "id": "cursor-question-1",
                        "name": "AskQuestion",
                        "input": {
                            "questions": [{
                                "id": "targets",
                                "prompt": "Which targets should I build?",
                                "allow_multiple": True,
                                "options": [
                                    {"id": "web", "label": "Web"},
                                    {"id": "desktop", "label": "Desktop"},
                                ],
                            }],
                        },
                    }],
                },
            }),
            json.dumps({
                "role": "user",
                "timestamp": "2026-07-14T11:00:02Z",
                "message": {"content": [{"type": "text", "text": "Web and Desktop"}]},
            }),
        ])

        messages = parse_conversation(raw, "cursor")

        self.assertEqual(messages[0].role, "tool")
        interaction = messages[0].interaction
        assert interaction is not None
        self.assertEqual(interaction["questions"][0]["type"], "multi_select")
        response = messages[1].interaction_response
        assert response is not None
        self.assertEqual(response["answers"][0]["selected_option_ids"], ["web", "desktop"])

    def test_cursor_free_text_does_not_match_one_character_option_id(self) -> None:
        raw = "\n".join([
            json.dumps({
                "role": "assistant",
                "message": {
                    "content": [{
                        "type": "tool_use",
                        "id": "cursor-question-2",
                        "name": "AskQuestion",
                        "input": {
                            "questions": [{
                                "id": "scope",
                                "prompt": "How should this work?",
                                "options": [
                                    {"id": "a", "label": "Automatic"},
                                    {"id": "m", "label": "Manual"},
                                ],
                            }],
                        },
                    }],
                },
            }),
            json.dumps({
                "role": "user",
                "message": {"content": [{"type": "text", "text": "I want a custom workflow"}]},
            }),
        ])

        messages = parse_conversation(raw, "cursor")

        response = messages[1].interaction_response
        assert response is not None
        self.assertEqual(response["answers"][0]["selected_option_ids"], [])

    def test_codex_request_user_input_call_and_output_are_preserved(self) -> None:
        raw = "\n".join([
            json.dumps({
                "type": "response_item",
                "timestamp": "2026-07-14T12:00:00Z",
                "payload": {
                    "type": "function_call",
                    "name": "request_user_input",
                    "call_id": "call-question-1",
                    "arguments": json.dumps({
                        "questions": [{
                            "id": "rollout",
                            "header": "Rollout",
                            "question": "Proceed with deployment?",
                            "options": [
                                {"label": "Proceed", "description": "Deploy the verified build."},
                                {"label": "Pause", "description": "Keep the build local."},
                            ],
                        }],
                    }),
                },
            }),
            json.dumps({
                "type": "response_item",
                "timestamp": "2026-07-14T12:00:03Z",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-question-1",
                    "output": json.dumps({
                        "answers": {"rollout": {"answers": ["Proceed"]}},
                    }),
                },
            }),
        ])

        messages = parse_conversation(raw, "codex")

        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].tool_name, "request_user_input")
        self.assertEqual(messages[0].interaction["questions"][0]["id"], "rollout")
        self.assertEqual(
            messages[1].interaction_response["answers"][0]["selected_option_ids"],
            ["Proceed"],
        )

    def test_codex_ordinary_tool_calls_and_outputs_are_preserved(self) -> None:
        raw = "\n".join([
            json.dumps({
                "type": "response_item",
                "timestamp": "2026-07-14T12:00:00Z",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "call-exec-1",
                    "input": "text(await tools.shell_command({command: 'Get-Date'}));",
                },
            }),
            json.dumps({
                "type": "response_item",
                "timestamp": "2026-07-14T12:00:01Z",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-exec-1",
                    "output": [
                        {"type": "input_text", "text": "Script completed\n"},
                        {"type": "input_text", "text": "Exit code: 0"},
                    ],
                },
            }),
        ])

        messages = parse_conversation(raw, "codex")

        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].role, "tool")
        self.assertEqual(messages[0].tool_name, "exec")
        self.assertIn("Get-Date", messages[0].tool_input)
        self.assertEqual(messages[0].raw_type, "tool_call")
        self.assertEqual(messages[1].role, "tool")
        self.assertEqual(messages[1].tool_name, "Tool result")
        self.assertEqual(messages[1].content, "Script completed\n\nExit code: 0")
        self.assertEqual(messages[1].raw_type, "tool_output")

    def test_codex_function_and_web_search_calls_are_preserved(self) -> None:
        raw = "\n".join([
            json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "shell_command",
                    "call_id": "call-shell-1",
                    "arguments": {"command": "Get-Process"},
                },
            }),
            json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "web_search_call",
                    "call_id": "call-search-1",
                    "query": "Memento documentation",
                    "status": "completed",
                },
            }),
        ])

        messages = parse_conversation(raw, "codex")

        self.assertEqual([message.tool_name for message in messages], [
            "shell_command",
            "web_search",
        ])
        self.assertIn("Get-Process", messages[0].tool_input)
        self.assertEqual(messages[1].tool_input, "Memento documentation")

    def test_question_response_can_resume_across_delta_boundaries(self) -> None:
        cursor_question_raw = json.dumps({
            "role": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "cursor-split-1",
                    "name": "AskQuestion",
                    "input": {
                        "questions": [{
                            "id": "choice",
                            "prompt": "Pick a path",
                            "options": [
                                {"id": "safe", "label": "Safe path"},
                                {"id": "fast", "label": "Fast path"},
                            ],
                        }],
                    },
                }],
            },
        })
        question = list(iter_conversation_messages(cursor_question_raw, "cursor"))[0]
        self.assertEqual(question.role, "tool")
        interaction = question.interaction
        assert interaction is not None

        cursor_response_raw = json.dumps({
            "role": "user",
            "message": {"content": [{"type": "text", "text": "Safe path"}]},
        })
        response = list(iter_conversation_messages(
            cursor_response_raw,
            "cursor",
            initial_question_interactions=[interaction],
        ))[0]

        self.assertEqual(response.interaction_response["interaction_id"], "cursor-split-1")
        self.assertEqual(
            response.interaction_response["answers"][0]["selected_option_ids"],
            ["safe"],
        )

    def test_nullable_transport_fields_do_not_create_or_crash_messages(self) -> None:
        cases = (
            (
                "codex",
                {"type": "event_msg", "payload": {"type": "agent_message", "message": None}},
            ),
            (
                "codex",
                {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": None}},
            ),
            (
                "codex",
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": None}],
                    },
                },
            ),
            ("codex", {"type": "event_msg", "payload": None}),
            ("claude_code", {"type": "assistant", "message": None}),
            (
                "claude_code",
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": None}],
                    },
                },
            ),
            (
                "cursor",
                {
                    "role": "user",
                    "message": {"content": [{"type": "text", "text": None}]},
                },
            ),
        )

        for tool_id, record in cases:
            with self.subTest(tool_id=tool_id, record=record):
                self.assertIsNone(parse_conversation_line(json.dumps(record), tool_id))

    def test_cursor_nullable_prose_preserves_structured_tool_call(self) -> None:
        raw = json.dumps({
            "role": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": None},
                    {"type": "tool_use", "name": "Read", "input": {"path": "a.py"}},
                ]
            },
        })

        message = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message.content, "")
        self.assertEqual(message.tool_calls, [{"name": "Read", "input": '{"path": "a.py"}'}])

    def test_codex_request_wrapper_keeps_only_the_human_request(self) -> None:
        wrapped = (
            "# Context from my IDE setup:\n\n"
            "## Open tabs:\n- REPORT.md\n\n"
            "## My request for Codex:\n"
            "Explain the drift and propose a fix."
        )
        raw = json.dumps({
            "type": "response_item",
            "timestamp": "2026-07-08T10:00:00Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": wrapped}],
            },
        })

        msg = parse_conversation_line(raw, "codex")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "user")
        self.assertEqual(msg.content, "Explain the drift and propose a fix.")

    def test_codex_files_wrapper_is_normalized_for_event_messages(self) -> None:
        wrapped = (
            "# Files mentioned by the user:\n\n"
            "## report.png\n\n"
            "## My request for Codex:\nRepair the card title."
        )
        raw = json.dumps({
            "type": "event_msg",
            "payload": {"type": "user_message", "message": wrapped},
        })

        msg = parse_conversation_line(raw, "codex")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "user")
        self.assertEqual(msg.content, "Repair the card title.")

    def test_codex_agents_envelope_is_system_context_not_a_prompt(self) -> None:
        content = (
            "# AGENTS.md instructions for C:\\repo\n\n"
            "<INSTRUCTIONS>Use PowerShell.</INSTRUCTIONS>"
        )
        raw = json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": content}],
            },
        })

        msg = parse_conversation_line(raw, "codex")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "system")
        self.assertEqual(msg.raw_type, "codex_context")
        self.assertEqual(msg.content, content)

    def test_codex_environment_context_is_preserved_as_system_context(self) -> None:
        role, content = normalize_codex_user_payload(
            "<environment_context><cwd>C:\\repo</cwd></environment_context>"
        )

        self.assertEqual(role, "system")
        self.assertIn("environment_context", content)

    def test_codex_recommended_plugins_bootstrap_is_system_context(self) -> None:
        # Minimized from the real Dreamland Yoga "Clarify 95% usage"
        # transcript. Codex emits this bootstrap as role=user even though it
        # is product-provided session context, not a human prompt.
        content = (
            "<recommended_plugins>\n"
            "Here is a list of plugins that are available but not installed.\n\n"
            "- Figma (figma@openai-curated-remote)\n"
            "- GitHub (github@openai-curated-remote)\n"
            "</recommended_plugins>\n"
            "# AGENTS.md instructions for C:\\repo\n"
            "<INSTRUCTIONS>Use PowerShell.</INSTRUCTIONS>\n"
            "<environment_context><cwd>C:\\repo</cwd></environment_context>"
        )
        raw = json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": content}],
            },
        })

        msg = parse_conversation_line(raw, "codex")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "system")
        self.assertEqual(msg.raw_type, "codex_context")
        self.assertEqual(msg.content, content)

    def test_codex_plain_prompt_is_not_over_normalized(self) -> None:
        role, content = normalize_codex_user_payload(
            "Please explain how AGENTS.md instructions are loaded."
        )

        self.assertEqual(role, "user")
        self.assertEqual(
            content,
            "Please explain how AGENTS.md instructions are loaded.",
        )

    def test_codex_plain_prompt_quoting_request_marker_is_not_truncated(self) -> None:
        prompt = (
            "Please preserve this template exactly:\n\n"
            "## My request for Codex:\nplaceholder"
        )

        role, content = normalize_codex_user_payload(prompt)

        self.assertEqual(role, "user")
        self.assertEqual(content, prompt)

    def test_codex_cross_transport_pair_is_one_canonical_prompt(self) -> None:
        rows = [
            {
                "type": "response_item",
                "timestamp": "2026-07-13T21:41:31.790Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Keep going"}],
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-13T21:41:31.791Z",
                "payload": {
                    "type": "user_message",
                    "client_id": "prompt-1",
                    "message": "Keep going",
                },
            },
        ]
        raw = "\n".join(json.dumps(row) for row in rows)

        messages = parse_conversation(raw, "codex")

        self.assertEqual(count_conversation_messages(raw, "codex"), 1)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].raw_type, "user_message")
        self.assertEqual(messages[0].source_id, "prompt-1")

    def test_codex_legacy_cross_transport_delay_still_pairs(self) -> None:
        rows = [
            {
                "type": "response_item",
                "timestamp": "2026-06-24T21:02:26.016Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Keep going"}],
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-06-24T21:02:26.848Z",
                "payload": {
                    "type": "user_message",
                    "client_id": "legacy-prompt",
                    "message": "Keep going",
                },
            },
        ]
        raw = "\n".join(json.dumps(row) for row in rows)

        messages = parse_conversation(raw, "codex")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].source_id, "legacy-prompt")

    def test_codex_aborted_turn_replay_preserves_both_prompt_attempts(self) -> None:
        def response(turn_id: str, timestamp: str) -> dict:
            return {
                "type": "response_item",
                "timestamp": timestamp,
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Delegate it"}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": turn_id,
                    },
                },
            }

        def event(client_id: str, timestamp: str) -> dict:
            return {
                "type": "event_msg",
                "timestamp": timestamp,
                "payload": {
                    "type": "user_message",
                    "client_id": client_id,
                    "message": "Delegate it",
                },
            }

        rows = [
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-a"},
            },
            response("turn-a", "2026-07-09T23:49:09.112Z"),
            event("prompt-a", "2026-07-09T23:49:09.112Z"),
            {
                "type": "event_msg",
                "payload": {"type": "turn_aborted", "turn_id": "turn-a"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-b"},
            },
            response("turn-b", "2026-07-09T23:49:09.113Z"),
            event("prompt-b", "2026-07-09T23:49:09.113Z"),
        ]
        raw = "\n".join(json.dumps(row) for row in rows)

        messages = parse_conversation(raw, "codex")

        self.assertEqual(
            [(message.role, message.raw_type) for message in messages],
            [
                ("user", "user_message"),
                ("system", "turn_aborted"),
                ("user", "user_message"),
            ],
        )
        self.assertEqual(
            [message.source_id for message in messages if message.role == "user"],
            ["prompt-a", "prompt-b"],
        )
        self.assertEqual(messages[0].source_turn_id, "turn-a")
        self.assertEqual(messages[2].source_turn_id, "turn-b")

    def test_codex_aborted_different_prompt_keeps_both_turns(self) -> None:
        rows = [
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-a"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-09T23:49:09.112Z",
                "payload": {
                    "type": "user_message",
                    "turn_id": "turn-a",
                    "client_id": "prompt-a",
                    "message": "First request",
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "turn_aborted", "turn_id": "turn-a"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-b"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-09T23:49:09.113Z",
                "payload": {
                    "type": "user_message",
                    "turn_id": "turn-b",
                    "client_id": "prompt-b",
                    "message": "Different request",
                },
            },
        ]
        raw = "\n".join(json.dumps(row) for row in rows)

        messages = parse_conversation(raw, "codex")

        self.assertEqual(
            [message.content for message in messages if message.role == "user"],
            ["First request", "Different request"],
        )
        self.assertEqual(messages[1].raw_type, "turn_aborted")

    def test_codex_delta_preserves_interruption_boundary(self) -> None:
        rows = [
            {
                "type": "event_msg",
                "payload": {"type": "turn_aborted", "turn_id": "turn-a"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-b"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-09T23:49:09.113Z",
                "payload": {
                    "type": "user_message",
                    "client_id": "prompt-b",
                    "message": "Delegate it",
                },
            },
        ]
        raw = "\n".join(json.dumps(row) for row in rows)

        messages = parse_conversation(raw, "codex")

        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].raw_type, "turn_aborted")
        self.assertEqual(messages[0].source_turn_id, "turn-a")
        self.assertEqual(messages[1].source_turn_id, "turn-b")

    def test_codex_interruption_marker_keeps_reason_and_elapsed_time(self) -> None:
        raw = json.dumps({
            "type": "event_msg",
            "timestamp": "2026-07-13T14:52:15.097Z",
            "payload": {
                "type": "turn_aborted",
                "turn_id": "turn-a",
                "reason": "interrupted",
                "duration_ms": 6444,
            },
        })

        messages = parse_conversation(raw, "codex")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].role, "system")
        self.assertEqual(messages[0].raw_type, "turn_aborted")
        self.assertEqual(
            messages[0].content,
            "Turn interrupted · Reason: interrupted · Elapsed: 6.444s",
        )

    def test_codex_attachment_suffix_still_pairs_with_event_prompt(self) -> None:
        rows = [
            {
                "type": "response_item",
                "timestamp": "2026-07-13T21:29:26.189Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": "Inspect this screenshot\n[local image metadata]",
                    }],
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-13T21:29:26.189Z",
                "payload": {
                    "type": "user_message",
                    "client_id": "prompt-with-image",
                    "message": "Inspect this screenshot",
                },
            },
        ]
        raw = "\n".join(json.dumps(row) for row in rows)

        messages = parse_conversation(raw, "codex")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].content, "Inspect this screenshot")

    def test_codex_assistant_transport_group_is_one_canonical_message(self) -> None:
        rows = [
            {
                "type": "event_msg",
                "timestamp": "2026-07-13T16:45:13.158Z",
                "payload": {
                    "type": "agent_message",
                    "message": "Final answer",
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-07-13T16:45:13.184Z",
                "payload": {
                    "type": "message",
                    "id": "assistant-response-id",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Final answer"}],
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-13T16:45:13.184Z",
                "payload": {"type": "token_count", "info": {}},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-13T16:45:13.213Z",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-id",
                    "last_agent_message": "Final answer",
                },
            },
        ]
        raw = "\n".join(json.dumps(row) for row in rows)

        messages = parse_conversation(raw, "codex")

        self.assertEqual(count_conversation_messages(raw, "codex"), 1)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].raw_type, "agent_message")
        self.assertEqual(messages[0].content, "Final answer")

    def test_codex_delayed_exact_assistant_transport_is_still_one_message(self) -> None:
        rows = [
            {
                "type": "event_msg",
                "timestamp": "2026-07-13T16:45:13.158Z",
                "payload": {"type": "agent_message", "message": "Final answer"},
            },
            {
                "type": "response_item",
                "timestamp": "2026-07-13T16:45:23.547Z",
                "payload": {
                    "type": "message",
                    "id": "assistant-response-id",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Final answer"}],
                },
            },
        ]
        raw = "\n".join(json.dumps(row) for row in rows)

        messages = parse_conversation(raw, "codex")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].content, "Final answer")

    def test_codex_delayed_prefix_messages_remain_distinct(self) -> None:
        rows = [
            {
                "type": "event_msg",
                "timestamp": "2026-07-13T16:45:13.158Z",
                "payload": {"type": "agent_message", "message": "Final answer"},
            },
            {
                "type": "response_item",
                "timestamp": "2026-07-13T16:45:23.547Z",
                "payload": {
                    "type": "message",
                    "id": "assistant-response-id",
                    "role": "assistant",
                    "content": [{
                        "type": "output_text",
                        "text": "Final answer with additional detail",
                    }],
                },
            },
        ]
        raw = "\n".join(json.dumps(row) for row in rows)

        messages = parse_conversation(raw, "codex")

        self.assertEqual(len(messages), 2)

    def test_codex_lone_assistant_response_is_not_discarded(self) -> None:
        raw = json.dumps({
            "type": "response_item",
            "timestamp": "2026-07-13T16:45:13.184Z",
            "payload": {
                "type": "message",
                "id": "assistant-response-id",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Only copy"}],
            },
        })

        messages = parse_conversation(raw, "codex")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].raw_type, "response_item")
        self.assertEqual(messages[0].content, "Only copy")

    def test_codex_lone_task_complete_message_is_not_discarded(self) -> None:
        raw = json.dumps({
            "type": "event_msg",
            "timestamp": "2026-07-13T16:45:13.213Z",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-id",
                "last_agent_message": "Recovered final answer",
            },
        })

        messages = parse_conversation(raw, "codex")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].raw_type, "task_complete")
        self.assertEqual(messages[0].content, "Recovered final answer")

    def test_codex_identical_same_second_prompts_keep_distinct_client_ids(self) -> None:
        rows = []
        for client_id, milliseconds in (("prompt-a", 100), ("prompt-b", 200)):
            timestamp = f"2026-07-13T21:41:31.{milliseconds:03d}Z"
            rows.extend([
                {
                    "type": "response_item",
                    "timestamp": timestamp,
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "continue"}],
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": timestamp,
                    "payload": {
                        "type": "user_message",
                        "client_id": client_id,
                        "message": "continue",
                    },
                },
            ])
        raw = "\n".join(json.dumps(row) for row in rows)

        messages = parse_conversation(raw, "codex")

        self.assertEqual(count_conversation_messages(raw, "codex"), 2)
        self.assertEqual([message.source_id for message in messages], [
            "prompt-a",
            "prompt-b",
        ])

    def test_claude_uuid_not_content_is_the_message_identity(self) -> None:
        def row(source_id: str) -> dict:
            return {
                "uuid": source_id,
                "type": "user",
                "timestamp": "2026-07-13T21:34:42.980Z",
                "message": {"role": "user", "content": "repeat"},
            }

        distinct = "\n".join(json.dumps(row(value)) for value in ("a", "b"))
        replayed = "\n".join(json.dumps(row("a")) for _ in range(2))

        self.assertEqual(count_conversation_messages(distinct, "claude_code"), 2)
        self.assertEqual(count_conversation_messages(replayed, "claude_code"), 1)

    def test_claude_queue_enqueue_preserves_interrupted_human_prompt(self) -> None:
        raw = json.dumps({
            "type": "queue-operation",
            "operation": "enqueue",
            "sessionId": "session-1",
            "timestamp": "2026-07-15T10:24:58.844Z",
            "content": "Please check the status of every ingestor",
        })

        messages = parse_conversation(raw, "claude_code")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].role, "user")
        self.assertEqual(messages[0].raw_type, "queued_user_message")
        self.assertEqual(
            messages[0].content,
            "Please check the status of every ingestor",
        )
        self.assertTrue(messages[0].source_id.startswith("claude-queue:"))

    def test_claude_queue_enqueue_and_canonical_user_are_one_prompt(self) -> None:
        content = "Roll out all schedulers"
        raw = "\n".join([
            json.dumps({
                "type": "queue-operation",
                "operation": "enqueue",
                "sessionId": "session-1",
                "timestamp": "2026-07-15T21:23:32.904Z",
                "content": content,
            }),
            json.dumps({
                "type": "queue-operation",
                "operation": "dequeue",
                "sessionId": "session-1",
                "timestamp": "2026-07-15T21:24:00.000Z",
            }),
            json.dumps({
                "type": "user",
                "uuid": "canonical-user-1",
                "timestamp": "2026-07-15T21:24:02.000Z",
                "message": {"role": "user", "content": content},
            }),
        ])

        messages = parse_conversation(raw, "claude_code")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].raw_type, "queued_user_message")
        self.assertEqual(messages[0].content, content)

    def test_claude_repeated_queue_prompts_are_reconciled_one_to_one(self) -> None:
        content = "keep going"
        rows = []
        for index in range(2):
            rows.append({
                "type": "queue-operation",
                "operation": "enqueue",
                "sessionId": "session-1",
                "timestamp": f"2026-07-15T21:2{index}:00.000Z",
                "content": content,
            })
        rows.append({
            "type": "user",
            "uuid": "canonical-user-1",
            "timestamp": "2026-07-15T21:22:00.000Z",
            "message": {"role": "user", "content": content},
        })

        messages = parse_conversation(
            "\n".join(json.dumps(row) for row in rows),
            "claude_code",
        )

        self.assertEqual(len(messages), 2)
        self.assertEqual(
            [message.raw_type for message in messages],
            ["queued_user_message", "queued_user_message"],
        )

    def test_claude_queue_transport_notifications_remain_hidden(self) -> None:
        raw = json.dumps({
            "type": "queue-operation",
            "operation": "enqueue",
            "sessionId": "session-1",
            "timestamp": "2026-07-15T21:24:00.000Z",
            "content": (
                "<task-notification>Background task completed"
                "</task-notification>"
            ),
        })

        self.assertEqual(parse_conversation(raw, "claude_code"), [])

    def test_claude_delta_queue_matches_are_consumed_one_to_one(self) -> None:
        first = type("QueueRow", (), {
            "content": "keep going",
            "timestamp": "2026-07-15T10:00:00Z",
        })()
        second = type("QueueRow", (), {
            "content": "keep going",
            "timestamp": "2026-07-15T10:01:00Z",
        })()
        queued: dict[str, list[object]] = {"keep going": [first, second]}

        self.assertIs(
            pop_matching_claude_queue_user(
                queued,
                "keep going",
                "2026-07-15T10:02:00Z",
            ),
            first,
        )
        self.assertIs(
            pop_matching_claude_queue_user(
                queued,
                "keep going",
                "2026-07-15T10:03:00Z",
            ),
            second,
        )
        self.assertEqual(queued["keep going"], [])

    def test_claude_delta_queue_match_is_bounded_in_time(self) -> None:
        row = type("QueueRow", (), {
            "content": "keep going",
            "timestamp": "2026-07-13T10:00:00Z",
        })()

        self.assertIsNone(pop_matching_claude_queue_user(
            {"keep going": [row]},
            "keep going",
            "2026-07-15T10:01:00Z",
        ))

    def test_cursor_preserves_identical_same_second_source_items(self) -> None:
        row = {
            "role": "user",
            "message": {
                "content": (
                    "<timestamp>Wednesday, Jun 24, 2026, 9:08 AM "
                    "(UTC-4)</timestamp>\n<user_query>repeat</user_query>"
                )
            },
        }
        raw = "\n".join(json.dumps(row) for _ in range(2))

        messages = parse_conversation(raw, "cursor")

        self.assertEqual(count_conversation_messages(raw, "cursor"), 2)
        self.assertEqual([message.content for message in messages], [
            "repeat",
            "repeat",
        ])

    def test_codex_session_metadata_uses_current_thread_and_root_ids(self) -> None:
        root_id = "11111111-1111-4111-8111-111111111111"
        current_id = "22222222-2222-4222-8222-222222222222"
        raw = json.dumps({
            "type": "session_meta",
            "payload": {
                "session_id": root_id,
                "id": current_id,
                "forked_from_id": root_id,
                "parent_thread_id": root_id,
                "thread_source": "subagent",
                "agent_path": "/root/reviewer",
                "agent_nickname": "Noether",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": root_id,
                            "depth": 1,
                            "agent_path": "/root/reviewer",
                            "agent_nickname": "Noether",
                        }
                    }
                },
            },
        })

        metadata = extract_codex_session_metadata(raw)

        self.assertEqual(metadata["session_id"], current_id)
        self.assertEqual(metadata["thread_id"], current_id)
        self.assertEqual(metadata["root_session_id"], root_id)
        self.assertEqual(metadata["parent_thread_id"], root_id)
        self.assertEqual(metadata["forked_from_id"], root_id)
        self.assertEqual(metadata["thread_source"], "subagent")
        self.assertEqual(metadata["agent_path"], "/root/reviewer")
        self.assertEqual(metadata["agent_nickname"], "Noether")
        self.assertEqual(metadata["agent_depth"], 1)

    def test_codex_session_metadata_survives_a_truncated_range_prefix(self) -> None:
        current_id = "33333333-3333-4333-8333-333333333333"
        raw = (
            '{"type":"session_meta","payload":{'
            f'"session_id":"{current_id}","id":"{current_id}",'
            '"thread_source":"user","base_instructions":"unfinished'
        )

        metadata = extract_codex_session_metadata(raw)

        self.assertEqual(metadata["session_id"], current_id)
        self.assertEqual(metadata["root_session_id"], current_id)
        self.assertEqual(metadata["thread_source"], "user")

    def test_claude_tool_result_is_not_classified_as_user(self) -> None:
        raw = json.dumps({
            "type": "user",
            "timestamp": "2026-07-07T10:00:00Z",
            "message": {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "tool-123",
                    "content": "alpha-\u001b[7mmatch\u001b[0m-omega",
                }],
            },
        })

        msg = parse_conversation_line(raw, "claude_code")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "tool")
        self.assertEqual(msg.raw_type, "tool_result")
        self.assertEqual(msg.tool_name, "Tool result")
        self.assertEqual(msg.content, "alpha-match-omega")

    def test_terminal_sequence_stripping_handles_csi_and_osc(self) -> None:
        value = "a\u001b[31mred\u001b[0m b\u001b]0;title\u0007c"
        self.assertEqual(strip_terminal_sequences(value), "ared bc")

    def test_terminal_sequence_stripping_handles_charset_and_fe_escapes(
        self,
    ) -> None:
        value = "a\u001b(B+ b\u001b)0- c\u001b7saved"
        self.assertEqual(strip_terminal_sequences(value), "a+ b- csaved")

    def test_terminal_sequence_stripping_removes_truncated_escape(self) -> None:
        value = "before\u001b …[+1214 chars] after"
        self.assertEqual(
            strip_terminal_sequences(value),
            "before …[+1214 chars] after",
        )

    def test_claude_standalone_tool_use_is_rendered_as_tool(self) -> None:
        raw = json.dumps({
            "type": "assistant",
            "timestamp": "2026-07-07T10:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Need to inspect the directory"},
                    {
                        "type": "tool_use",
                        "name": "Run Terminal Command",
                        "input": {"command": "Get-ChildItem C:\\\\Users"},
                    },
                ],
            },
        })

        msg = parse_conversation_line(raw, "claude_code")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "tool")
        self.assertEqual(msg.raw_type, "tool_use")
        self.assertEqual(msg.tool_name, "Run Terminal Command")
        self.assertIn("Get-ChildItem", msg.tool_input)

    def test_claude_mixed_record_keeps_prose_and_every_tool_in_order(self) -> None:
        raw = json.dumps({
            "type": "assistant",
            "uuid": "claude-mixed-1",
            "timestamp": "2026-07-07T10:00:00Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-1",
                "content": [
                    {"type": "text", "text": "I will inspect both files."},
                    {
                        "type": "tool_use",
                        "id": "tool-read",
                        "name": "Read",
                        "input": {"path": "/tmp/one.py"},
                    },
                    {"type": "text", "text": "Now I will compare them."},
                    {
                        "type": "tool_use",
                        "id": "tool-shell",
                        "name": "Shell",
                        "input": {"command": "diff one.py two.py"},
                    },
                ],
            },
        })

        messages = parse_conversation(raw, "claude_code")

        self.assertEqual(
            [(message.role, message.tool_name) for message in messages],
            [
                ("assistant", ""),
                ("tool", "Read"),
                ("assistant", ""),
                ("tool", "Shell"),
            ],
        )
        self.assertEqual(messages[0].content, "I will inspect both files.")
        self.assertEqual(messages[2].content, "Now I will compare them.")
        self.assertTrue(all("[Tool:" not in message.content for message in messages))
        self.assertEqual(len({message.source_id for message in messages}), 4)
        self.assertEqual(messages[0].model, "claude-opus-4-1")

    def test_claude_mixed_user_record_keeps_each_tool_result(self) -> None:
        raw = json.dumps({
            "type": "user",
            "uuid": "claude-results-1",
            "timestamp": "2026-07-07T10:00:01Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-read",
                        "content": "first result",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-shell",
                        "content": "second result",
                    },
                ],
            },
        })

        messages = parse_conversation(raw, "claude_code")

        self.assertEqual([message.role for message in messages], ["tool", "tool"])
        self.assertEqual(
            [message.content for message in messages],
            ["first result", "second result"],
        )
        self.assertEqual(
            [message.tool_call_id for message in messages],
            ["tool-read", "tool-shell"],
        )

    def test_claude_local_command_is_compact_tool_context(self) -> None:
        raw = json.dumps({
            "type": "user",
            "timestamp": "2026-07-07T10:00:00Z",
            "message": {
                "role": "user",
                "content": (
                    "<local-command-caveat>Caveat text</local-command-caveat>\n"
                    "<command-name>/model</command-name>\n"
                    "<command-message>model</command-message>\n"
                    "<command-args>opus</command-args>\n"
                    "<local-command-stdout>Set model to opus</local-command-stdout>"
                ),
            },
        })

        msg = parse_conversation_line(raw, "claude_code")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "tool")
        self.assertEqual(msg.raw_type, "local_command")
        self.assertEqual(msg.tool_name, "/model")
        self.assertEqual(msg.tool_input, "opus")
        self.assertEqual(msg.content, "Set model to opus")

    def test_claude_local_command_caveat_is_still_hidden(self) -> None:
        raw = json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": (
                    "<local-command-caveat>Generated locally</local-command-caveat>"
                ),
            },
        })

        self.assertIsNone(parse_conversation_line(raw, "claude_code"))

    def test_claude_meta_prompt_is_session_context_not_human_input(self) -> None:
        # Real Claude Code slash-command expansion records carry isMeta=true.
        raw = json.dumps({
            "type": "user",
            "isMeta": True,
            "timestamp": "2026-06-26T13:29:54.177Z",
            "message": {
                "role": "user",
                "content": (
                    "# /loop — schedule a recurring or self-paced prompt\n\n"
                    "Synthetic command instructions.\n\n"
                    "## Input\n\n15m Check prod for auth errors."
                ),
            },
        })

        msg = parse_conversation_line(raw, "claude_code")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "system")
        self.assertEqual(msg.raw_type, "claude_context")

    def test_claude_compaction_summary_is_session_context(self) -> None:
        raw = json.dumps({
            "type": "user",
            "isCompactSummary": True,
            "isVisibleInTranscriptOnly": True,
            "message": {
                "role": "user",
                "content": (
                    "This session is being continued from a previous "
                    "conversation that ran out of context."
                ),
            },
        })

        msg = parse_conversation_line(raw, "claude_code")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "system")
        self.assertEqual(msg.raw_type, "claude_context")

    def test_cursor_timestamp_envelope_is_removed_and_parsed(self) -> None:
        raw = json.dumps({
            "role": "user",
            "message": {
                "content": [{
                    "type": "text",
                    "text": (
                        "<timestamp>Wednesday, Jun 24, 2026, 9:08 AM "
                        "(UTC-4)</timestamp>\n"
                        "<user_query>\nMove this workspace to Windows.\n"
                        "</user_query>"
                    ),
                }],
            },
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "user")
        self.assertEqual(msg.content, "Move this workspace to Windows.")
        self.assertEqual(msg.timestamp, "2026-06-24T09:08:00-04:00")

    def test_cursor_external_links_context_is_separated_from_prompt(self) -> None:
        content = (
            "<external_links>\n"
            "### Potentially Relevant Websearch Results\n"
            "Website URL: https://example.com\n"
            "</external_links>\n"
            "<timestamp>Friday, Jun 26, 2026, 12:35 PM (UTC-4)</timestamp>\n\n"
            "don't raise the gap, we haven't had any oom issues"
        )
        raw = json.dumps({
            "role": "user",
            "message": {"content": content},
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(
            msg.content,
            "don't raise the gap, we haven't had any oom issues",
        )
        self.assertEqual(msg.timestamp, "2026-06-26T12:35:00-04:00")
        self.assertIn("Potentially Relevant Websearch Results", msg.session_context)

    def test_cursor_system_notification_is_context_not_human_input(self) -> None:
        content = (
            "<system_notification>\n"
            "The following task has finished. If you were already aware, "
            "ignore this notification and do not restate prior responses.\n\n"
            "<task>\n"
            "kind: shell\n"
            "status: success\n"
            "task_id: 15893\n"
            "title: Measure BAN runtime footprint\n"
            "</task>\n"
            "</system_notification>\n"
            "<user_query>Briefly inform the user about the task result and "
            "perform any follow-up actions (if needed). If there's no "
            "follow-ups needed, don't explicitly say that.</user_query>"
        )
        raw = json.dumps({
            "type": "user",
            "role": "user",
            "id": "notif-1",
            "timestamp": "2026-07-21T12:57:06Z",
            "message": {"content": content},
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "system")
        self.assertEqual(msg.raw_type, "cursor_context")
        self.assertIn("system_notification", msg.content)
        self.assertIn("Measure BAN runtime footprint", msg.content)
        self.assertIn("Briefly inform the user about the task result", msg.content)

    def test_cursor_image_attachments_do_not_block_prompt_or_timestamp(self) -> None:
        content = (
            "[Image] [Image]\n"
            "<image_files>\n"
            "The following images were provided by the user:\n"
            "1. C:\\Users\\intpa\\.cursor\\assets\\first-image.png\n"
            "2. /home/intpa/.cursor/assets/second-image.jpg\n"
            "</image_files>\n"
            "<timestamp>Monday, Jun 29, 2026, 4:30 PM (UTC-4)</timestamp>\n"
            "<user_query>Why did this fail?</user_query>"
        )
        raw = json.dumps({
            "role": "user",
            "message": {"content": content},
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, "Why did this fail?")
        self.assertEqual(msg.timestamp, "2026-06-29T16:30:00-04:00")
        self.assertEqual(
            msg.attachments,
            [
                {"type": "image", "name": "first-image.png"},
                {"type": "image", "name": "second-image.jpg"},
            ],
        )
        self.assertNotIn("image_files", msg.session_context)

    def test_cursor_literal_image_files_markup_in_prompt_is_preserved(self) -> None:
        content = "Explain how <image_files> markup works in Cursor."
        raw = json.dumps({
            "role": "user",
            "message": {"content": content},
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, content)
        self.assertEqual(msg.attachments, [])

    def test_cursor_image_marker_without_path_does_not_block_timestamp(self) -> None:
        content = (
            "[Image]\n"
            "<timestamp>Tuesday, Jun 23, 2026, 2:43 PM (UTC-4)</timestamp>\n"
            "<user_query>Check the screenshot.</user_query>"
        )
        raw = json.dumps({
            "role": "user",
            "message": {"content": content},
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, "Check the screenshot.")
        self.assertEqual(msg.timestamp, "2026-06-23T14:43:00-04:00")
        self.assertEqual(msg.attachments, [{"type": "image", "name": "Image 1"}])

    def test_cursor_plugin_context_is_separated_from_prompt(self) -> None:
        content = (
            '<plugin_info kind="matched_installed">\n'
            "display_name: Datadog\n"
            "</plugin_info>\n\n"
            "Can you inspect the dashboard without modifying anything?"
        )
        raw = json.dumps({
            "role": "user",
            "message": {"content": content},
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(
            msg.content,
            "Can you inspect the dashboard without modifying anything?",
        )
        self.assertIn("display_name: Datadog", msg.session_context)

    def test_cursor_utc_timestamp_envelope_uses_explicit_utc(self) -> None:
        raw = json.dumps({
            "role": "user",
            "message": {
                "content": (
                    "<timestamp>Monday, Jun 15, 2026, 7:51 PM "
                    "(UTC)</timestamp>\n"
                    "<user_query>Continue the investigation.</user_query>"
                ),
            },
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, "Continue the investigation.")
        self.assertEqual(msg.timestamp, "2026-06-15T19:51:00+00:00")

    def test_cursor_positive_fractional_utc_offset_is_parsed(self) -> None:
        raw = json.dumps({
            "role": "user",
            "message": {
                "content": (
                    "<timestamp>Friday, Jun 12, 2026, 8:42 AM "
                    "(UTC+5:30)</timestamp>\n"
                    "<user_query>Check the deployment.</user_query>"
                ),
            },
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, "Check the deployment.")
        self.assertEqual(msg.timestamp, "2026-06-12T08:42:00+05:30")

    def test_cursor_native_timestamp_wins_over_envelope_timestamp(self) -> None:
        raw = json.dumps({
            "role": "user",
            "timestamp": "2026-06-24T13:09:00Z",
            "message": {
                "content": (
                    "<timestamp>Wednesday, Jun 24, 2026, 9:08 AM "
                    "(UTC-4)</timestamp>\n"
                    "<user_query>Use the native timestamp.</user_query>"
                ),
            },
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, "Use the native timestamp.")
        self.assertEqual(msg.timestamp, "2026-06-24T13:09:00Z")

    def test_cursor_legacy_user_query_wrapper_without_timestamp_is_removed(self) -> None:
        raw = json.dumps({
            "role": "user",
            "message": {
                "content": "<user_query>Plain wrapped prompt.</user_query>",
            },
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, "Plain wrapped prompt.")
        self.assertEqual(msg.timestamp, "")

    def test_cursor_normalizer_is_noop_without_valid_timestamp_envelope(self) -> None:
        content = "<user_query>Backfill must not alter this.</user_query>"

        normalized, timestamp = normalize_cursor_user_payload(content)

        self.assertEqual(normalized, content)
        self.assertEqual(timestamp, "")

    def test_cursor_normalizer_handles_stored_prompt_without_query_wrapper(self) -> None:
        content = (
            "<timestamp>Wednesday, Jun 24, 2026, 9:08 AM "
            "(UTC-4)</timestamp>\nAlready-normalized stored prompt."
        )

        normalized, timestamp = normalize_cursor_user_payload(content)

        self.assertEqual(normalized, "Already-normalized stored prompt.")
        self.assertEqual(timestamp, "2026-06-24T09:08:00-04:00")

    def test_cursor_impossible_utc_offset_is_preserved(self) -> None:
        content = (
            "<timestamp>Wednesday, Jun 24, 2026, 9:08 AM "
            "(UTC+14:30)</timestamp>\n"
            "<user_query>Keep impossible metadata literal.</user_query>"
        )

        normalized, timestamp = normalize_cursor_user_payload(content)

        self.assertEqual(normalized, content)
        self.assertEqual(timestamp, "")

    def test_cursor_mid_prompt_literal_tags_are_preserved(self) -> None:
        content = (
            "Explain why <timestamp>Wednesday, Jun 24, 2026, 9:08 AM "
            "(UTC-4)</timestamp> and <user_query> are shown."
        )
        raw = json.dumps({
            "role": "user",
            "message": {"content": content},
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, content)
        self.assertEqual(msg.timestamp, "")

    def test_cursor_malformed_leading_timestamp_is_preserved(self) -> None:
        content = (
            "<timestamp>not a Cursor timestamp</timestamp>\n"
            "<user_query>Keep this literal example.</user_query>"
        )
        raw = json.dumps({
            "role": "user",
            "message": {"content": content},
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, content)
        self.assertEqual(msg.timestamp, "")

    def test_cursor_assistant_markup_is_not_normalized(self) -> None:
        content = (
            "<timestamp>Wednesday, Jun 24, 2026, 9:08 AM "
            "(UTC-4)</timestamp>\n"
            "<user_query>This is assistant-authored markup.</user_query>"
        )
        raw = json.dumps({
            "role": "assistant",
            "message": {"content": content},
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, content)
        self.assertEqual(msg.timestamp, "")

    def test_cursor_redacted_transport_text_becomes_structured_tool_call(self) -> None:
        raw = json.dumps({
            "role": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "[REDACTED]"},
                    {
                        "type": "tool_use",
                        "name": "TodoWrite",
                        "input": {
                            "merge": False,
                            "todos": [{"id": "1", "status": "in_progress"}],
                        },
                    },
                ],
            },
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "assistant")
        self.assertEqual(msg.content, "")
        self.assertEqual(msg.tool_calls[0]["name"], "TodoWrite")
        self.assertEqual(
            json.loads(msg.tool_calls[0]["input"])["merge"],
            False,
        )

    def test_cursor_keeps_prose_separate_from_multiple_tool_calls(self) -> None:
        raw = json.dumps({
            "role": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "I will inspect both files."},
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"path": "/tmp/one.py"},
                    },
                    {
                        "type": "toolCall",
                        "name": "Shell",
                        "arguments": {"command": "ls -la /tmp"},
                    },
                ],
            },
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, "I will inspect both files.")
        self.assertEqual(
            [call["name"] for call in msg.tool_calls],
            ["Read", "Shell"],
        )
        self.assertNotIn("[Tool:", msg.content)

    def test_cursor_removes_redacted_transport_line_appended_to_prose(self) -> None:
        raw = json.dumps({
            "role": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": "Running the next check.\n[REDACTED]",
                    },
                    {
                        "type": "tool_use",
                        "name": "Shell",
                        "input": {"command": "ls -la"},
                    },
                ],
            },
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, "Running the next check.")
        self.assertEqual(msg.tool_calls[0]["name"], "Shell")

    def test_cursor_call_only_assistant_message_is_retained(self) -> None:
        raw = json.dumps({
            "role": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"path": "/tmp/results.jsonl"},
                }],
            },
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, "")
        self.assertEqual(len(msg.tool_calls), 1)

    def test_cursor_call_only_rows_keep_count_and_pagination_in_lockstep(self) -> None:
        rows = [
            {
                "role": "user",
                "message": {"content": "Inspect the file."},
            },
            {
                "role": "assistant",
                "message": {
                    "content": [{
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"path": "/tmp/results.jsonl"},
                    }],
                },
            },
            {
                "role": "assistant",
                "message": {"content": "The file is valid."},
            },
        ]
        raw_content = "\n".join(json.dumps(row) for row in rows)

        total = count_conversation_messages(raw_content, "cursor")
        page = parse_conversation(
            raw_content,
            "cursor",
            offset=1,
            limit=1,
        )

        self.assertEqual(total, 3)
        self.assertEqual(len(page), 1)
        self.assertEqual(page[0].role, "tool")
        self.assertEqual(page[0].raw_type, "tool_call")
        self.assertEqual(page[0].tool_name, "Read")
        self.assertIn("results.jsonl", page[0].tool_input)

    def test_cursor_composite_assistant_record_expands_to_semantic_rows(self) -> None:
        raw = json.dumps({
            "role": "assistant",
            "timestamp": "2026-07-15T10:00:00Z",
            "message": {
                "content": [
                    {"type": "text", "text": "I will inspect both files."},
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"path": "/tmp/one.py"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Shell",
                        "input": {"command": "python -m pytest"},
                    },
                ],
            },
        })

        messages = parse_conversation(raw, "cursor")

        self.assertEqual(
            [(message.role, message.raw_type) for message in messages],
            [
                ("assistant", "assistant"),
                ("tool", "tool_call"),
                ("tool", "tool_call"),
            ],
        )
        self.assertEqual(messages[0].content, "I will inspect both files.")
        self.assertEqual(
            [message.tool_name for message in messages[1:]],
            ["Read", "Shell"],
        )
        self.assertTrue(all(message.timestamp for message in messages))
        self.assertFalse(any(message.tool_calls for message in messages))

    def test_cursor_tool_result_block_expands_without_a_fake_user_row(self) -> None:
        raw = json.dumps({
            "role": "user",
            "timestamp": "2026-07-15T10:00:01Z",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "cursor-call-1",
                    "content": "command completed",
                }],
            },
        })

        messages = parse_conversation(raw, "cursor")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].role, "tool")
        self.assertEqual(messages[0].raw_type, "tool_output")
        self.assertEqual(messages[0].tool_call_id, "cursor-call-1")
        self.assertEqual(messages[0].content, "command completed")

    def test_cursor_malformed_tool_fields_are_safe_and_calls_are_bounded(self) -> None:
        calls = [
            {"type": "tool_use", "name": ["not", "a", "name"], "input": None},
            "not a content block",
        ]
        calls.extend(
            {"type": "tool_use", "name": f"Tool{i}", "input": {"n": i}}
            for i in range(40)
        )
        raw = json.dumps({
            "role": "assistant",
            "message": {"content": calls},
        })

        msg = parse_conversation_line(raw, "cursor")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(len(msg.tool_calls), 32)
        self.assertEqual(msg.tool_calls[0], {"name": "Tool", "input": "null"})

    def test_antigravity_message_preserves_separate_thinking(self) -> None:
        raw = json.dumps({
            "type": "assistant",
            "timestamp": "2026-04-05T10:00:00Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Final answer"}],
            },
            "response_text": "Final answer",
            "thinking_text": "Internal reasoning",
            "content_source": "response",
        })

        msg = parse_conversation_line(raw, "antigravity")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.role, "assistant")
        self.assertEqual(msg.content, "Final answer")
        self.assertEqual(msg.thinking, "Internal reasoning")
        self.assertEqual(msg.raw_type, "response")

    def test_antigravity_message_falls_back_to_thinking_when_response_missing(self) -> None:
        raw = json.dumps({
            "type": "assistant",
            "timestamp": "2026-04-05T10:00:00Z",
            "message": {"role": "assistant", "content": []},
            "thinking_text": "Only thinking available",
            "fallback_source": "thinking_fallback",
        })

        msg = parse_conversation_line(raw, "antigravity")

        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.content, "Only thinking available")
        self.assertEqual(msg.thinking, "Only thinking available")
        self.assertEqual(msg.raw_type, "thinking_fallback")


if __name__ == "__main__":
    unittest.main()
