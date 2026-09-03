"""
Phase 6 — POST /actions end to end.

Each test runs against a freshly truncated + seeded `agentgate_test` database.
The route's `get_db` dependency is overridden with a NullPool session factory
(one session per HTTP request, matching the real dependency's semantics).
Persistence is always checked from a *separate* inspection session, i.e. from
committed state, not the request's own session.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.action_requests.models import ActionRequest
from app.agents.models import Agent
from app.audit import verify_audit_chain
from app.audit.models import AuditEvent
from app.catalog.models import Merchant, Product
from app.core.config import get_settings
from app.core.db import get_db
from app.core.enums import ActionRequestStatus, Verdict
from app.main import app
from app.policy.models import Decision
from app.seed import MERCHANT_NAME, _agents, _products

D = Decimal

_ALL_TABLES = (
    "agent, product, merchant, action_request, decision, approval, "
    "payment_attempt, webhook_event, audit_event"
)


async def _reset(engine) -> None:
    async with engine.begin() as conn:
        await conn.exec_driver_sql("ALTER TABLE audit_event DISABLE TRIGGER USER")
        await conn.exec_driver_sql(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE")
        await conn.exec_driver_sql("ALTER TABLE audit_event ENABLE TRIGGER USER")


class _Api:
    def __init__(self, client: AsyncClient, session_factory, ids: dict) -> None:
        self.client = client
        self._factory = session_factory
        self.ids = ids

    def session(self):
        return self._factory()


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

    def find(mapping: dict, needle: str):
        return next(v for k, v in mapping.items() if needle in k)

    ids = {
        "active_agent": find(agents, "Reference Buyer"),
        "suspended_agent": find(agents, "Dormant Partner"),
        "restricted_agent": find(agents, "Read-Only Comparison"),
        "velocity": find(products, "Velocity Pro"),
        "trailblaze": find(products, "Trailblaze"),
        "treadmill": find(products, "Treadmill"),
        "out_of_stock": find(products, "Cloudstep Recovery"),
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
            yield _Api(client, factory, ids)
    finally:
        app.dependency_overrides.pop(get_db, None)
        await _reset(engine)
        await engine.dispose()


def _body(api: _Api, agent: str, product: str, **over) -> dict:
    body = {"agent_id": str(api.ids[agent]), "product_id": str(api.ids[product])}
    body.update(over)
    return body


async def _rows(api: _Api):
    async with api.session() as s:
        ars = (await s.scalars(select(ActionRequest))).all()
        decisions = (await s.scalars(select(Decision))).all()
        audits = (
            await s.scalars(select(AuditEvent).order_by(AuditEvent.seq.asc()))
        ).all()
        return ars, decisions, audits


# --- COUNTER_OFFER: the seeded Velocity Pro scenario ----------------------
async def test_velocity_pro_20pct_counter_offer_end_to_end(api) -> None:
    resp = await api.client.post(
        "/actions", json=_body(api, "active_agent", "velocity", requested_discount_pct="20.00")
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["verdict"] == "COUNTER_OFFER"
    assert data["rule_id"] == "RULE_DISCOUNT_POLICY"
    assert data["policy_version"] == "v1"
    assert data["counter_offer"] == {"price": "9000.00", "discount_pct": "10.00"}

    ars, decisions, audits = await _rows(api)
    assert len(ars) == 1 and len(decisions) == 1
    ar, decision = ars[0], decisions[0]
    assert str(ar.id) == data["action_request_id"]
    assert ar.status is ActionRequestStatus.DECIDED
    assert ar.requested_discount_pct == D("20.00")
    assert str(decision.id) == data["decision_id"]
    assert decision.verdict is Verdict.COUNTER_OFFER
    assert decision.counter_offer_price == D("9000.00")
    assert decision.counter_offer_discount_pct == D("10.00")
    assert decision.policy_rule_id == "RULE_DISCOUNT_POLICY"

    assert [a.event_type for a in audits] == ["ACTION_REQUEST_RECEIVED", "POLICY_EVALUATED"]
    assert audits[0].ref_id == ar.id and audits[1].ref_id == ar.id
    assert audits[1].prev_hash == audits[0].hash
    async with api.session() as s:
        result = await verify_audit_chain(s)
    assert result.valid and result.checked_events == 2


# --- ALLOW --------------------------------------------------------------
async def test_allow_end_to_end(api) -> None:
    resp = await api.client.post(
        "/actions",
        json=_body(api, "active_agent", "trailblaze", quantity=2, requested_discount_pct="5"),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["verdict"] == "ALLOW"
    assert data["rule_id"] == "RULE_OK"
    assert data["counter_offer"] is None

    ars, decisions, audits = await _rows(api)
    assert len(ars) == 1 and len(decisions) == 1
    assert decisions[0].verdict is Verdict.ALLOW
    assert decisions[0].counter_offer_price is None
    assert [a.event_type for a in audits] == ["ACTION_REQUEST_RECEIVED", "POLICY_EVALUATED"]
    async with api.session() as s:
        assert (await verify_audit_chain(s)).valid


# --- DENY -------------------------------------------------------------
async def test_deny_inactive_agent(api) -> None:
    resp = await api.client.post(
        "/actions",
        json=_body(api, "suspended_agent", "velocity", requested_discount_pct="20"),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["verdict"] == "DENY"
    assert data["rule_id"] == "RULE_AGENT_ACTIVE"
    assert data["counter_offer"] is None

    ars, decisions, audits = await _rows(api)
    assert len(ars) == 1 and decisions[0].verdict is Verdict.DENY
    assert len(audits) == 2  # request still persisted; the resource is valid


async def test_deny_action_not_permitted(api) -> None:
    resp = await api.client.post("/actions", json=_body(api, "restricted_agent", "trailblaze"))
    assert resp.status_code == 200
    assert resp.json()["rule_id"] == "RULE_ACTION_PERMISSION"
    assert resp.json()["verdict"] == "DENY"


async def test_deny_insufficient_stock(api) -> None:
    # Cloudstep Recovery Slide has stock 0; price keeps it under the txn cap.
    resp = await api.client.post(
        "/actions", json=_body(api, "active_agent", "out_of_stock", quantity=1)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["verdict"] == "DENY"
    assert data["rule_id"] == "RULE_STOCK_AVAILABLE"
    assert data["counter_offer"] is None


# --- NEEDS_APPROVAL ---------------------------------------------------
async def test_needs_approval_high_value(api) -> None:
    # Treadmill ₹45,000 vs the reference buyer's ₹25,000 cap.
    resp = await api.client.post("/actions", json=_body(api, "active_agent", "treadmill"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["verdict"] == "NEEDS_APPROVAL"
    assert data["rule_id"] == "RULE_TRANSACTION_CAP"
    assert data["counter_offer"] is None

    ars, decisions, audits = await _rows(api)
    assert decisions[0].verdict is Verdict.NEEDS_APPROVAL
    # NEEDS_APPROVAL also emits APPROVAL_REQUESTED so the queue entry has an
    # auditable origin (Phase 7).
    assert [a.event_type for a in audits] == [
        "ACTION_REQUEST_RECEIVED",
        "POLICY_EVALUATED",
        "APPROVAL_REQUESTED",
    ]
    async with api.session() as s:
        assert (await verify_audit_chain(s)).valid


# --- proposed_price and ACCEPT_COUNTER_OFFER paths -----------------
async def test_proposed_price_below_floor_counter_offers(api) -> None:
    resp = await api.client.post(
        "/actions", json=_body(api, "active_agent", "velocity", proposed_price="8000.00")
    )
    assert resp.json()["verdict"] == "COUNTER_OFFER"
    assert resp.json()["counter_offer"]["price"] == "9000.00"


async def test_accept_counter_offer_at_floor_is_allowed(api) -> None:
    resp = await api.client.post(
        "/actions",
        json=_body(
            api, "active_agent", "velocity",
            action_type="ACCEPT_COUNTER_OFFER", proposed_price="9000.00",
        ),
    )
    assert resp.status_code == 200
    assert resp.json()["verdict"] == "ALLOW"


# --- unknown resources -> 404, nothing persisted -----------------
async def test_unknown_agent_returns_404_and_persists_nothing(api) -> None:
    body = {"agent_id": str(uuid.uuid4()), "product_id": str(api.ids["velocity"])}
    resp = await api.client.post("/actions", json=body)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "AGENT_NOT_FOUND"
    ars, decisions, audits = await _rows(api)
    assert (ars, decisions, audits) == ([], [], [])


async def test_unknown_product_returns_404_and_persists_nothing(api) -> None:
    body = {"agent_id": str(api.ids["active_agent"]), "product_id": str(uuid.uuid4())}
    resp = await api.client.post("/actions", json=body)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "PRODUCT_NOT_FOUND"
    ars, decisions, audits = await _rows(api)
    assert (ars, decisions, audits) == ([], [], [])


# --- request validation (422) -----------------------------------
async def test_malformed_uuid_is_422(api) -> None:
    resp = await api.client.post(
        "/actions", json={"agent_id": "not-a-uuid", "product_id": str(api.ids["velocity"])}
    )
    assert resp.status_code == 422


async def test_json_float_money_is_rejected(api) -> None:
    resp = await api.client.post(
        "/actions",
        json=_body(api, "active_agent", "velocity", requested_discount_pct=20.5),
    )
    assert resp.status_code == 422
    # string form of the same value is accepted
    ok = await api.client.post(
        "/actions",
        json=_body(api, "active_agent", "velocity", requested_discount_pct="20.5"),
    )
    assert ok.status_code == 200


async def test_non_positive_quantity_is_422(api) -> None:
    resp = await api.client.post(
        "/actions", json=_body(api, "active_agent", "velocity", quantity=0)
    )
    assert resp.status_code == 422


async def test_unknown_field_is_422(api) -> None:
    resp = await api.client.post(
        "/actions", json=_body(api, "active_agent", "velocity", surprise="x")
    )
    assert resp.status_code == 422


# --- faithful Decision persistence + parsed_payload -----------
async def test_decision_row_is_faithful_copy_of_policy_decision(api) -> None:
    resp = await api.client.post(
        "/actions", json=_body(api, "active_agent", "velocity", requested_discount_pct="20")
    )
    data = resp.json()
    async with api.session() as s:
        d = (await s.scalars(select(Decision))).one()
        ar = (await s.scalars(select(ActionRequest))).one()
    assert d.verdict.value == data["verdict"]
    assert d.policy_rule_id == data["rule_id"]
    assert d.reason == data["reason"]
    assert d.policy_version == data["policy_version"]
    assert str(d.counter_offer_price) == data["counter_offer"]["price"]
    assert str(d.counter_offer_discount_pct) == data["counter_offer"]["discount_pct"]
    assert ar.parsed_payload["source"] == "http"
    assert ar.parsed_payload["requested_discount_pct"] == "20"
    assert ar.raw_input is None and ar.confidence is None


# --- atomicity: failure after partial work rolls everything back ---
async def test_atomicity_rollback_when_audit_write_fails(api, monkeypatch) -> None:
    from app.action_requests import service as svc

    real = svc.append_audit_event
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:  # fail on POLICY_EVALUATED, after real DB work happened
            raise RuntimeError("simulated audit failure")
        return await real(*args, **kwargs)

    monkeypatch.setattr(svc, "append_audit_event", flaky)

    resp = await api.client.post(
        "/actions", json=_body(api, "active_agent", "velocity", requested_discount_pct="20")
    )
    assert resp.status_code == 500

    ars, decisions, audits = await _rows(api)
    assert (ars, decisions, audits) == ([], [], []), "no partial state may survive"


# --- guardrails: no LLM / no Razorpay in this layer ----------
async def test_action_api_does_not_import_llm_or_razorpay() -> None:
    """The Action API layer is deterministic: no AI client, no payment client."""
    from pathlib import Path

    import app.action_requests.service as svc

    forbidden = ("anthropic", "openai", "razorpay")
    pkg_dir = Path(svc.__file__).parent
    for path in sorted(pkg_dir.glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                assert not any(mod in stripped for mod in forbidden), (
                    f"{path.name}:{lineno} imports a forbidden module: {stripped}"
                )
        # no float() coercion of money in this layer
        src = path.read_text(encoding="utf-8")
        assert " float(" not in src and "=float(" not in src, f"{path.name}: no float() coercion"
