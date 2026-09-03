"""
Phase 8 — Razorpay webhook handling: raw-body signature verification,
deduplication, state transitions, unknown/unmatched events, and audit integrity
(including on the failure path).

`FakeRazorpayClient.verify_webhook_signature` runs the exact HMAC-SHA256 the SDK
uses, so these tests exercise real signature logic without the network.
"""
from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agents.models import Agent
from app.audit import verify_audit_chain
from app.audit.models import AuditEvent
from app.catalog.models import Merchant, Product
from app.core.config import get_settings
from app.core.db import get_db
from app.core.enums import PaymentStatus
from app.main import app
from app.razorpay.client import DisabledRazorpayClient, get_razorpay_client
from app.razorpay.models import PaymentAttempt
from app.seed import MERCHANT_NAME, _agents, _products
from app.webhooks.models import WebhookEvent
from tests._fakes import FakeRazorpayClient

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
    def __init__(self, client, factory, ids, fake):
        self.client = client
        self._factory = factory
        self.ids = ids
        self.fake = fake

    def session(self):
        return self._factory()

    async def executed_allow_attempt(self) -> tuple[str, str, str]:
        """Create an ALLOW decision and execute it. Returns (decision_id, action_request_id, plink_id)."""
        body = {
            "agent_id": str(self.ids["active_agent"]),
            "product_id": str(self.ids["trailblaze"]),
            "quantity": 1,
        }
        d = (await self.client.post("/actions", json=body)).json()
        ex = (await self.client.post(f"/payments/{d['decision_id']}/execute")).json()
        return d["decision_id"], d["action_request_id"], ex["razorpay_payment_link_id"]

    async def post_webhook(self, body: dict | bytes, *, event_id: str = "evt_test_1", sign: bool = True):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        headers = {"X-Razorpay-Event-Id": event_id, "Content-Type": "application/json"}
        if sign:
            headers["X-Razorpay-Signature"] = self.fake.sign(raw)
        return await self.client.post("/webhooks/razorpay", content=raw, headers=headers)


@pytest_asyncio.fixture
async def api():
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    fake = FakeRazorpayClient()

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
        "trailblaze": next(v for k, v in products.items() if "Trailblaze" in k),
    }

    async def _override_get_db():
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_razorpay_client] = lambda: fake
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield _Api(client, factory, ids, fake)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_razorpay_client, None)
        await _reset(engine)
        await engine.dispose()


def _link_paid_body(plink_id: str, decision_id: str, ar_id: str, *, event: str = "payment_link.paid", status: str = "paid") -> dict:
    return {
        "entity": "event",
        "event": event,
        "contains": ["payment_link", "payment"],
        "payload": {
            "payment_link": {
                "entity": {
                    "id": plink_id,
                    "status": status,
                    "notes": {"decision_id": decision_id, "action_request_id": ar_id},
                }
            },
            "payment": {"entity": {"id": "pay_testcap", "status": "captured"}},
        },
        "created_at": 1_700_000_000,
    }


async def _events(api: _Api) -> list[str]:
    async with api.session() as s:
        return [
            r.event_type
            for r in (await s.scalars(select(AuditEvent).order_by(AuditEvent.seq.asc()))).all()
        ]


async def _chain_ok(api: _Api) -> bool:
    async with api.session() as s:
        return (await verify_audit_chain(s)).valid


async def _pa_status(api: _Api) -> PaymentStatus:
    async with api.session() as s:
        return (await s.scalars(select(PaymentAttempt))).one().status


async def _webhook_rows(api: _Api) -> list[WebhookEvent]:
    async with api.session() as s:
        return (await s.scalars(select(WebhookEvent))).all()


# ============================================================
async def test_valid_payment_link_paid_marks_paid(api) -> None:
    did, arid, plink = await api.executed_allow_attempt()
    r = await api.post_webhook(_link_paid_body(plink, did, arid))
    assert r.status_code == 200
    assert r.json()["status"] == "processed"
    assert r.json()["payment_status"] == "PAID"

    assert await _pa_status(api) is PaymentStatus.PAID
    rows = await _webhook_rows(api)
    assert len(rows) == 1
    assert rows[0].signature_valid is True
    assert rows[0].event_id == "evt_test_1"
    assert rows[0].payment_attempt_id is not None
    assert rows[0].processed_at is not None

    evs = await _events(api)
    assert evs[-3:] == ["WEBHOOK_RECEIVED", "PAYMENT_STATUS_UPDATED", "PAYMENT_EXECUTION_SUCCEEDED"]
    assert await _chain_ok(api)


