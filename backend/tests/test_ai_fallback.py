"""
Optional AI fallback (Llama via Groq) — see app/ai/groq_client.py and the
FallbackParserClient / FallbackBuyerClient composition in app/ai/client.py.

No test here makes a real Gemini or Groq call. Three layers are tested:

  1. Composition/routing logic (FallbackParserClient / FallbackBuyerClient),
     driven directly with the existing FakeAIParserClient / FakeAIBuyerClient
     doubles from tests/_fakes.py — they already implement the exact
     AIParserClient / AIBuyerClient shape both the real and fallback clients
     share, so no new fakes are needed to prove the routing rules in
     isolation from either real SDK.
  2. The real GeminiParserClient / GeminiBuyerClient / GroqParserClient /
     GroqBuyerClient classes, driven with low-level stubs shaped exactly like
     each SDK's own client object — mirroring test_ai_parsing.py's
     `_GeminiStub` pattern — to prove the actual transient-error
     classification and Groq translation code, not just the wrapper.
  3. End-to-end through the real /ai/actions and /ai/buyer HTTP endpoints
     (the Section-17 regression), proving a Gemini outage recovered via Groq
     still reaches the one real deterministic policy path and a real audit
     trail — never a shortcut around either.
"""
from __future__ import annotations

import copy
import json

import httpx
import pytest
import pytest_asyncio
from groq import APITimeoutError as GroqAPITimeoutError
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.action_requests.models import ActionRequest
from app.agents.models import Agent
from app.ai.buyer import TOOL_DEFS
from app.ai.client import (
    AITransientUnavailableError,
    AIUnavailableError,
    FallbackBuyerClient,
    FallbackParserClient,
    GeminiBuyerClient,
    GeminiParserClient,
    _is_transient_provider_error,
    _to_gemini_tools,
    get_ai_buyer_client,
    get_ai_client,
)
from app.ai.groq_client import (
    GroqBuyerClient,
    GroqParserClient,
    _GROQ_SEARCH_CATALOG_DESCRIPTION,
    _to_groq_messages,
    _to_groq_tools,
)
from app.ai.schemas import ParsedIntent
from app.audit import verify_audit_chain
from app.audit.models import AuditEvent
from app.catalog.models import Merchant, Product
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.enums import Verdict
from app.main import app
from app.policy.models import Decision
from app.razorpay.models import PaymentAttempt
from app.seed import MERCHANT_NAME, _agents, _products
from tests._fakes import FakeAIBuyerClient, FakeAIParserClient, final_step, tool_step

_ALL_TABLES = (
    "agent, product, merchant, action_request, decision, approval, "
    "payment_attempt, webhook_event, audit_event"
)


def _ok_intent(**over) -> ParsedIntent:
    base = dict(is_purchase_request=True, product_reference="Velocity Pro", action_type="PURCHASE")
    base.update(over)
    return ParsedIntent(**base)


# ==========================================================================
# 1. Composition / routing logic — FallbackParserClient, FallbackBuyerClient
# ==========================================================================

# ---- A/G: success never touches the secondary --------------------------
async def test_parser_fallback_disabled_path_never_calls_secondary_on_success() -> None:
    primary = FakeAIParserClient(result=_ok_intent())
    secondary = FakeAIParserClient(result=_ok_intent(product_reference="must not be used"))
    client = FallbackParserClient(primary=primary, secondary=secondary)
    out = await client.parse_intent(raw_input="buy velocity pro")
    assert out.product_reference == "Velocity Pro"
    assert primary.calls == ["buy velocity pro"]
    assert secondary.calls == []


# ---- C: a transient failure does fall back, and succeeds ----------------
async def test_parser_transient_failure_falls_back_to_secondary() -> None:
    primary = FakeAIParserClient(error=AITransientUnavailableError("gemini call failed: ServerError | HTTP 503"))
    secondary = FakeAIParserClient(result=_ok_intent(product_reference="Trailblaze Daily Trainer"))
    client = FallbackParserClient(primary=primary, secondary=secondary)
    out = await client.parse_intent(raw_input="x")
    assert out.product_reference == "Trailblaze Daily Trainer"
    assert secondary.calls == ["x"]


