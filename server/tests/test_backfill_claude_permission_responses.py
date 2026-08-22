from __future__ import annotations

import uuid

from server.scripts.backfill_claude_permission_responses import (
    ExecutedToolCall,
    PermissionDocument,
    plan_permission_response_repairs,
)


def _document(*, status: str = "answered", answers: list | None = None):
    interaction_id = "memento-permission-exact"
    response = {
        "kind": "question_response",
        "interaction_id": interaction_id,
        "status": status,
        "answers": [] if answers is None else answers,
        "raw_text": "",
    }
    machine_id = uuid.uuid4()
    return PermissionDocument(
        id=uuid.uuid4(),
        machine_id=machine_id,
        root_session_id="root-session",
        metadata={
            "_interaction_history": [{
                "status": status,
                "interaction": {
                    "id": interaction_id,
                    "interaction_type": "permission_request",
                    "requested_tool": "Bash",
                    "tool_input": {
                        "command": "ls -la",
                        "description": "List files",
                    },
                },
                "response": response,
            }],
        },
    )


def _executed(document: PermissionDocument, *, command: str = "ls -la"):
    return ExecutedToolCall(
        machine_id=document.machine_id,
        root_session_id=document.root_session_id,
        tool_name="Bash",
        tool_input={"command": command, "description": "List files"},
        tool_call_id="toolu-exact",
    )


def test_exact_executed_permission_is_repaired_as_allowed() -> None:
    document = _document()

    repairs = plan_permission_response_repairs([document], [_executed(document)])

    assert len(repairs) == 1
    response = repairs[0].repaired_entry["response"]
    assert response["raw_text"] == "Yes"
    assert response["answers"] == [{
        "question_id": "permission-decision",
        "text": "Yes",
        "selected_option_ids": ["allow"],
    }]
    assert (
        repairs[0].repaired_entry["response_backfill"]
        == "exact_executed_tool_result_v1"
    )


def test_permission_without_exact_execution_evidence_is_untouched() -> None:
    document = _document()

    assert plan_permission_response_repairs(
        [document],
        [_executed(document, command="different command")],
    ) == []
    assert plan_permission_response_repairs([document], []) == []


def test_recorded_or_non_answered_permission_is_never_overwritten() -> None:
    recorded = _document(answers=[{
        "question_id": "permission-decision",
        "text": "No",
        "selected_option_ids": ["deny"],
    }])
    pending = _document(status="pending")

    assert plan_permission_response_repairs([recorded], [_executed(recorded)]) == []
    assert plan_permission_response_repairs([pending], [_executed(pending)]) == []


def test_execution_evidence_is_machine_and_root_session_scoped() -> None:
    document = _document()
    wrong_machine = ExecutedToolCall(
        machine_id=uuid.uuid4(),
        root_session_id=document.root_session_id,
        tool_name="Bash",
        tool_input={"command": "ls -la", "description": "List files"},
        tool_call_id="toolu-other-machine",
    )
    wrong_session = ExecutedToolCall(
        machine_id=document.machine_id,
        root_session_id="other-session",
        tool_name="Bash",
        tool_input={"command": "ls -la", "description": "List files"},
        tool_call_id="toolu-other-session",
    )

    assert plan_permission_response_repairs(
        [document],
        [wrong_machine, wrong_session],
    ) == []
