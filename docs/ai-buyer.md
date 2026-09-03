# AI buyer agent (Phase 10)

A bounded, multi-step LLM agent that pursues a user's shopping goal against the
merchant's catalogue **through AgentGate**. It is modelled as an untrusted
counterparty: it browses, it proposes, and it reacts to verdicts — it never
decides one, and it never moves money.

## Endpoint

```
POST /ai/buyer   { agent_id, goal }
```

- `agent_id` — the registered `agent` row the buyer operates as. Every
  `request_action` the agent makes is bound to **this** id; the model has no
  field to act as anyone else.
- `goal` — the natural-language shopping goal (1–2000 chars), e.g.
  *"find running shoes under ₹5,000 and buy a pair"*.

Response: `BuyerRunResponse` — `goal`, `outcome`, `summary`, `request_action_count`,
`steps_used`, `final_decision` (the last `request_action`'s deterministic
decision, echoed), and a `transcript` (model text / tool calls / tool results).

| status | when |
|---|---|
| `200` | run completed (see `outcome`) |
| `404` | `AGENT_NOT_FOUND` — unknown `agent_id`, nothing persisted |
| `503` | `AI_DISABLED` — `AI_ENABLED=false`, nothing persisted |
| `422` | malformed body |

`outcome` ∈ `purchased` · `counter_offer_accepted` · `counter_offer_received` ·
`needs_approval` · `denied` · `no_action` · `budget_exhausted` · `ai_unavailable`.

## Tools the agent has — and doesn't

| Tool | Kind | Effect |
|---|---|---|
| `search_catalog(query?, category?, max_price_inr?)` | read-only | `ProductView` list (price-ascending) |
| `get_product(product_id)` | read-only | one `ProductView` |
| `compare_products(product_ids[])` | read-only | `ProductView` list |
| `request_action(product_id, action_type, quantity?, requested_discount_pct?, proposed_price?)` | write-shaped | routes through `evaluate_action` → returns a verdict |

**There is no `create_order` / `create_payment_link` / `capture` / `refund` /
`approve` tool.** The four tools above are the entire surface. A test asserts the
model is only ever handed these four names, and that a full purchase run creates
**zero** `PaymentAttempt` rows and emits no `PAYMENT_*` audit event. An `ALLOW`
from `request_action` means *policy permits the action* — payment execution is
`POST /payments/{decision_id}/execute`, which the agent cannot reach.

### `ProductView` hides the policy boundary

`search_catalog` / `get_product` / `compare_products` return
`{product_id, name, description, category, price, in_stock, stock}` — **never**
`max_discount_pct` or `min_margin_price`. The untrusted agent learns a commercial
boundary only by *receiving a `COUNTER_OFFER`* back, never by reading the
merchant's ceiling out of the catalogue. (`app/catalog/queries.py::ProductView`.)

## The loop (`app/ai/buyer.py::run_buyer_agent`)

```
load Agent (404 if unknown)
messages = [goal + budget notice]
for step in 1..AI_BUYER_MAX_STEPS:
    step = client.next_step(messages, TOOL_DEFS)        [Anthropic messages.create, one turn]
        AIUnavailableError → stop, outcome = ai_unavailable
    if step is a final answer → stop, summary = text
    for each tool_call in step:
        request_action:
            if request_action_count == AI_BUYER_MAX_REQUEST_ACTIONS → tool result {"error": "budget exhausted"}
            else: coerce inputs → ActionRequestCreate → evaluate_action(…, ai_context=…) → verdict back to the model
        search/get/compare: run the read query
        unknown tool: {"error": …}
    append assistant turn + tool_results to messages
else:  # ran out of steps
    outcome = budget_exhausted
```

- **`request_action` uses the ONE policy path.** It builds the existing
  `ActionRequestCreate` (re-validated, `_reject_float`), then calls
  `evaluate_action` with an `AiParseContext` (`source: "ai_buyer"`,
  `confidence=1`, synthesised `raw_input`). So every buyer action produces the
  same `ACTION_REQUEST_RECEIVED → ACTION_PARSED → POLICY_EVALUATED
  [→ APPROVAL_REQUESTED]` chain as any other request, queryable by `agent_id`.
