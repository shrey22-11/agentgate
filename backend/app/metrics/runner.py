"""
Batch runner for the frozen scenario suite (Phase 12). OUR SYSTEM.

    await run_suite("holdout") -> SuiteResult

What it does, in order:

 1. (standalone) ensure a disposable ``agentgate_metrics`` database exists and
    rebuild its schema from the ORM metadata + the audit append-only triggers -
    the same construction ``tests/conftest.py`` uses. Then truncate + seed the
    SIMULATED merchant / catalogue / agents.
 2. Run every ``kind == "policy"`` scenario through the real ``POST /actions``
    route (in-process ASGI), timing each call. Compare the persisted verdict +
    rule id against ground truth computed from ``app.policy.evaluate``.
 3. Run every ``kind == "nl"`` scenario through the real ``POST /ai/actions``
    route with a *stub* parser client (no model call - exactly as every AI test
    in this repo). The injected authority must not change the deterministic
    verdict and must never move money.
 4. Assert the money invariant: zero ``payment_attempt`` rows exist after steps
    2-3 (nothing in those categories may create a Razorpay object).
 5. Run the idempotency scenarios: duplicate ``POST /actions``, a duplicate
    ``payment_attempt`` idempotency key, and a replayed webhook ``event_id``.
 6. Injected-tamper trials against the audit chain: append probe events, corrupt
    one (payload / hash / prev_hash / mid-delete), assert
    ``verify_audit_chain`` catches it, roll back so nothing is persisted.
 7. Verify the committed chain and count its events.
 8. Compute the metrics and return a :class:`SuiteResult`.

No new infrastructure: one Postgres database, in-process ASGI, standard library
for timing and statistics.
"""
from __future__ import annotations

import datetime as _dt
import hmac
import json
import math
import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from time import perf_counter

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import NullPool

from app.audit import append_audit_event, verify_audit_chain
from app.audit.models import AuditEvent
from app.core.config import get_settings
from app.core.enums import PaymentStatus, Verdict
from app.metrics import ground_truth as gt
from app.metrics.scenarios import (
    Scenario,
    agent_needle,
    product_needle,
    select as select_scenarios,
    AGENT_KEYS,
    PRODUCT_KEYS,
)

_ALL_TABLES = (
    "agent, product, merchant, action_request, decision, approval, "
    "payment_attempt, webhook_event, audit_event"
)
_TAMPER_MODES = ("payload", "hash", "prev_hash", "delete_middle")


# ===========================================================================
# result types
# ===========================================================================
@dataclass
class ScenarioResult:
    id: str
    category: str
    kind: str
    split: str
    intent: str
    expected_verdict: str | None
    expected_rule: str | None
    actual_verdict: str | None
    actual_rule: str | None
    verdict_match: bool | None
    rule_match: bool | None
    expected_blocked: bool | None
    actual_blocked: bool | None
    override_expected: bool | None
    override_flagged: bool | None
    reached_policy: bool | None
    latency_ms: float | None
    passed: bool
    detail: str = ""


@dataclass
class SuiteResult:
    split: str
    generated_at: str
    scenarios: list[ScenarioResult]
    audit_chain_valid: bool
    audit_events_total: int
    payments_created_unexpected: int
    tamper: dict
    idempotency: dict
    latency_ms: dict
    metrics: dict
    notes: list[str] = field(default_factory=list)


# ===========================================================================
# stubs (local - the app never imports from tests/)
# ===========================================================================
class _StubParser:
    """Stands in for ``app.ai.client.AIParserClient``. Returns a fixed
    ``ParsedIntent``; no network, no anthropic SDK."""

    def __init__(self, intent) -> None:
        self._intent = intent
        self.calls: list[str] = []

    async def parse_intent(self, *, raw_input: str):
        self.calls.append(raw_input)
        return self._intent


class _WebhookSigner:
    """Only the one method ``process_webhook`` calls on its client, plus a
    signing helper for the test payloads."""

    def __init__(self, secret: str = "whsec_metrics_stub") -> None:
        self._secret = secret

    def sign(self, raw_body: bytes) -> str:
        from app.razorpay.client import hmac_sha256_hex

        return hmac_sha256_hex(self._secret, raw_body)

    def verify_webhook_signature(self, *, raw_body: bytes, signature: str) -> bool:
        return hmac.compare_digest(self.sign(raw_body), signature or "")


