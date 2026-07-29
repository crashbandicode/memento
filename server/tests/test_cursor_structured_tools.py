from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.api.conversations import (  # noqa: E402
    _parsed_tool_calls,
    _stored_attachments,
    _stored_tool_calls,
)
from server.services.conversation_parser import (  # noqa: E402
    NormalizedMessage,
    iter_conversation_messages,
)
from server.services.ingest_service import (  # noqa: E402
    _conversation_message_metadata,
    _pending_question_interactions,
    _update_pending_question_ids,
    iter_stored_conversation_messages,
)


class CursorStructuredToolStorageTests(unittest.TestCase):
    @staticmethod
    def _plan_mode_record(
        status: str,
        *,
        target_mode: str = "plan",
        reason: str = "",
    ) -> dict:
        record = {
            "type": "cursor_state_tool",
            "role": "tool",
            "id": "plan-1:tool",
            "timestamp": "2026-07-26T22:42:27Z",
            "tool_name": "switch_mode",
            "tool_status": status,
            "tool_input": json.dumps({
                "fromModeId": "agent",
                "toModeId": target_mode,
                "explanation": "Confirm the architecture before editing.",
            }),
            "tool_call_id": "call-plan-1",
            "content": f"Status: {status}\n\n{{}}",
        }
        if reason:
            record["tool_status_reason"] = reason
        return record

    def test_cursor_plan_mode_request_is_pending_tool_interaction(self) -> None:
        messages = list(iter_conversation_messages(
            json.dumps(self._plan_mode_record("loading")),
            "cursor",
        ))

        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertEqual(message.role, "tool")
        self.assertIsNotNone(message.interaction)
        self.assertEqual(message.interaction["interaction_type"], "mode_switch")
        self.assertEqual(message.interaction["to_mode"], "plan")
        self.assertFalse(message.interaction["questions"][0]["allow_custom"])
        self.assertIsNone(message.interaction_response)

        pending_ids: set[str] = set()
        _update_pending_question_ids(pending_ids, message)
        self.assertEqual(pending_ids, {"call-plan-1"})

    def test_cursor_plan_mode_request_skipped_and_answered_states(self) -> None:
        skipped = next(iter_conversation_messages(
            json.dumps(self._plan_mode_record("cancelled", reason="timeout")),
            "cursor",
        ))
        self.assertEqual(skipped.interaction_response["status"], "cancelled")
        self.assertEqual(skipped.interaction_response["raw_text"], "Skipped (timeout)")

        pending_ids = {"call-plan-1"}
        _update_pending_question_ids(pending_ids, skipped)
        self.assertEqual(pending_ids, set())

        accepted_record = self._plan_mode_record("completed")
        later_user = {
            "type": "user",
            "role": "user",
            "id": "user-after-plan",
            "timestamp": "2026-07-26T22:43:00Z",
            "message": {"content": "Now continue with the implementation."},
        }
        accepted, user = list(iter_conversation_messages(
            "\n".join((json.dumps(accepted_record), json.dumps(later_user))),
            "cursor",
        ))
        self.assertEqual(accepted.interaction_response["status"], "answered")
        self.assertEqual(
            accepted.interaction_response["answers"][0]["selected_option_ids"],
            ["plan"],
        )
        self.assertEqual(user.role, "user")
        self.assertIsNone(user.interaction_response)

    def test_cursor_switch_mode_non_plan_target_is_not_interaction(self) -> None:
        message = next(iter_conversation_messages(
            json.dumps(self._plan_mode_record("completed", target_mode="agent")),
            "cursor",
        ))

        self.assertEqual(message.role, "tool")
        self.assertIsNone(message.interaction)
        self.assertIsNone(message.interaction_response)

    def test_cursor_compat_plan_mode_tool_result_is_linked(self) -> None:
        raw = "\n".join([
            json.dumps({
                "role": "assistant",
                "message": {
                    "content": [{
                        "type": "toolCall",
                        "call_id": "call-plan-1",
                        "name": "switch_mode",
                        "arguments": {
                            "fromModeId": "agent",
                            "toModeId": "plan",
                            "explanation": "Confirm the architecture first.",
                        },
                    }],
                },
            }),
            json.dumps({
                "role": "assistant",
                "message": {
                    "content": [{
                        "type": "toolResult",
                        "call_id": "call-plan-1",
                        "output": "{}",
                    }],
                },
            }),
        ])

        request, response = list(iter_conversation_messages(raw, "cursor"))

        self.assertEqual(request.role, "tool")
        self.assertEqual(request.interaction["interaction_type"], "mode_switch")
        self.assertEqual(response.role, "tool")
        self.assertEqual(response.interaction_response["status"], "answered")

    def test_cursor_plan_mode_rejected_result_is_skipped(self) -> None:
        record = self._plan_mode_record("completed")
        record["content"] = '{"rejected": true}'

        message = next(iter_conversation_messages(json.dumps(record), "cursor"))

        self.assertEqual(message.interaction_response["status"], "cancelled")
        self.assertEqual(message.interaction_response["raw_text"], "Skipped")

    def test_cursor_state_question_keeps_multiselect_and_inline_answer(self) -> None:
        record = {
            "type": "cursor_state_tool",
            "role": "tool",
            "id": "question-1",
            "timestamp": "2026-07-20T12:00:00Z",
            "tool_name": "ask_question",
            "tool_input": json.dumps({
                "questions": [{
                    "id": "targets",
                    "prompt": "Which targets?",
                    "allowMultiple": True,
                    "options": [
                        {"id": "api", "label": "API"},
                        {"id": "web", "label": "Web"},
                    ],
                }],
            }),
            "content": json.dumps({
                "answers": [{
                    "questionId": "targets",
                    "selectedOptionIds": ["api", "web"],
                    "freeformText": "",
                }],
            }),
        }

        messages = list(iter_conversation_messages(json.dumps(record), "cursor"))

        self.assertEqual(len(messages), 1)
        question = messages[0].interaction["questions"][0]
        self.assertEqual(question["type"], "multi_select")
        answer = messages[0].interaction_response["answers"][0]
        self.assertEqual(answer["selected_option_ids"], ["api", "web"])

    def test_cursor_state_submitted_status_keeps_structured_answer(self) -> None:
        answer_payload = {
            "answers": [{
                "questionId": "target",
                "selectedOptionIds": ["api"],
                "freeformText": "",
            }]
        }
        record = {
            "type": "cursor_state_tool",
            "role": "tool",
            "id": "question-1",
            "timestamp": "2026-07-20T12:00:00Z",
            "tool_name": "ask_question",
            "tool_status": "submitted",
            "tool_input": json.dumps({
                "questions": [{
                    "id": "target",
                    "prompt": "Which target?",
                    "options": [{"id": "api", "label": "API"}],
                }]
            }),
            "content": (
                "Status: submitted\n\n"
                + json.dumps(answer_payload)
            ),
        }

        messages = list(iter_conversation_messages(json.dumps(record), "cursor"))

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].interaction_response["status"], "answered")
        self.assertEqual(
            messages[0].interaction_response["answers"][0]["selected_option_ids"],
            ["api"],
        )

    def test_cursor_state_todowrite_uses_authoritative_final_todos(self) -> None:
        record = {
            "type": "cursor_state_tool",
            "role": "tool",
            "id": "todo-result-1",
            "timestamp": "2026-07-20T12:00:00Z",
            "tool_name": "todo_write",
            "tool_input": json.dumps({"merge": True}),
            "content": json.dumps({
                "success": True,
                "finalTodos": [
                    {"id": "1", "content": "Inspect", "status": "completed"},
                    {"id": "2", "content": "Repair", "status": "in_progress"},
                ],
            }),
        }

        messages = list(iter_conversation_messages(json.dumps(record), "cursor"))

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].task_state["source"], "cursor")
        self.assertEqual(messages[0].task_state["completed_count"], 1)
        self.assertEqual(messages[0].task_state["total_count"], 2)
        self.assertEqual(messages[0].task_state["active_task_id"], "2")

    def test_cursor_state_projection_keeps_thinking_identity_tasks_and_tools(
        self,
    ) -> None:
        records = [
            {
                "type": "user",
                "role": "user",
                "id": "user-1",
                "timestamp": "2026-07-18T14:19:00Z",
                "model": "grok-4.5",
                "reasoning_effort": "high",
                "message": {"content": "Free the resources"},
            },
            {
                "type": "cursor_state_thinking",
                "role": "assistant",
                "id": "thought-1",
                "timestamp": "2026-07-18T14:19:01Z",
                "model": "grok-4.5",
                "reasoning_effort": "high",
                "message": {
                    "content": [{
                        "type": "thinking",
                        "thinking": "I should stop the cron safely.",
                    }],
                },
            },
            {
                "type": "cursor_state_task",
                "role": "tool",
                "id": "tasks-1",
                "timestamp": "2026-07-18T14:19:02Z",
                "tool_name": "Task progress 4/5",
                "tool_input": json.dumps({
                    "tasks": [
                        {"id": "1", "content": "Inspect", "status": "completed"},
                        {"id": "2", "content": "Report", "status": "in_progress"},
                    ],
                    "is_current": True,
                }),
                "content": "4 of 5 tasks complete",
            },
            {
                "type": "cursor_state_tool",
                "role": "tool",
                "id": "tool-1",
                "timestamp": "2026-07-18T14:19:03Z",
                "tool_name": "PowerShell",
                "tool_input": '{"command":"Stop-Process"}',
                "tool_call_id": "call-1",
                "content": "Status: cancelled",
            },
        ]

        messages = list(iter_conversation_messages(
            "\n".join(json.dumps(record) for record in records),
            "cursor",
        ))

        self.assertEqual([message.role for message in messages], [
            "user", "assistant", "tool", "tool",
        ])
        self.assertEqual(messages[1].content, "")
        self.assertEqual(messages[1].thinking, "I should stop the cron safely.")
        self.assertEqual(messages[1].model, "grok-4.5")
        self.assertEqual(messages[1].reasoning_effort, "high")
        self.assertEqual(messages[1].source_id, "thought-1")
        self.assertEqual(messages[2].tool_name, "Task progress 4/5")
        self.assertEqual(messages[2].task_state["completed_count"], 1)
        self.assertEqual(messages[2].task_state["active_task_id"], "2")
        self.assertTrue(messages[2].task_state["is_current"])
        self.assertEqual(messages[3].tool_name, "PowerShell")
        self.assertEqual(messages[3].tool_call_id, "call-1")

        stored = list(iter_stored_conversation_messages(
            "\n".join(json.dumps(record) for record in records),
            "cursor",
        ))
        thinking_rows = [
            row for row in stored if row[0].raw_type == "cursor_state_thinking"
        ]
        self.assertEqual(len(thinking_rows), 1)
        self.assertEqual(thinking_rows[0][1], "")
        self.assertEqual(
            thinking_rows[0][2]["thinking"],
            "I should stop the cron safely.",
        )

    def test_task_snapshots_cover_codex_claude_and_embedded_cursor_tools(self) -> None:
        codex_records = [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "functions.update_plan",
                    "call_id": "plan-1",
                    "arguments": json.dumps({
                        "plan": [
                            {"step": "Inspect", "status": "in_progress"},
                            {"step": "Report", "status": "pending"},
                        ],
                    }),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "update_plan",
                    "call_id": "plan-2",
                    "arguments": json.dumps({
                        "plan": [
                            {"step": "Inspect", "status": "completed"},
                            {"step": "Report", "status": "in_progress"},
                        ],
                    }),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "search-1",
                    "output": "#### [Button: Copy]",
                },
            },
        ]
        codex = list(iter_conversation_messages(
            "\n".join(json.dumps(record) for record in codex_records),
            "codex",
        ))
        self.assertEqual(codex[0].task_state["completed_count"], 0)
        self.assertEqual(codex[1].task_state["completed_count"], 1)
        self.assertEqual(codex[1].task_state["active_task_id"], codex[1].task_state["tasks"][1]["id"])
        self.assertIsNone(codex[2].task_state)

        claude_records = [
            {
                "type": "assistant",
                "uuid": "create-row",
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": "create-call",
                        "name": "TaskCreate",
                        "input": {
                            "subject": "Inspect collector",
                            "description": "Find the state source",
                        },
                    }],
                },
            },
            {
                "type": "user",
                "uuid": "create-result",
                "message": {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": "create-call",
                        "content": "Task #7 created successfully: Inspect collector",
                    }],
                },
            },
            {
                "type": "assistant",
                "uuid": "update-row",
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": "update-call",
                        "name": "TaskUpdate",
                        "input": {
                            "taskId": "7",
                            "status": "completed",
                        },
                    }],
                },
            },
            {
                "type": "assistant",
                "uuid": "list-call-row",
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": "list-call",
                        "name": "TaskList",
                        "input": {},
                    }],
                },
            },
            {
                "type": "user",
                "uuid": "list-result",
                "message": {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": "list-call",
                        "content": "#7 [completed] Inspect collector\n#8 [pending] Deploy collector",
                    }],
                },
            },
            {
                "type": "user",
                "uuid": "unrelated-result",
                "message": {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": "search-call",
                        "content": (
                            "#### [Button: Copy]\n"
                            "#1 [internal] load build definition"
                        ),
                    }],
                },
            },
        ]
        claude = list(iter_conversation_messages(
            "\n".join(json.dumps(record) for record in claude_records),
            "claude_code",
        ))
        self.assertEqual(claude[1].task_state["tasks"][0]["id"], "7")
        self.assertEqual(claude[2].task_state["completed_count"], 1)
        self.assertIsNone(claude[3].task_state)
        self.assertEqual(claude[4].task_state["total_count"], 2)
        self.assertEqual(claude[4].task_state["active_task_id"], "8")
        self.assertIsNone(claude[5].task_state)

        cursor_record = {
            "role": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "name": "TodoWrite",
                    "input": {
                        "merge": False,
                        "todos": [{
                            "id": "cursor-1",
                            "content": "Profile",
                            "status": "in_progress",
                        }],
                    },
                }],
            },
        }
        cursor = list(iter_conversation_messages(
            json.dumps(cursor_record),
            "cursor",
        ))
        self.assertEqual(cursor[0].task_state["active_task_id"], "cursor-1")

    def test_codex_exec_extracts_nested_update_plan_without_evaluating_js(self) -> None:
        records = [
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "exec-plan-1",
                    "input": (
                        'const r = await tools.update_plan({'
                        'explanation:"Use {bounded, safe} parsing",'
                        'plan:['
                        '{step:"Inventory current code",status:"completed"},'
                        '{step:"Implement page stream",status:"completed"},'
                        '{step:"Replace archive recheck",status:"in_progress"},'
                        '{step:"Run validation",status:"pending"},'
                        '{step:"Run ETL proof",status:"pending"},'
                        '{step:"Update handoff",status:"pending"},'
                        ']}); text(r);'
                    ),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "ordinary-exec",
                    "input": 'text("tools.update_plan({plan:[not executable]})");',
                },
            },
        ]

        messages = list(iter_conversation_messages(
            "\n".join(json.dumps(record) for record in records),
            "codex",
        ))

        self.assertEqual(messages[0].task_state["total_count"], 6)
        self.assertEqual(messages[0].task_state["completed_count"], 2)
        self.assertEqual(
            messages[0].task_state["active_task_id"],
            messages[0].task_state["tasks"][2]["id"],
        )
        self.assertEqual(
            messages[0].task_state["tasks"][2]["content"],
            "Replace archive recheck",
        )
        self.assertIsNone(messages[1].task_state)

    def test_ingest_metadata_and_both_api_paths_have_the_same_shape(self) -> None:
        message = NormalizedMessage(
            role="assistant",
            content="I will inspect it.",
            thinking="separate reasoning",
            attachments=[
                {"type": "image", "name": "screenshot.png"},
            ],
            tool_calls=[
                {"name": "Read", "input": '{"path":"/tmp/input.json"}'},
                {
                    "name": "AskQuestion",
                    "input": '{"questions":[{"id":"ship","prompt":"Ship it?","options":[{"id":"yes","label":"Yes"}]}]}',
                },
            ],
            interaction={
                "kind": "question",
                "id": "question-1",
                "source": "cursor",
                "tool_name": "AskQuestion",
                "questions": [{
                    "id": "ship",
                    "header": "",
                    "prompt": "Ship it?",
                    "type": "single_select",
                    "allow_custom": True,
                    "options": [{"id": "yes", "label": "Yes"}],
                }],
            },
            interaction_response={
                "kind": "question_response",
                "interaction_id": "question-1",
                "status": "answered",
                "answers": [{
                    "question_id": "ship",
                    "text": "Yes",
                    "selected_option_ids": ["yes"],
                }],
                "raw_text": "Yes",
            },
            task_state={
                "version": 1,
                "source": "cursor",
                "revision": 3,
                "is_current": True,
                "completed_count": 1,
                "total_count": 2,
                "active_task_id": "2",
                "tasks": [],
            },
            agent_event={
                "version": 1,
                "agent_path": "/root/audit_parser",
                "agent_thread_id": "agent-1",
                "label": "Audit Parser",
                "kind": "completed",
            },
        )

        metadata = _conversation_message_metadata(message)

        self.assertEqual(metadata["thinking"], "separate reasoning")
        self.assertEqual(metadata["interaction"], message.interaction)
        self.assertEqual(metadata["interaction_response"], message.interaction_response)
        self.assertEqual(metadata["task_state"], message.task_state)
        self.assertEqual(metadata["agent_event"], message.agent_event)
        self.assertEqual(
            _stored_attachments(metadata),
            [{"type": "image", "name": "screenshot.png"}],
        )
        parsed_calls = _parsed_tool_calls(message)
        stored_calls = _stored_tool_calls(metadata)
        self.assertEqual(stored_calls, parsed_calls)
        self.assertEqual(parsed_calls[1]["interaction"]["questions"][0]["id"], "ship")

    def test_db_fallback_rejects_malformed_metadata_safely(self) -> None:
        self.assertEqual(_stored_tool_calls(None), [])
        self.assertEqual(_stored_tool_calls({"tool_calls": "not-an-array"}), [])
        self.assertEqual(
            _stored_tool_calls({
                "tool_calls": [
                    None,
                    {"name": "Read", "input": {"path": "/tmp/a"}},
                ],
            }),
            [{"name": "Read", "input": '{"path": "/tmp/a"}'}],
        )

    def test_delta_lookback_does_not_revive_stale_cursor_question(self) -> None:
        interaction = {
            "kind": "question",
            "id": "cursor-question-1",
            "source": "cursor",
            "questions": [],
        }
        recent_rows = [
            SimpleNamespace(line_number=15, metadata_={}),
            SimpleNamespace(
                line_number=10,
                metadata_={"tool_calls": [{"interaction": interaction}]},
            ),
        ]

        self.assertEqual(_pending_question_interactions(recent_rows), [])

    def test_delta_lookback_keeps_immediate_cursor_and_id_linked_questions(self) -> None:
        cursor_interaction = {
            "kind": "question",
            "id": "cursor-question-1",
            "source": "cursor",
            "questions": [],
        }
        codex_interaction = {
            "kind": "question",
            "id": "codex-question-1",
            "source": "codex",
            "questions": [],
        }
        recent_rows = [
            SimpleNamespace(line_number=20, metadata_={}),
            SimpleNamespace(
                line_number=18,
                metadata_={"tool_calls": [{"interaction": cursor_interaction}]},
            ),
            SimpleNamespace(line_number=2, metadata_={"interaction": codex_interaction}),
        ]

        self.assertEqual(
            _pending_question_interactions(recent_rows),
            [codex_interaction, cursor_interaction],
        )

    def test_delta_lookback_does_not_revive_answered_question(self) -> None:
        interaction = {
            "kind": "question",
            "id": "codex-question-answered",
            "source": "codex",
            "questions": [],
        }
        recent_rows = [
            SimpleNamespace(line_number=10, metadata_={"interaction": interaction}),
            SimpleNamespace(
                line_number=11,
                metadata_={
                    "interaction_response": {
                        "interaction_id": "codex-question-answered",
                        "status": "answered",
                    },
                },
            ),
        ]

        self.assertEqual(_pending_question_interactions(recent_rows), [])

    def test_delta_lookback_does_not_revive_question_after_human_reply(self) -> None:
        interaction = {
            "kind": "question",
            "id": "cursor-question-with-unlinked-reply",
            "source": "cursor",
            "questions": [],
        }
        recent_rows = [
            SimpleNamespace(
                line_number=10,
                role="tool",
                content="Status: pending",
                metadata_={"interaction": interaction},
                timestamp="2026-07-20T12:00:00Z",
            ),
            SimpleNamespace(
                line_number=11,
                role="user",
                content="Use the first option and continue.",
                metadata_={},
                timestamp="2026-07-20T12:01:00Z",
            ),
            SimpleNamespace(
                line_number=12,
                role="tool",
                content="Status: pending",
                metadata_={"interaction": interaction},
                timestamp="2026-07-20T12:00:00Z",
            ),
        ]

        self.assertEqual(_pending_question_interactions(recent_rows), [])


if __name__ == "__main__":
    unittest.main()
