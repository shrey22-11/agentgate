"""
Phase 11 — read-only endpoints backing the merchant UI.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agents.models import Agent
from app.catalog.models import Merchant, Product
from app.core.config import get_settings
from app.core.db import get_db
from app.main import app
from app.seed import MERCHANT_NAME, _agents, _products

_ALL_TABLES = (
    "agent, product, merchant, action_request, decision, approval, "
    "payment_attempt, webhook_event, audit_event"
)


async def _reset(engine) -> None:
    async with engine.begin() as conn:
        await conn.exec_driver_sql("ALTER TABLE audit_event DISABLE TRIGGER USER")
        await conn.exec_driver_sql(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE")
        await conn.exec_driver_sql("ALTER TABLE audit_event ENABLE TRIGGER USER")


@pytest_asyncio.fixture
async def api():
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _reset(engine)
    async with factory() as s:
        merchant = Merchant(name=MERCHANT_NAME, policy_version="v1")
        s.add(merchant)
        await s.flush()
        s.add_all(_products(merchant.id))
        s.add_all(_agents())
        await s.commit()
    async with factory() as s:
        products = {p.name: p.id for p in (await s.scalars(select(Product))).all()}
        agents = {a.name: a.id for a in (await s.scalars(select(Agent))).all()}
    ids = {
        "active_agent": next(v for k, v in agents.items() if "Reference Buyer" in k),
        "suspended_agent": next(v for k, v in agents.items() if "Dormant" in k),
        "velocity": next(v for k, v in products.items() if "Velocity Pro" in k),
        "trailblaze": next(v for k, v in products.items() if "Trailblaze" in k),
        "treadmill": next(v for k, v in products.items() if "Treadmill" in k),
    }

    async def _override_get_db():
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, ids
    finally:
        app.dependency_overrides.pop(get_db, None)
        await _reset(engine)
        await engine.dispose()


async def _decide(client, agent_id, product_id, **body):
    r = await client.post(
        "/actions",
        json={"agent_id": str(agent_id), "product_id": str(product_id), **body},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_list_products(api) -> None:
    client, _ = api
    r = await client.get("/catalog/products")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 6
    v = next(p for p in rows if "Velocity Pro" in p["name"])
    assert v["price"] == "10000.00"
    assert v["max_discount_pct"] == "10.00"
    assert v["min_margin_price"] == "8800.00"
    assert set(v) == {"id", "name", "description", "category", "price", "stock",
                      "max_discount_pct", "min_margin_price"}


async def test_list_agents(api) -> None:
    client, _ = api
    r = await client.get("/catalog/agents")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 3
    buyer = next(a for a in rows if "Reference Buyer" in a["name"])
    assert buyer["type"] == "AI_BUYER"
    assert buyer["status"] == "ACTIVE"
    assert "PURCHASE" in buyer["allowed_actions"]


async def test_audit_events_and_chain(api) -> None:
    client, ids = api
    assert (await client.get("/audit/events")).json() == []
    chain0 = (await client.get("/audit/chain")).json()
    assert chain0 == {"valid": True, "checked_events": 0, "failure": None,
                      "failure_detail": None, "event_id": None, "event_seq": None}

    d = await _decide(client, ids["active_agent"], ids["velocity"], requested_discount_pct="20")

    events = (await client.get("/audit/events")).json()
    types = [e["event_type"] for e in events]
    assert "ACTION_REQUEST_RECEIVED" in types and "POLICY_EVALUATED" in types
    assert events[0]["seq"] > events[-1]["seq"]  # most-recent first

    by_ref = (await client.get(f"/audit/events?ref_id={d['action_request_id']}")).json()
    assert {e["ref_id"] for e in by_ref} == {d["action_request_id"]}

    chain = (await client.get("/audit/chain")).json()
    assert chain["valid"] is True and chain["checked_events"] == len(events)


async def test_dashboard_summary(api) -> None:
    client, ids = api
    await _decide(client, ids["active_agent"], ids["trailblaze"], quantity=1)          # ALLOW
    await _decide(client, ids["suspended_agent"], ids["velocity"])                     # DENY
    await _decide(client, ids["active_agent"], ids["velocity"], requested_discount_pct="20")  # COUNTER_OFFER
    await _decide(client, ids["active_agent"], ids["treadmill"])                       # NEEDS_APPROVAL

    s = (await client.get("/dashboard/summary")).json()
    assert s["action_requests_total"] == 4
    assert s["decisions_by_verdict"] == {"ALLOW": 1, "DENY": 1, "COUNTER_OFFER": 1, "NEEDS_APPROVAL": 1}
    assert s["approvals_pending"] == 1
    assert s["approvals_resolved"] == {"APPROVED": 0, "REJECTED": 0}
    assert s["audit_events"] > 0
    assert s["audit_chain_valid"] is True
    assert len(s["recent_decisions"]) == 4
    assert s["recent_decisions"][0]["agent_name"] is not None
    assert s["recent_decisions"][0]["product_name"] is not None


async def test_audit_events_limit_validation(api) -> None:
    client, _ = api
    assert (await client.get("/audit/events?limit=0")).status_code == 422
    assert (await client.get("/audit/events?limit=5000")).status_code == 422
    assert (await client.get("/audit/events?limit=10")).status_code == 200
