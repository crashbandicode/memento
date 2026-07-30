"""User-scoped hierarchical conversation task queries."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import User
from ..db.session import get_db
from ..middleware.auth import get_current_user
from ..services.conversation_tasks import (
    TaskCursorError,
    TaskDocumentNotFound,
    TaskSelectorAmbiguous,
    query_conversation_tasks,
)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

TaskStatus = Literal[
    "outstanding",
    "all",
    "pending",
    "in_progress",
    "blocked",
    "completed",
    "cancelled",
]


@router.get("")
async def get_tasks(
    document_id: UUID | None = None,
    thread_id: str | None = Query(default=None, max_length=512),
    agent_id: str | None = Query(default=None, max_length=512),
    subagent_id: str | None = Query(default=None, max_length=512),
    tool: str | None = Query(default=None, max_length=50),
    status: TaskStatus = "outstanding",
    include_history: bool = False,
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(10, ge=1, le=20),
    max_tasks: int = Query(100, ge=1, le=200),
    history_limit: int = Query(0, ge=0, le=5),
    db: AsyncSession = Depends(get_db),  # noqa: B008
    user: User = Depends(get_current_user),  # noqa: B008
) -> dict:
    """Return recursive task trees without reparsing transcript content."""
    try:
        return await query_conversation_tasks(
            db,
            user,
            document_id=document_id,
            thread_id=thread_id,
            agent_id=agent_id,
            subagent_id=subagent_id,
            tool=tool,
            status=status,
            include_history=include_history,
            cursor=cursor,
            limit=limit,
            max_tasks=max_tasks,
            history_limit=history_limit,
        )
    except TaskDocumentNotFound as exc:
        # Deliberately identical for absent and unauthorized document UUIDs.
        raise HTTPException(status_code=404, detail="Document not found") from exc
    except TaskSelectorAmbiguous as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ambiguous_selector",
                "candidates": exc.candidates,
            },
        ) from exc
    except TaskCursorError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_cursor", "message": str(exc)},
        ) from exc