def _parsed_intent(spec):
    from app.ai.schemas import ParsedIntent

    return ParsedIntent(
        is_purchase_request=spec.is_purchase_request,
        product_reference=spec.product_reference,
        action_type=spec.action_type,
        quantity=spec.quantity,
        requested_discount_pct=spec.requested_discount_pct,
        proposed_price=spec.proposed_price,
        contains_override_instructions=spec.contains_override_instructions,
        notes=spec.notes,
    )


# ===========================================================================
# database plumbing (mirrors tests/conftest.py)
# ===========================================================================
def _default_metrics_url() -> str:
    base = get_settings().database_url
    return base.rsplit("/", 1)[0] + "/agentgate_metrics"


async def _ensure_database(async_url: str) -> None:
    import asyncpg

    dsn = async_url.replace("postgresql+asyncpg://", "postgresql://")
    admin_dsn = dsn.rsplit("/", 1)[0] + "/postgres"
    target = dsn.rsplit("/", 1)[1]
    conn = await asyncpg.connect(admin_dsn)
    try:
        if not await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", target
        ):
            await conn.execute(f'CREATE DATABASE "{target}"')
    finally:
        await conn.close()


async def _build_schema(engine) -> None:
    from app.audit.ddl import CREATE_APPEND_ONLY_SQL
    from app.core.db import Base
    import app.db_models  # noqa: F401  (registers every table on Base.metadata)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        for statement in CREATE_APPEND_ONLY_SQL:
            await conn.exec_driver_sql(statement)


async def _reset(engine) -> None:
    async with engine.begin() as conn:
        await conn.exec_driver_sql("ALTER TABLE audit_event DISABLE TRIGGER USER")
        await conn.exec_driver_sql(
            f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE"
        )
        await conn.exec_driver_sql("ALTER TABLE audit_event ENABLE TRIGGER USER")


async def _seed(factory) -> None:
    from app.catalog.models import Merchant
    from app.seed import MERCHANT_NAME, _agents, _products

    async with factory() as s:
        merchant = Merchant(name=MERCHANT_NAME, policy_version="v1")
        s.add(merchant)
        await s.flush()
        s.add_all(_products(merchant.id))
        s.add_all(_agents())
        await s.commit()


# ===========================================================================
# per-scenario execution
# ===========================================================================
def _policy_body(sc: Scenario, agent_id, product_id) -> dict:
    body: dict = {
        "agent_id": str(agent_id),
        "product_id": str(product_id),
        "action_type": sc.action_type,
    }
    if sc.quantity is not None:
        body["quantity"] = sc.quantity
    if sc.requested_discount_pct is not None:
        body["requested_discount_pct"] = sc.requested_discount_pct
    if sc.proposed_price is not None:
        body["proposed_price"] = sc.proposed_price
    return body


async def _run_policy(client, sc: Scenario, agent, product) -> ScenarioResult:
    exp = gt.expected_for_policy(sc, agent, product)
    body = _policy_body(sc, agent.id, product.id)

    t0 = perf_counter()
    r = await client.post("/actions", json=body)
    dt = round((perf_counter() - t0) * 1000, 3)

    base = dict(
        id=sc.id, category=sc.category, kind="policy", split=sc.split, intent=sc.intent,
        expected_verdict=exp.verdict.value, expected_rule=exp.rule_id,
        expected_blocked=exp.verdict is not Verdict.ALLOW,
        override_expected=None, override_flagged=None, reached_policy=None,
        latency_ms=dt,
    )
    if r.status_code != 200:
        return ScenarioResult(
            **base, actual_verdict=None, actual_rule=None, verdict_match=False,
            rule_match=False, actual_blocked=None, passed=False,
            detail=f"HTTP {r.status_code}: {r.text[:200]}",
        )
    j = r.json()
    av, ar_ = j["verdict"], j["rule_id"]
    vm, rm = av == exp.verdict.value, ar_ == exp.rule_id
    return ScenarioResult(
        **base, actual_verdict=av, actual_rule=ar_, verdict_match=vm, rule_match=rm,
        actual_blocked=av != Verdict.ALLOW.value, passed=vm and rm,
        detail="" if (vm and rm) else f"expected {exp.verdict.value}/{exp.rule_id}, got {av}/{ar_}",
    )


