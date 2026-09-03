"""
Read-only audit endpoints for the timeline UI (Phase 11).

    GET /audit/events?limit=&ref_id=   most-recent-first list of audit events
    GET /audit/chain                   verify_audit_chain() as JSON

These never write. `payload` is returned as-is (it is already the canonical
JSON-safe structure the hash was computed over — see docs/audit.md).
"""
from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import AuditEvent
from app.audit.verify import verify_audit_chain
from app.core.db import get_db

router = APIRouter(prefix="/audit", tags=["audit"])


class AuditEventOut(BaseModel):
    id: uuid.UUID
    seq: int
    ref_type: str
    ref_id: uuid.UUID
    event_type: str
    payload: dict
    prev_hash: str
    hash: str
    created_at: dt.datetime


class AuditChainOut(BaseModel):
    valid: bool
    checked_events: int
    failure: str | None
    failure_detail: str | None
    event_id: str | None
    event_seq: int | None


@router.get("/events", response_model=list[AuditEventOut])
async def list_events(
    session: AsyncSession = Depends(get_db),
    limit: int = Query(default=200, ge=1, le=1000),
    ref_id: uuid.UUID | None = Query(default=None),
) -> list[AuditEventOut]:
    stmt = select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(limit)
    if ref_id is not None:
        stmt = select(AuditEvent).where(AuditEvent.ref_id == ref_id).order_by(
            AuditEvent.seq.desc()
        ).limit(limit)
    rows = (await session.scalars(stmt)).all()
    return [AuditEventOut.model_validate(e, from_attributes=True) for e in rows]


@router.get("/chain", response_model=AuditChainOut)
async def chain(session: AsyncSession = Depends(get_db)) -> AuditChainOut:
    result = await verify_audit_chain(session)
    return AuditChainOut(
        valid=result.valid,
        checked_events=result.checked_events,
        failure=result.failure,
        failure_detail=result.failure_detail,
        event_id=result.event_id,
        event_seq=result.event_seq,
    )
