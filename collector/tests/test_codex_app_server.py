"""Contract tests for the Codex app-server adapter against a scripted fake.

The fake speaks the exact wire dialect (JSON-RPC/JSONL, ``jsonrpc`` field
omitted) so these tests pin the adapter's handshake, turn lifecycle,
question/approval answering, delta coalescing, interrupt semantics, and
process-death behavior without touching the real CLI.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from collector.agents.codex_app_server import CodexAppServerAdapter
from collector.agents.jsonl_rpc import RpcTransportClosed

FAKE_SERVER = Path(__file__).resolve().parent / "fake_codex_app_server.py"


class _EventLog:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event_type: str, payload: dict) -> None:
        with self._lock:
            self.events.append((event_type, payload))

    def wait_for(
        self,
        event_type: str,
        *,
        where=lambda payload: True,
        timeout: float = 15.0,
    ) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                for kind, payload in self.events:
                    if kind == event_type and where(payload):
                        return payload
            time.sleep(0.02)
        raise AssertionError(f"event {event_type} not observed; saw {self.types()}")

    def types(self) -> list[str]:
        with self._lock:
            return [kind for kind, _ in self.events]


def _adapter(
    events: _EventLog,
    *,
    approval=lambda method, params: {"decision": "accept"},
    user_input=lambda params: {"answers": {}},
) -> CodexAppServerAdapter:
    adapter = CodexAppServerAdapter(
        on_event=events,
        approval_handler=approval,
        user_input_handler=user_input,
        command=[sys.executable, str(FAKE_SERVER)],
        request_timeout=15.0,
    )
    adapter.start()
    return adapter


def test_handshake_thread_and_streaming_turn() -> None:
    events = _EventLog()
    adapter = _adapter(events)
    try:
        assert adapter.server_info.get("userAgent") == "fake-codex/1.0"

        thread = adapter.thread_start(model="gpt-5.6-terra")
        assert thread["id"] == "thr_fake"
        events.wait_for("thread/started")

        turn = adapter.turn_start(thread["id"], "say hello")
        assert turn["status"] == "inProgress"
        events.wait_for("turn/completed")

        # Streaming deltas were coalesced into counters, never forwarded.
        assert "item/agentMessage/delta" not in events.types()
        counters = adapter.delta_counters(turn["id"])
        assert counters["item_msg"]["events"] == 3
        assert counters["item_msg"]["characters"] == len("Hello world")

        # Item lifecycle is forwarded as bounded summaries without content.
        completed = [
            payload
            for kind, payload in events.events
            if kind == "item/completed" and payload["item"].get("id") == "item_msg"
        ]
        assert completed and "text" not in completed[0]["item"]

        events.wait_for("thread/tokenUsage/updated")
    finally:
        adapter.stop()


def test_request_user_input_answers_are_delivered_and_fenced() -> None:
    events = _EventLog()
    seen_questions: list[dict] = []

    def _answer(params: dict) -> dict:
        seen_questions.append(params)
        return {"answers": {"q1": {"answers": ["left"]}}}

    adapter = _adapter(events, user_input=_answer)
    try:
        thread = adapter.thread_start()
        adapter.turn_start(thread["id"], "please ASK me")
        events.wait_for("turn/completed")

        assert seen_questions and seen_questions[0]["itemId"] == "item_ask"
        assert seen_questions[0]["isBlocking"] is True
        echoed = [
            payload
            for kind, payload in events.events
            if kind == "item/completed" and payload["item"].get("id") == "item_ask"
        ]
        assert echoed  # the fake only completes the item after a valid answer
        events.wait_for("serverRequest/resolved")
    finally:
        adapter.stop()


@pytest.mark.parametrize(
    ("decision", "expected_status"),
    [("accept", "completed"), ("decline", "declined")],
)
def test_command_approvals_control_item_outcome(decision, expected_status) -> None:
    events = _EventLog()
    seen: list[tuple[str, dict]] = []

    def _approve(method: str, params: dict) -> dict:
        seen.append((method, params))
        return {"decision": decision}

    adapter = _adapter(events, approval=_approve)
    try:
        thread = adapter.thread_start()
        adapter.turn_start(thread["id"], "APPROVE this")
        events.wait_for("turn/completed")

        method, params = seen[0]
        assert method == "item/commandExecution/requestApproval"
        assert params["itemId"] == "item_cmd"
        outcome = [
            payload["item"]["status"]
            for kind, payload in events.events
            if kind == "item/completed" and payload["item"].get("id") == "item_cmd"
        ]
        assert outcome == [expected_status]
    finally:
        adapter.stop()


def test_interrupt_yields_interrupted_terminal_turn() -> None:
    events = _EventLog()
    adapter = _adapter(events)
    try:
        thread = adapter.thread_start()
        turn = adapter.turn_start(thread["id"], "long work")
        adapter.turn_interrupt(thread["id"], turn["id"])
        payload = events.wait_for(
            "turn/completed",
            where=lambda item: item.get("turn", {}).get("status") == "interrupted",
        )
        assert payload["turn"]["status"] == "interrupted"
    finally:
        adapter.stop()


def test_malformed_stream_line_does_not_kill_the_channel() -> None:
    events = _EventLog()
    adapter = _adapter(events)
    try:
        thread = adapter.thread_start()
        adapter.turn_start(thread["id"], "GARBAGE then continue")
        events.wait_for("turn/completed")
    finally:
        adapter.stop()


def test_process_death_fails_pending_work_with_transport_error() -> None:
    events = _EventLog()
    adapter = _adapter(events)
    try:
        thread = adapter.thread_start()
        adapter.turn_start(thread["id"], "HANGUP now")
        events.wait_for("codex.process_closed")
        assert adapter.alive is False
        with pytest.raises(RpcTransportClosed):
            adapter.turn_start(thread["id"], "after death")
    finally:
        adapter.stop()


def test_steer_returns_active_turn_id() -> None:
    events = _EventLog()
    adapter = _adapter(events)
    try:
        thread = adapter.thread_start()
        turn = adapter.turn_start(thread["id"], "base work")
        steered = adapter.turn_steer(thread["id"], turn["id"], "focus on tests")
        assert steered == turn["id"]
    finally:
        adapter.stop()


def test_thread_resume_round_trip() -> None:
    events = _EventLog()
    adapter = _adapter(events)
    try:
        resumed = adapter.thread_resume("thr_previous")
        assert resumed["id"] == "thr_previous"
        assert resumed["resumed"] is True
    finally:
        adapter.stop()


def test_resolve_codex_command_disables_code_mode(monkeypatch) -> None:
    """Managed app-servers must not expose the code-mode shell bypass."""
    from collector.agents.codex_app_server import resolve_codex_command

    monkeypatch.setenv("MEMENTO_CODEX_COMMAND", "C:/fake/codex.exe")
    assert resolve_codex_command() == [
        "C:/fake/codex.exe",
        "app-server",
        "-c",
        "features.plugins=false",
        "-c",
        "features.default_mode_request_user_input=true",
    ]