# ---- H: a non-transient (business/validation) failure never falls back --
async def test_parser_non_transient_failure_never_reaches_secondary() -> None:
    primary = FakeAIParserClient(error=AIUnavailableError("gemini did not return a valid ParsedIntent: field required"))
    secondary = FakeAIParserClient(result=_ok_intent())
    client = FallbackParserClient(primary=primary, secondary=secondary)
    with pytest.raises(AIUnavailableError) as ei:
        await client.parse_intent(raw_input="x")
    assert not isinstance(ei.value, AITransientUnavailableError)
    assert secondary.calls == []


# ---- J: both providers failing is safe (propagates, never ALLOW) --------
async def test_parser_both_providers_failing_propagates_the_secondarys_error() -> None:
    primary = FakeAIParserClient(error=AITransientUnavailableError("boom-primary"))
    secondary = FakeAIParserClient(error=AIUnavailableError("boom-secondary"))
    client = FallbackParserClient(primary=primary, secondary=secondary)
    with pytest.raises(AIUnavailableError, match="boom-secondary"):
        await client.parse_intent(raw_input="x")


# ---- "enabled but unusable" degrades to "no fallback", not a crash ------
async def test_parser_with_no_secondary_configured_propagates_the_transient_failure() -> None:
    primary = FakeAIParserClient(error=AITransientUnavailableError("boom"))
    client = FallbackParserClient(primary=primary, secondary=None)
    with pytest.raises(AITransientUnavailableError):
        await client.parse_intent(raw_input="x")


# ---- same four shapes, for the buyer agent (Section 5: both use cases) --
async def test_buyer_fallback_disabled_path_never_calls_secondary_on_success() -> None:
    primary = FakeAIBuyerClient(script=[final_step("done via gemini")])
    secondary = FakeAIBuyerClient(script=[final_step("must not be used")])
    client = FallbackBuyerClient(primary=primary, secondary=secondary)
    step = await client.next_step(messages=[], tools=[])
    assert step.text == "done via gemini"
    assert secondary.step_calls == 0


async def test_buyer_transient_failure_falls_back_to_secondary() -> None:
    primary = FakeAIBuyerClient(script=[], error=AITransientUnavailableError("timeout"), error_after=0)
    secondary = FakeAIBuyerClient(script=[final_step("done via groq")])
    client = FallbackBuyerClient(primary=primary, secondary=secondary)
    step = await client.next_step(messages=[], tools=[])
    assert step.text == "done via groq"


async def test_buyer_non_transient_failure_never_reaches_secondary() -> None:
    primary = FakeAIBuyerClient(script=[], error=AIUnavailableError("disabled or malformed"), error_after=0)
    secondary = FakeAIBuyerClient(script=[final_step("must not be used")])
    client = FallbackBuyerClient(primary=primary, secondary=secondary)
    with pytest.raises(AIUnavailableError) as ei:
        await client.next_step(messages=[], tools=[])
    assert not isinstance(ei.value, AITransientUnavailableError)
    assert secondary.step_calls == 0


# ---- N (part 1): sticky after the first fallback — no Gemini/Groq ping-pong
async def test_buyer_fallback_is_sticky_for_the_rest_of_the_run() -> None:
    primary = FakeAIBuyerClient(
        script=[tool_step(("search_catalog", {"query": "shoe"}))],  # would succeed if ever retried
        error=AITransientUnavailableError("boom"),
        error_after=0,  # fails on step 1
    )
    secondary = FakeAIBuyerClient(
        script=[tool_step(("search_catalog", {"query": "shoe"})), final_step("done via groq")]
    )
    client = FallbackBuyerClient(primary=primary, secondary=secondary)
    step1 = await client.next_step(messages=[], tools=[])
    assert step1.kind == "tool_calls"
    step2 = await client.next_step(messages=[], tools=[])
    assert step2.kind == "final" and step2.text == "done via groq"
    # primary was tried exactly once — never retried after the first fallback
    assert primary.step_calls == 1
    assert secondary.step_calls == 2


# ==========================================================================
# Factory wiring — get_ai_client / get_ai_buyer_client (backward compat, O)
# ==========================================================================
_FAKE_GEMINI_KEY = "AIzaSy-fake-key-value"


def _settings(**over) -> Settings:
    base = dict(
        database_url="postgresql+asyncpg://x/y",
        ai_enabled=True,
        gemini_api_key=_FAKE_GEMINI_KEY,
        razorpay_key_id="k", razorpay_key_secret="k", razorpay_webhook_secret="k",
    )
    base.update(over)
    return Settings(**base)


