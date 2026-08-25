import logging
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from fastapi import HTTPException, Response
from starlette.requests import Request

from server.api.events import (
    EVENT_STREAM_COOKIE,
    create_event_session,
    event_stream,
)
from server.logging_filters import SensitiveQueryFilter, redact_sensitive_query_values
from server.middleware.auth import (
    create_access_token,
    create_event_stream_token,
    decode_event_stream_token,
)


def request(*, scheme: str = "https") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": scheme,
            "path": "/api/events/session",
            "raw_path": b"/api/events/session",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("memento.test", 443),
        }
    )


class EventStreamAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_cookie_is_short_lived_scoped_and_httponly(self) -> None:
        response = Response()
        user_id = uuid.uuid4()

        result = await create_event_session(
            request=request(),
            response=response,
            user=SimpleNamespace(id=user_id),
        )

        self.assertTrue(result["ok"])
        # The scoped token is also returned in the body so an embedded webview,
        # which cannot send the SameSite=lax cookie cross-site, can hand it back
        # as the stream's ``token`` query param.
        self.assertEqual(
            decode_event_stream_token(result["stream_token"])["sub"], str(user_id)
        )
        cookie = response.headers["set-cookie"]
        self.assertIn(f"{EVENT_STREAM_COOKIE}=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Max-Age=900", cookie)
        self.assertIn("Path=/api/events", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertIn("Secure", cookie)

    async def test_stream_accepts_scoped_credential_by_cookie_or_query(self) -> None:
        user_id = str(uuid.uuid4())
        scoped = create_event_stream_token(user_id)

        # Same-origin browsers use the cookie; embedded webviews pass the SAME
        # scoped token as the query fallback. Both are accepted.
        for kwargs in ({"event_session": scoped, "token": None},
                       {"event_session": None, "token": scoped}):
            response = await event_stream(**kwargs)
            self.assertEqual(response.media_type, "text/event-stream")

        # The main access token is never a valid stream credential on either
        # channel — the query fallback must not accept a long-lived JWT.
        access = create_access_token(user_id, "owner")
        for kwargs in ({"event_session": access, "token": None},
                       {"event_session": None, "token": access},
                       {"event_session": None, "token": None}):
            with self.assertRaises(HTTPException) as raised:
                await event_stream(**kwargs)
            self.assertEqual(raised.exception.status_code, 401)

    async def test_stream_forwards_query_cursor_for_resumable_replay(self) -> None:
        user_id = str(uuid.uuid4())
        captured: list[tuple[str, str | None]] = []

        async def subscribed(current_user_id: str, cursor: str | None):
            captured.append((current_user_id, cursor))
            yield {
                "id": "43-0",
                "type": "file_synced",
                "data": {"document_id": "doc"},
                "timestamp": 1.0,
            }

        with patch("server.api.events.subscribe", new=subscribed):
            response = await event_stream(
                event_session=create_event_stream_token(user_id),
                token=None,
                last_event_id="41-0",
                cursor="42-0",
            )
            frame = await anext(response.body_iterator)

        self.assertEqual(captured, [(user_id, "42-0")])
        self.assertTrue(frame.startswith("id: 43-0\n"))


class SensitiveQueryFilterTests(unittest.TestCase):
    def test_redacts_credentials_but_preserves_other_query_values(self) -> None:
        path = "/api/events/stream?token=secret&cursor=12&code=oauth-secret"

        redacted = redact_sensitive_query_values(path)

        self.assertEqual(
            redacted,
            "/api/events/stream?token=[REDACTED]&cursor=12&code=[REDACTED]",
        )

    def test_uvicorn_access_args_are_sanitized_before_formatting(self) -> None:
        record = logging.LogRecord(
            "uvicorn.access",
            20,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            (
                "127.0.0.1:1234",
                "GET",
                "/api/events/stream?token=secret",
                "1.1",
                200,
            ),
            None,
        )

        self.assertTrue(SensitiveQueryFilter().filter(record))
        self.assertNotIn("secret", record.getMessage())
        self.assertIn("token=[REDACTED]", record.getMessage())
