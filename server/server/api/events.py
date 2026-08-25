"""SSE endpoint — real-time event stream for the frontend dashboard."""

from __future__ import annotations

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import StreamingResponse

from ..db.models import User
from ..middleware.auth import (
    EVENT_STREAM_TOKEN_EXPIRE_MINUTES,
    create_event_stream_token,
    decode_event_stream_token,
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
) -> dict:
    """Issue a short-lived credential scoped to the SSE endpoints.

    One token is delivered two ways: an HttpOnly cookie for same-origin
    browsers, and the same value in the response body so embedded webviews (the
    desktop app's cross-origin iframe, where a SameSite=lax cookie is not sent
    on the stream request) can pass it back as the ``token`` query param. The
    credential is events-scoped and expires in 15 minutes, so exposing it to
    the already-authenticated client is a bounded, low-value tradeoff.
    """
    stream_token = create_event_stream_token(str(user.id))
    response.set_cookie(
        key=EVENT_STREAM_COOKIE,
        value=stream_token,
        max_age=EVENT_STREAM_COOKIE_MAX_AGE,
        httponly=True,
        secure=_request_is_secure(request),
        samesite="lax",
        path="/api/events",
    )
    return {"ok": True, "stream_token": stream_token}


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
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    cursor: str | None = Query(None),
) -> StreamingResponse:
    """Stream live updates using an events-scoped credential.

    Same-origin browsers authenticate with the HttpOnly cookie. Embedded
    webviews (the desktop app's cross-origin iframe) can't send a SameSite=lax
    cookie on this request, so they pass the same scoped, short-lived token as
    the ``token`` query param instead — it is not the main JWT and is redacted
    from logs.
    """
    credential = event_session or token
    if not credential:
        raise HTTPException(status_code=401, detail="Missing event stream session")
    try:
        payload = decode_event_stream_token(credential)
    except HTTPException:
        raise HTTPException(status_code=401, detail="Invalid token")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    resume_id = cursor if isinstance(cursor, str) else None
    if resume_id is None and isinstance(last_event_id, str):
        resume_id = last_event_id

    async def generate():
        async for event in subscribe(user_id, resume_id):
            yield format_sse(event)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
