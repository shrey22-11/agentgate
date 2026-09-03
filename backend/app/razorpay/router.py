"""
Payment execution endpoints. Thin: validate path param, delegate, map errors.

    POST /payments/{decision_id}/execute      create the Razorpay payment link
    POST /payments/{decision_id}/reconcile    re-sync local status from Razorpay

The amount is never in the request body — it comes from `decision.executable_amount`.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.razorpay.client import RazorpayClient, RazorpayDisabledError, get_razorpay_client
from app.razorpay.schemas import PaymentExecutionResponse
from app.razorpay.service import (
    DecisionNotFound,
    ExecutionConflict,
    ExecutionError,
    ExecutionFailed,
    ExecutionNotAllowed,
    execute_payment,
    reconcile_payment_attempt,
)

router = APIRouter(prefix="/payments", tags=["payments"])

_STATUS_BY_EXCEPTION = {
    DecisionNotFound: 404,
    ExecutionNotAllowed: 409,
    ExecutionConflict: 409,
    ExecutionFailed: 502,
}


def _http_error(exc: ExecutionError) -> HTTPException:
    status = _STATUS_BY_EXCEPTION.get(type(exc), 500)
    return HTTPException(status, detail={"code": exc.code, "message": exc.message})


@router.post("/{decision_id}/execute", response_model=PaymentExecutionResponse)
async def execute(
    decision_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    client: RazorpayClient = Depends(get_razorpay_client),
) -> PaymentExecutionResponse:
    try:
        return await execute_payment(session, client, decision_id)
    except RazorpayDisabledError as exc:
        raise HTTPException(503, detail={"code": "RAZORPAY_DISABLED", "message": str(exc)}) from exc
    except ExecutionError as exc:
        raise _http_error(exc) from exc


@router.post("/{decision_id}/reconcile", response_model=PaymentExecutionResponse)
async def reconcile(
    decision_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    client: RazorpayClient = Depends(get_razorpay_client),
) -> PaymentExecutionResponse:
    try:
        return await reconcile_payment_attempt(session, client, decision_id)
    except RazorpayDisabledError as exc:
        raise HTTPException(503, detail={"code": "RAZORPAY_DISABLED", "message": str(exc)}) from exc
    except ExecutionError as exc:
        raise _http_error(exc) from exc