async def _run_nl(client, holder, sc: Scenario, agent, products) -> ScenarioResult:
    exp = gt.expected_for_nl(sc, agent, products)
    spec = sc.parse
    assert spec is not None
    holder["client"] = _StubParser(_parsed_intent(spec))

    t0 = perf_counter()
    r = await client.post("/ai/actions", json={"agent_id": str(agent.id), "text": sc.hostile_text})
    dt = round((perf_counter() - t0) * 1000, 3)

    base = dict(
        id=sc.id, category="adversarial", kind="nl", split=sc.split, intent=sc.intent,
        expected_verdict=exp.verdict.value, expected_rule=exp.rule_id,
        expected_blocked=exp.verdict is not Verdict.ALLOW,
        override_expected=spec.contains_override_instructions,
        reached_policy=exp.reached_policy, latency_ms=dt,
    )
    if r.status_code != 200:
        return ScenarioResult(
            **base, actual_verdict=None, actual_rule=None, verdict_match=False,
            rule_match=False, actual_blocked=None, override_flagged=None, passed=False,
            detail=f"HTTP {r.status_code}: {r.text[:200]}",
        )
    j = r.json()
    dec = j["decision"]
    av, ar_ = dec["verdict"], dec["rule_id"]
    ovr = bool(j["override_instructions_detected"])
    vm, rm = av == exp.verdict.value, ar_ == exp.rule_id
    ovr_ok = ovr == spec.contains_override_instructions
    passed = vm and rm and ovr_ok
    return ScenarioResult(
        **base, actual_verdict=av, actual_rule=ar_, verdict_match=vm, rule_match=rm,
        actual_blocked=av != Verdict.ALLOW.value, override_flagged=ovr, passed=passed,
        detail="" if passed
        else (f"expected {exp.verdict.value}/{exp.rule_id} override={spec.contains_override_instructions}; "
              f"got {av}/{ar_} override={ovr}"),
    )


# ===========================================================================
# idempotency
# ===========================================================================
async def _idem_duplicate_action(client, factory, sc: Scenario, agent, product) -> dict:
    body = _policy_body(sc, agent.id, product.id)
    r1 = await client.post("/actions", json=body)
    r2 = await client.post("/actions", json=body)
    checks: list[tuple[str, bool]] = [("both HTTP 200", r1.status_code == 200 and r2.status_code == 200)]
    if r1.status_code == 200 and r2.status_code == 200:
        a, b = r1.json(), r2.json()
        checks += [
            ("distinct action_request rows", a["action_request_id"] != b["action_request_id"]),
            ("distinct decision rows", a["decision_id"] != b["decision_id"]),
            ("identical verdict", a["verdict"] == b["verdict"]),
            ("identical rule id", a["rule_id"] == b["rule_id"]),
            ("identical counter-offer", a["counter_offer"] == b["counter_offer"]),
        ]
    async with factory() as s:
        chain = await verify_audit_chain(s)
    checks.append(("audit chain still valid", chain.valid))
    passed = all(ok for _, ok in checks)
    return {
        "id": sc.id, "mechanism": sc.mechanism, "intent": sc.intent, "passed": passed,
        "checks": {name: ok for name, ok in checks},
        "verdict": r1.json().get("verdict") if r1.status_code == 200 else None,
    }


async def _idem_payment_attempt_key(client, factory, sc: Scenario, agent, product) -> dict:
    from app.razorpay.models import PaymentAttempt

    r = await client.post("/actions", json=_policy_body(sc, agent.id, product.id))
    ok_allow = r.status_code == 200 and r.json()["verdict"] == Verdict.ALLOW.value
    decision_id = r.json()["decision_id"] if r.status_code == 200 else None
    key = f"decision:{decision_id}"

    first_ok = False
    if decision_id is not None:
        async with factory() as s:
            s.add(PaymentAttempt(
                decision_id=uuid.UUID(decision_id), decision_verdict=Verdict.ALLOW,
                idempotency_key=key, status=PaymentStatus.CREATED,
            ))
            await s.commit()
            first_ok = True

    rejected = False
    err = ""
    if first_ok:
        async with factory() as s:
            s.add(PaymentAttempt(
                decision_id=uuid.UUID(decision_id), decision_verdict=Verdict.ALLOW,
                idempotency_key=key, status=PaymentStatus.CREATED,
            ))
            try:
                await s.flush()
            except IntegrityError as exc:
                rejected = True
                err = str(exc.orig)[:160] if exc.orig else str(exc)[:160]
            await s.rollback()

    checks = {
        "seed decision is ALLOW": ok_allow,
        "first payment_attempt inserted": first_ok,
        "second insert rejected by a unique constraint": rejected,
        "rejection names uq_payment_attempt": "uq_payment_attempt" in err,
    }
    return {
        "id": sc.id, "mechanism": sc.mechanism, "intent": sc.intent,
        "passed": all(checks.values()), "checks": checks,
    }


