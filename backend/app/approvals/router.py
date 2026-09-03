"""
Approval endpoints. Thin: validate, delegate, map known errors.

    GET  /approvals/pending
    POST /approvals/{decision_id}/approve
    POST /approvals/{decision_id}/reject

404 — no such decision. 409 — decision is not NEEDS_APPROVAL, or already
resolved. 200 — resolved. Everything else rolls back (500).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.approvals.schemas import (
    ApprovalResolutionRequest,
    ApprovalResolutionResponse,
    PendingApprovalItem,
)
from app.approvals.service import (
    ApprovalConflict,
    DecisionNotFound,
    list_pending_approvals,
    resolve_approval,
)
from app.core.db import get_db
from app.core.enums import ApprovalOutcome

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("/pending", response_model=list[PendingApprovalItem])
async def get_pending(
    session: AsyncSession = Depends(get_db),
) -> list[PendingApprovalItem]:
    return await list_pending_approvals(session)


async def _resolve(
    decision_id: uuid.UUID,
    body: ApprovalResolutionRequest,
    session: AsyncSession,
    outcome: ApprovalOutcome,
) -> ApprovalResolutionResponse:
    try:
        return await resolve_approval(
            session,
            decision_id=decision_id,
            outcome=outcome,
            approver=body.approver,
            reason=body.reason,
        )
    except DecisionNotFound as exc:
        raise HTTPException(404, detail={"code": exc.code, "message": exc.message}) from exc
    except ApprovalConflict as exc:
        raise HTTPException(409, detail={"code": exc.code, "message": exc.message}) from exc


@router.post("/{decision_id}/approve", response_model=ApprovalResolutionResponse)
async def approve(
    decision_id: uuid.UUID,
    body: ApprovalResolutionRequest,
    session: AsyncSession = Depends(get_db),
) -> ApprovalResolutionResponse:
    return await _resolve(decision_id, body, session, ApprovalOutcome.APPROVED)


@router.post("/{decision_id}/reject", response_model=ApprovalResolutionResponse)
async def reject(
    decision_id: uuid.UUID,
    body: ApprovalResolutionRequest,
    session: AsyncSession = Depends(get_db),
) -> ApprovalResolutionResponse:
    return await _resolve(decision_id, body, session, ApprovalOutcome.REJECTED)
