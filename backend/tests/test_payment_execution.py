"""
Phase 8 — payment execution: DB-level authorisation invariant, eligibility,
the execute endpoint, idempotency/concurrency, Razorpay failure, reconciliation,
and config.

Uses `FakeRazorpayClient` (see tests/_fakes.py). Real test-mode verification is
separate — see docs/payment-execution.md.
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agents.models import Agent
from app.approvals.models import Approval
from app.audit import verify_audit_chain
from app.audit.models import AuditEvent
from app.catalog.models import Merchant, Product
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.enums import ApprovalOutcome, PaymentStatus, Verdict
from app.main import app
from app.policy.models import Decision
from app.razorpay.client import DisabledRazorpayClient, RazorpayDisabledError, get_razorpay_client
from app.razorpay.eligibility import can_execute
from app.razorpay.models import PaymentAttempt
from app.razorpay.service import execute_payment, reconcile_payment_attempt
from app.seed import MERCHANT_NAME, _agents, _products
from tests._fakes import FakeRazorpayClient

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
    def __init__(self, client, factory, ids, fake):
        self.client = client
        self._factory = factory
        self.ids = ids
        self.fake = fake

    def session(self):
        return self._factory()

    async def make_decision(self, agent: str, product: str, **over) -> dict:
        body = {"agent_id": str(self.ids[agent]), "product_id": str(self.ids[product])}
        body.update(over)
        r = await self.client.post("/actions", json=body)
        assert r.status_code == 200, r.text
        return r.json()

    async def approve(self, decision_id: str, approver: str = "ops@merchant"):
        return await self.client.post(
            f"/approvals/{decision_id}/approve", json={"approver": approver}
        )

    async def reject(self, decision_id: str, approver: str = "cfo@merchant"):
        return await self.client.post(
            f"/approvals/{decision_id}/reject", json={"approver": approver}
        )

    async def execute(self, decision_id: str):
        return await self.client.post(f"/payments/{decision_id}/execute")

    async def status(self, decision_id: str):
        return await self.client.get(f"/payments/{decision_id}")

    async def reconcile(self, decision_id: str):
        return await self.client.post(f"/payments/{decision_id}/reconcile")


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

    def find(m, needle):
        return next(v for k, v in m.items() if needle in k)

    ids = {
        "active_agent": find(agents, "Reference Buyer"),
        "suspended_agent": find(agents, "Dormant Partner"),
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


async def _decision(api: _Api, decision_id: str) -> Decision:
    async with api.session() as s:
        return await s.get(Decision, uuid.UUID(decision_id))


async def _events(api: _Api) -> list[str]:
    async with api.session() as s:
        rows = (await s.scalars(select(AuditEvent).order_by(AuditEvent.seq.asc()))).all()
        return [r.event_type for r in rows]


async def _chain_ok(api: _Api) -> bool:
    async with api.session() as s:
        return (await verify_audit_chain(s)).valid


# ============================ eligibility ================================
async def test_eligibility_allow(api) -> None:
    d = await api.make_decision("active_agent", "trailblaze", quantity=1)
    async with api.session() as s:
        elig = await can_execute(s, await s.get(Decision, uuid.UUID(d["decision_id"])))
    assert elig.eligible and elig.reason_code == "ALLOW_VERDICT" and elig.approval is None


async def test_eligibility_needs_approval_approved(api) -> None:
    d = await api.make_decision("active_agent", "treadmill")
    await api.approve(d["decision_id"])
    async with api.session() as s:
        elig = await can_execute(s, await s.get(Decision, uuid.UUID(d["decision_id"])))
    assert elig.eligible and elig.reason_code == "NEEDS_APPROVAL_APPROVED"
    assert elig.approval is not None and elig.approval.outcome is ApprovalOutcome.APPROVED


async def test_eligibility_needs_approval_not_approved(api) -> None:
    d = await api.make_decision("active_agent", "treadmill")
    async with api.session() as s:
        elig = await can_execute(s, await s.get(Decision, uuid.UUID(d["decision_id"])))
    assert not elig.eligible and elig.reason_code == "NEEDS_APPROVAL_NOT_APPROVED"


async def test_eligibility_needs_approval_rejected(api) -> None:
    d = await api.make_decision("active_agent", "treadmill")
    await api.reject(d["decision_id"])
    async with api.session() as s:
        elig = await can_execute(s, await s.get(Decision, uuid.UUID(d["decision_id"])))
    assert not elig.eligible and elig.reason_code == "NEEDS_APPROVAL_REJECTED"


@pytest.mark.parametrize(
    "maker",
    [("suspended_agent", "velocity", {}), ("active_agent", "velocity", {"requested_discount_pct": "20"})],
)
async def test_eligibility_non_executable_verdicts(api, maker) -> None:
    agent, product, over = maker
    d = await api.make_decision(agent, product, **over)
    assert d["verdict"] in {"DENY", "COUNTER_OFFER"}
    async with api.session() as s:
        elig = await can_execute(s, await s.get(Decision, uuid.UUID(d["decision_id"])))
    assert not elig.eligible and elig.reason_code == "VERDICT_NOT_EXECUTABLE"


# ==================== DB-level authorisation invariant ===================
async def _raw_ids(api: _Api, d: dict):
    async with api.session() as s:
        dec = await s.get(Decision, uuid.UUID(d["decision_id"]))
        appr = (
            await s.scalars(select(Approval).where(Approval.decision_id == dec.id))
        ).one_or_none()
        return dec, appr


async def test_db_blocks_payment_for_deny(api) -> None:
    d = await api.make_decision("suspended_agent", "velocity")
    dec, _ = await _raw_ids(api, d)
    async with api.session() as s:
        s.add(
            PaymentAttempt(
                decision_id=dec.id, decision_verdict=Verdict.DENY,
                idempotency_key=f"x-{uuid.uuid4()}",
            )
        )
        with pytest.raises(IntegrityError):
            await s.flush()


async def test_db_blocks_payment_for_counter_offer(api) -> None:
    d = await api.make_decision("active_agent", "velocity", requested_discount_pct="20")
    dec, _ = await _raw_ids(api, d)
    async with api.session() as s:
        s.add(
            PaymentAttempt(
                decision_id=dec.id, decision_verdict=Verdict.COUNTER_OFFER,
                idempotency_key=f"x-{uuid.uuid4()}",
            )
        )
        with pytest.raises(IntegrityError):
            await s.flush()


async def test_db_blocks_needs_approval_without_approval(api) -> None:
    d = await api.make_decision("active_agent", "treadmill")
    dec, _ = await _raw_ids(api, d)
    async with api.session() as s:
        s.add(
            PaymentAttempt(
                decision_id=dec.id, decision_verdict=Verdict.NEEDS_APPROVAL,
                approval_outcome=ApprovalOutcome.APPROVED,  # but approval_id NULL
                idempotency_key=f"x-{uuid.uuid4()}",
            )
        )
        with pytest.raises(IntegrityError):
            await s.flush()


async def test_db_blocks_needs_approval_with_rejected_approval(api) -> None:
    d = await api.make_decision("active_agent", "treadmill")
    await api.reject(d["decision_id"])
    dec, appr = await _raw_ids(api, d)
    async with api.session() as s:
        s.add(
            PaymentAttempt(
                decision_id=dec.id, decision_verdict=Verdict.NEEDS_APPROVAL,
                approval_id=appr.id, approval_outcome=ApprovalOutcome.APPROVED,  # lie
                idempotency_key=f"x-{uuid.uuid4()}",
            )
        )
        with pytest.raises(IntegrityError):
            await s.flush()  # composite FK (id, 'APPROVED') has no match


async def test_db_allows_payment_for_allow(api) -> None:
    d = await api.make_decision("active_agent", "trailblaze", quantity=1)
    dec, _ = await _raw_ids(api, d)
    async with api.session() as s:
        s.add(
            PaymentAttempt(
                decision_id=dec.id, decision_verdict=Verdict.ALLOW,
                idempotency_key=f"x-{uuid.uuid4()}",
            )
        )
        await s.flush()  # must not raise


async def test_db_allows_payment_for_approved_needs_approval(api) -> None:
    d = await api.make_decision("active_agent", "treadmill")
    await api.approve(d["decision_id"])
    dec, appr = await _raw_ids(api, d)
    async with api.session() as s:
        s.add(
            PaymentAttempt(
                decision_id=dec.id, decision_verdict=Verdict.NEEDS_APPROVAL,
                approval_id=appr.id, approval_outcome=ApprovalOutcome.APPROVED,
                idempotency_key=f"x-{uuid.uuid4()}",
            )
        )
        await s.flush()  # must not raise


# ========================= execute endpoint =============================
async def test_execute_allow_creates_payment_link(api) -> None:
    d = await api.make_decision("active_agent", "trailblaze", quantity=1)
    r = await api.execute(d["decision_id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "PENDING"
    assert body["razorpay_payment_link_id"].startswith("plink_test_")
    assert body["short_url"].startswith("https://rzp.test/")
    assert body["amount"] == "4500.00"
    assert body["already_existed"] is False

    async with api.session() as s:
        pa = (await s.scalars(select(PaymentAttempt))).one()
        assert pa.status is PaymentStatus.PENDING
        assert pa.decision_verdict is Verdict.ALLOW
        assert pa.approval_id is None
        assert pa.idempotency_key == f"decision:{d['decision_id']}"
        dec = await s.get(Decision, uuid.UUID(d["decision_id"]))
        assert dec.executable_amount == D("4500.00")

    assert await _events(api) == [
        "ACTION_REQUEST_RECEIVED", "POLICY_EVALUATED",
        "PAYMENT_EXECUTION_STARTED", "PAYMENT_EXECUTION_CREATED",
    ]
    assert await _chain_ok(api)
    assert len(api.fake.create_calls) == 1
    assert api.fake.create_calls[0]["amount_paise"] == 450000


async def test_execute_amount_comes_from_decision_not_request(api) -> None:
    d = await api.make_decision("active_agent", "treadmill")  # ₹45,000 -> NEEDS_APPROVAL
    await api.approve(d["decision_id"])
    r = await api.execute(d["decision_id"])
    assert r.status_code == 200
    assert r.json()["amount"] == "45000.00"
    assert api.fake.create_calls[0]["amount_paise"] == 4500000

    async with api.session() as s:
        pa = (await s.scalars(select(PaymentAttempt))).one()
        appr = (await s.scalars(select(Approval))).one()
        assert pa.decision_verdict is Verdict.NEEDS_APPROVAL
        assert pa.approval_id == appr.id
        assert pa.approval_outcome is ApprovalOutcome.APPROVED


@pytest.mark.parametrize(
    "maker, code",
    [
        (("suspended_agent", "velocity", {}), "VERDICT_NOT_EXECUTABLE"),
        (("active_agent", "velocity", {"requested_discount_pct": "20"}), "VERDICT_NOT_EXECUTABLE"),
    ],
)
async def test_execute_rejects_non_executable_verdict(api, maker, code) -> None:
    agent, product, over = maker
    d = await api.make_decision(agent, product, **over)
    r = await api.execute(d["decision_id"])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == code
    async with api.session() as s:
        assert (await s.scalars(select(PaymentAttempt))).all() == []
    assert "PAYMENT_EXECUTION_STARTED" not in await _events(api)


async def test_execute_rejects_unapproved_needs_approval(api) -> None:
    d = await api.make_decision("active_agent", "treadmill")
    r = await api.execute(d["decision_id"])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "NEEDS_APPROVAL_NOT_APPROVED"


async def test_execute_rejects_rejected_needs_approval(api) -> None:
    d = await api.make_decision("active_agent", "treadmill")
    await api.reject(d["decision_id"])
    r = await api.execute(d["decision_id"])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "NEEDS_APPROVAL_REJECTED"


async def test_execute_unknown_decision_404(api) -> None:
    r = await api.execute(str(uuid.uuid4()))
    assert r.status_code == 404


# ===================== idempotency / concurrency ========================
async def test_sequential_duplicate_execute_reuses_attempt(api) -> None:
    d = await api.make_decision("active_agent", "trailblaze", quantity=1)
    first = (await api.execute(d["decision_id"])).json()
    second_resp = await api.execute(d["decision_id"])
    assert second_resp.status_code == 200
    second = second_resp.json()
    assert second["already_existed"] is True
    assert second["payment_attempt_id"] == first["payment_attempt_id"]
    assert len(api.fake.create_calls) == 1
    async with api.session() as s:
        assert len((await s.scalars(select(PaymentAttempt))).all()) == 1


async def test_concurrent_execute_creates_one_object(api) -> None:
    """
    Two executions race on the same decision. The decision row lock serialises
    them: one creates the payment object, the other sees an attempt already
    in progress and gets a conflict. Never two Razorpay objects, never two rows.
    """
    from app.razorpay.service import ExecutionConflict

    d = await api.make_decision("active_agent", "trailblaze", quantity=1)
    decision_id = uuid.UUID(d["decision_id"])
    url = get_settings().database_url
    e1 = create_async_engine(url, poolclass=NullPool)
    e2 = create_async_engine(url, poolclass=NullPool)
    m1 = async_sessionmaker(e1, expire_on_commit=False)
    m2 = async_sessionmaker(e2, expire_on_commit=False)
    try:
        async with m1() as s1, m2() as s2:
            results = await asyncio.gather(
                execute_payment(s1, api.fake, decision_id),
                execute_payment(s2, api.fake, decision_id),
                return_exceptions=True,
            )
        ok = [r for r in results if not isinstance(r, Exception)]
        conflicts = [r for r in results if isinstance(r, ExecutionConflict)]
        assert len(ok) == 1 and len(conflicts) == 1
        assert conflicts[0].code == "EXECUTION_IN_PROGRESS"
        async with m1() as s:
            assert len((await s.scalars(select(PaymentAttempt))).all()) == 1
        assert len(api.fake.create_calls) == 1
    finally:
        await e1.dispose()
        await e2.dispose()


# ========================= Razorpay failure ============================
async def test_razorpay_failure_marks_attempt_failed_not_paid(api) -> None:
    api.fake.fail_create = True
    d = await api.make_decision("active_agent", "trailblaze", quantity=1)
    r = await api.execute(d["decision_id"])
    assert r.status_code == 502
    assert r.json()["detail"]["code"] == "RAZORPAY_CREATE_FAILED"

    async with api.session() as s:
        pa = (await s.scalars(select(PaymentAttempt))).one()
        assert pa.status is PaymentStatus.FAILED
        assert pa.razorpay_payment_link_id is None

    evs = await _events(api)
    assert evs[-2:] == ["PAYMENT_EXECUTION_STARTED", "PAYMENT_EXECUTION_FAILED"]
    assert "PAYMENT_EXECUTION_CREATED" not in evs
    assert "PAYMENT_EXECUTION_SUCCEEDED" not in evs
    assert await _chain_ok(api)

    async with api.session() as s:
        failed_ev = (
            await s.scalars(
                select(AuditEvent).where(AuditEvent.event_type == "PAYMENT_EXECUTION_FAILED")
            )
        ).one()
        # no secrets / raw body in the audit payload
        assert "whsec" not in str(failed_ev.payload)
        assert failed_ev.payload["error"].startswith("payment_link.create failed")


async def test_retry_after_failure_is_terminal_conflict(api) -> None:
    api.fake.fail_create = True
    d = await api.make_decision("active_agent", "trailblaze", quantity=1)
    assert (await api.execute(d["decision_id"])).status_code == 502
    api.fake.fail_create = False
    retry = await api.execute(d["decision_id"])
    assert retry.status_code == 409
    assert retry.json()["detail"]["code"] == "EXECUTION_TERMINAL_FAILED"
    assert len(api.fake.create_calls) == 1


# ========================= reconciliation =============================
async def _orphan_attempt(api: _Api, decision_id: str) -> uuid.UUID:
    """A CREATED attempt with no Razorpay id, as left by a crash between Txn1 and Txn2."""
    async with api.session() as s:
        dec = await s.get(Decision, uuid.UUID(decision_id))
        pa = PaymentAttempt(
            decision_id=dec.id, decision_verdict=Verdict.ALLOW,
            idempotency_key=f"decision:{decision_id}", status=PaymentStatus.CREATED,
        )
        s.add(pa)
        await s.flush()
        pid = pa.id
        await s.commit()
    return pid


async def test_reconcile_adopts_orphaned_razorpay_object(api) -> None:
    d = await api.make_decision("active_agent", "trailblaze", quantity=1)
    pid = await _orphan_attempt(api, d["decision_id"])
    api.fake.register_link(str(pid), "plink_test_recovered", status="created")

    r = await api.reconcile(d["decision_id"])
    assert r.status_code == 200
    assert r.json()["status"] == "PENDING"
    assert r.json()["razorpay_payment_link_id"] == "plink_test_recovered"

    async with api.session() as s:
        pa = await s.get(PaymentAttempt, pid)
        assert pa.status is PaymentStatus.PENDING
    evs = await _events(api)
    assert "PAYMENT_EXECUTION_CREATED" in evs and "PAYMENT_STATUS_UPDATED" in evs
    assert await _chain_ok(api)


async def test_reconcile_marks_failed_when_no_object_exists(api) -> None:
    d = await api.make_decision("active_agent", "trailblaze", quantity=1)
    pid = await _orphan_attempt(api, d["decision_id"])
    # nothing registered in the fake -> no object was ever created

    r = await api.reconcile(d["decision_id"])
    assert r.status_code == 200
    assert r.json()["status"] == "FAILED"
    async with api.session() as s:
        pa = await s.get(PaymentAttempt, pid)
        assert pa.status is PaymentStatus.FAILED
    assert "PAYMENT_EXECUTION_FAILED" in await _events(api)
    assert await _chain_ok(api)


async def test_reconcile_pending_to_paid(api) -> None:
    d = await api.make_decision("active_agent", "trailblaze", quantity=1)
    await api.execute(d["decision_id"])
    async with api.session() as s:
        pa = (await s.scalars(select(PaymentAttempt))).one()
    api.fake.set_reference_status(str(pa.id), "paid")

    r = await api.reconcile(d["decision_id"])
    assert r.status_code == 200 and r.json()["status"] == "PAID"
    async with api.session() as s:
        assert (await s.scalars(select(PaymentAttempt))).one().status is PaymentStatus.PAID
    assert "PAYMENT_STATUS_UPDATED" in await _events(api)
    assert await _chain_ok(api)


async def test_reconcile_noop_when_terminal(api) -> None:
    d = await api.make_decision("active_agent", "trailblaze", quantity=1)
    await api.execute(d["decision_id"])
    async with api.session() as s:
        pa = (await s.scalars(select(PaymentAttempt))).one()
    api.fake.set_reference_status(str(pa.id), "paid")
    await api.reconcile(d["decision_id"])  # -> PAID
    before = await _events(api)
    r = await api.reconcile(d["decision_id"])  # again
    assert r.status_code == 200 and r.json()["already_existed"] is True
    assert await _events(api) == before  # no new audit events


# ============================= config ===============================
def test_get_razorpay_client_disabled_by_default() -> None:
    assert isinstance(get_razorpay_client(), DisabledRazorpayClient)


async def test_execute_returns_503_when_client_disabled(api) -> None:
    app.dependency_overrides[get_razorpay_client] = lambda: DisabledRazorpayClient()
    try:
        d = await api.make_decision("active_agent", "trailblaze", quantity=1)
        r = await api.execute(d["decision_id"])
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "RAZORPAY_DISABLED"
    finally:
        app.dependency_overrides[get_razorpay_client] = lambda: api.fake


async def test_disabled_client_raises() -> None:
    with pytest.raises(RazorpayDisabledError):
        await DisabledRazorpayClient().create_payment_link()


def test_settings_reject_enabled_with_placeholder_credentials() -> None:
    with pytest.raises(Exception):
        Settings(
            database_url="postgresql+asyncpg://x/y",
            gemini_api_key="k",
            razorpay_enabled=True,
            razorpay_key_id="rzp_test_placeholder",
            razorpay_key_secret="placeholder_secret",
            razorpay_webhook_secret="placeholder_webhook_secret",
        )


def test_settings_accept_enabled_with_real_looking_credentials() -> None:
    s = Settings(
        database_url="postgresql+asyncpg://x/y",
        gemini_api_key="k",
        razorpay_enabled=True,
        razorpay_key_id="rzp_test_ABC123",
        razorpay_key_secret="secret_ABC123",
        razorpay_webhook_secret="whsec_ABC123",
    )
    assert s.razorpay_enabled is True


def test_settings_public_base_url_strips_trailing_slash() -> None:
    s = Settings(
        database_url="postgresql+asyncpg://x/y",
        gemini_api_key="k",
        razorpay_key_id="x",
        razorpay_key_secret="x",
        razorpay_webhook_secret="x",
        public_base_url="https://agentgate.example.com/",
    )
    assert s.public_base_url == "https://agentgate.example.com"


# ==================== GET /payments/{decision_id} (Phase 14 UI) ====================
# Read-only status for the customer-facing payment-result page: no Razorpay
# call, no row lock, no write — just what execute/reconcile/the webhook already
# recorded.

async def test_status_unknown_decision_404(api) -> None:
    r = await api.status(str(uuid.uuid4()))
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DECISION_NOT_FOUND"


async def test_status_before_execute_is_409_no_attempt(api) -> None:
    d = await api.make_decision("active_agent", "trailblaze", quantity=1)
    r = await api.status(d["decision_id"])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "NO_PAYMENT_ATTEMPT"


async def test_status_matches_execute_response(api) -> None:
    d = await api.make_decision("active_agent", "trailblaze", quantity=1)
    executed = (await api.execute(d["decision_id"])).json()

    r = await api.status(d["decision_id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "PENDING"
    assert body["amount"] == executed["amount"] == "4500.00"
    assert body["razorpay_payment_link_id"] == executed["razorpay_payment_link_id"]
    assert body["short_url"] == executed["short_url"]
    # a GET never creates or mutates an attempt, and never calls Razorpay again
    assert body["already_existed"] is True
    assert len(api.fake.create_calls) == 1


async def test_status_does_not_call_razorpay(api) -> None:
    """A poll loop hitting this every few seconds must never touch Razorpay."""
    d = await api.make_decision("active_agent", "trailblaze", quantity=1)
    await api.execute(d["decision_id"])
    for _ in range(5):
        r = await api.status(d["decision_id"])
        assert r.status_code == 200
    assert len(api.fake.create_calls) == 1


# ==================== callback_url (Phase 14 customer return flow) ====================

async def test_execute_passes_no_callback_when_public_base_url_unset(api) -> None:
    d = await api.make_decision("active_agent", "trailblaze", quantity=1)
    await api.execute(d["decision_id"])
    assert api.fake.create_calls[0]["callback_url"] is None


async def test_execute_passes_callback_url_when_public_base_url_set(api, monkeypatch) -> None:
    from app.core.config import Settings as _Settings
    import app.razorpay.service as service_module

    stub = _Settings(
        database_url="postgresql+asyncpg://x/y",
        razorpay_key_id="x",
        razorpay_key_secret="x",
        razorpay_webhook_secret="x",
        public_base_url="https://agentgate.example.com",
    )
    monkeypatch.setattr(service_module, "get_settings", lambda: stub)

    d = await api.make_decision("active_agent", "trailblaze", quantity=1)
    await api.execute(d["decision_id"])

    call = api.fake.create_calls[0]
    assert call["callback_url"] == (
        f"https://agentgate.example.com/?payment_callback=1&decision_id={d['decision_id']}"
    )
    assert call["callback_method"] == "get"
