"""
POST /actions — an external agent requests a commercial action.

Thin: validate (Pydantic), delegate to the service, map a missing resource to
404. Every policy verdict (ALLOW / DENY / NEEDS_APPROVAL / COUNTER_OFFER) is a
successful evaluation and returns HTTP 200 — a DENY is not an HTTP error.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.action_requests.schemas import ActionDecisionResponse, ActionRequestCreate
from app.action_requests.service import ResourceNotFound, evaluate_action
from app.core.db import get_db

router = APIRouter(prefix="/actions", tags=["actions"])


@router.post("", response_model=ActionDecisionResponse)
async def create_action(
    body: ActionRequestCreate, session: AsyncSession = Depends(get_db)
) -> ActionDecisionResponse:
    try:
        return await evaluate_action(session, body)
    except ResourceNotFound as exc:
        raise HTTPException(
            status_code=404, detail={"code": exc.code, "message": exc.message}
        ) from exc
