"""On-demand request profiling.

Off by default. An operator enables it by setting BOTH
``MEMENTO_PROFILING_ENABLED=1`` and a ``MEMENTO_PROFILING_TOKEN`` secret, then
profiles any single request by adding ``?profile=1`` and the matching token
(header ``X-Memento-Profile-Token`` or query ``profile_token``). The response
is replaced with a pyinstrument HTML flame graph of that request — including
time spent across ``await`` points, since pyinstrument runs in async mode.

Security / overhead:
- When disabled (the default, and always in normal production) ``dispatch`` is
  a single boolean check then straight passthrough — no import, no timers.
- Even when enabled, a request is only profiled if it both asks for it AND
  presents the secret; a missing/incorrect token is treated exactly like a
  normal request (constant-time compare, no signal that profiling exists).
- The flame graph contains timings and code paths only, never request bodies
  or credentials.

For profiling background work (Celery ingest workers) rather than a request,
attach ``py-spy`` to the worker process instead — see docs/PROFILING.md.
"""

from __future__ import annotations

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from ..config import settings


def _wants_profile(request: Request) -> bool:
    return (
        "profile" in request.query_params
        or request.headers.get("x-memento-profile") is not None
    )


def _token_ok(request: Request) -> bool:
    expected = settings.profiling_token
    if not expected:
        return False
    provided = (
        request.headers.get("x-memento-profile-token")
        or request.query_params.get("profile_token")
        or ""
    )
    # Constant-time compare; both operands are str.
    return secrets.compare_digest(provided, expected)


class ProfilingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        # Fast path: disabled (default) or this request isn't asking to profile.
        if not settings.profiling_enabled or not _wants_profile(request):
            return await call_next(request)
        # Asked to profile but not authorized → behave like a normal request so
        # the feature is invisible to anyone without the secret.
        if not _token_ok(request):
            return await call_next(request)

        # Lazy import so there is no cost on the normal path.
        from pyinstrument import Profiler

        profiler = Profiler(async_mode="enabled")
        profiler.start()
        try:
            await call_next(request)
        finally:
            profiler.stop()
        return HTMLResponse(profiler.output_html())
