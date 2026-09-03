"""
Phase 7 — the human approval flow over NEEDS_APPROVAL decisions.

Same test shape as test_action_api.py: a freshly truncated + seeded
`agentgate_test` DB per test, `get_db` overridden with a NullPool factory,
persistence always checked from a separate inspection session (committed state).
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.action_requests.models import ActionRequest
from app.agents.models import Agent
from app.approvals.models import Approval
from app.audit import verify_audit_chain
from app.audit.models import AuditEvent
from app.catalog.models import Merchant, Product
from app.core.config import get_settings
from app.core.db import get_db
from app.core.enums import ApprovalOutcome, Verdict
from app.main import app
from app.policy.models import Decision
from app.razorpay.models import PaymentAttempt
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


class _Api:
    def __init__(self, client, session_factory, ids):
        self.client = client
        self._factory = session_factory
        self.ids = ids

    def session(self):
        return self._factory()

    async def make_decision(self, agent: str, product: str, **over) -> dict:
        body = {"agent_id": str(self.ids[agent]), "product_id": str(self.ids[product])}
        body.update(over)
        resp = await self.client.post("/actions", json=body)
        assert resp.status_code == 200, resp.text
        return resp.json()


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

    def find(mapping, needle):
        return next(v for k, v in mapping.items() if needle in k)

    ids = {
        "active_agent": find(agents, "Reference Buyer"),
        "suspended_agent": find(agents, "Dormant Partner"),
        "velocity": find(products, "Velocity Pro"),
        "trailblaze": find(products, "Trailblaze"),
        "treadmill": find(products, "Treadmill"),
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


async def _needs_approval(api: _Api) -> dict:
    data = await api.make_decision("active_agent", "treadmill")
    assert data["verdict"] == "NEEDS_APPROVAL"
    return data


async def _counts(api: _Api):
    async with api.session() as s:
        approvals = (await s.scalars(select(Approval))).all()
        payments = (await s.scalars(select(PaymentAttempt))).all()
        resolved_events = (
            await s.scalars(
                select(AuditEvent).where(AuditEvent.event_type == "APPROVAL_RESOLVED")
            )
        ).all()
        return approvals, payments, resolved_events


# --- pending queue -------------------------------------------------------
async def test_needs_approval_decision_appears_in_pending(api) -> None:
    d = await _needs_approval(api)
    resp = await api.client.get("/approvals/pending")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    item = items[0]
    assert item["decision_id"] == d["decision_id"]
    assert item["action_request_id"] == d["action_request_id"]
    assert item["original_rule_id"] == "RULE_TRANSACTION_CAP"
    assert "45000" in item["original_reason"]
    assert item["product_name"].startswith("Home Marathon Treadmill")
    assert item["agent_name"].startswith("AgentGate Reference Buyer")
    assert item["product_price"] == "45000.00"


async def test_other_verdicts_do_not_appear_in_pending(api) -> None:
    await api.make_decision("active_agent", "trailblaze", quantity=1)          # ALLOW
    await api.make_decision("suspended_agent", "velocity")                     # DENY
    await api.make_decision("active_agent", "velocity", requested_discount_pct="20")  # COUNTER_OFFER
    resp = await api.client.get("/approvals/pending")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_resolved_decision_leaves_pending_queue(api) -> None:
    d = await _needs_approval(api)
    ok = await api.client.post(
        f"/approvals/{d['decision_id']}/approve", json={"approver": "ops@merchant"}
    )
    assert ok.status_code == 200
    assert (await api.client.get("/approvals/pending")).json() == []


# --- approve ---------------------------------------------------------
async def test_approve_persists_and_audits_without_touching_the_decision(api) -> None:
    d = await _needs_approval(api)
    resp = await api.client.post(
        f"/approvals/{d['decision_id']}/approve", json={"approver": "ops@merchant"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "APPROVED"
    assert body["approver"] == "ops@merchant"
    assert body["decision_id"] == d["decision_id"]
    assert body["action_request_id"] == d["action_request_id"]
    assert body["reason"] is None
    assert body["resolved_at"]

    async with api.session() as s:
        approval = (await s.scalars(select(Approval))).one()
        assert approval.outcome is ApprovalOutcome.APPROVED
        assert approval.approver == "ops@merchant"
        assert str(approval.decision_id) == d["decision_id"]

        decision = (await s.scalars(select(Decision))).one()
        assert decision.verdict is Verdict.NEEDS_APPROVAL  # unchanged
        assert decision.policy_rule_id == "RULE_TRANSACTION_CAP"
        assert decision.reason == d["reason"]

        ar = (await s.scalars(select(ActionRequest))).one()
        assert ar.status.value == "DECIDED"  # unchanged

        events = (
            await s.scalars(select(AuditEvent).order_by(AuditEvent.seq.asc()))
        ).all()
        assert [e.event_type for e in events][-1] == "APPROVAL_RESOLVED"
        resolved = events[-1]
        assert resolved.ref_id == ar.id
        assert resolved.payload["outcome"] == "APPROVED"
        assert resolved.payload["original_verdict"] == "NEEDS_APPROVAL"
        assert resolved.payload["approver"] == "ops@merchant"

        assert (await verify_audit_chain(s)).valid
        assert (await s.scalars(select(PaymentAttempt))).all() == []


async def test_reject_persists_reason_and_audits(api) -> None:
    d = await _needs_approval(api)
    resp = await api.client.post(
        f"/approvals/{d['decision_id']}/reject",
        json={"approver": "cfo@merchant", "reason": "over budget this quarter"},
    )
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "REJECTED"
    assert resp.json()["reason"] == "over budget this quarter"

    approvals, payments, resolved_events = await _counts(api)
    assert len(approvals) == 1 and approvals[0].outcome is ApprovalOutcome.REJECTED
    assert approvals[0].reason == "over budget this quarter"
    assert payments == []
    assert len(resolved_events) == 1
    assert resolved_events[0].payload["outcome"] == "REJECTED"
    assert resolved_events[0].payload["reason"] == "over budget this quarter"


# --- unknown decision ------------------------------------------------
@pytest.mark.parametrize("verb", ["approve", "reject"])
async def test_resolve_unknown_decision_is_404(api, verb) -> None:
    resp = await api.client.post(
        f"/approvals/{uuid.uuid4()}/{verb}", json={"approver": "ops@merchant"}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "DECISION_NOT_FOUND"
    approvals, _, resolved = await _counts(api)
    assert approvals == [] and resolved == []


# --- wrong verdict -------------------------------------------------
@pytest.mark.parametrize("verb", ["approve", "reject"])
@pytest.mark.parametrize(
    "maker",
    [
        ("active_agent", "trailblaze", {"quantity": 1}),               # ALLOW
        ("suspended_agent", "velocity", {}),                           # DENY
        ("active_agent", "velocity", {"requested_discount_pct": "20"}),  # COUNTER_OFFER
    ],
)
async def test_cannot_resolve_non_needs_approval_decision(api, verb, maker) -> None:
    agent, product, over = maker
    d = await api.make_decision(agent, product, **over)
    assert d["verdict"] in {"ALLOW", "DENY", "COUNTER_OFFER"}
    resp = await api.client.post(
        f"/approvals/{d['decision_id']}/{verb}", json={"approver": "ops@merchant"}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "DECISION_NOT_PENDING_APPROVAL"
    approvals, _, resolved = await _counts(api)
    assert approvals == [] and resolved == []


# --- double resolution -------------------------------------------
@pytest.mark.parametrize(
    "first, second",
    [
        ("approve", "approve"),
        ("approve", "reject"),
        ("reject", "approve"),
        ("reject", "reject"),
    ],
)
async def test_second_resolution_is_conflict(api, first, second) -> None:
    d = await _needs_approval(api)
    first_resp = await api.client.post(
        f"/approvals/{d['decision_id']}/{first}", json={"approver": "first@merchant"}
    )
    assert first_resp.status_code == 200
    first_outcome = first_resp.json()["outcome"]

    second_resp = await api.client.post(
        f"/approvals/{d['decision_id']}/{second}", json={"approver": "second@merchant"}
    )
    assert second_resp.status_code == 409
    assert second_resp.json()["detail"]["code"] == "APPROVAL_ALREADY_RESOLVED"

    approvals, _, resolved = await _counts(api)
    assert len(approvals) == 1
    assert approvals[0].outcome.value == first_outcome
    assert approvals[0].approver == "first@merchant"
    assert len(resolved) == 1


async def test_unique_constraint_blocks_a_second_approval_row(api) -> None:
    d = await _needs_approval(api)
    async with api.session() as s:
        s.add(
            Approval(
                decision_id=uuid.UUID(d["decision_id"]),
                approver="a",
                outcome=ApprovalOutcome.APPROVED,
            )
        )
        await s.flush()
        s.add(
            Approval(
                decision_id=uuid.UUID(d["decision_id"]),
                approver="b",
                outcome=ApprovalOutcome.REJECTED,
            )
        )
        with pytest.raises(IntegrityError):
            await s.flush()


async def test_concurrent_resolutions_serialise_to_one(api) -> None:
    import asyncio

    from app.approvals.service import ApprovalConflict, resolve_approval
    from app.core.enums import ApprovalOutcome

    d = await _needs_approval(api)
    decision_id = uuid.UUID(d["decision_id"])

    url = get_settings().database_url
    engine_a = create_async_engine(url, poolclass=NullPool)
    engine_b = create_async_engine(url, poolclass=NullPool)
    make_a = async_sessionmaker(engine_a, expire_on_commit=False)
    make_b = async_sessionmaker(engine_b, expire_on_commit=False)
    try:
        async with make_a() as sa, make_b() as sb:
            # A takes the row lock and does not commit yet.
            await sa.execute(
                select(Decision).where(Decision.id == decision_id).with_for_update()
            )
            second = asyncio.create_task(
                resolve_approval(
                    sb,
                    decision_id=decision_id,
                    outcome=ApprovalOutcome.APPROVED,
                    approver="b@merchant",
                    reason=None,
                )
            )
            await asyncio.sleep(0.4)
            assert not second.done(), "B must block on A's row lock"

            # A resolves and commits.
            await resolve_approval(
                sa,
                decision_id=decision_id,
                outcome=ApprovalOutcome.APPROVED,
                approver="a@merchant",
                reason=None,
            )
            with pytest.raises(ApprovalConflict):
                await asyncio.wait_for(second, timeout=5)
            await sb.rollback()

        async with make_a() as s:
            approvals = (await s.scalars(select(Approval))).all()
            assert len(approvals) == 1 and approvals[0].approver == "a@merchant"
    finally:
        await engine_a.dispose()
        await engine_b.dispose()


# --- body validation -----------------------------------------
@pytest.mark.parametrize(
    "bad_body",
    [
        {},                                  # missing approver
        {"approver": "   "},                 # blank approver
        {"approver": ""},                    # empty approver
        {"approver": "ok", "surprise": 1},   # unknown field
        {"approver": "ok", "reason": "x" * 2001},  # reason too long
    ],
)
async def test_invalid_body_is_422(api, bad_body) -> None:
    d = await _needs_approval(api)
    resp = await api.client.post(f"/approvals/{d['decision_id']}/approve", json=bad_body)
    assert resp.status_code == 422
    approvals, _, resolved = await _counts(api)
    assert approvals == [] and resolved == []


# --- atomicity --------------------------------------------
async def test_atomicity_rollback_when_audit_write_fails(api, monkeypatch) -> None:
    from app.approvals import service as svc

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(svc, "append_audit_event", boom)

    d = await _needs_approval(api)
    resp = await api.client.post(
        f"/approvals/{d['decision_id']}/approve", json={"approver": "ops@merchant"}
    )
    assert resp.status_code == 500

    approvals, payments, resolved = await _counts(api)
    assert approvals == [], "no Approval row may survive a failed audit write"
    assert resolved == [], "no APPROVAL_RESOLVED event may survive"
    assert payments == []


# --- guardrail --------------------------------------------
def test_approval_source_does_not_import_llm_or_razorpay() -> None:
    from pathlib import Path

    import app.approvals.service as svc

    forbidden = ("anthropic", "openai", "razorpay")
    pkg_dir = Path(svc.__file__).parent
    for path in sorted(pkg_dir.glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            s = line.strip()
            if s.startswith(("import ", "from ")):
                assert not any(m in s for m in forbidden), f"{path.name}:{lineno}: {s}"