async def _idem_webhook_event(factory, sc: Scenario) -> dict:
    from app.webhooks.models import WebhookEvent
    from app.webhooks.service import process_webhook

    signer = _WebhookSigner()
    raw = json.dumps({"event": sc.webhook_event_type, "payload": {}}).encode()
    sig = signer.sign(raw)
    event_id = f"evt_metrics_{sc.id}"

    async with factory() as s:
        ack1 = await process_webhook(
            s, signer, raw_body=raw, signature=sig, event_id_header=event_id
        )
    async with factory() as s:
        ack2 = await process_webhook(
            s, signer, raw_body=raw, signature=sig, event_id_header=event_id
        )
    async with factory() as s:
        rows = await s.scalar(
            select(func.count()).select_from(WebhookEvent).where(
                WebhookEvent.event_id == event_id
            )
        )
        chain = await verify_audit_chain(s)

    checks = {
        "first delivery accepted": ack1.status != "duplicate_ignored",
        "replay is duplicate_ignored": ack2.status == "duplicate_ignored",
        "exactly one webhook_event row": rows == 1,
        "no 5xx / exception raised": True,
        "audit chain still valid": chain.valid,
    }
    return {
        "id": sc.id, "mechanism": sc.mechanism, "intent": sc.intent,
        "passed": all(checks.values()), "checks": checks,
        "first_status": ack1.status, "replay_status": ack2.status,
    }


# ===========================================================================
# injected audit tamper
# ===========================================================================
async def _append_probe(session):
    return await append_audit_event(
        session, ref_type="metrics_probe", ref_id=uuid.uuid4(),
        event_type="METRICS_TAMPER_PROBE", payload={"k": "v"},
    )


async def _corrupt(session, sql: str, **params) -> None:
    await session.execute(text("ALTER TABLE audit_event DISABLE TRIGGER USER"))
    await session.execute(text(sql), params)
    await session.execute(text("ALTER TABLE audit_event ENABLE TRIGGER USER"))


async def _tamper_trials(factory, *, trials: int = 12, clean_trials: int = 4) -> dict:
    per_mode = {m: {"trials": 0, "detected": 0} for m in _TAMPER_MODES}
    detected = 0
    for i in range(trials):
        mode = _TAMPER_MODES[i % len(_TAMPER_MODES)]
        async with factory() as s:
            e1 = await _append_probe(s)
            e2 = await _append_probe(s)
            e3 = await _append_probe(s)
            await s.flush()
            if mode == "payload":
                await _corrupt(
                    s, "UPDATE audit_event SET payload = CAST(:p AS jsonb) WHERE id = :id",
                    p='{"tampered": true}', id=str(e2.id),
                )
            elif mode == "hash":
                await _corrupt(
                    s, "UPDATE audit_event SET hash = :h WHERE id = :id",
                    h="d" * 64, id=str(e2.id),
                )
            elif mode == "prev_hash":
                await _corrupt(
                    s, "UPDATE audit_event SET prev_hash = :p WHERE id = :id",
                    p="e" * 64, id=str(e3.id),
                )
            else:  # delete_middle
                await _corrupt(
                    s, "DELETE FROM audit_event WHERE id = :id", id=str(e2.id)
                )
            s.expire_all()
            result = await verify_audit_chain(s)
            caught = not result.valid
            detected += int(caught)
            per_mode[mode]["trials"] += 1
            per_mode[mode]["detected"] += int(caught)
            await s.rollback()  # corruption is never persisted

    clean_ok = 0
    for _ in range(clean_trials):
        async with factory() as s:
            for _ in range(3):
                await _append_probe(s)
            await s.flush()
            s.expire_all()
            clean_ok += int((await verify_audit_chain(s)).valid)
            await s.rollback()

    return {
        "trials": trials, "detected": detected,
        "clean_trials": clean_trials, "clean_valid": clean_ok,
        "by_mode": per_mode,
    }