async def test_invalid_signature_changes_nothing(api) -> None:
    did, arid, plink = await api.executed_allow_attempt()
    before = await _events(api)

    raw = json.dumps(_link_paid_body(plink, did, arid)).encode("utf-8")
    r = await api.client.post(
        "/webhooks/razorpay",
        content=raw,
        headers={"X-Razorpay-Signature": "deadbeef" * 8, "X-Razorpay-Event-Id": "evt_bad"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_SIGNATURE"
    assert await _webhook_rows(api) == []
    assert await _events(api) == before
    assert await _pa_status(api) is PaymentStatus.PENDING


async def test_missing_signature_header_rejected(api) -> None:
    did, arid, plink = await api.executed_allow_attempt()
    r = await api.post_webhook(_link_paid_body(plink, did, arid), sign=False)
    assert r.status_code == 400
    assert await _webhook_rows(api) == []


async def test_duplicate_webhook_is_idempotent(api) -> None:
    did, arid, plink = await api.executed_allow_attempt()
    body = _link_paid_body(plink, did, arid)

    first = await api.post_webhook(body, event_id="evt_dup")
    assert first.status_code == 200 and first.json()["status"] == "processed"

    second = await api.post_webhook(body, event_id="evt_dup")
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate_ignored"

    assert len(await _webhook_rows(api)) == 1
    assert await _pa_status(api) is PaymentStatus.PAID
    assert (await _events(api)).count("WEBHOOK_RECEIVED") == 1
    assert (await _events(api)).count("PAYMENT_STATUS_UPDATED") == 1
    assert await _chain_ok(api)


async def test_unknown_event_type_is_acknowledged(api) -> None:
    did, arid, plink = await api.executed_allow_attempt()
    body = _link_paid_body(plink, did, arid, event="payment_link.some_future_event")
    r = await api.post_webhook(body, event_id="evt_unknown")
    assert r.status_code == 200
    assert r.json()["status"] == "received_unknown_event"

    assert await _pa_status(api) is PaymentStatus.PENDING  # unchanged
    assert len(await _webhook_rows(api)) == 1
    evs = await _events(api)
    assert evs[-1] == "WEBHOOK_RECEIVED"
    assert "PAYMENT_STATUS_UPDATED" not in evs
    assert await _chain_ok(api)


async def test_webhook_for_untracked_object_is_recorded_not_applied(api) -> None:
    await api.executed_allow_attempt()
    body = {
        "entity": "event",
        "event": "payment_link.paid",
        "payload": {"payment_link": {"entity": {"id": "plink_not_ours", "status": "paid"}}},
        "created_at": 1,
    }
    r = await api.post_webhook(body, event_id="evt_foreign")
    assert r.status_code == 200
    assert r.json()["status"] == "received_unmatched"
    assert len(await _webhook_rows(api)) == 1
    assert await _pa_status(api) is PaymentStatus.PENDING
    assert (await _events(api))[-1] == "WEBHOOK_RECEIVED"
    assert await _chain_ok(api)


async def test_payment_link_expired_marks_expired(api) -> None:
    did, arid, plink = await api.executed_allow_attempt()
    body = _link_paid_body(plink, did, arid, event="payment_link.expired", status="expired")
    r = await api.post_webhook(body, event_id="evt_exp")
    assert r.status_code == 200
    assert await _pa_status(api) is PaymentStatus.EXPIRED
    evs = await _events(api)
    assert "PAYMENT_STATUS_UPDATED" in evs and "PAYMENT_EXECUTION_SUCCEEDED" not in evs


async def test_webhook_on_already_paid_attempt_records_no_transition(api) -> None:
    did, arid, plink = await api.executed_allow_attempt()
    await api.post_webhook(_link_paid_body(plink, did, arid), event_id="evt_paid1")
    before = await _events(api)

    # a second, distinct event that would also mean "paid"
    cap = {
        "entity": "event",
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": "pay_2", "status": "captured", "notes": {"decision_id": did}}}},
        "created_at": 2,
    }
    r = await api.post_webhook(cap, event_id="evt_paid2")
    assert r.status_code == 200
    assert await _pa_status(api) is PaymentStatus.PAID
    after = await _events(api)
    assert after[len(before):] == ["WEBHOOK_RECEIVED"]  # recorded, but no new status update
    assert await _chain_ok(api)


async def test_payment_captured_matches_via_notes(api) -> None:
    did, arid, plink = await api.executed_allow_attempt()
    cap = {
        "entity": "event",
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": "pay_notes", "status": "captured", "notes": {"decision_id": did}}}},
        "created_at": 3,
    }
    r = await api.post_webhook(cap, event_id="evt_capnotes")
    assert r.status_code == 200
    assert await _pa_status(api) is PaymentStatus.PAID


async def test_malformed_json_body_rejected(api) -> None:
    await api.executed_allow_attempt()
    r = await api.post_webhook(b"this is not json")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "MALFORMED_WEBHOOK"
    assert await _webhook_rows(api) == []


async def test_webhook_returns_503_when_disabled(api) -> None:
    did, arid, plink = await api.executed_allow_attempt()  # with the fake
    app.dependency_overrides[get_razorpay_client] = lambda: DisabledRazorpayClient()
    try:
        r = await api.post_webhook(_link_paid_body(plink, did, arid), event_id="evt_disabled")
        assert r.status_code == 503
        assert await _webhook_rows(api) == []
    finally:
        app.dependency_overrides[get_razorpay_client] = lambda: api.fake


async def test_audit_failure_rolls_back_webhook(api, monkeypatch) -> None:
    did, arid, plink = await api.executed_allow_attempt()
    before = await _events(api)

    from app.webhooks import service as wsvc

    async def boom(*a, **k):
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(wsvc, "append_audit_event", boom)

    r = await api.post_webhook(_link_paid_body(plink, did, arid), event_id="evt_auditfail")
    assert r.status_code == 500
    assert await _webhook_rows(api) == []          # rolled back
    assert await _pa_status(api) is PaymentStatus.PENDING  # unchanged
    assert await _events(api) == before
    assert await _chain_ok(api)
