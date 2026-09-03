"""
Phase 3 — ORM model + schema sanity.

These are not policy tests (those arrive in Phase 4). They check that the
schema the rest of the system will lean on is actually shaped the way the
architecture-freeze doc says: string-backed enums, money as Decimal, UUID keys.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select, text

from app.agents.models import Agent
from app.catalog.models import Merchant, Product
from app.core.db import Base
from app.core.enums import AgentStatus, AgentType

EXPECTED_TABLES = {
    "merchant",
    "product",
    "agent",
    "action_request",
    "decision",
    "approval",
    "payment_attempt",
    "webhook_event",
    "audit_event",
}


def test_all_expected_tables_registered() -> None:
    assert EXPECTED_TABLES <= set(Base.metadata.tables)


async def test_enum_columns_persist_as_string_values(db_session) -> None:
    agent = Agent(
        name="Enum Probe (SIMULATED)",
        type=AgentType.AI_BUYER,
        status=AgentStatus.ACTIVE,
        max_transaction_amount=Decimal("1000.00"),
        allowed_actions=["PURCHASE"],
    )
    db_session.add(agent)
    await db_session.flush()

    # Raw read: the stored value must be the enum *value*, not "AgentType.ACTIVE"
    # or the member name.
    raw = await db_session.execute(
        text("SELECT type, status FROM agent WHERE id = :id"), {"id": agent.id}
    )
    assert raw.one() == ("AI_BUYER", "ACTIVE")


async def test_money_round_trips_as_decimal(db_session) -> None:
    merchant = Merchant(name="Decimal Co. (SIMULATED)")
    db_session.add(merchant)
    await db_session.flush()

    product = Product(
        merchant_id=merchant.id,
        name="Precise Widget (SIMULATED)",
        category="misc",
        price=Decimal("1234.50"),
        stock=3,
        max_discount_pct=Decimal("10.00"),
        min_margin_price=Decimal("1000.00"),
    )
    db_session.add(product)
    await db_session.flush()
    await db_session.refresh(product)

    assert isinstance(product.price, Decimal)
    assert product.price == Decimal("1234.50")
    assert isinstance(product.min_margin_price, Decimal)


async def test_product_and_merchant_link(db_session) -> None:
    merchant = Merchant(name="Linked Co. (SIMULATED)")
    merchant.products.append(
        Product(
            name="Linked Item (SIMULATED)",
            category="misc",
            price=Decimal("500.00"),
            stock=1,
            max_discount_pct=Decimal("5.00"),
            min_margin_price=Decimal("450.00"),
        )
    )
    db_session.add(merchant)
    await db_session.flush()

    got = (
        await db_session.scalars(
            select(Product).where(Product.merchant_id == merchant.id)
        )
    ).all()
    assert len(got) == 1
    assert got[0].merchant_id == merchant.id