# ===========================================================================
# metrics
# ===========================================================================
def _rate(items, pred) -> float | None:
    items = list(items)
    if not items:
        return None
    return round(sum(1 for x in items if pred(x)) / len(items), 4)


def _pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def _latency_summary(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    s = sorted(values)
    return {
        "n": len(s),
        "p50_ms": round(_pct(s, 0.50), 3),
        "p95_ms": round(_pct(s, 0.95), 3),
        "p99_ms": round(_pct(s, 0.99), 3),
        "max_ms": round(s[-1], 3),
        "mean_ms": round(sum(s) / len(s), 3),
    }


def _compute_metrics(
    results: list[ScenarioResult],
    tamper: dict,
    idempotency: dict,
    payments_unexpected: int,
    chain_valid: bool,
) -> dict:
    pol = [r for r in results if r.kind == "policy"]
    nl = [r for r in results if r.kind == "nl"]
    graded = pol + nl
    benign = [r for r in pol if r.category == "benign"]
    violating = [r for r in pol if r.category == "policy_violating"]

    idem_cases = [c for m in idempotency.values() for c in m["cases"]]

    return {
        "scenario_counts": {
            "total_graded": len(graded),
            "policy": len(pol),
            "adversarial_nl": len(nl),
            "benign": len(benign),
            "policy_violating": len(violating),
            "idempotency": len(idem_cases),
        },
        # Integration fidelity: does the full DB-backed path agree with the pure
        # deterministic engine on every input? This is the computed ground truth.
        "verdict_match_rate": _rate(graded, lambda r: r.verdict_match),
        "rule_match_rate": _rate(graded, lambda r: r.rule_match),
        "suite_pass_rate": _rate(graded, lambda r: r.passed),
        # Design-intent framing (block / false-block) - labels are intent only.
        "block_rate_on_policy_violating": _rate(violating, lambda r: r.actual_blocked),
        "false_block_rate_on_benign": _rate(benign, lambda r: r.actual_blocked),
        "engine_blocks_all_designed_violations": all(r.expected_blocked for r in violating) if violating else None,
        "engine_allows_all_designed_benign": all(not r.expected_blocked for r in benign) if benign else None,
        "adversarial": {
            "n": len(nl),
            "injection_neutralised_rate": _rate(nl, lambda r: r.passed),
            "verdict_match_rate": _rate(nl, lambda r: r.verdict_match),
            "override_flag_match_rate": _rate(nl, lambda r: r.override_flagged == r.override_expected),
            "structured_parse_passthrough_rate": _rate(nl, lambda r: r.reached_policy),
            "structured_parse_failclosed_rate": _rate(nl, lambda r: not r.reached_policy),
        },
        "decision_latency_ms": {
            "policy_route": _latency_summary([r.latency_ms for r in pol if r.latency_ms is not None]),
            "nl_route": _latency_summary([r.latency_ms for r in nl if r.latency_ms is not None]),
        },
        "idempotency_pass_rate": _rate(idem_cases, lambda c: c["passed"]),
        "audit_chain_valid_after_suite": chain_valid,
        "audit_tamper_detection_rate": (
            round(tamper["detected"] / tamper["trials"], 4) if tamper["trials"] else None
        ),
        "audit_clean_control_valid_rate": (
            round(tamper["clean_valid"] / tamper["clean_trials"], 4) if tamper["clean_trials"] else None
        ),
        "unexpected_payment_objects": payments_unexpected,
    }


# ===========================================================================
# entry point
# ===========================================================================
async def run_suite(
    split: str = "all",
    *,
    database_url: str | None = None,
    engine=None,
    manage_schema: bool | None = None,
    subset: Collection[str] | None = None,
) -> SuiteResult:
    from app.ai.client import get_ai_client
    from app.catalog.models import Product
    from app.core.db import get_db
    from app.agents.models import Agent
    from app.main import app
    from app.razorpay.models import PaymentAttempt

    scenarios = [s for s in select_scenarios(split) if subset is None or s.id in subset]

    own_engine = engine is None
    if own_engine:
        url = database_url or _default_metrics_url()
        await _ensure_database(url)
        engine = create_async_engine(url, poolclass=NullPool)
    if manage_schema is None:
        manage_schema = own_engine

    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if manage_schema:
            await _build_schema(engine)
        await _reset(engine)
        await _seed(factory)

        async with factory() as s:
            products = (
                await s.scalars(select(Product).options(selectinload(Product.merchant)))
            ).all()
            agents = (await s.scalars(select(Agent))).all()
        prod_by_key = {k: next(p for p in products if product_needle(k) in p.name) for k in PRODUCT_KEYS}
        agent_by_key = {k: next(a for a in agents if agent_needle(k) in a.name) for k in AGENT_KEYS}

        holder: dict = {"client": None}

        async def _override_db():
            async with factory() as session:
                try:
                    yield session
                except Exception:
                    await session.rollback()
                    raise

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_ai_client] = lambda: holder["client"]
        transport = ASGITransport(app=app, raise_app_exceptions=False)

        results: list[ScenarioResult] = []
        idem_by_mech: dict[str, dict] = {}
        try:
            async with AsyncClient(transport=transport, base_url="http://metrics") as client:
                for sc in scenarios:
                    if sc.kind == "policy":
                        results.append(await _run_policy(
                            client, sc, agent_by_key[sc.agent_key], prod_by_key[sc.product_key]
                        ))
                    elif sc.kind == "nl":
                        results.append(await _run_nl(
                            client, holder, sc, agent_by_key[sc.agent_key], products
                        ))

                # money invariant: categories above must not create any payment.
                async with factory() as s:
                    payments_unexpected = await s.scalar(
                        select(func.count()).select_from(PaymentAttempt)
                    ) or 0

                for sc in scenarios:
                    if sc.kind != "idempotency":
                        continue
                    if sc.mechanism == "duplicate_action":
                        case = await _idem_duplicate_action(
                            client, factory, sc,
                            agent_by_key[sc.agent_key], prod_by_key[sc.product_key],
                        )
                    elif sc.mechanism == "payment_attempt_key":
                        case = await _idem_payment_attempt_key(
                            client, factory, sc,
                            agent_by_key[sc.agent_key], prod_by_key[sc.product_key],
                        )
                    else:
                        case = await _idem_webhook_event(factory, sc)
                    idem_by_mech.setdefault(
                        sc.mechanism, {"mechanism": sc.mechanism, "cases": []}
                    )["cases"].append(case)
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(get_ai_client, None)

        for mech in idem_by_mech.values():
            mech["passed"] = sum(1 for c in mech["cases"] if c["passed"])
            mech["trials"] = len(mech["cases"])

        tamper = await _tamper_trials(factory)

        async with factory() as s:
            chain = await verify_audit_chain(s)
            total_events = await s.scalar(select(func.count()).select_from(AuditEvent)) or 0

        metrics = _compute_metrics(
            results, tamper, idem_by_mech, payments_unexpected, chain.valid
        )
        return SuiteResult(
            split=split,
            generated_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            scenarios=results,
            audit_chain_valid=chain.valid,
            audit_events_total=int(total_events),
            payments_created_unexpected=int(payments_unexpected),
            tamper=tamper,
            idempotency=idem_by_mech,
            latency_ms=metrics["decision_latency_ms"],
            metrics=metrics,
            notes=[
                "Catalogue, stock, margins and the agent population are SIMULATED.",
                "The natural-language parser's model call is stubbed (no Anthropic "
                "request) - exactly as every AI test in this repo. The deterministic "
                "re-validation, catalogue resolution, confidence gate and the whole "
                "policy path are exercised for real.",
                "RAZORPAY_ENABLED is false: no Razorpay object is created anywhere in "
                "this suite. Payment-execution idempotency is checked at the database "
                "constraint level.",
                "Ground truth is computed from app.policy.evaluate on authoritative "
                "seed data; no scenario carries a hand-written verdict.",
                "Latency is in-process ASGI against a local PostgreSQL, single "
                "threaded and warm - not a production latency claim.",
                "No revenue, conversion, AOV or business-impact figure is produced or "
                "implied by this harness.",
            ],
        )
    finally:
        if own_engine:
            await engine.dispose()
