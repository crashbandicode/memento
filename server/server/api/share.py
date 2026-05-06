"""Public share links — project timelines and daily reports.

Three audiences on three endpoint groups:
  - Authenticated owner:
      POST   /api/share              create a share for a timeline/daily
      GET    /api/share              list my shares with view counts
      GET    /api/share/{token}/views  detailed access log
      DELETE /api/share/{token}      revoke
  - Anonymous visitor (what the /s/<token> public page calls):
      GET    /api/share/public/{token}        metadata + records a view
      GET    /api/share/public/{token}/data   actual content (read-only)

The public endpoints intentionally do NOT go through get_current_user so a
link recipient without a Memento account can open the shared view. The
tradeoff is they see whatever the owner put in the target (conversations,
artifacts, etc.) — the owner is responsible for not sharing anything with
secrets. We don't run the sanitizer on the public side per product call.
"""

from __future__ import annotations

import base64
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ShareLink, ShareView, User
from ..db.session import get_db
from ..middleware.auth import get_current_user
from ..services.geoip import lookup as geoip_lookup

router = APIRouter(prefix="/api/share", tags=["share"])


def _gen_token() -> str:
    """24 bytes → 40-char unpadded base32, URL-safe, copy-pasteable."""
    raw = secrets.token_bytes(24)
    return base64.b32encode(raw).decode("ascii").rstrip("=").lower()


# ---------------------------------------------------------------------------
# Owner-side (requires auth)
# ---------------------------------------------------------------------------
class CreateShareBody(BaseModel):
    kind: str           # "timeline" | "daily"
    target_id: str      # project uuid string OR YYYY-MM-DD
    title: str | None = None
    expires_in_days: int | None = None   # None = never


@router.post("")
async def create_share(
    body: CreateShareBody,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    if body.kind not in ("timeline", "daily"):
        raise HTTPException(status_code=400, detail="kind must be 'timeline' or 'daily'")
    # Validate target_id shape per kind so we don't persist garbage.
    if body.kind == "timeline":
        try:
            uuid.UUID(body.target_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="timeline target_id must be a project UUID")
    else:  # daily
        try:
            datetime.strptime(body.target_id, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="daily target_id must be YYYY-MM-DD")

    expires_at = None
    if body.expires_in_days and body.expires_in_days > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)

    # Collision-retry generation — 40 char b32 collisions are astronomically
    # unlikely, but cheap to guard against.
    for _ in range(5):
        token = _gen_token()
        existing = (await db.execute(
            select(ShareLink.id).where(ShareLink.token == token).limit(1)
        )).scalar_one_or_none()
        if not existing:
            break
    else:
        raise HTTPException(status_code=500, detail="token generation failed")

    link = ShareLink(
        token=token,
        kind=body.kind,
        target_id=body.target_id,
        owner_user_id=_user.id,
        title=body.title,
        expires_at=expires_at,
    )
    db.add(link)
    await db.commit()
    await db.refresh(link)

    return {
        "token": token,
        "kind": link.kind,
        "target_id": link.target_id,
        "title": link.title,
        "expires_at": link.expires_at.isoformat() if link.expires_at else None,
        "created_at": link.created_at.isoformat(),
    }


