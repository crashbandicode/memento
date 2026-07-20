import unittest
import uuid

from server.scripts.backfill_codex_task_states import (
    CodexTaskRow,
    plan_task_state_overlays,
)


def _row(row_id: int, document_id: uuid.UUID, line: int, source: str):
    return CodexTaskRow(
        id=row_id,
        document_id=document_id,
        line_number=line,
        metadata={
            "tool_name": "exec",
            "tool_call_id": f"call-{row_id}",
            "tool_input": source,
        },
    )


def _tool_row(
    row_id: int,
    document_id: uuid.UUID,
    line: int,
    *,
    tool_id: str,
    tool_name: str,
    tool_input: str,
    content: str = "",
    message_type: str = "tool_use",
):
    return CodexTaskRow(
        id=row_id,
        document_id=document_id,
        line_number=line,
        tool_id=tool_id,
        message_type=message_type,
        content=content,
        metadata={"tool_name": tool_name, "tool_input": tool_input},
    )


class CodexTaskStateBackfillTests(unittest.TestCase):
    def test_cursor_todo_rows_use_the_shared_task_tracker(self):
        document_id = uuid.uuid4()
        rows = [
            _tool_row(
                1,
                document_id,
                10,
                tool_id="cursor",
                tool_name="TodoWrite",
                tool_input=(
                    '{"todos":['
                    '{"id":"1","content":"Inspect","status":"completed"},'
                    '{"id":"2","content":"Repair","status":"in_progress"}'
                    ']}'
                ),
            )
        ]

        updates = plan_task_state_overlays(rows)

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].task_state["source"], "cursor")
        self.assertEqual(updates[0].task_state["completed_count"], 1)
        self.assertEqual(updates[0].task_state["total_count"], 2)

    def test_existing_cross_tool_snapshot_is_never_overwritten(self):
        document_id = uuid.uuid4()
        row = _tool_row(
            1,
            document_id,
            10,
            tool_id="claude_code",
            tool_name="TodoWrite",
            tool_input=(
                '{"todos":[{"id":"1","content":"Keep",'
                '"status":"pending"}]}'
            ),
        )
        persisted = CodexTaskRow(
            **{
                **row.__dict__,
                "metadata": {
                    **row.metadata,
                    "task_state": {"version": 1, "source": "claude_code"},
                },
            }
        )

        self.assertEqual(plan_task_state_overlays([persisted]), [])

    def test_overlay_projects_six_tasks_and_tracks_later_replacement(self):
        document_id = uuid.uuid4()
        rows = [
            _row(1, document_id, 20, (
                'const r=await tools.update_plan({plan:['
                '{step:"One",status:"completed"},'
                '{step:"Two",status:"completed"},'
                '{step:"Three",status:"in_progress"},'
                '{step:"Four",status:"pending"},'
                '{step:"Five",status:"pending"},'
                '{step:"Six",status:"pending"}]});'
            )),
            _row(2, document_id, 30, (
                'await tools.update_plan({plan:['
                '{step:"Finish",status:"in_progress"}]})'
            )),
        ]

        updates = plan_task_state_overlays(rows)

        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[0].task_state["total_count"], 6)
        self.assertEqual(updates[0].task_state["completed_count"], 2)
        self.assertIs(updates[0].task_state["is_current"], True)
        self.assertEqual(updates[1].task_state["revision"], 2)
        self.assertEqual(updates[1].task_state["tasks"][0]["content"], "Finish")

    def test_overlay_ignores_malformed_and_is_idempotent(self):
        document_id = uuid.uuid4()
        malformed = _row(
            1,
            document_id,
            10,
            'text("tools.update_plan({plan:[not executable]})")',
        )
        valid = _row(
            2,
            document_id,
            20,
            'await tools.update_plan({plan:[{step:"Safe",status:"pending"}]})',
        )
        first = plan_task_state_overlays([malformed, valid])
        persisted = CodexTaskRow(
            id=valid.id,
            document_id=valid.document_id,
            line_number=valid.line_number,
            metadata={**valid.metadata, "task_state": first[0].task_state},
        )

        self.assertEqual(len(first), 1)
        self.assertEqual(plan_task_state_overlays([malformed, persisted]), [])
