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


class CodexTaskStateBackfillTests(unittest.TestCase):
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
