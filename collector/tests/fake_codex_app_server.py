"""Deterministic fake ``codex app-server`` for adapter contract tests.

Speaks the real wire dialect: JSON-RPC 2.0 over JSONL with the ``jsonrpc``
field omitted. Turn behavior is scripted by markers in the user input text:

- ``ASK``      → emits an ``item/tool/requestUserInput`` server request and
                 echoes the received answers into a completed item.
- ``APPROVE``  → emits an ``item/commandExecution/requestApproval`` request
                 and completes the item with the received decision.
- ``GARBAGE``  → writes a malformed line mid-stream before continuing.
- ``HANGUP``   → exits the process mid-turn without a terminal event.
- otherwise    → streams three agentMessage deltas and completes.
"""

from __future__ import annotations

import json
import sys
import threading

_write_lock = threading.Lock()
_pending_answers: dict[int, threading.Event] = {}
_answers: dict[int, dict] = {}
_next_server_request_id = 1000


def _send(message: dict) -> None:
    with _write_lock:
        sys.stdout.buffer.write((json.dumps(message) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()


def _send_raw(text: str) -> None:
    with _write_lock:
        sys.stdout.buffer.write((text + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()


def _server_request(method: str, params: dict) -> dict:
    global _next_server_request_id
    _next_server_request_id += 1
    request_id = _next_server_request_id
    event = threading.Event()
    _pending_answers[request_id] = event
    _send({"id": request_id, "method": method, "params": params})
    if not event.wait(30):
        raise TimeoutError(f"no client answer for {method}")
    _send(
        {
            "method": "serverRequest/resolved",
            "params": {"threadId": "thr_fake", "requestId": request_id},
        }
    )
    return _answers.pop(request_id)


def _run_turn(turn_id: str, text: str) -> None:
    _send({"method": "turn/started", "params": {"turn": {"id": turn_id, "status": "inProgress", "items": []}}})

    if "GARBAGE" in text:
        _send_raw("this is not json {{{")

    if "HANGUP" in text:
        # Hard process death mid-turn; sys.exit would only end this thread.
        import os

        os._exit(3)

    if "ASK" in text:
        answer = _server_request(
            "item/tool/requestUserInput",
            {
                "threadId": "thr_fake",
                "turnId": turn_id,
                "itemId": "item_ask",
                "isBlocking": True,
                "questions": [
                    {
                        "id": "q1",
                        "header": "Choice",
                        "question": "Which path?",
                        "options": [
                            {"label": "left", "description": "go left"},
                            {"label": "right", "description": "go right"},
                        ],
                        "isOther": False,
                        "isSecret": False,
                    }
                ],
            },
        )
        _send(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thr_fake",
                    "turnId": turn_id,
                    "item": {
                        "id": "item_ask",
                        "type": "toolCall",
                        "status": "completed",
                        "echoedAnswers": answer,
                    },
                },
            }
        )

    if "PERMS" in text:
        result = _server_request(
            "item/permissions/requestApproval",
            {
                "threadId": "thr_fake",
                "turnId": turn_id,
                "itemId": "item_perm",
                "environmentId": "local",
                "cwd": "/tmp",
                "reason": "Need write access",
                "permissions": {
                    "fileSystem": {"write": ["/a", "/b"]},
                    "network": {"enabled": True},
                },
            },
        )
        _send(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thr_fake",
                    "turnId": turn_id,
                    "item": {
                        "id": "item_perm",
                        "type": "toolCall",
                        "status": "completed",
                        "grantedResponse": result,
                    },
                },
            }
        )

    if "APPROVE" in text:
        decision = _server_request(
            "item/commandExecution/requestApproval",
            {
                "threadId": "thr_fake",
                "turnId": turn_id,
                "itemId": "item_cmd",
                "command": "rm -rf ./scratch",
                "cwd": "/tmp",
                "reason": "cleanup",
            },
        )
        status = "completed" if decision.get("decision") == "accept" else "declined"
        _send(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thr_fake",
                    "turnId": turn_id,
                    "item": {"id": "item_cmd", "type": "commandExecution", "status": status},
                },
            }
        )

    _send(
        {
            "method": "item/started",
            "params": {
                "threadId": "thr_fake",
                "turnId": turn_id,
                "item": {"id": "item_msg", "type": "agentMessage", "text": ""},
            },
        }
    )
    for chunk in ("Hel", "lo ", "world"):
        _send(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "thr_fake",
                    "turnId": turn_id,
                    "itemId": "item_msg",
                    "delta": chunk,
                },
            }
        )
    _send(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr_fake",
                "turnId": turn_id,
                "item": {"id": "item_msg", "type": "agentMessage", "text": "Hello world"},
            },
        }
    )
    _send(
        {
            "method": "thread/tokenUsage/updated",
            "params": {"threadId": "thr_fake", "tokenUsage": {"input": 12, "output": 3}},
        }
    )
    _send(
        {
            "method": "turn/completed",
            "params": {"turn": {"id": turn_id, "status": "completed", "items": []}},
        }
    )


def main() -> None:
    initialized = False
    turn_counter = 0
    for raw in sys.stdin.buffer:
        raw = raw.strip()
        if not raw:
            continue
        message = json.loads(raw)
        method = message.get("method")
        message_id = message.get("id")

        if method is None and message_id is not None:
            event = _pending_answers.pop(message_id, None)
            if event is not None:
                _answers[message_id] = message.get("result") or {}
                event.set()
            continue

        if method == "initialize":
            initialized = True
            _send(
                {
                    "id": message_id,
                    "result": {
                        "userAgent": "fake-codex/1.0",
                        "codexHome": "/fake/.codex",
                        "platformFamily": "fake",
                        "platformOs": "fake-os",
                    },
                }
            )
        elif method == "initialized":
            continue
        elif not initialized and message_id is not None:
            _send({"id": message_id, "error": {"code": -32600, "message": "Not initialized"}})
        elif method == "thread/start":
            _send({"id": message_id, "result": {"thread": {"id": "thr_fake", "preview": ""}}})
            _send({"method": "thread/started", "params": {"thread": {"id": "thr_fake"}}})
        elif method == "thread/resume":
            thread_id = (message.get("params") or {}).get("threadId")
            _send({"id": message_id, "result": {"thread": {"id": thread_id, "resumed": True}}})
        elif method == "turn/start":
            turn_counter += 1
            turn_id = f"turn_{turn_counter}"
            text = "".join(
                part.get("text", "")
                for part in (message.get("params") or {}).get("input", [])
            )
            _send({"id": message_id, "result": {"turn": {"id": turn_id, "status": "inProgress", "items": []}}})
            threading.Thread(target=_run_turn, args=(turn_id, text), daemon=True).start()
        elif method == "turn/steer":
            params = message.get("params") or {}
            _send({"id": message_id, "result": {"turnId": params.get("expectedTurnId")}})
        elif method == "turn/interrupt":
            params = message.get("params") or {}
            _send({"id": message_id, "result": {}})
            _send(
                {
                    "method": "turn/completed",
                    "params": {"turn": {"id": params.get("turnId"), "status": "interrupted", "items": []}},
                }
            )
        elif message_id is not None:
            _send({"id": message_id, "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
