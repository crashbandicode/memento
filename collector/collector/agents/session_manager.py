"""Managed agent sessions: one adapter child process per live session.

Routes ``agent.*`` control commands to the right adapter, escalates native
approval/question requests to Memento as durable pending interactions, and
reports lifecycle evidence through the control event spool. Sessions exist
only while Memento manages them — nothing here touches view-only
transcripts, and adapter processes die with their session.

Fencing mirrors the server: every command carries ``control_session_id``;
turn-scoped commands carry the exact native turn id; answers carry the exact
``interaction_id`` minted when the question was escalated. A stale fence is
an explicit failure (`admission.stale_turn`), never a best-effort guess.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import CollectorConfig
from .codex_app_server import CodexAdapterError, CodexAppServerAdapter, resolve_codex_command
from .control_event_spool import ControlEventSpool
from .jsonl_rpc import RpcResponseError, RpcTransportClosed

logger = logging.getLogger("collector.agents.sessions")

ADAPTER_CODEX = "codex_app_server"

# Answer wait ceiling. Codex marks questions blocking; a day-old unanswered
# question is abandoned honestly rather than parked forever.
_INTERACTION_WAIT_SECONDS = 24 * 3600

AGENT_COMMANDS = (
    "agent.session.start",
    "agent.session.resume",
    "agent.session.close",
    "agent.turn.send",
    "agent.turn.steer",
    "agent.turn.interrupt",
    "agent.interaction.answer",
    "agent.approval.respond",
)

CommandOutcome = tuple[str, str | None, dict]


class _InteractionCancelled(Exception):
    pass


@dataclass
class _PendingInteraction:
    interaction_id: str
    kind: str  # "question" | "approval"
    method: str
    native_turn_id: str
    params: dict
    event: threading.Event = field(default_factory=threading.Event)
    response: dict | None = None
    cancelled: bool = False


@dataclass
class ManagedSession:
    control_session_id: str
    adapter: CodexAppServerAdapter
    native_thread_id: str | None = None
    active_turn_id: str | None = None
    state: str = "starting"
    pending: dict[str, _PendingInteraction] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


AdapterFactory = Callable[..., CodexAppServerAdapter]


class AgentSessionManager:
    def __init__(
        self,
        config: CollectorConfig,
        spool: ControlEventSpool,
        *,
        adapter_factory: AdapterFactory | None = None,
        client_version: str = "dev",
    ) -> None:
        self._config = config
        self._spool = spool
        self._adapter_factory = adapter_factory or self._default_adapter_factory
        self._client_version = client_version
        self._sessions: dict[str, ManagedSession] = {}
        self._lock = threading.Lock()

    # -- capabilities ------------------------------------------------------------

    def codex_available(self) -> bool:
        try:
            resolve_codex_command()
            return True
        except CodexAdapterError:
            return False

    def agents_capabilities(self) -> dict:
        return {
            "codex": {
                "adapter": ADAPTER_CODEX,
                "available": self.codex_available(),
                "commands": list(AGENT_COMMANDS),
            }
        }

    def supported_commands(self) -> list[str]:
        return list(AGENT_COMMANDS) if self.codex_available() else []

    # -- lifecycle -----------------------------------------------------------------

    def shutdown(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            self._cancel_pendings(session)
            try:
                session.adapter.stop()
            except Exception:
                logger.exception("Adapter stop failed for %s", session.control_session_id)

    # -- command surface --------------------------------------------------------------

    def execute(self, kind: str, payload: dict) -> CommandOutcome:
        try:
            if kind == "agent.session.start":
                return self._session_start(payload)
            if kind == "agent.session.resume":
                return self._session_resume(payload)
            if kind == "agent.session.close":
                return self._session_close(payload)
            session = self._require_session(payload)
            if kind == "agent.turn.send":
                return self._turn_send(session, payload)
            if kind == "agent.turn.steer":
                return self._turn_steer(session, payload)
            if kind == "agent.turn.interrupt":
                return self._turn_interrupt(session, payload)
            if kind == "agent.interaction.answer":
                return self._resolve_interaction(session, payload, expected_kind="question")
            if kind == "agent.approval.respond":
                return self._resolve_interaction(session, payload, expected_kind="approval")
            return "failed", "capability.unsupported", {"kind": kind}
        except _UnknownSession as exc:
            return "failed", "admission.unknown_command", {"reason": str(exc)}
        except RpcTransportClosed as exc:
            return "failed", "adapter.process_failed", {"error": str(exc)[:256]}
        except RpcResponseError as exc:
            code = "admission.stale_turn" if exc.code == -32600 else "agent.request_rejected"
            return "failed", code, {"rpc_code": exc.code, "message": exc.message[:256]}
        except TimeoutError as exc:
            return "failed", "agent.timeout", {"error": str(exc)[:256]}
        except CodexAdapterError as exc:
            return "failed", "adapter.process_failed", {"error": str(exc)[:256]}

    # -- handlers -----------------------------------------------------------------------

    def _session_start(self, payload: dict) -> CommandOutcome:
        control_session_id = _required(payload, "control_session_id")
        options = dict(payload.get("options") or {})
        with self._lock:
            if control_session_id in self._sessions:
                session = self._sessions[control_session_id]
                # Idempotent replay: report the existing binding.
                return "completed", None, {
                    "native_thread_id": session.native_thread_id,
                    "already_started": True,
                }
        adapter = self._adapter_factory(
            control_session_id=control_session_id,
            cwd=options.get("cwd"),
        )
        session = ManagedSession(control_session_id=control_session_id, adapter=adapter)
        self._bind_adapter(session)
        try:
            adapter.start()
            with self._lock:
                self._sessions[control_session_id] = session
            self._spool.emit(
                "adapter.session_started",
                control_session_id=control_session_id,
                adapter=ADAPTER_CODEX,
                details={"cwd": options.get("cwd")},
            )
            thread_options: dict[str, Any] = {}
            for key, wire in (
                ("cwd", "cwd"),
                ("model", "model"),
                ("approval_policy", "approvalPolicy"),
                ("sandbox", "sandbox"),
                ("personality", "personality"),
            ):
                if options.get(key) is not None:
                    thread_options[wire] = options[key]
            thread = session.adapter.thread_start(**thread_options)
            session.native_thread_id = str(thread.get("id") or "")
            session.state = "active"
            self._spool.emit(
                "adapter.native_bound",
                control_session_id=control_session_id,
                native_session_id=session.native_thread_id,
                adapter=ADAPTER_CODEX,
            )
            detail: dict = {"native_thread_id": session.native_thread_id}
            initial = payload.get("initial_message")
            if initial:
                turn = session.adapter.turn_start(
                    session.native_thread_id,
                    str(initial),
                    client_message_id=payload.get("client_message_id"),
                    model=options.get("model"),
                    effort=options.get("effort"),
                )
                session.active_turn_id = str(turn.get("id") or "")
                detail["native_turn_id"] = session.active_turn_id
            return "completed", None, detail
        except Exception:
            self._abort_startup(session)
            raise

    def _session_resume(self, payload: dict) -> CommandOutcome:
        control_session_id = _required(payload, "control_session_id")
        native_session_id = _required(payload, "native_session_id")
        with self._lock:
            existing = self._sessions.get(control_session_id)
        if existing is not None and existing.adapter.alive:
            return "completed", None, {
                "native_thread_id": existing.native_thread_id,
                "already_started": True,
            }
        adapter = self._adapter_factory(
            control_session_id=control_session_id,
            cwd=(payload.get("options") or {}).get("cwd"),
        )
        session = ManagedSession(control_session_id=control_session_id, adapter=adapter)
        self._bind_adapter(session)
        try:
            adapter.start()
            with self._lock:
                self._sessions[control_session_id] = session
            thread = session.adapter.thread_resume(native_session_id)
            session.native_thread_id = str(thread.get("id") or native_session_id)
            session.state = "active"
            self._spool.emit(
                "adapter.session_resumed",
                control_session_id=control_session_id,
                native_session_id=session.native_thread_id,
                adapter=ADAPTER_CODEX,
            )
            return "completed", None, {"native_thread_id": session.native_thread_id}
        except Exception:
            self._abort_startup(session)
            raise

    def _abort_startup(self, session: ManagedSession) -> None:
        """Remove and stop a partially initialized adapter without leaking it."""
        with self._lock:
            if self._sessions.get(session.control_session_id) is session:
                self._sessions.pop(session.control_session_id, None)
        self._cancel_pendings(session)
        session.state = "failed"
        try:
            session.adapter.stop()
        except Exception:
            logger.exception(
                "Adapter cleanup failed for %s", session.control_session_id
            )

    def _session_close(self, payload: dict) -> CommandOutcome:
        control_session_id = _required(payload, "control_session_id")
        with self._lock:
            session = self._sessions.pop(control_session_id, None)
        if session is None:
            return "completed", None, {"already_closed": True}
        self._cancel_pendings(session)
        session.state = "closed"
        try:
            session.adapter.stop()
        finally:
            self._spool.emit(
                "adapter.session_closed",
                control_session_id=control_session_id,
                native_session_id=session.native_thread_id,
            )
        return "completed", None, {"closed": True}

    def _turn_send(self, session: ManagedSession, payload: dict) -> CommandOutcome:
        turn = session.adapter.turn_start(
            _required_native_thread(session),
            _required(payload, "text"),
            client_message_id=payload.get("client_message_id"),
            model=payload.get("model"),
            effort=payload.get("effort"),
        )
        session.active_turn_id = str(turn.get("id") or "")
        return "completed", None, {"native_turn_id": session.active_turn_id}

    def _turn_steer(self, session: ManagedSession, payload: dict) -> CommandOutcome:
        expected = _required(payload, "expected_turn_id")
        turn_id = session.adapter.turn_steer(
            _required_native_thread(session),
            expected,
            _required(payload, "text"),
            client_message_id=payload.get("client_message_id"),
        )
        return "completed", None, {"native_turn_id": turn_id}

    def _turn_interrupt(self, session: ManagedSession, payload: dict) -> CommandOutcome:
        turn_id = str(payload.get("turn_id") or session.active_turn_id or "")
        if not turn_id:
            return "failed", "admission.stale_turn", {"reason": "no_active_turn"}
        session.adapter.turn_interrupt(_required_native_thread(session), turn_id)
        return "completed", None, {"interrupt_requested": True, "native_turn_id": turn_id}

    def _resolve_interaction(
        self, session: ManagedSession, payload: dict, *, expected_kind: str
    ) -> CommandOutcome:
        interaction_id = _required(payload, "interaction_id")
        with session.lock:
            pending = session.pending.get(interaction_id)
            if pending is None or pending.kind != expected_kind:
                return "failed", "admission.stale_turn", {
                    "reason": "interaction_not_pending",
                    "interaction_id": interaction_id,
                }
            if expected_kind == "question":
                pending.response = {"answers": payload.get("answers") or {}}
            else:
                pending.response = self._approval_response(pending, payload)
            pending.event.set()
        return "completed", None, {"resolved": True, "interaction_id": interaction_id}

    @staticmethod
    def _approval_response(pending: _PendingInteraction, payload: dict) -> dict:
        decision = str(payload.get("decision") or "decline")
        if pending.method == "item/permissions/requestApproval":
            granted: dict = {}
            if decision in ("accept", "acceptForSession"):
                # A user-selected subset wins; omitting one means grant the
                # requested profile. Codex ignores anything not requested, so
                # pass-through of the subset is safe by protocol contract.
                subset = payload.get("granted_permissions")
                granted = (
                    subset
                    if isinstance(subset, dict)
                    else (pending.params.get("permissions") or {})
                )
            response: dict = {"permissions": granted}
            if decision == "acceptForSession":
                response["scope"] = "session"
            return response
        return {"decision": decision}

    # -- adapter escalation ---------------------------------------------------------------

    def _default_adapter_factory(
        self, *, control_session_id: str, cwd: str | None
    ) -> CodexAppServerAdapter:
        # Handlers are bound after construction via _bind_adapter.
        return CodexAppServerAdapter(
            on_event=lambda kind, payload: None,
            approval_handler=lambda method, params: {"decision": "decline"},
            user_input_handler=lambda params: {"answers": {}},
            cwd=cwd,
            client_version=self._client_version,
        )

    def _bind_adapter(self, session: ManagedSession) -> None:
        adapter = session.adapter
        adapter.on_event = lambda kind, payload: self._on_adapter_event(session, kind, payload)
        adapter.approval_handler = lambda method, params: self._escalate(
            session, "approval", method, params
        )
        adapter.user_input_handler = lambda params: self._escalate(
            session, "question", "item/tool/requestUserInput", params
        )

    def _escalate(self, session: ManagedSession, kind: str, method: str, params: dict) -> dict:
        interaction_id = str(uuid.uuid4())
        pending = _PendingInteraction(
            interaction_id=interaction_id,
            kind=kind,
            method=method,
            native_turn_id=str(params.get("turnId") or ""),
            params=params,
        )
        with session.lock:
            session.pending[interaction_id] = pending
        self._spool.emit(
            "adapter.interaction_pending",
            control_session_id=session.control_session_id,
            native_session_id=session.native_thread_id,
            native_turn_id=pending.native_turn_id,
            interaction_id=interaction_id,
            adapter=ADAPTER_CODEX,
            details={"kind": kind, "method": method, "request": params},
        )
        try:
            if not pending.event.wait(_INTERACTION_WAIT_SECONDS):
                raise TimeoutError("interaction answer wait expired")
            if pending.cancelled or pending.response is None:
                raise _InteractionCancelled(interaction_id)
            return pending.response
        finally:
            with session.lock:
                session.pending.pop(interaction_id, None)
            self._spool.emit(
                "adapter.interaction_resolved",
                control_session_id=session.control_session_id,
                native_turn_id=pending.native_turn_id,
                interaction_id=interaction_id,
                outcome="cancelled" if pending.cancelled or pending.response is None else "answered",
            )

    def _on_adapter_event(self, session: ManagedSession, kind: str, payload: dict) -> None:
        if kind == "turn/started":
            turn = payload.get("turn") or {}
            session.active_turn_id = str(turn.get("id") or session.active_turn_id or "")
            self._spool.emit(
                "adapter.turn_started",
                control_session_id=session.control_session_id,
                native_session_id=session.native_thread_id,
                native_turn_id=session.active_turn_id,
            )
            return
        if kind == "turn/completed":
            turn = payload.get("turn") or {}
            turn_id = str(turn.get("id") or "")
            status = str(turn.get("status") or "unknown")
            if session.active_turn_id == turn_id:
                session.active_turn_id = None
            self._cancel_pendings(session, turn_id=turn_id or None)
            self._spool.emit(
                "adapter.turn_completed",
                control_session_id=session.control_session_id,
                native_session_id=session.native_thread_id,
                native_turn_id=turn_id,
                outcome=status,
            )
            return
        if kind == "error":
            self._spool.emit(
                "adapter.native_error",
                control_session_id=session.control_session_id,
                native_turn_id=session.active_turn_id,
                error_code="agent.request_rejected",
                details={"error": payload.get("error")},
            )
            return
        if kind == "codex.process_closed":
            session.state = "failed"
            self._cancel_pendings(session)
            with self._lock:
                self._sessions.pop(session.control_session_id, None)
            self._spool.emit(
                "adapter.process_failed",
                control_session_id=session.control_session_id,
                native_session_id=session.native_thread_id,
                error_code="adapter.process_failed",
                details={"exit_code": payload.get("exit_code")},
            )
            return

    def _cancel_pendings(self, session: ManagedSession, *, turn_id: str | None = None) -> None:
        with session.lock:
            targets = [
                pending
                for pending in session.pending.values()
                if turn_id is None or pending.native_turn_id == turn_id
            ]
            for pending in targets:
                pending.cancelled = True
                pending.event.set()

    def _require_session(self, payload: dict) -> ManagedSession:
        control_session_id = _required(payload, "control_session_id")
        with self._lock:
            session = self._sessions.get(control_session_id)
        if session is None:
            raise _UnknownSession(f"no managed session {control_session_id}")
        return session

    def session_snapshot(self) -> list[dict]:
        """Bounded local view for diagnostics/capability reporting."""
        with self._lock:
            sessions = list(self._sessions.values())
        return [
            {
                "control_session_id": s.control_session_id,
                "native_thread_id": s.native_thread_id,
                "state": s.state,
                "active_turn_id": s.active_turn_id,
                "pending_interactions": len(s.pending),
                "adapter_alive": s.adapter.alive,
            }
            for s in sessions
        ]


class _UnknownSession(Exception):
    pass


def _required(payload: dict, key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise _UnknownSession(f"missing required field {key}")
    return value


def _required_native_thread(session: ManagedSession) -> str:
    if not session.native_thread_id:
        raise _UnknownSession("session has no native thread bound")
    return session.native_thread_id
