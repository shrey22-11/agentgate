"""
The AI buyer agent runner (Phase 10).

A bounded multi-step loop: the model searches/compares the catalogue and makes
`request_action` calls; each `request_action` routes through the ONE policy path
(`evaluate_action`) and returns a deterministic verdict the model cannot change.

Hard guarantees (all enforced here, not by the model):
  * the model has exactly four tools — three read-only, one write-shaped
    (`request_action`). There is no payment / approval / refund tool.
  * `request_action` is bound to the run's fixed `agent_id`; the model has no
    field to act as a different agent.
  * a run gets at most `ai_buyer_max_steps` model turns and
    `ai_buyer_max_request_actions` `request_action` calls.
  * the catalogue tools never expose `max_discount_pct` / `min_margin_price` —
    the agent learns a boundary only from a `COUNTER_OFFER` it receives back.
  * a `request_action` never creates a PaymentAttempt (that is a separate
    endpoint, not a tool). An ALLOW means "policy permits", not "money moved".
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.action_requests.schemas import ActionDecisionResponse, ActionRequestCreate
from app.action_requests.service import (
    AiParseContext,
    ResourceNotFound,
    evaluate_action,
)
from app.agents.models import Agent
from app.ai.client import AIBuyerClient, AIUnavailableError, BuyerStep
from app.ai.schemas import BuyerRunResponse, BuyerTranscriptEntry
from app.catalog.queries import compare_products, get_product, search_catalog
from app.core.config import get_settings
from app.core.enums import ActionType, Verdict

_READ_TOOLS = {"search_catalog", "get_product", "compare_products"}

# Sent to the model on every step. Numbers are strings to keep the JSON simple
# and to keep binary floats out (mirrors app/ai/schemas.py::ParsedIntent).
TOOL_DEFS: list[dict] = [
    {
        "name": "search_catalog",
        "description": "Search the merchant catalogue. All arguments optional.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "keyword to match name/description/category"},
                "category": {"type": "string"},
                "max_price_inr": {"type": "string", "description": "upper price bound, e.g. '5000'"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_product",
        "description": "Full detail for one product by its catalogue id.",
        "input_schema": {
            "type": "object",
            "properties": {"product_id": {"type": "string"}},
            "required": ["product_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "compare_products",
        "description": "Side-by-side view of 2+ products by id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_ids": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["product_ids"],
            "additionalProperties": False,
        },
    },
    {
        "name": "request_action",
        "description": (
            "Ask AgentGate to permit a purchase or a discount. Returns a verdict "
            "(ALLOW / DENY / NEEDS_APPROVAL / COUNTER_OFFER) that is final."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string", "description": "id from a catalogue tool"},
                "action_type": {"type": "string", "enum": ["PURCHASE", "ACCEPT_COUNTER_OFFER"]},
                "quantity": {"type": "integer"},
                "requested_discount_pct": {"type": "string", "description": "the discount you are asking for, e.g. '20'"},
                "proposed_price": {"type": "string", "description": "a per-unit price you offer, e.g. '9000'"},
            },
            "required": ["product_id", "action_type"],
            "additionalProperties": False,
        },
    },
]


class _ToolInputError(Exception):
    pass


def _as_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _decimal_or_none(raw, field: str) -> Decimal | None:
    if raw is None:
        return None
    if isinstance(raw, (float, bool)):
        raise _ToolInputError(f"{field} must be a string number, not a float")
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise _ToolInputError(f"{field} {raw!r} is not a number") from exc
    if not value.is_finite():
        raise _ToolInputError(f"{field} {raw!r} is not finite")
    return value


def _uuid_or_none(raw) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return None


async def _exec_read_tool(session: AsyncSession, name: str, tool_input: dict) -> dict:
    if name == "search_catalog":
        try:
            max_price = _decimal_or_none(tool_input.get("max_price_inr"), "max_price_inr")
        except _ToolInputError as exc:
            return {"error": str(exc)}
        views = await search_catalog(
            session,
            query=tool_input.get("query"),
            category=tool_input.get("category"),
            max_price_inr=max_price,
        )
        return {"count": len(views), "results": [v.as_dict() for v in views]}

    if name == "get_product":
        pid = _uuid_or_none(tool_input.get("product_id"))
        if pid is None:
            return {"error": "product_id must be a valid id from a catalogue tool"}
        view = await get_product(session, pid)
        return view.as_dict() if view is not None else {"error": "no such product"}

    if name == "compare_products":
        raw_ids = tool_input.get("product_ids") or []
        ids = [pid for pid in (_uuid_or_none(x) for x in raw_ids) if pid is not None]
        if not ids:
            return {"error": "product_ids must be a list of valid ids"}
        views = await compare_products(session, ids)
        return {"products": [v.as_dict() for v in views]}

    return {"error": f"unknown tool {name!r}"}


async def _exec_request_action(
    session: AsyncSession, *, agent_id: uuid.UUID, goal: str, tool_input: dict
) -> tuple[dict, ActionDecisionResponse | None]:
    pid = _uuid_or_none(tool_input.get("product_id"))
    if pid is None:
        return {"error": "product_id must be a valid id from a catalogue tool"}, None

    action_raw = str(tool_input.get("action_type", "PURCHASE")).upper()
    if action_raw not in ("PURCHASE", "ACCEPT_COUNTER_OFFER"):
        return {"error": f"action_type must be PURCHASE or ACCEPT_COUNTER_OFFER, got {action_raw!r}"}, None

    try:
        discount = _decimal_or_none(tool_input.get("requested_discount_pct"), "requested_discount_pct")
        price = _decimal_or_none(tool_input.get("proposed_price"), "proposed_price")
    except _ToolInputError as exc:
        return {"error": str(exc)}, None

    quantity = tool_input.get("quantity")
    if quantity is not None:
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            return {"error": "quantity must be an integer"}, None

    try:
        body = ActionRequestCreate(
            agent_id=agent_id,
            product_id=pid,
            action_type=ActionType(action_raw),
            quantity=quantity,
            requested_discount_pct=discount,
            proposed_price=price,
        )
    except ValidationError as exc:
        first = exc.errors()[0]["msg"] if exc.errors() else str(exc)
        return {"error": f"invalid request_action: {first}"}, None

    ai_context = AiParseContext(
        raw_input=(
            f"[ai_buyer] goal={goal!r}; requested {action_raw} product={pid} "
            f"discount_pct={discount} proposed_price={price} quantity={quantity}"
        ),
        confidence=Decimal("1"),
        parsed_payload={
            "source": "ai_buyer",
            "goal": goal,
            "action_type": action_raw,
            "product_id": str(pid),
            "requested_discount_pct": _as_str(discount),
            "proposed_price": _as_str(price),
            "quantity": quantity,
        },
        audit_payload={
            "success": True,
            "source": "ai_buyer",
            "confidence": Decimal("1"),
            "action_type": action_raw,
            "product_id": str(pid),
            "requested_discount_pct": _as_str(discount),
        },
    )

    try:
        decision = await evaluate_action(session, body, ai_context=ai_context)
    except ResourceNotFound as exc:
        return {"error": exc.message}, None

    result = {
        "verdict": decision.verdict.value,
        "rule_id": decision.rule_id,
        "reason": decision.reason,
        "counter_offer": (
            {
                "price": str(decision.counter_offer.price),
                "discount_pct": str(decision.counter_offer.discount_pct),
            }
            if decision.counter_offer is not None
            else None
        ),
        "action_request_id": str(decision.action_request_id),
        "decision_id": str(decision.decision_id),
    }
    return result, decision


def _outcome(
    *,
    ai_unavailable: bool,
    budget_hit: bool,
    final_decision: ActionDecisionResponse | None,
    last_action_type: str | None,
):
    if ai_unavailable:
        return "ai_unavailable"
    if final_decision is not None:
        verdict = final_decision.verdict
        if verdict is Verdict.ALLOW:
            return (
                "counter_offer_accepted"
                if last_action_type == "ACCEPT_COUNTER_OFFER"
                else "purchased"
            )
        if verdict is Verdict.COUNTER_OFFER:
            return "counter_offer_received"
        if verdict is Verdict.NEEDS_APPROVAL:
            return "needs_approval"
        return "denied"
    if budget_hit:
        return "budget_exhausted"
    return "no_action"


async def run_buyer_agent(
    session: AsyncSession,
    client: AIBuyerClient,
    *,
    agent_id: uuid.UUID,
    goal: str,
) -> BuyerRunResponse:
    settings = get_settings()
    max_steps = settings.ai_buyer_max_steps
    max_request_actions = settings.ai_buyer_max_request_actions

    agent = (
        await session.scalars(select(Agent).where(Agent.id == agent_id))
    ).one_or_none()
    if agent is None:
        raise ResourceNotFound("AGENT_NOT_FOUND", f"No agent with id {agent_id}")

    messages: list[dict] = [
        {
            "role": "user",
            "content": (
                f"Shopping goal: {goal}\n\n"
                f"You have at most {max_steps} steps and {max_request_actions} "
                "request_action calls. Begin."
            ),
        }
    ]
    transcript: list[BuyerTranscriptEntry] = []
    request_action_count = 0
    final_decision: ActionDecisionResponse | None = None
    last_action_type: str | None = None
    summary: str | None = None
    ai_unavailable = False
    budget_hit = False
    steps_used = 0

    for step in range(1, max_steps + 1):
        steps_used = step
        try:
            buyer_step: BuyerStep = await client.next_step(messages=messages, tools=TOOL_DEFS)
        except AIUnavailableError as exc:
            ai_unavailable = True
            summary = f"buyer agent stopped: {exc}"
            transcript.append(
                BuyerTranscriptEntry(step=step, kind="model_text", detail=summary)
            )
            break

        if buyer_step.text:
            transcript.append(
                BuyerTranscriptEntry(step=step, kind="model_text", detail=buyer_step.text)
            )

        if buyer_step.kind == "final":
            summary = buyer_step.text
            break

        messages.append({"role": "assistant", "content": buyer_step.assistant_content})
        tool_results: list[dict] = []
        for call in buyer_step.tool_calls:
            transcript.append(
                BuyerTranscriptEntry(
                    step=step, kind="tool_call", tool=call.name, detail=call.input
                )
            )
            if call.name == "request_action":
                if request_action_count >= max_request_actions:
                    result: dict = {
                        "error": "request_action budget exhausted for this run"
                    }
                else:
                    request_action_count += 1
                    result, decision = await _exec_request_action(
                        session, agent_id=agent_id, goal=goal, tool_input=call.input
                    )
                    if decision is not None:
                        final_decision = decision
                        last_action_type = str(
                            call.input.get("action_type", "PURCHASE")
                        ).upper()
            elif call.name in _READ_TOOLS:
                result = await _exec_read_tool(session, call.name, call.input)
            else:
                result = {"error": f"unknown tool {call.name!r}"}

            transcript.append(
                BuyerTranscriptEntry(
                    step=step, kind="tool_result", tool=call.name, detail=result
                )
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": json.dumps(result),
                }
            )
        messages.append({"role": "user", "content": tool_results})
    else:
        # for-loop finished without break -> ran out of model turns
        budget_hit = True
        summary = summary or "buyer agent stopped: step budget exhausted"

    outcome = _outcome(
        ai_unavailable=ai_unavailable,
        budget_hit=budget_hit,
        final_decision=final_decision,
        last_action_type=last_action_type,
    )

    return BuyerRunResponse(
        goal=goal,
        outcome=outcome,
        summary=summary,
        request_action_count=request_action_count,
        steps_used=steps_used,
        final_decision=final_decision,
        transcript=transcript,
    )
