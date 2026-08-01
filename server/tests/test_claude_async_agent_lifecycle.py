from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from server.services.conversation_hierarchy import (  # noqa: E402
    merge_subagent_event_summaries,
)
from server.services.conversation_parser import parse_conversation  # noqa: E402


def _agent_exchange(*, status: str | None) -> str:
    result = {"agentId": "aacbce856db239d85"}
    if status is not None:
        result["status"] = status
    return "\n".join([
        json.dumps({
            "type": "assistant",
            "uuid": "launch-row",
            "timestamp": "2026-07-31T12:02:46.851Z",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": "toolu_async_launch",
                    "name": "Agent",
                    "input": {
                        "description": "Doc staleness sweep",
                        "subagent_type": "general-purpose",
                        "run_in_background": status is not None,
                    },
                }],
            },
        }),
        json.dumps({
            "type": "user",
            "uuid": "result-row",
            "timestamp": "2026-07-31T12:02:46.887Z",
            "toolUseResult": result,
            "message": {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "toolu_async_launch",
                    "content": (
                        "Async agent launched successfully. "
                        "The agent is working in the background."
                        if status is not None
                        else "Implementation complete."
                    ),
                }],
            },
        }),
    ])


class ClaudeAsyncAgentLifecycleTests(unittest.TestCase):
    def test_async_launched_result_projects_running_status(self) -> None:
        messages = parse_conversation(
            _agent_exchange(status="async_launched"),
            "claude_code",
        )

        events = [message.agent_event for message in messages]
        self.assertEqual(
            [event["kind"] for event in events],
            ["started"],
        )
        self.assertEqual(events[-1]["status"], "async_launched")
        self.assertTrue(events[-1]["is_background"])
        self.assertEqual(
            events[-1]["agent_tool_use_id"],
            "toolu_async_launch",
        )
        self.assertEqual(events[-1]["agent_thread_id"], "aacbce856db239d85")
        self.assertNotIn("completed_at", events[-1])

        summaries = merge_subagent_event_summaries([], events)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["status"], "running")
        self.assertIsNone(summaries[0]["completed_at"])

    def test_foreground_result_without_status_remains_completed(self) -> None:
        messages = parse_conversation(
            _agent_exchange(status=None),
            "claude_code",
        )

        result_event = messages[-1].agent_event
        self.assertEqual(result_event["kind"], "completed")
        self.assertEqual(result_event["status"], "completed")
        self.assertIn("completed_at", result_event)


if __name__ == "__main__":
    unittest.main()
