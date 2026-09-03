"""
Seed data — SIMULATED.

Everything created here is synthetic: one fictional merchant, its catalogue,
stock and margin figures, and a small population of agents. None of it is real
Razorpay data or a real merchant's numbers. The values are chosen so the four
demo scenarios and the scenario harness have something concrete to run against:

  * "Trailblaze Daily Trainer" (₹4,500, 15% headroom) — the happy-path purchase.
  * "Velocity Pro Marathon Racer" (₹10,000, 10% cap, ₹8,800 floor) — a 20%
    request counter-offers to exactly ₹9,000 = max(10%-off 9000, floor 8800).
  * "Home Marathon Treadmill T9" (₹45,000) — above the reference buyer's
    ₹25,000 auto-approve limit, so it lands in the approval queue.
  * "Cloudstep Recovery Slide" (stock 0) — the stock-unavailable path.
  * "Dormant Partner Bot" (SUSPENDED) and "Read-Only Comparison Bot" (no write
    actions allowed) — the agent-status and action-whitelist denials.

Run:  python -m app.seed           # seeds only if the DB is empty
      python -m app.seed --reset   # wipes the simulated rows first
"""
from __future__ import annotations

import asyncio
import sys
from decimal import Decimal

from sqlalchemy import delete, select

from app.agents.models import Agent
from app.catalog.models import Merchant, Product
from app.core.db import AsyncSessionLocal
from app.core.enums import ActionType, AgentStatus, AgentType

MERCHANT_NAME = "Northwind Running Co. (SIMULATED)"

_D = Decimal


def _products(merchant_id) -> list[Product]:
    # (name, category, price, stock, max_discount_pct, min_margin_price)
    rows = [
        ("Trailblaze Daily Trainer (SIMULATED)", "running-shoes", "4500.00", 40, "15.00", "3600.00"),
        ("Velocity Pro Marathon Racer (SIMULATED)", "running-shoes", "10000.00", 12, "10.00", "8800.00"),
        ("Summit Trail Ultra (SIMULATED)", "running-shoes", "7800.00", 5, "12.00", "6800.00"),
        ("Featherlite 5K Flat (SIMULATED)", "running-shoes", "3200.00", 25, "20.00", "2400.00"),
        ("Cloudstep Recovery Slide (SIMULATED)", "recovery", "2200.00", 0, "10.00", "1900.00"),
        ("Home Marathon Treadmill T9 (SIMULATED)", "equipment", "45000.00", 3, "8.00", "41000.00"),
    ]
    return [
        Product(
            merchant_id=merchant_id,
            name=name,
            description=f"{name} — simulated catalogue item for AgentGate demos.",
            category=category,
            price=_D(price),
            stock=stock,
            max_discount_pct=_D(mdp),
            min_margin_price=_D(mmp),
        )
        for (name, category, price, stock, mdp, mmp) in rows
    ]


def _agents() -> list[Agent]:
    purchase_actions = [ActionType.PURCHASE.value, ActionType.ACCEPT_COUNTER_OFFER.value]
    return [
        Agent(
            name="AgentGate Reference Buyer (SIMULATED)",
            type=AgentType.AI_BUYER,
            status=AgentStatus.ACTIVE,
            max_transaction_amount=_D("25000.00"),
            allowed_actions=purchase_actions,
        ),
        Agent(
            name="Dormant Partner Bot (SIMULATED)",
            type=AgentType.EXTERNAL,
            status=AgentStatus.SUSPENDED,
            max_transaction_amount=_D("25000.00"),
            allowed_actions=purchase_actions,
        ),
        Agent(
            name="Read-Only Comparison Bot (SIMULATED)",
            type=AgentType.EXTERNAL,
            status=AgentStatus.ACTIVE,
            max_transaction_amount=_D("5000.00"),
            allowed_actions=[],  # no write-shaped action permitted
        ),
    ]


async def seed(*, reset: bool = False) -> None:
    async with AsyncSessionLocal() as session:
        existing = (await session.scalars(select(Merchant))).first()
        if existing and not reset:
            print("DB already has a merchant; nothing seeded. Use --reset to replace.")
            return
        if existing:
            # Order matters only loosely thanks to CASCADE from merchant->product;
            # agents are independent. Wipe both simulated sets.
            await session.execute(delete(Agent))
            await session.execute(delete(Merchant))
            await session.flush()

        merchant = Merchant(name=MERCHANT_NAME, policy_version="v1")
        session.add(merchant)
        await session.flush()  # assign merchant.id

        session.add_all(_products(merchant.id))
        session.add_all(_agents())
        await session.commit()

        n_products = len(_products(merchant.id))
        n_agents = len(_agents())
        print(
            f"Seeded merchant '{MERCHANT_NAME}' with {n_products} products "
            f"and {n_agents} agents (all SIMULATED)."
        )


if __name__ == "__main__":
    asyncio.run(seed(reset="--reset" in sys.argv[1:]))
