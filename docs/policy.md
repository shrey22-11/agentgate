# AgentGate policy engine (Phase 4)

**OUR SYSTEM.** Deterministic, pure, in-memory. Given a validated
`PolicyInput`, `app.policy.evaluate()` returns exactly one `PolicyDecision`.

The engine calls no LLM, no Razorpay, no HTTP, no database. The same input
always produces an equal output. It is total: it never raises for a policy
reason — genuinely malformed input yields `DENY / RULE_INPUT_INVALID`.

## Phase boundary

Phase 4 is decision logic only. It does **not**:

- expose an HTTP endpoint,
- persist a `decision` row (Phase 5),
- move money or call Razorpay (Phase 8),
- call the AI parser or handle prompt injection (Phase 9),
- run approval workflow (Phase 6).

## The four verdicts

| Verdict | Meaning | Produced when |
|---|---|---|
| `ALLOW` | The request satisfies every rule and may proceed toward execution. | No rule fired. `rule_id = RULE_OK`. |
| `DENY` | The request violates policy and must not proceed. Nothing is charged. | Agent inactive, action not permitted, quantity over stock, or input invalid. |
| `NEEDS_APPROVAL` | Commercially plausible but outside the agent's delegated authority. Goes to the human approval queue. | Effective transaction amount exceeds the agent's per-transaction limit. |
| `COUNTER_OFFER` | The requested price is below the policy floor, but a valid deal exists at the floor. | Effective unit price is below `max(discount-cap price, margin floor)`. Carries `counter_offer_price` and `counter_offer_discount_pct`. |

## Evaluation precedence

The first rule that fires decides the verdict; later rules are not consulted.

| # | Rule ID | Verdict on fire | Condition |
|---|---|---|---|
| 0 | `RULE_INPUT_INVALID` | `DENY` | Required product fields missing; `min_margin_price > price`; a `proposed_price` that contradicts a stated `requested_discount_pct`. |
| 1 | `RULE_AGENT_ACTIVE` | `DENY` | `agent_status != ACTIVE`. |
| 2 | `RULE_ACTION_PERMISSION` | `DENY` | `action_type` not in the agent's `allowed_actions`. |
| 3 | `RULE_TRANSACTION_CAP` | `NEEDS_APPROVAL` | `effective_unit_price * quantity > agent_max_transaction_amount`. |
| 4 | `RULE_STOCK_AVAILABLE` | `DENY` | `quantity > product_stock`. |
| 5 | `RULE_DISCOUNT_POLICY` / `RULE_PRICE_FLOOR` | `COUNTER_OFFER` | `effective_unit_price < floor_price`. `RULE_PRICE_FLOOR` when the margin floor is the binding constraint, `RULE_DISCOUNT_POLICY` when the discount cap is. |
| 6 | `RULE_OK` | `ALLOW` | Nothing above fired. |

Ordering rationale:

- **Agent identity and authority (1–3) before anything about the catalogue.** An
  inactive or unauthorised agent is denied even if the commercial terms are fine.
- **Transaction cap (3) before stock (4).** The cap is a question about the
  *agent's* delegated authority; stock is a *merchant fulfilment* question. A
  large order from an out-of-authority agent is routed to approval, not denied
  for stock. (A reviewable call — documented so it is a decision, not an accident.)
- **Commercial terms (5) last**, so an out-of-stock request is denied outright
  and never produces a `COUNTER_OFFER` for inventory that does not exist.

Boundary behaviour is `<=` throughout: an amount exactly at the cap, a quantity
exactly equal to stock, and a discount exactly at the cap all pass.

## Effective transaction amount

```
quantity              = requested_quantity or 1
effective_unit_price  = proposed_price                                if given
                        else price * (100 - requested_discount_pct)/100  if a discount given
                        else price
effective_txn_amount  = effective_unit_price * quantity
```

If both `proposed_price` and `requested_discount_pct` are supplied they must
agree within 0.01 percentage points, otherwise the input is rejected as
inconsistent (`RULE_INPUT_INVALID`).

## Counter-offer discipline

> **The counter-offer amount is calculated deterministically by the policy
> engine (`app.counter_offer.compute_floor`). No LLM is allowed to generate or
> override a commercial boundary.**

This is structural, not a convention: nothing in `app/counter_offer/engine.py`
or `app/policy/engine.py` can import or call a model client. There is a test
(`test_policy_and_counter_offer_source_never_uses_float`, alongside the
counter-offer value tests) that reads the source and fails on `float(`.

### Formula

```
discounted_at_cap = list_price * (100 - max_discount_pct) / 100
floor_price       = max(discounted_at_cap, min_margin_price)
```

`floor_price` is always within `[min_margin_price, list_price]`.

### Worked example — Velocity Pro (from `app/seed.py`)

```
list_price        = ₹10,000.00
max_discount_pct  = 10%
min_margin_price  = ₹8,800.00

discounted_at_cap = 10000 * (100 - 10) / 100 = ₹9,000.00
floor_price       = max(9000.00, 8800.00)    = ₹9,000.00
```

An agent requesting **20% off** proposes `₹8,000.00`, which is below
`₹9,000.00`, so:

```
verdict                    = COUNTER_OFFER
rule_id                    = RULE_DISCOUNT_POLICY   (discount cap is binding, not margin)
counter_offer_price        = ₹9,000.00
counter_offer_discount_pct = 10.00%
```

The agent's accept/reject of this counter-offer arrives later as its own
`ActionRequest` (`action_type = ACCEPT_COUNTER_OFFER`) and is re-evaluated by
the identical path — defence in depth.

## Money and rounding

- All monetary values are `Decimal`, never `float`, end to end.
- Money quantized to paise (`0.01`), percentages to `0.01`, both with
  `ROUND_HALF_UP` — one rule for the whole codebase.
- Every seeded demo scenario is exact at paise, so rounding never changes a demo
  number; the rule exists for the general case and is covered by a
  `₹999.99 / 7.5%` test.

## Invalid input — how it fails safely

Two layers, both fail closed:

1. **Structural** — `PolicyInput` is a frozen Pydantic v2 model with
   `extra="forbid"` and per-field bounds (money ≥ 0, percentages 0–100,
   quantity ≥ 1). Malformed data cannot be constructed; the caller gets a
   `ValidationError`. Phase 5 will wrap construction and turn a failure into a
   `DENY` decision.
2. **Semantic** — cross-field checks that need policy context (missing product
   data for a purchase, `min_margin_price > price`, contradictory price/discount)
   run inside the engine and return `DENY / RULE_INPUT_INVALID` rather than
   raising, keeping `evaluate()` total.

## Files

```
app/policy/input.py       PolicyInput  (frozen Pydantic snapshot)
app/policy/decision.py    PolicyDecision (frozen dataclass)
app/policy/rules.py       stable rule-id constants + precedence doc
app/policy/engine.py      evaluate(PolicyInput) -> PolicyDecision
app/policy/__init__.py    public surface: evaluate, PolicyInput, PolicyDecision, rules
app/counter_offer/engine.py   compute_floor(...) -> CounterOffer
tests/test_policy_engine.py    39 tests
tests/test_counter_offer.py    17 tests
```
