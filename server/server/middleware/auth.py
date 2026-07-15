"""Authentication middleware and utilities."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, Header, HTTPException, status
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db.models import User
from ..db.session import get_db


EVENT_STREAM_TOKEN_EXPIRE_MINUTES = 15


def is_single_user_allowed(user: User | None) -> bool:
    """Whether ``user`` may access a single-user deployment."""
    return not settings.single_user_mode or bool(user and user.role in {"owner", "admin"})


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def create_access_token(user_id: str, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {"sub": user_id, "role": role, "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def create_event_stream_token(user_id: str) -> str:
    """Create a short-lived, scope-limited credential for an SSE cookie."""
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=EVENT_STREAM_TOKEN_EXPIRE_MINUTES
    )
    payload = {"sub": user_id, "scope": "events", "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from e


def decode_event_stream_token(token: str) -> dict:
    """Decode a token that is valid only for the live event stream."""
    payload = decode_token(token)
    if payload.get("scope") != "events":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid event stream token",
        )
    return payload


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

async def verify_collector_token(
    x_collector_token: str = Header(...),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Verify the collector auth token. Returns the owning User.

    Supports per-user tokens (User.collector_token) and legacy global token
    (settings.collector_token → maps to the first owner user).
    """
    # Try per-user token first
    result = await db.execute(select(User).where(User.collector_token == x_collector_token))
    user = result.scalar_one_or_none()
    if user and user.status == "active" and is_single_user_allowed(user):
        return user

    # Fallback: legacy global token → owner user
    if secrets.compare_digest(x_collector_token, settings.collector_token):
        result = await db.execute(
            select(User).where(User.role == "owner", User.status == "active").limit(1)
        )
        owner = result.scalar_one_or_none()
        if owner and is_single_user_allowed(owner):
            return owner

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid collector token")


async def get_current_user(
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Extract and validate the current user from JWT token."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing authorization")

    token = authorization[7:]
    payload = decode_token(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if user is None or user.status != "active" or not is_single_user_allowed(user):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

    return user


async def get_optional_user(
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """Like get_current_user but returns None if no token provided."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        return await get_current_user(authorization, db)
    except HTTPException:
        return None


def require_role(*roles: str):
    """Dependency factory that requires the user to have one of the given roles."""
    async def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return user
    return checker
