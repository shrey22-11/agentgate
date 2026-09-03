"""
AI endpoints.

    POST /ai/actions   parse one natural-language request -> deterministic decision
    POST /ai/buyer     run the bounded multi-step buyer agent toward a goal

Both are thin: validate, delegate, map the two non-decision failures
(unknown agent -> 404, AI disabled -> 503).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.action_requests.service import ResourceNotFound
from app.ai.buyer import run_buyer_agent
from app.ai.client import (
    AIBuyerClient,
    AIDisabledError,
    AIParserClient,
    get_ai_buyer_client,
    get_ai_client,
)
from app.ai.parser import parse_natural_language_action
from app.ai.schemas import (
    BuyerRunRequest,
    BuyerRunResponse,
    NLActionRequest,
    NLActionResponse,
)
from app.core.db import get_db

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/actions", response_model=NLActionResponse)
async def parse_action(
    body: NLActionRequest,
    session: AsyncSession = Depends(get_db),
    client: AIParserClient = Depends(get_ai_client),
) -> NLActionResponse:
    try:
        return await parse_natural_language_action(
            session, client, agent_id=body.agent_id, raw_input=body.text
        )
    except ResourceNotFound as exc:
        raise HTTPException(
            status_code=404, detail={"code": exc.code, "message": exc.message}
        ) from exc
    except AIDisabledError as exc:
        raise HTTPException(
            status_code=503, detail={"code": "AI_DISABLED", "message": str(exc)}
        ) from exc


@router.post("/buyer", response_model=BuyerRunResponse)
async def run_buyer(
    body: BuyerRunRequest,
    session: AsyncSession = Depends(get_db),
    client: AIBuyerClient = Depends(get_ai_buyer_client),
) -> BuyerRunResponse:
    try:
        return await run_buyer_agent(
            session, client, agent_id=body.agent_id, goal=body.goal
        )
    except ResourceNotFound as exc:
        raise HTTPException(
            status_code=404, detail={"code": exc.code, "message": exc.message}
        ) from exc
    except AIDisabledError as exc:
        raise HTTPException(
            status_code=503, detail={"code": "AI_DISABLED", "message": str(exc)}
        ) from exc
