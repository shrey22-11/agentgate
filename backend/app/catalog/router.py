"""
Read-only catalogue + agent listings for the merchant UI (Phase 11).

Unlike the buyer-agent's `app.catalog.queries` views, these are merchant-facing:
they DO include `max_discount_pct` / `min_margin_price` and the agent's limits —
this is the merchant looking at their own policy inputs.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.models import Agent
from app.catalog.models import Product
from app.core.db import get_db
from app.core.enums import AgentStatus, AgentType

router = APIRouter(prefix="/catalog", tags=["catalog"])


class ProductOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    category: str
    price: Decimal
    stock: int
    max_discount_pct: Decimal
    min_margin_price: Decimal


class AgentOut(BaseModel):
    id: uuid.UUID
    name: str
    type: AgentType
    status: AgentStatus
    max_transaction_amount: Decimal
    allowed_actions: list[str]


@router.get("/products", response_model=list[ProductOut])
async def list_products(session: AsyncSession = Depends(get_db)) -> list[ProductOut]:
    rows = (
        await session.scalars(select(Product).order_by(Product.category, Product.price))
    ).all()
    return [ProductOut.model_validate(p, from_attributes=True) for p in rows]


@router.get("/agents", response_model=list[AgentOut])
async def list_agents(session: AsyncSession = Depends(get_db)) -> list[AgentOut]:
    rows = (await session.scalars(select(Agent).order_by(Agent.name))).all()
    return [AgentOut.model_validate(a, from_attributes=True) for a in rows]