- **Counter-offer handling.** A `COUNTER_OFFER` result carries a `price`. The
  agent may call `request_action` again with `action_type =
  "ACCEPT_COUNTER_OFFER"` and `proposed_price` = that price; the policy engine
  re-evaluates it exactly like any request (proposed price == floor → `ALLOW`).
  Rejecting is just… not doing that (`outcome = counter_offer_received`).
- **The agent cannot bypass anything.** `test_agent_proposing_one_rupee_still_
  gets_the_engine_floor`: a `request_action` with `proposed_price="1"` on the
  ₹10,000 / 10%-cap / ₹8,800-floor product returns `COUNTER_OFFER` at **₹9,000**
  — the engine's number — never `ALLOW` at ₹1. An inactive agent's run is
  `DENY / RULE_AGENT_ACTIVE`; a ₹45,000 product is `NEEDS_APPROVAL`.

## Budgets

Hard caps enforced by the loop, not the model:

| setting | default | meaning |
|---|---|---|
| `AI_BUYER_MAX_STEPS` | 8 | model turns per run |
| `AI_BUYER_MAX_REQUEST_ACTIONS` | 3 | `request_action` calls per run (enough for: try discount → get counter → accept) |

A `request_action` beyond the cap returns a `{"error": "budget exhausted"}` tool
result (never reaches the engine); the model can still wrap up. Running out of
steps ends the run with `outcome = budget_exhausted` (unless a decision was
already made, which is reported instead).

## Failure handling

| Failure | Behaviour |
|---|---|
| `AI_ENABLED=false` | `503`, nothing persisted |
| unknown `agent_id` | `404`, nothing persisted |
| Anthropic error/timeout **at step 1** | `200`, `outcome=ai_unavailable`, nothing persisted |
| Anthropic error **mid-run** | `200`, `outcome=ai_unavailable`, `final_decision` = whatever the agent already got (those decisions are committed and audited) |
| model calls `request_action` with a non-existent `product_id` | tool result `{"error": …}`, nothing persisted for that call, run continues |
| malformed number / bad `action_type` in a tool input | tool result `{"error": …}`, no decision, run continues |
| malformed body | `422` |

Each `request_action` commits its own transaction (Phase 6 behaviour). A run is
a *sequence* of independently-audited decisions, not one atomic unit — which is
correct: the agent making three requests is three real decisions.

## Persistence

- **Decisions the agent triggers are persisted** — `action_request` (with
  `parsed_payload.source = "ai_buyer"` and the goal), `decision`, and the audit
  chain. Queryable by `agent_id`.
- **The transcript is not persisted** — it is returned in the HTTP response for
  the caller / UI. There is no agent-transcript table (none in the frozen
  schema); adding one is out of scope for Phase 10.

## Configuration

Reuses `AI_ENABLED`, `ANTHROPIC_API_KEY`, `AI_MODEL`,
`AI_REQUEST_TIMEOUT_SECONDS` from Phase 9, plus `AI_BUYER_MAX_STEPS` /
`AI_BUYER_MAX_REQUEST_ACTIONS`. `get_ai_buyer_client()` returns
`DisabledBuyerClient` when disabled, `AnthropicBuyerClient` when enabled. The
`anthropic` SDK stays confined to `app/ai/client.py`.

## Testing

27 tests in `tests/test_ai_buyer.py`, all via a scripted `FakeAIBuyerClient`
(returns pre-built `BuyerStep`s; can raise after N steps). **No real Anthropic
call.** Coverage: search→purchase→ALLOW; counter-offer→accept→ALLOW;
counter-offer→stop; the ₹1 lowball still floored at the engine's ₹9,000;
inactive→DENY; high-value→NEEDS_APPROVAL; impersonation attempt ignored; both
budgets; catalogue tools (and their absence of policy fields); hallucinated
product ids; malformed tool numbers; provider failure at step 1 and mid-run;
disabled→503; unknown agent→404; body validation; the four-tools-only guard; and
an import guard that `app/ai/buyer.py` never imports `razorpay` / `payment` /
`webhook`.

## Not verified against the real Anthropic API

All Phase 10 behaviour is tested with the fake client. A live run (set
`AI_ENABLED=true` + a real key, `POST /ai/buyer`) has **not** been executed — no
key is available in this environment. The `AnthropicBuyerClient` sends
`messages.create` with `tools=TOOL_DEFS` and reads `stop_reason` /
`tool_use` blocks per the SDK's documented shapes.
