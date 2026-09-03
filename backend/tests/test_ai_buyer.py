"""
Phase 10 — the bounded AI buyer agent.

Every test drives the agent with a scripted `FakeAIBuyerClient`; no real
Anthropic call. The point is that the agent — however it behaves — cannot get a
verdict it did not earn from the deterministic policy engine, cannot see the
merchant's discount/margin policy, and cannot touch Razorpay.
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
from app.ai.client import AIUnavailableError, AnthropicBuyerClient, DisabledBuyerClient, get_ai_buyer_client
from app.audit import verify_audit_chain
from app.audit.models import AuditEvent
from app.catalog.models import Merchant, Product
from app.core.config import get_settings
from app.core.db import get_db
from app.core.enums import Verdict
from app.main import app
from app.policy.models import Decision
from app.razorpay.models import PaymentAttempt
from app.seed import MERCHANT_NAME, _agents, _products
from tests._fakes import FakeAIBuyerClient, final_step, tool_step

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
    def __init__(self, client, factory, ids):
        self.client = client
        self._factory = factory
        self.ids = ids
        self.last_fake: FakeAIBuyerClient | None = None

    def session(self):
        return self._factory()

    async def run(self, *steps, agent: str = "active_agent", goal: str = "buy something nice",
                  error=None, error_after: int = 0):
        fake = FakeAIBuyerClient(script=list(steps), error=error, error_after=error_after)
        self.last_fake = fake
        app.dependency_overrides[get_ai_buyer_client] = lambda: fake
        return await self.client.post(
            "/ai/buyer", json={"agent_id": str(self.ids[agent]), "goal": goal}
        )

    async def rows(self):
        async with self.session() as s:
            ars = (await s.scalars(select(ActionRequest))).all()
            decisions = (await s.scalars(select(Decision))).all()
            audits = (await s.scalars(select(AuditEvent).order_by(AuditEvent.seq.asc()))).all()
            payments = (await s.scalars(select(PaymentAttempt))).all()
            return ars, decisions, audits, payments

    async def chain_ok(self) -> bool:
        async with self.session() as s:
            return (await verify_audit_chain(s)).valid


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

    def find(m, needle):
        return next(v for k, v in m.items() if needle in k)

    ids = {
        "active_agent": find(agents, "Reference Buyer"),
        "suspended_agent": find(agents, "Dormant Partner"),
        "velocity": find(products, "Velocity Pro"),
        "trailblaze": find(products, "Trailblaze"),
        "treadmill": find(products, "Treadmill"),
        "featherlite": find(products, "Featherlite"),
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
        app.dependency_overrides.pop(get_ai_buyer_client, None)
        await _reset(engine)
        await engine.dispose()


def _ra(api: _Api, product: str, **fields) -> tuple[str, dict]:
    return ("request_action", {"product_id": str(api.ids[product]), "action_type": "PURCHASE", **fields})


# ========================= happy paths ================================
async def test_search_then_purchase_is_allowed(api) -> None:
    r = await api.run(
        tool_step(("search_catalog", {"query": "trailblaze"})),
        tool_step(_ra(api, "trailblaze", quantity=1)),
        final_step("Bought the Trailblaze Daily Trainer."),
        goal="buy the trailblaze daily trainer",
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "purchased"
    assert body["final_decision"]["verdict"] == "ALLOW"
    assert body["request_action_count"] == 1
    assert body["summary"] == "Bought the Trailblaze Daily Trainer."

    ars, decisions, audits, payments = await api.rows()
    assert len(ars) == 1 and len(decisions) == 1 and payments == []
    assert decisions[0].verdict is Verdict.ALLOW
    assert ars[0].parsed_payload["source"] == "ai_buyer"
    assert [e.event_type for e in audits] == [
        "ACTION_REQUEST_RECEIVED", "ACTION_PARSED", "POLICY_EVALUATED",
    ]
    assert await api.chain_ok()
    assert not any(e.event_type.startswith("PAYMENT_") for e in audits)

    # the search result the model saw carried no policy fields
    search_results = next(
        e for e in body["transcript"]
        if e["kind"] == "tool_result" and e["tool"] == "search_catalog"
    )["detail"]["results"]
    assert search_results
    for row in search_results:
        assert set(row) == {"product_id", "name", "description", "category", "price", "in_stock", "stock"}
        assert "max_discount_pct" not in row and "min_margin_price" not in row


async def test_counter_offer_then_accept(api) -> None:
    r = await api.run(
        tool_step(("search_catalog", {"query": "velocity"})),
        tool_step(_ra(api, "velocity", requested_discount_pct="20")),
        tool_step(_ra(api, "velocity", action_type="ACCEPT_COUNTER_OFFER", proposed_price="9000")),
        final_step("Accepted AgentGate's ₹9,000 counter-offer."),
    )
    body = r.json()
    assert body["outcome"] == "counter_offer_accepted"
    assert body["final_decision"]["verdict"] == "ALLOW"
    assert body["request_action_count"] == 2

    ars, decisions, audits, payments = await api.rows()
    verdicts = sorted(d.verdict.value for d in decisions)
    assert verdicts == ["ALLOW", "COUNTER_OFFER"]
    assert payments == []
    assert await api.chain_ok()


async def test_counter_offer_received_not_accepted(api) -> None:
    r = await api.run(
        tool_step(_ra(api, "velocity", requested_discount_pct="20")),
        final_step("AgentGate counter-offered ₹9,000; stopping for user review."),
    )
    body = r.json()
    assert body["outcome"] == "counter_offer_received"
    assert body["final_decision"]["verdict"] == "COUNTER_OFFER"
    assert body["final_decision"]["counter_offer"] == {"price": "9000.00", "discount_pct": "10.00"}


# ================== the agent cannot bypass policy ====================
async def test_agent_proposing_one_rupee_still_gets_the_engine_floor(api) -> None:
    r = await api.run(
        tool_step(_ra(api, "velocity", proposed_price="1")),
        final_step("tried a lowball"),
    )
    body = r.json()
    assert body["final_decision"]["verdict"] == "COUNTER_OFFER"  # never ALLOW at ₹1
    assert body["final_decision"]["counter_offer"]["price"] == "9000.00"  # engine's number


async def test_inactive_agent_run_is_denied_by_policy(api) -> None:
    r = await api.run(
        tool_step(_ra(api, "velocity")),
        final_step("blocked"),
        agent="suspended_agent",
    )
    body = r.json()
    assert body["outcome"] == "denied"
    assert body["final_decision"]["verdict"] == "DENY"
    assert body["final_decision"]["rule_id"] == "RULE_AGENT_ACTIVE"


async def test_high_value_run_needs_approval(api) -> None:
    r = await api.run(
        tool_step(_ra(api, "treadmill")),
        final_step("routed to approval"),
    )
    body = r.json()
    assert body["outcome"] == "needs_approval"
    assert body["final_decision"]["verdict"] == "NEEDS_APPROVAL"
    _, _, audits, _ = await api.rows()
    assert "APPROVAL_REQUESTED" in [e.event_type for e in audits]


async def test_llm_cannot_impersonate_a_different_agent(api) -> None:
    # an extra agent_id in the tool input is ignored; the run's agent is used
    other = str(api.ids["suspended_agent"])
    r = await api.run(
        tool_step(("request_action", {
            "product_id": str(api.ids["trailblaze"]),
            "action_type": "PURCHASE",
            "agent_id": other,  # ignored
        })),
        final_step("done"),
    )
    assert r.json()["final_decision"]["verdict"] == "ALLOW"  # active agent, not suspended
    ars, _, _, _ = await api.rows()
    assert str(ars[0].agent_id) == str(api.ids["active_agent"])


# ========================= budgets ===================================
async def test_request_action_budget_is_enforced(api) -> None:
    # default max_request_actions is 3
    r = await api.run(
        tool_step(_ra(api, "trailblaze")),
        tool_step(_ra(api, "trailblaze")),
        tool_step(_ra(api, "trailblaze")),
        tool_step(_ra(api, "trailblaze")),  # 4th -> refused, never reaches the engine
        final_step("stopped"),
    )
    body = r.json()
    assert body["request_action_count"] == 3
    _, decisions, _, _ = await api.rows()
    assert len(decisions) == 3
    refused = [
        e for e in body["transcript"]
        if e["kind"] == "tool_result" and isinstance(e["detail"], dict) and "budget" in str(e["detail"].get("error", "")).lower()
    ]
    assert refused


async def test_step_budget_is_enforced(api) -> None:
    # default max_steps is 8 — feed 8 read-only steps and no final
    r = await api.run(*[tool_step(("search_catalog", {"query": "shoe"})) for _ in range(8)])
    body = r.json()
    assert body["outcome"] == "budget_exhausted"
    assert body["steps_used"] == 8
    assert body["final_decision"] is None
    assert api.last_fake.step_calls == 8


# ================== catalogue read tools =============================
async def test_get_product_and_compare(api) -> None:
    vid, tid = str(api.ids["velocity"]), str(api.ids["trailblaze"])
    r = await api.run(
        tool_step(("get_product", {"product_id": vid})),
        tool_step(("compare_products", {"product_ids": [vid, tid]})),
        final_step("compared"),
    )
    tr = r.json()["transcript"]
    gp = next(e for e in tr if e["kind"] == "tool_result" and e["tool"] == "get_product")["detail"]
    assert gp["product_id"] == vid and "max_discount_pct" not in gp
    cmp = next(e for e in tr if e["kind"] == "tool_result" and e["tool"] == "compare_products")["detail"]
    assert {p["product_id"] for p in cmp["products"]} == {vid, tid}


async def test_search_respects_max_price(api) -> None:
    r = await api.run(
        tool_step(("search_catalog", {"query": "running", "max_price_inr": "5000"})),
        final_step("looked"),
    )
    results = next(
        e for e in r.json()["transcript"]
        if e["kind"] == "tool_result" and e["tool"] == "search_catalog"
    )["detail"]["results"]
    assert results
    assert all(Decimal(row["price"]) <= Decimal("5000") for row in results)


# ================== hallucination / bad input ========================
async def test_hallucinated_product_id_in_request_action(api) -> None:
    bogus = str(uuid.uuid4())
    r = await api.run(
        tool_step(("request_action", {"product_id": bogus, "action_type": "PURCHASE"})),
        final_step("that product does not exist"),
    )
    body = r.json()
    assert body["final_decision"] is None
    assert body["outcome"] == "no_action"
    ars, decisions, _, _ = await api.rows()
    assert (ars, decisions) == ([], [])  # nothing persisted for a non-existent product
    err = next(
        e for e in body["transcript"]
        if e["kind"] == "tool_result" and e["tool"] == "request_action"
    )["detail"]
    assert "error" in err


async def test_hallucinated_product_id_in_get_product(api) -> None:
    r = await api.run(
        tool_step(("get_product", {"product_id": str(uuid.uuid4())})),
        final_step("no such product"),
    )
    detail = next(
        e for e in r.json()["transcript"]
        if e["kind"] == "tool_result" and e["tool"] == "get_product"
    )["detail"]
    assert detail == {"error": "no such product"}


async def test_malformed_decimal_tool_input_is_rejected(api) -> None:
    r = await api.run(
        tool_step(("request_action", {
            "product_id": str(api.ids["velocity"]), "action_type": "PURCHASE",
            "requested_discount_pct": "twenty-ish",
        })),
        final_step("bad number"),
    )
    err = next(
        e for e in r.json()["transcript"]
        if e["kind"] == "tool_result" and e["tool"] == "request_action"
    )["detail"]
    assert "error" in err
    _, decisions, _, _ = await api.rows()
    assert decisions == []


# ================== provider / config failures =======================
async def test_ai_unavailable_at_step_one(api) -> None:
    r = await api.run(final_step("unused"), error=AIUnavailableError("timeout"))
    body = r.json()
    assert r.status_code == 200
    assert body["outcome"] == "ai_unavailable"
    assert body["final_decision"] is None
    assert body["steps_used"] == 1
    ars, decisions, audits, payments = await api.rows()
    assert (ars, decisions, audits, payments) == ([], [], [], [])


async def test_ai_unavailable_after_a_committed_action(api) -> None:
    r = await api.run(
        tool_step(_ra(api, "trailblaze")),
        error=AIUnavailableError("boom"),
        error_after=1,  # step 1 runs, step 2 raises
    )
    body = r.json()
    assert body["outcome"] == "ai_unavailable"
    assert body["final_decision"]["verdict"] == "ALLOW"  # step 1's decision survives
    _, decisions, _, _ = await api.rows()
    assert len(decisions) == 1
    assert await api.chain_ok()


async def test_disabled_returns_503_persists_nothing(api) -> None:
    app.dependency_overrides[get_ai_buyer_client] = lambda: DisabledBuyerClient()
    r = await api.client.post(
        "/ai/buyer", json={"agent_id": str(api.ids["active_agent"]), "goal": "buy stuff"}
    )
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "AI_DISABLED"
    assert await api.rows() == ([], [], [], [])


async def test_unknown_agent_404(api) -> None:
    app.dependency_overrides[get_ai_buyer_client] = lambda: FakeAIBuyerClient(script=[final_step("x")])
    r = await api.client.post(
        "/ai/buyer", json={"agent_id": str(uuid.uuid4()), "goal": "buy stuff"}
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "AGENT_NOT_FOUND"
    assert await api.rows() == ([], [], [], [])


@pytest.mark.parametrize("body", [
    {"goal": "hi"},                                    # missing agent_id
    {"agent_id": "not-a-uuid", "goal": "hi"},          # bad uuid
    {"agent_id": str(uuid.uuid4()), "goal": ""},       # empty goal
    {"agent_id": str(uuid.uuid4()), "goal": "x" * 3000},  # oversized
    {"agent_id": str(uuid.uuid4()), "goal": "hi", "extra": 1},  # unknown field
])
async def test_body_validation(api, body) -> None:
    app.dependency_overrides[get_ai_buyer_client] = lambda: FakeAIBuyerClient(script=[final_step("x")])
    assert (await api.client.post("/ai/buyer", json=body)).status_code == 422


# ================== boundary guards =================================
def test_get_ai_buyer_client_disabled_by_default() -> None:
    assert isinstance(get_ai_buyer_client(), DisabledBuyerClient)


async def test_anthropic_buyer_client_normalises_sdk_exception() -> None:
    class _Stub:
        class messages:  # noqa: N801
            @staticmethod
            async def create(**_):
                raise TimeoutError("network")

    c = AnthropicBuyerClient(api_key="x", model="m", timeout_seconds=1, _client=_Stub())
    with pytest.raises(AIUnavailableError):
        await c.next_step(messages=[], tools=[])


async def test_buyer_only_ever_sees_four_tools_none_payment(api) -> None:
    await api.run(tool_step(("search_catalog", {})), final_step("done"))
    names = {t["name"] for t in api.last_fake.tool_defs_seen}
    assert names == {"search_catalog", "get_product", "compare_products", "request_action"}
    assert not any("pay" in n or "refund" in n or "capture" in n for n in names)


def test_buyer_module_does_not_import_razorpay_or_payment() -> None:
    from pathlib import Path

    import app.ai.buyer as b

    forbidden = ("razorpay", "payment", "webhook")
    for lineno, line in enumerate(Path(b.__file__).read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if s.startswith(("import ", "from ")):
            assert not any(m in s for m in forbidden), f"buyer.py:{lineno}: {s}"
