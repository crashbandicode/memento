from __future__ import annotations

import threading
from types import SimpleNamespace

import httpx
import pytest

from collector.control_channel import (
    ControlChannel,
    UnsupportedServerError,
    capability_snapshot,
)


class _FakeClient:
    """Records requests and answers each path with a scripted response."""

    def __init__(self, responses: dict[str, list[tuple[int, dict]]]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict]] = []

    def post(self, path: str, *, json: dict) -> httpx.Response:
        self.requests.append((path, json))
        for prefix, scripted in self.responses.items():
            if path.startswith(prefix) and scripted:
                status_code, body = scripted.pop(0)
                request = httpx.Request("POST", f"https://memento.invalid{path}")
                return httpx.Response(status_code, request=request, json=body)
        raise AssertionError(f"unexpected request: {path}")


def _channel(client: _FakeClient, execute) -> ControlChannel:
    channel = object.__new__(ControlChannel)
    channel._client = client
    channel._execute = execute
    channel._wait_seconds = 0
    channel._lease_seconds = 300
    channel._max_commands = 4
    channel._version = "test"
    channel._stop = threading.Event()
    channel._config = SimpleNamespace(
        platform="TestOS",
        server=SimpleNamespace(url="https://memento.invalid", token="token"),
        device_id="device",
        device_name="device-name",
    )
    return channel


def _command(**overrides) -> dict:
    command = {
        "id": "6a2f7f0e-6f2b-4f2f-9d9a-1e2d3c4b5a69",
        "lease_id": "0e1d2c3b-4a59-4687-9daf-abcdefabcdef",
        "kind": "conversation.repair",
        "payload": {"paths": []},
    }
    command.update(overrides)
    return command


def test_command_is_not_executed_until_ack_succeeds() -> None:
    executed: list[str] = []
    client = _FakeClient(
        {
            "/api/control/poll": [(200, {"commands": [_command()]})],
            "/api/control/commands/": [(502, {})],
        }
    )
    channel = _channel(client, lambda kind, payload: executed.append(kind) or ("completed", None, {}))

    assert channel.poll_once() == 0
    assert executed == []
    # No completion may be reported for a command that was never acknowledged.
    assert all("/complete" not in path for path, _ in client.requests)


def test_successful_command_reports_fenced_outcome() -> None:
    command = _command()
    client = _FakeClient(
        {
            "/api/control/poll": [(200, {"commands": [command]})],
            f"/api/control/commands/{command['id']}/ack": [(200, {})],
            f"/api/control/commands/{command['id']}/complete": [(200, {})],
        }
    )
    channel = _channel(client, lambda kind, payload: ("completed", None, {"queued": 1}))

    assert channel.poll_once() == 1

    complete_path, body = client.requests[-1]
    assert complete_path == f"/api/control/commands/{command['id']}/complete"
    assert body["lease_id"] == command["lease_id"]
    assert body["status"] == "completed"
    assert body["detail"] == {"queued": 1}
    assert isinstance(body["elapsed_ms"], int)


def test_executor_exception_becomes_stable_failure_outcome() -> None:
    command = _command()
    client = _FakeClient(
        {
            "/api/control/poll": [(200, {"commands": [command]})],
            f"/api/control/commands/{command['id']}/ack": [(200, {})],
            f"/api/control/commands/{command['id']}/complete": [(200, {})],
        }
    )

    def _boom(kind: str, payload: dict):
        raise RuntimeError("watcher exploded")

    channel = _channel(client, _boom)
    assert channel.poll_once() == 1

    _, body = client.requests[-1]
    assert body["status"] == "failed"
    assert body["error_code"] == "command.execution_failed"
    assert "watcher exploded" in body["detail"]["error"]


def test_superseded_outcome_stops_retrying() -> None:
    command = _command()
    client = _FakeClient(
        {
            "/api/control/poll": [(200, {"commands": [command]})],
            f"/api/control/commands/{command['id']}/ack": [(200, {})],
            f"/api/control/commands/{command['id']}/complete": [(409, {})],
        }
    )
    channel = _channel(client, lambda kind, payload: ("completed", None, {}))

    assert channel.poll_once() == 1
    complete_requests = [
        path for path, _ in client.requests if path.endswith("/complete")
    ]
    # First-writer-wins on the server: exactly one report, no retries.
    assert len(complete_requests) == 1


def test_poll_carries_capability_snapshot() -> None:
    client = _FakeClient({"/api/control/poll": [(200, {"commands": []})]})
    channel = _channel(client, lambda kind, payload: ("completed", None, {}))

    channel.poll_once()

    _, body = client.requests[0]
    capabilities = body["capabilities"]
    assert capabilities["schema_version"] == 1
    assert capabilities["control"]["long_poll"] is True
    assert capabilities["control"]["outcome_reporting"] is True
    assert "device.resync" in capabilities["control"]["commands"]
    assert "conversation.repair" in capabilities["control"]["commands"]


def test_pre_rollout_server_is_reported_distinctly() -> None:
    client = _FakeClient({"/api/control/poll": [(404, {})]})
    channel = _channel(client, lambda kind, payload: ("completed", None, {}))

    with pytest.raises(UnsupportedServerError):
        channel.poll_once()


def test_capability_snapshot_is_bounded_and_versioned() -> None:
    config = SimpleNamespace(platform="Windows")
    snapshot = capability_snapshot(config)
    assert snapshot["schema_version"] == 1
    assert snapshot["platform"] == "Windows"
    assert snapshot["agents"] == {}


def test_run_loop_survives_unexpected_exceptions(monkeypatch) -> None:
    """A dead channel thread silently kills the machine's heartbeat.

    Any exception a poll cycle can raise — not just transport errors — must
    leave the loop running (observed live 2026-08-22: a stalled heartbeat is
    indistinguishable from a healthy idle collector server-side).
    """
    import collector.control_channel as module

    monkeypatch.setattr(module, "_ERROR_BACKOFF_INITIAL", 0.01)
    monkeypatch.setattr(module, "_ERROR_BACKOFF_MAX", 0.02)

    calls: list[int] = []

    class _ExplodingClient:
        def post(self, path: str, *, json: dict):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("malformed body / unexpected bug")
            channel._stop.set()
            request = httpx.Request("POST", f"https://memento.invalid{path}")
            return httpx.Response(200, request=request, json={"commands": []})

    channel = _channel(_ExplodingClient(), lambda kind, payload: ("completed", None, {}))

    channel._run()  # must return via stop event, not raise

    assert len(calls) >= 2  # survived the RuntimeError and polled again