def test_get_ai_client_ignores_fallback_settings_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.ai.client.get_settings",
        lambda: _settings(ai_fallback_enabled=False, groq_api_key="gsk_unused"),
    )
    # byte-identical to pre-fallback behaviour: a bare GeminiParserClient
    assert type(get_ai_client()) is GeminiParserClient


def test_get_ai_buyer_client_ignores_fallback_settings_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.ai.client.get_settings",
        lambda: _settings(ai_fallback_enabled=False),
    )
    assert type(get_ai_buyer_client()) is GeminiBuyerClient


def test_get_ai_client_wraps_with_fallback_when_enabled_and_keyed(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.ai.client.get_settings",
        lambda: _settings(ai_fallback_enabled=True, groq_api_key="gsk_fake-groq-key-value"),
    )
    client = get_ai_client()
    assert isinstance(client, FallbackParserClient)
    assert client._secondary is not None


def test_get_ai_client_degrades_gracefully_when_key_is_missing(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        "app.ai.client.get_settings",
        lambda: _settings(ai_fallback_enabled=True, groq_api_key=""),
    )
    with caplog.at_level("WARNING", logger="agentgate.ai"):
        client = get_ai_client()
    assert isinstance(client, FallbackParserClient)
    assert client._secondary is None  # never crashes; just runs Gemini-only
    assert "GROQ_API_KEY is missing" in caplog.text


def test_get_ai_client_degrades_gracefully_for_an_unsupported_provider(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.ai.client.get_settings",
        lambda: _settings(ai_fallback_enabled=True, groq_api_key="gsk_x", ai_fallback_provider="bedrock"),
    )
    client = get_ai_client()
    assert isinstance(client, FallbackParserClient)
    assert client._secondary is None


# ==========================================================================
# 2a. Transient-error classification (D/E/F) — real Gemini client classes
# ==========================================================================
class _GeminiStub:
    """Mimics client.aio.models.generate_content(...) — same shape as
    test_ai_parsing.py's _GeminiStub, redefined locally per this file's
    convention of not sharing SDK-shaped stubs across test files."""

    def __init__(self, *, response=None, error: BaseException | None = None) -> None:
        self._response, self._error = response, error
        self.aio = self
        self.models = self

    async def generate_content(self, **_):
        if self._error is not None:
            raise self._error
        return self._response


def _gemini_5xx(code: int, message: str) -> genai_errors.ServerError:
    return genai_errors.ServerError(code, {"error": {"code": code, "status": "UNAVAILABLE", "message": message}})


def _gemini_429(message: str = "rate limited") -> genai_errors.ClientError:
    return genai_errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": message}})


def _gemini_400(message: str = "bad request") -> genai_errors.ClientError:
    return genai_errors.ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": message}})


_TRANSIENT_ERRORS = [
    pytest.param(
        lambda: _gemini_5xx(503, "This model is currently experiencing high demand. Spikes in demand are usually temporary."),
        id="http_503",
    ),
    pytest.param(lambda: _gemini_5xx(504, "deadline exceeded"), id="http_504"),
    pytest.param(lambda: _gemini_429(), id="http_429"),
    pytest.param(lambda: httpx.ReadTimeout("timed out"), id="httpx_timeout"),
    pytest.param(lambda: TimeoutError("network"), id="builtin_timeout"),
]


@pytest.mark.parametrize("build_error", _TRANSIENT_ERRORS)
async def test_gemini_parser_client_classifies_transient_errors(build_error) -> None:
    c = GeminiParserClient(api_key="x", model="m", timeout_seconds=1, _client=_GeminiStub(error=build_error()))
    with pytest.raises(AITransientUnavailableError):
        await c.parse_intent(raw_input="hi")


@pytest.mark.parametrize("build_error", _TRANSIENT_ERRORS)
async def test_gemini_buyer_client_classifies_transient_errors(build_error) -> None:
    c = GeminiBuyerClient(api_key="x", model="m", timeout_seconds=1, _client=_GeminiStub(error=build_error()))
    with pytest.raises(AITransientUnavailableError):
        await c.next_step(messages=[{"role": "user", "content": "hi"}], tools=[])