@router.get("")
async def list_my_shares(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[dict]:
    rows = (await db.execute(
        select(
            ShareLink,
            func.count(ShareView.id).label("view_count"),
        )
        .outerjoin(ShareView, ShareView.share_id == ShareLink.id)
        .where(ShareLink.owner_user_id == _user.id)
        .group_by(ShareLink.id)
        .order_by(desc(ShareLink.created_at))
    )).all()
    return [
        {
            "token": link.token,
            "kind": link.kind,
            "target_id": link.target_id,
            "title": link.title,
            "expires_at": link.expires_at.isoformat() if link.expires_at else None,
            "revoked_at": link.revoked_at.isoformat() if link.revoked_at else None,
            "created_at": link.created_at.isoformat(),
            "view_count": int(vc or 0),
        }
        for link, vc in rows
    ]


@router.get("/{token}/views")
async def list_share_views(
    token: str,
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[dict]:
    link = (await db.execute(
        select(ShareLink).where(ShareLink.token == token)
    )).scalar_one_or_none()
    if not link or link.owner_user_id != _user.id:
        raise HTTPException(status_code=404)

    rows = (await db.execute(
        select(ShareView)
        .where(ShareView.share_id == link.id)
        .order_by(desc(ShareView.viewed_at))
        .limit(limit)
    )).scalars().all()
    return [
        {
            "id": v.id,
            "ip": str(v.ip) if v.ip else None,
            "country": v.country,
            "region": v.region,
            "city": v.city,
            "user_agent": (v.user_agent or "")[:400],
            "referer": v.referer,
            "viewed_at": v.viewed_at.isoformat(),
        }
        for v in rows
    ]


@router.delete("/{token}")
async def revoke_share(
    token: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    link = (await db.execute(
        select(ShareLink).where(ShareLink.token == token)
    )).scalar_one_or_none()
    if not link or link.owner_user_id != _user.id:
        raise HTTPException(status_code=404)
    link.revoked_at = datetime.now(timezone.utc)
    await db.commit()
    return {"status": "revoked"}


# ---------------------------------------------------------------------------
# Visitor-side (NO auth)
# ---------------------------------------------------------------------------
def _client_ip(request: Request) -> str | None:
    """Prefer X-Forwarded-For from our nginx; fall back to socket peer."""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    xr = request.headers.get("X-Real-IP")
    if xr:
        return xr.strip()
    return request.client.host if request.client else None


async def _load_active_share(db: AsyncSession, token: str) -> ShareLink:
    link = (await db.execute(
        select(ShareLink).where(ShareLink.token == token)
    )).scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=404)
    if link.revoked_at is not None:
        raise HTTPException(status_code=410, detail="revoked")
    if link.expires_at and link.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="expired")
    return link


async def _record_view(db: AsyncSession, link: ShareLink, request: Request) -> None:
    ip = _client_ip(request)
    geo = geoip_lookup(ip) if ip else {}
    ua = request.headers.get("User-Agent")
    referer = request.headers.get("Referer")
    db.add(ShareView(
        share_id=link.id,
        ip=ip,
        country=geo.get("country"),
        region=geo.get("region"),
        city=geo.get("city"),
        user_agent=ua,
        referer=referer,
    ))
    await db.commit()


@router.get("/public/{token}")
async def get_public_share(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Meta + owner display name. Also records one view row per call — the
    frontend fires this on mount and then loads /data in a second request;
    we count the meta call so a visitor who opens and backs out still shows
    up in the access log."""
    link = await _load_active_share(db, token)
    owner = (await db.execute(
        select(User).where(User.id == link.owner_user_id)
    )).scalar_one_or_none()

    await _record_view(db, link, request)

    view_count = (await db.execute(
        select(func.count()).select_from(ShareView).where(ShareView.share_id == link.id)
    )).scalar() or 0

    return {
        "kind": link.kind,
        "target_id": link.target_id,
        "title": link.title,
        "owner_name": (owner.name if owner and owner.name else (owner.email.split("@")[0] if owner else "")),
        "expires_at": link.expires_at.isoformat() if link.expires_at else None,
        "created_at": link.created_at.isoformat(),
        "view_count": int(view_count),
    }


@router.get("/public/{token}/data")
async def get_public_share_data(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return the actual target data from the owner's perspective.

    We reuse the existing project/daily service functions but bypass the
    user_filter by synthesizing a minimal "impersonation" — only the
    specifically shared target is returned.
    """
    link = await _load_active_share(db, token)

    if link.kind == "timeline":
        # Delegate to get_project_conversations with owner identity + small
        # max_messages_per_session to keep payload manageable.
        owner = (await db.execute(
            select(User).where(User.id == link.owner_user_id)
        )).scalar_one_or_none()
        if not owner:
            raise HTTPException(status_code=404)
        from .projects import get_project_conversations
        try:
            data = await get_project_conversations(
                project_id=uuid.UUID(link.target_id),
                session_offset=0,
                session_limit=10,
                max_messages_per_session=80,
                order="asc",
                db=db,
                _user=owner,
            )
        except HTTPException:
            raise
        return {"kind": "timeline", "data": data}

    # daily
    from .daily import get_daily
    owner = (await db.execute(
        select(User).where(User.id == link.owner_user_id)
    )).scalar_one_or_none()
    if not owner:
        raise HTTPException(status_code=404)
    data = await get_daily(
        date_str=link.target_id,
        tz_offset=0,
        db=db,
        _user=owner,
    )
    return {"kind": "daily", "data": data}
