import logging
from types import SimpleNamespace
import unittest
import uuid

from fastapi import HTTPException, Response
from starlette.requests import Request

from server.api.events import (
    EVENT_STREAM_COOKIE,
    create_event_session,
    event_stream,
)
from server.logging_filters import SensitiveQueryFilter, redact_sensitive_query_values
from server.middleware.auth import create_access_token, create_event_stream_token


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

        result = await create_event_session(
            request=request(),
            response=response,
            user=SimpleNamespace(id=uuid.uuid4()),
        )

        self.assertEqual(result, {"ok": True})
        cookie = response.headers["set-cookie"]
        self.assertIn(f"{EVENT_STREAM_COOKIE}=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Max-Age=900", cookie)
        self.assertIn("Path=/api/events", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertIn("Secure", cookie)

    async def test_stream_accepts_only_scoped_cookie(self) -> None:
        user_id = str(uuid.uuid4())

        response = await event_stream(
            event_session=create_event_stream_token(user_id),
            token=None,
        )

        self.assertEqual(response.media_type, "text/event-stream")
        with self.assertRaises(HTTPException) as raised:
            await event_stream(
                event_session=create_access_token(user_id, "owner"),
                token=None,
            )
        self.assertEqual(raised.exception.status_code, 401)


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