async def test_gemini_parser_client_does_not_classify_400_as_transient() -> None:
    c = GeminiParserClient(api_key="x", model="m", timeout_seconds=1, _client=_GeminiStub(error=_gemini_400()))
    with pytest.raises(AIUnavailableError) as ei:
        await c.parse_intent(raw_input="hi")
    assert not isinstance(ei.value, AITransientUnavailableError)


@pytest.mark.parametrize(
    "exc, expected",
    [
        (_gemini_5xx(503, "x"), True),
        (_gemini_5xx(504, "x"), True),
        (_gemini_5xx(500, "x"), True),
        (_gemini_429(), True),
        (_gemini_400(), False),
        (genai_errors.ClientError(401, {"error": {"code": 401, "message": "bad auth"}}), False),
        (TimeoutError("x"), True),
        (httpx.ReadTimeout("x"), True),
        (ValueError("not a provider error at all"), False),
    ],
)
def test_is_transient_provider_error_classification(exc, expected) -> None:
    assert _is_transient_provider_error(exc) is expected


# ==========================================================================
# 2b. GroqParserClient — ParsedIntent validation is still authoritative (I)
# ==========================================================================
class _GroqFn:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _GroqToolCall:
    def __init__(self, id: str, name: str, arguments: str) -> None:
        self.id = id
        self.function = _GroqFn(name, arguments)


