"""Managed-session manager against the real adapter + scripted fake CLI.

Every test drives the same objects production uses — AgentSessionManager,
CodexAppServerAdapter, ControlEventSpool — with only the codex executable
swapped for the deterministic fake app-server.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from collector.agents.codex_app_server import CodexAdapterError, CodexAppServerAdapter
from collector.agents.control_event_spool import ControlEventSpool
from collector.agents.session_manager import (
    AGENT_COMMANDS,
    AgentSessionManager,
    _PendingInteraction,
)

FAKE_SERVER = Path(__file__).resolve().parent / "fake_codex_app_server.py"


def _manager(tmp_path: Path) -> tuple[AgentSessionManager, ControlEventSpool]:
    spool = ControlEventSpool(tmp_path / "events.jsonl", tmp_path / "state.json")
    config = SimpleNamespace(
        platform="TestOS",
        server=SimpleNamespace(url="https://memento.invalid", token="token"),
        device_id="device",
        device_name="device-name",
    )

    def factory(*, control_session_id: str, cwd: str | None) -> CodexAppServerAdapter:
        return CodexAppServerAdapter(
            on_event=lambda kind, payload: None,
            approval_handler=lambda method, params: {"decision": "decline"},
            user_input_handler=lambda params: {"answers": {}},
            command=[sys.executable, str(FAKE_SERVER)],
            cwd=cwd,
            request_timeout=15.0,
        )

    return AgentSessionManager(config, spool, adapter_factory=factory), spool


def _spool_events(spool: ControlEventSpool) -> list[dict]:
    if not spool.spool_path.exists():
        return []
    return [
        json.loads(line)
        for line in spool.spool_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _wait_for_event(spool: ControlEventSpool, event_type: str, *, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in _spool_events(spool):
            if event["event_type"] == event_type:
                return event
        time.sleep(0.02)
    raise AssertionError(
        f"{event_type} not spooled; saw {[e['event_type'] for e in _spool_events(spool)]}"
    )


def test_session_start_binds_native_thread_and_spools_lifecycle(tmp_path: Path) -> None:
    manager, spool = _manager(tmp_path)
    try:
        status, error_code, detail = manager.execute(
            "agent.session.start",
            {"control_session_id": "cs-1", "options": {"model": "gpt-5.6-terra"}},
        )
        assert (status, error_code) == ("completed", None)
        assert detail["native_thread_id"] == "thr_fake"
        _wait_for_event(spool, "adapter.session_started")
        bound = _wait_for_event(spool, "adapter.native_bound")
        assert bound["native_session_id"] == "thr_fake"

        # Idempotent replay of the same start reports the existing binding.
        status, _, detail = manager.execute(
            "agent.session.start", {"control_session_id": "cs-1"}
        )
        assert status == "completed"
        assert detail["already_started"] is True
    finally:
        manager.shutdown()


def test_send_and_completed_turn_lifecycle(tmp_path: Path) -> None:
    manager, spool = _manager(tmp_path)
    try:
        manager.execute("agent.session.start", {"control_session_id": "cs-2"})
        status, error_code, detail = manager.execute(
            "agent.turn.send", {"control_session_id": "cs-2", "text": "say hello"}
        )
        assert (status, error_code) == ("completed", None)
        assert detail["native_turn_id"].startswith("turn_")
        completed = _wait_for_event(spool, "adapter.turn_completed")
        assert completed["outcome"] == "completed"
        assert completed["native_turn_id"] == detail["native_turn_id"]
    finally:
        manager.shutdown()


def test_question_escalation_answer_round_trip(tmp_path: Path) -> None:
    manager, spool = _manager(tmp_path)
    try:
        manager.execute("agent.session.start", {"control_session_id": "cs-3"})
        manager.execute(
            "agent.turn.send", {"control_session_id": "cs-3", "text": "please ASK me"}
        )
        pending = _wait_for_event(spool, "adapter.interaction_pending")
        assert pending["details"]["kind"] == "question"
        assert pending["details"]["request"]["questions"][0]["id"] == "q1"
        interaction_id = pending["interaction_id"]

        status, error_code, detail = manager.execute(
            "agent.interaction.answer",
            {
                "control_session_id": "cs-3",
                "interaction_id": interaction_id,
                "answers": {"q1": {"answers": ["left"]}},
            },
        )
        assert (status, error_code) == ("completed", None)
        assert detail["resolved"] is True
        resolved = _wait_for_event(spool, "adapter.interaction_resolved")
        assert resolved["outcome"] == "answered"
        _wait_for_event(spool, "adapter.turn_completed")
    finally:
        manager.shutdown()


def test_stale_interaction_answer_is_fenced(tmp_path: Path) -> None:
    manager, _spool = _manager(tmp_path)
    try:
        manager.execute("agent.session.start", {"control_session_id": "cs-4"})
        status, error_code, detail = manager.execute(
            "agent.interaction.answer",
            {
                "control_session_id": "cs-4",
                "interaction_id": "never-issued",
                "answers": {},
            },
        )
        assert status == "failed"
        assert error_code == "admission.stale_turn"
        assert detail["reason"] == "interaction_not_pending"
    finally:
        manager.shutdown()


def test_approval_decline_round_trip(tmp_path: Path) -> None:
    manager, spool = _manager(tmp_path)
    try:
        manager.execute("agent.session.start", {"control_session_id": "cs-5"})
        manager.execute(
            "agent.turn.send", {"control_session_id": "cs-5", "text": "APPROVE this"}
        )
        pending = _wait_for_event(spool, "adapter.interaction_pending")
        assert pending["details"]["kind"] == "approval"

        status, error_code, _ = manager.execute(
            "agent.approval.respond",
            {
                "control_session_id": "cs-5",
                "interaction_id": pending["interaction_id"],
                "decision": "decline",
            },
        )
        assert (status, error_code) == ("completed", None)
        _wait_for_event(spool, "adapter.turn_completed")
    finally:
        manager.shutdown()


def test_permission_grants_selected_subset_only(tmp_path: Path) -> None:
    manager, spool = _manager(tmp_path)
    try:
        manager.execute("agent.session.start", {"control_session_id": "cs-perm"})
        manager.execute(
            "agent.turn.send", {"control_session_id": "cs-perm", "text": "PERMS please"}
        )
        pending = _wait_for_event(spool, "adapter.interaction_pending")
        assert pending["details"]["kind"] == "approval"
        assert pending["details"]["request"]["permissions"]["fileSystem"]["write"] == ["/a", "/b"]

        status, error_code, _ = manager.execute(
            "agent.approval.respond",
            {
                "control_session_id": "cs-perm",
                "interaction_id": pending["interaction_id"],
                "decision": "acceptForSession",
                # Anything omitted from the subset is denied.
                "granted_permissions": {"fileSystem": {"write": ["/a"]}},
            },
        )
        assert (status, error_code) == ("completed", None)
        _wait_for_event(spool, "adapter.turn_completed")
    finally:
        manager.shutdown()


def test_interrupt_and_close(tmp_path: Path) -> None:
    manager, spool = _manager(tmp_path)
    try:
        manager.execute("agent.session.start", {"control_session_id": "cs-6"})
        _, _, sent = manager.execute(
            "agent.turn.send", {"control_session_id": "cs-6", "text": "base work"}
        )
        status, error_code, detail = manager.execute(
            "agent.turn.interrupt",
            {"control_session_id": "cs-6", "turn_id": sent["native_turn_id"]},
        )
        assert (status, error_code) == ("completed", None)
        assert detail["interrupt_requested"] is True

        status, _, detail = manager.execute(
            "agent.session.close", {"control_session_id": "cs-6"}
        )
        assert (status, detail["closed"]) == ("completed", True)
        _wait_for_event(spool, "adapter.session_closed")

        status, _, detail = manager.execute(
            "agent.session.close", {"control_session_id": "cs-6"}
        )
        assert detail["already_closed"] is True
    finally:
        manager.shutdown()


def test_adapter_death_fails_session_and_later_commands(tmp_path: Path) -> None:
    manager, spool = _manager(tmp_path)
    try:
        manager.execute("agent.session.start", {"control_session_id": "cs-7"})
        manager.execute(
            "agent.turn.send", {"control_session_id": "cs-7", "text": "HANGUP now"}
        )
        failed = _wait_for_event(spool, "adapter.process_failed")
        assert failed["error_code"] == "adapter.process_failed"

        status, error_code, _ = manager.execute(
            "agent.turn.send", {"control_session_id": "cs-7", "text": "after death"}
        )
        assert status == "failed"
        assert error_code == "admission.unknown_command"
    finally:
        manager.shutdown()


def test_unknown_session_and_resume_round_trip(tmp_path: Path) -> None:
    manager, _spool = _manager(tmp_path)
    try:
        status, error_code, _ = manager.execute(
            "agent.turn.send", {"control_session_id": "cs-none", "text": "hi"}
        )
        assert (status, error_code) == ("failed", "admission.unknown_command")

        status, _, detail = manager.execute(
            "agent.session.resume",
            {"control_session_id": "cs-8", "native_session_id": "thr_previous"},
        )
        assert status == "completed"
        assert detail["native_thread_id"] == "thr_previous"
    finally:
        manager.shutdown()


def test_failed_thread_start_stops_and_forgets_partial_adapter(tmp_path: Path) -> None:
    spool = ControlEventSpool(tmp_path / "events.jsonl", tmp_path / "state.json")
    config = SimpleNamespace(
        platform="TestOS",
        server=SimpleNamespace(url="https://memento.invalid", token="token"),
        device_id="device",
        device_name="device-name",
    )

    class FailingAdapter:
        def __init__(self) -> None:
            self.alive = False
            self.stopped = False
            self.on_event = lambda kind, payload: None
            self.approval_handler = lambda method, params: {"decision": "decline"}
            self.user_input_handler = lambda params: {"answers": {}}

        def start(self) -> None:
            self.alive = True

        def stop(self) -> None:
            self.alive = False
            self.stopped = True

        def thread_start(self, **options: object) -> dict:
            raise CodexAdapterError("scripted thread/start failure")

    adapter = FailingAdapter()
    manager = AgentSessionManager(
        config,
        spool,
        adapter_factory=lambda **_kwargs: adapter,
    )
    try:
        status, error_code, detail = manager.execute(
            "agent.session.start", {"control_session_id": "cs-partial"}
        )
        assert (status, error_code) == ("failed", "adapter.process_failed")
        assert "thread/start failure" in detail["error"]
        assert adapter.stopped is True
        assert manager.session_snapshot() == []
    finally:
        manager.shutdown()


def test_approval_response_builds_exact_permission_subsets() -> None:
    pending = _PendingInteraction(
        interaction_id="i-1",
        kind="approval",
        method="item/permissions/requestApproval",
        native_turn_id="turn-1",
        params={"permissions": {"fileSystem": {"write": ["/a", "/b"]}}},
    )

    subset = AgentSessionManager._approval_response(
        pending,
        {
            "decision": "acceptForSession",
            "granted_permissions": {"fileSystem": {"write": ["/a"]}},
        },
    )
    assert subset == {
        "permissions": {"fileSystem": {"write": ["/a"]}},
        "scope": "session",
    }

    # Omitting the subset grants exactly what was requested.
    full = AgentSessionManager._approval_response(pending, {"decision": "accept"})
    assert full == {"permissions": {"fileSystem": {"write": ["/a", "/b"]}}}

    # Declining grants nothing regardless of any provided subset.
    declined = AgentSessionManager._approval_response(
        pending,
        {"decision": "decline", "granted_permissions": {"fileSystem": {"write": ["/a"]}}},
    )
    assert declined == {"permissions": {}}


def test_agent_capability_surface_is_stable() -> None:
    assert AGENT_COMMANDS == (
        "agent.session.start",
        "agent.session.resume",
        "agent.session.close",
        "agent.turn.send",
        "agent.turn.steer",
        "agent.turn.interrupt",
        "agent.interaction.answer",
        "agent.approval.respond",
    )
