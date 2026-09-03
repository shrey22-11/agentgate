"""
POST /webhooks/razorpay

Reads the raw request body (for signature verification), the
`X-Razorpay-Signature` header, and the `X-Razorpay-Event-Id` header, then hands
everything to `process_webhook`. Always returns 200 for anything Razorpay could
legitimately send that we accept; 400 for a bad/absent signature or non-JSON
body; 503 if Razorpay integration is disabled.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.razorpay.client import RazorpayClient, RazorpayDisabledError, get_razorpay_client
from app.webhooks.schemas import WebhookAck
from app.webhooks.service import (
    WebhookMalformed,
    WebhookSignatureInvalid,
    process_webhook,
)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
_log = logging.getLogger("agentgate.webhooks")


@router.post("/razorpay", response_model=WebhookAck)
async def razorpay_webhook(
    request: Request,
    session: AsyncSession = Depends(get_db),
    client: RazorpayClient = Depends(get_razorpay_client),
) -> WebhookAck:
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature")
    event_id = request.headers.get("X-Razorpay-Event-Id")

    try:
        return await process_webhook(
            session,
            client,
            raw_body=raw_body,
            signature=signature,
            event_id_header=event_id,
        )
    except RazorpayDisabledError as exc:
        raise HTTPException(503, detail={"code": "RAZORPAY_DISABLED"}) from exc
    except WebhookSignatureInvalid as exc:
        # No secret material in the log; just that a delivery failed verification.
        _log.warning("rejected Razorpay webhook: signature verification failed")
        raise HTTPException(400, detail={"code": "INVALID_SIGNATURE"}) from exc
    except WebhookMalformed as exc:
        raise HTTPException(400, detail={"code": "MALFORMED_WEBHOOK"}) from exc