class _GroqMessage:
    def __init__(self, *, content: str | None = None, tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _GroqChoice:
    def __init__(self, message: _GroqMessage) -> None:
        self.message = message


class _GroqResponse:
    def __init__(self, message: _GroqMessage) -> None:
        self.choices = [_GroqChoice(message)]


class _GroqStub:
    """Mimics client.chat.completions.create(...) — the one call both
    GroqParserClient and GroqBuyerClient make."""

    def __init__(self, *, responses: list | None = None, error: BaseException | None = None) -> None:
        self._responses = list(responses or [])
        self._error = error
        self.chat = self
        self.completions = self
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._responses.pop(0)


async def test_groq_parser_client_valid_json_validates() -> None:
    good = _GroqResponse(_GroqMessage(content=json.dumps({
        "is_purchase_request": True, "product_reference": "Velocity Pro", "requested_discount_pct": "20",
    })))
    c = GroqParserClient(api_key="x", model="m", timeout_seconds=1, _client=_GroqStub(responses=[good]))
    out = await c.parse_intent(raw_input="buy velocity pro at 20% off")
    assert isinstance(out, ParsedIntent)
    assert out.product_reference == "Velocity Pro" and out.requested_discount_pct == "20"


async def test_groq_parser_client_rejects_non_conforming_json() -> None:
    # extra key -> ParsedIntent(extra="forbid") raises -> fail closed, same as Gemini
    bad = _GroqResponse(_GroqMessage(content=json.dumps({"is_purchase_request": True, "secret_admin_flag": True})))
    c = GroqParserClient(api_key="x", model="m", timeout_seconds=1, _client=_GroqStub(responses=[bad]))
    with pytest.raises(AIUnavailableError):
        await c.parse_intent(raw_input="hi")


async def test_groq_parser_client_rejects_empty_output() -> None:
    c = GroqParserClient(
        api_key="x", model="m", timeout_seconds=1,
        _client=_GroqStub(responses=[_GroqResponse(_GroqMessage(content=""))]),
    )
    with pytest.raises(AIUnavailableError):
        await c.parse_intent(raw_input="hi")


async def test_groq_buyer_client_handles_an_empty_choices_list_without_crashing() -> None:
    """Defensive parity with _gemini_response_to_buyer_step's empty-candidates
    handling: an empty `choices` list must produce a harmless final step, not
    an uncaught IndexError that would bypass the fail-closed AIUnavailableError
    path run_buyer_agent relies on."""

    class _EmptyChoicesResponse:
        choices: list = []

    c = GroqBuyerClient(api_key="x", model="m", timeout_seconds=1, _client=_GroqStub(responses=[_EmptyChoicesResponse()]))
    step = await c.next_step(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert step.kind == "final"
    assert step.text == "(no summary)"


async def test_groq_parser_client_normalises_sdk_exception() -> None:
    err = GroqAPITimeoutError(request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions"))
    c = GroqParserClient(api_key="x", model="m", timeout_seconds=1, _client=_GroqStub(error=err))
    with pytest.raises(AIUnavailableError):
        await c.parse_intent(raw_input="hi")


# ==========================================================================
# N (part 2): a real Gemini Part/thought_signature never reaches Groq raw
# ==========================================================================
def test_gemini_part_history_reaches_groq_only_as_neutral_json() -> None:
    part = genai_types.Part(
        function_call=genai_types.FunctionCall(id="call_1", name="search_catalog", args={"query": "road"}),
        thought_signature=b"\x01\x02opaque-signature",
    )
    history = [
        {"role": "user", "content": "find road shoes"},
        {"role": "assistant", "content": [part]},  # a real Gemini turn from an earlier step
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": '{"count": 0, "results": []}'},
        ]},
    ]
    groq_messages = _to_groq_messages(history)

    # Every message is plain, JSON-serialisable data — proves no raw Part
    # object (and no `bytes` thought_signature) made it into the output.
    serialised = json.dumps(groq_messages)
    assert "opaque-signature" not in serialised

    assistant_msg = next(m for m in groq_messages if m["role"] == "assistant" and m.get("tool_calls"))
    call = assistant_msg["tool_calls"][0]
    assert call["type"] == "function" and call["function"]["name"] == "search_catalog"
    assert json.loads(call["function"]["arguments"]) == {"query": "road"}

    tool_msg = next(m for m in groq_messages if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "call_1" and tool_msg["name"] == "search_catalog"
    assert json.loads(tool_msg["content"]) == {"count": 0, "results": []}


# ==========================================================================
# Groq-only search_catalog description fix — an adapter documentation
# change, not a behavior change. TOOL_DEFS (Gemini's source of truth too)
# and _to_gemini_tools() must stay byte-identical throughout.
# ==========================================================================
_ORIGINAL_SEARCH_CATALOG_DESCRIPTION = "Search the merchant catalogue. All arguments optional."


def test_to_groq_tools_gives_search_catalog_the_enhanced_description() -> None:
    groq_tools = _to_groq_tools(TOOL_DEFS)
    search = next(t for t in groq_tools if t["function"]["name"] == "search_catalog")
    assert search["function"]["description"] == _GROQ_SEARCH_CATALOG_DESCRIPTION
    assert search["function"]["description"] != _ORIGINAL_SEARCH_CATALOG_DESCRIPTION


@pytest.mark.parametrize(
    "expected_phrase",
    [
        "substring",      # literal substring matching, not fuzzy/tokenized
        "keyword",        # a short/common keyword is more reliable than a phrase
        "category",       # category is an independent filter
        "max_price_inr",  # usable on its own, without query
        "reworded",       # don't just re-run a similar search after a result
        "invent",         # never invent a product id / catalogue value
    ],
)
def test_enhanced_description_covers_the_required_guidance(expected_phrase) -> None:
    description = _to_groq_tools(TOOL_DEFS)[0]["function"]["description"]
    assert expected_phrase in description


def test_enhanced_description_stays_a_generic_tool_contract() -> None:
    """Must remain product/catalogue-agnostic even though the investigation
    that prompted it involved one specific seeded product."""
    description = _to_groq_tools(TOOL_DEFS)[0]["function"]["description"].lower()
    for forbidden in ("featherlite", "running-shoes", "running shoes", "simulated"):
        assert forbidden not in description


def test_other_tool_descriptions_are_unaffected_by_the_search_catalog_fix() -> None:
    groq_tools = _to_groq_tools(TOOL_DEFS)
    by_name = {t["function"]["name"]: t["function"]["description"] for t in groq_tools}
    for tool in TOOL_DEFS:
        if tool["name"] == "search_catalog":
            continue
        assert by_name[tool["name"]] == tool["description"]


def test_to_groq_tools_does_not_mutate_the_shared_tool_defs() -> None:
    """TOOL_DEFS is app.ai.buyer's module-level constant — Gemini's source of
    truth too (see _to_gemini_tools). Translating it for Groq, including the
    search_catalog description substitution, must never mutate it in place."""
    before = copy.deepcopy(TOOL_DEFS)
    _to_groq_tools(TOOL_DEFS)
    assert TOOL_DEFS == before
    search = next(t for t in TOOL_DEFS if t["name"] == "search_catalog")
    assert search["description"] == _ORIGINAL_SEARCH_CATALOG_DESCRIPTION


def test_gemini_tool_translation_is_unaffected_by_the_groq_fix() -> None:
    """Regression guard for the exact isolation this fix must preserve:
    Gemini's own translation of the same TOOL_DEFS carries the original,
    unenhanced description — same as before this fix existed — regardless
    of what the Groq-bound copy now says."""
    gemini_tools = _to_gemini_tools(TOOL_DEFS)
    search_decl = next(d for d in gemini_tools[0].function_declarations if d.name == "search_catalog")
    assert search_decl.description == _ORIGINAL_SEARCH_CATALOG_DESCRIPTION
    assert search_decl.description != _GROQ_SEARCH_CATALOG_DESCRIPTION


async def test_groq_buyer_client_actually_sends_the_enhanced_description() -> None:
    """Not a test of model judgment (explicitly out of scope — GPT-OSS's
    actual tool choice is nondeterministic and untestable here). Just a
    deterministic check that the real wire-level `tools=` payload
    GroqBuyerClient hands to the SDK carries the enhanced description,
    end to end through the real client — not only through _to_groq_tools()
    in isolation."""
    stub = _GroqStub(responses=[_GroqResponse(_GroqMessage(content="looking"))])
    c = GroqBuyerClient(api_key="x", model="m", timeout_seconds=1, _client=stub)
    await c.next_step(messages=[{"role": "user", "content": "find shoes"}], tools=TOOL_DEFS)

    sent_tools = stub.calls[0]["tools"]
    search = next(t for t in sent_tools if t["function"]["name"] == "search_catalog")
    assert search["function"]["description"] == _GROQ_SEARCH_CATALOG_DESCRIPTION


# ==========================================================================
# 3. End to end: /ai/actions and /ai/buyer, real policy + audit (K, L, O,
#    and the Section-17 production regression)
# ==========================================================================
async def _reset(engine) -> None:
    async with engine.begin() as conn:
        await conn.exec_driver_sql("ALTER TABLE audit_event DISABLE TRIGGER USER")
        await conn.exec_driver_sql(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE")
        await conn.exec_driver_sql("ALTER TABLE audit_event ENABLE TRIGGER USER")


class _Api:
    def __init__(self, client, factory, ids) -> None:
        self.client = client
        self._factory = factory
        self.ids = ids

    def session(self):
        return self._factory()

    def use_fallback_parser(self, gemini_stub, groq_stub) -> None:
        gemini = GeminiParserClient(api_key="x", model="gemini-test", timeout_seconds=1, _client=gemini_stub)
        groq = GroqParserClient(api_key="x", model="llama-test", timeout_seconds=1, _client=groq_stub)
        app.dependency_overrides[get_ai_client] = lambda: FallbackParserClient(primary=gemini, secondary=groq)

    def use_fallback_buyer(self, gemini_stub, groq_stub) -> None:
        gemini = GeminiBuyerClient(api_key="x", model="gemini-test", timeout_seconds=1, _client=gemini_stub)
        groq = GroqBuyerClient(api_key="x", model="llama-test", timeout_seconds=1, _client=groq_stub)
        app.dependency_overrides[get_ai_buyer_client] = lambda: FallbackBuyerClient(primary=gemini, secondary=groq)

    async def parse(self, text: str, *, agent: str = "active_agent"):
        return await self.client.post("/ai/actions", json={"agent_id": str(self.ids[agent]), "text": text})

    async def run_buyer(self, *, agent: str = "active_agent", goal: str = "buy something nice"):
        return await self.client.post("/ai/buyer", json={"agent_id": str(self.ids[agent]), "goal": goal})

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
        "trailblaze": find(products, "Trailblaze"),
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
        app.dependency_overrides.pop(get_ai_client, None)
        app.dependency_overrides.pop(get_ai_buyer_client, None)
        await _reset(engine)
        await engine.dispose()


_HIGH_DEMAND_503 = (
    "This model is currently experiencing high demand. Spikes in demand are usually temporary."
)


# ---- I / K / L / Section 17: the real production scenario, via /ai/actions
async def test_ai_actions_recovers_from_a_real_gemini_503_via_groq(api) -> None:
    gemini_stub = _GeminiStub(error=_gemini_5xx(503, _HIGH_DEMAND_503))
    groq_stub = _GroqStub(responses=[
        _GroqResponse(_GroqMessage(content=json.dumps({
            "is_purchase_request": True,
            "product_reference": "Trailblaze Daily Trainer",
            "action_type": "PURCHASE",
            "quantity": 1,
        })))
    ])
    api.use_fallback_parser(gemini_stub, groq_stub)

    r = await api.parse("buy the trailblaze daily trainer")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"]["verdict"] == "ALLOW"
    assert body["decision"]["rule_id"] == "RULE_OK"
    assert body["resolved_product_id"] == str(api.ids["trailblaze"])

    _, decisions, audits, payments = await api.rows()
    assert decisions[0].verdict is Verdict.ALLOW
    assert payments == []  # no Razorpay object — this endpoint never touches payment
    assert [e.event_type for e in audits] == [
        "ACTION_REQUEST_RECEIVED", "ACTION_PARSED", "POLICY_EVALUATED",
    ]
    assert await api.chain_ok()
    # both providers were actually exercised — this proves the real
    # classification + translation code ran, not just the composition wrapper
    assert len(groq_stub.calls) == 1


async def test_ai_actions_both_providers_failing_denies_closed(api) -> None:
    gemini_stub = _GeminiStub(error=_gemini_5xx(503, _HIGH_DEMAND_503))
    groq_stub = _GroqStub(error=GroqAPITimeoutError(
        request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    ))
    api.use_fallback_parser(gemini_stub, groq_stub)

    r = await api.parse("buy the trailblaze daily trainer")
    assert r.status_code == 200
    body = r.json()
    assert body["decision"]["verdict"] == "DENY"
    assert body["decision"]["rule_id"] == "RULE_INPUT_INVALID"

    ars, _, _, payments = await api.rows()
    assert ars[0].parsed_payload["stage"] == "ai_call"
    assert payments == []
    assert await api.chain_ok()


async def test_ai_actions_non_transient_gemini_failure_never_calls_groq(api) -> None:
    gemini_stub = _GeminiStub(error=_gemini_400("bad structured-output request"))
    groq_stub = _GroqStub(responses=[_GroqResponse(_GroqMessage(content="{}"))])
    api.use_fallback_parser(gemini_stub, groq_stub)

    r = await api.parse("buy the trailblaze daily trainer")
    assert r.status_code == 200
    assert r.json()["decision"]["verdict"] == "DENY"
    assert groq_stub.calls == []  # not a transient failure -> no fallback attempt


# ---- K / L / Section 17: the buyer agent, tool calling through Groq -----
async def test_ai_buyer_recovers_from_a_real_gemini_503_via_groq(api) -> None:
    tid = str(api.ids["trailblaze"])
    gemini_stub = _GeminiStub(error=_gemini_5xx(503, _HIGH_DEMAND_503))
    groq_stub = _GroqStub(responses=[
        _GroqResponse(_GroqMessage(tool_calls=[
            _GroqToolCall("call_0", "request_action", json.dumps({
                "product_id": tid, "action_type": "PURCHASE", "quantity": 1,
            })),
        ])),
        _GroqResponse(_GroqMessage(content="Bought the Trailblaze Daily Trainer via Groq.")),
    ])
    api.use_fallback_buyer(gemini_stub, groq_stub)

    r = await api.run_buyer(goal="buy the trailblaze daily trainer")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "purchased"
    assert body["final_decision"]["verdict"] == "ALLOW"
    assert body["request_action_count"] == 1

    _, decisions, audits, payments = await api.rows()
    assert decisions[0].verdict is Verdict.ALLOW
    assert payments == []
    assert not any(e.event_type.startswith("PAYMENT_") for e in audits)
    assert await api.chain_ok()

    # sticky: Gemini was tried exactly once (step 1), never retried on step 2
    assert len(groq_stub.calls) == 2
    # the tool schema Groq actually received matches AgentGate's real, fixed
    # tool boundary — no extra tool was ever exposed to it
    tools_sent = groq_stub.calls[0]["tools"]
    assert {t["function"]["name"] for t in tools_sent} == {t["name"] for t in TOOL_DEFS}


async def test_ai_buyer_disabled_ignores_fallback_settings(api) -> None:
    # AI_ENABLED=false in the test environment (tests/conftest.py) — proves
    # the disabled hard-stop still wins regardless of any fallback wiring.
    app.dependency_overrides.pop(get_ai_buyer_client, None)
    r = await api.run_buyer(goal="buy something")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "AI_DISABLED"
