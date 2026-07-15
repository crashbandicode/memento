"""SSE endpoint — real-time event stream for the frontend dashboard."""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from ..db.models import User
from ..middleware.auth import (
    EVENT_STREAM_TOKEN_EXPIRE_MINUTES,
    create_event_stream_token,
    decode_event_stream_token,
    decode_token,
    get_current_user,
)
from ..services.sse_service import format_sse, subscribe

router = APIRouter(prefix="/api/events", tags=["events"])
EVENT_STREAM_COOKIE = "memento_event_session"
EVENT_STREAM_COOKIE_MAX_AGE = EVENT_STREAM_TOKEN_EXPIRE_MINUTES * 60


def _request_is_secure(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "")
    forwarded_scheme = forwarded.split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or forwarded_scheme == "https"


@router.post("/session")
async def create_event_session(
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
) -> dict[str, bool]:
    """Issue a short-lived HttpOnly cookie scoped to the SSE endpoints."""
    response.set_cookie(
        key=EVENT_STREAM_COOKIE,
        value=create_event_stream_token(str(user.id)),
        max_age=EVENT_STREAM_COOKIE_MAX_AGE,
        httponly=True,
        secure=_request_is_secure(request),
        samesite="lax",
        path="/api/events",
    )
    return {"ok": True}


@router.delete("/session")
async def clear_event_session(response: Response) -> dict[str, bool]:
    """Remove the browser's stream-only credential on explicit logout."""
    response.delete_cookie(
        key=EVENT_STREAM_COOKIE,
        path="/api/events",
        httponly=True,
        samesite="lax",
    )
    return {"ok": True}


@router.get("/stream")
async def event_stream(
    event_session: str | None = Cookie(None, alias=EVENT_STREAM_COOKIE),
    token: str | None = Query(None, deprecated=True),
) -> StreamingResponse:
    """Stream live updates using a scoped cookie.

    The optional query token is a redacted, temporary compatibility path for
    tabs opened before the cookie rollout. New clients never put JWTs in URLs.
    """
    if not event_session and not token:
        raise HTTPException(status_code=401, detail="Missing event stream session")
    try:
        payload = (
            decode_event_stream_token(event_session)
            if event_session
            else decode_token(token or "")
        )
    except HTTPException:
        raise HTTPException(status_code=401, detail="Invalid token")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    async def generate():
        async for event in subscribe(user_id):
            yield format_sse(event)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
