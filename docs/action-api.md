# Action API (Phase 6)

**OUR SYSTEM.** Deterministic. No LLM, no Razorpay, no background work.

One endpoint connects the existing pieces into a real workflow:

```
POST /actions
```

An external agent asks to perform a commercial action; the merchant's
deterministic policy decides; the request, the decision, and two audit events
are persisted atomically; the decision is returned.

## Request

`Content-Type: application/json`

| field | type | required | notes |
|---|---|---|---|
| `agent_id` | UUID | yes | which agent is asking |
| `product_id` | UUID | yes | which product |
| `action_type` | `"PURCHASE"` \| `"ACCEPT_COUNTER_OFFER"` | no (default `PURCHASE`) | |
| `quantity` | integer ≥ 1 | no | defaults to 1 in the policy engine |
| `requested_discount_pct` | decimal 0–100 | no | **JSON string or integer**, e.g. `"20.00"` or `20` |
| `proposed_price` | decimal ≥ 0 | no | per-unit price the agent offers |

Unknown fields are rejected (`extra="forbid"` → 422). A **JSON float** in
`requested_discount_pct` / `proposed_price` is rejected (422): binary floating
point has no place in payments-adjacent input. Money and percentages are
`Decimal` end to end.

`agent_id` / `product_id` identify *which* agent and product. The agent's
`status`, `allowed_actions`, `max_transaction_amount` and the product's `price`,
`stock`, `max_discount_pct`, `min_margin_price` are **read from the database** —
never taken from the request.

```json
{
  "agent_id": "162fca80-78c6-49df-9006-0e0855ff7485",
  "product_id": "3362c078-b641-4b52-b82e-b2ba60547b5d",
  "requested_discount_pct": "20.00"
}
```

## Response

`200 OK` for every policy verdict (see *Verdict semantics*).

```json
{
  "action_request_id": "1798bd77-f9ba-4a5e-8b82-e41c4d2faa41",
  "decision_id": "f77fd35f-0056-49e4-b1d6-efa046735783",
  "verdict": "COUNTER_OFFER",
  "rule_id": "RULE_DISCOUNT_POLICY",
  "reason": "Requested unit price ₹8000.00 (20.00% off) is below the 10.00% maximum discount (₹9000.00 from list ₹10000.00). Counter-offered at ₹9000.00.",
  "policy_version": "v1",
  "counter_offer": { "price": "9000.00", "discount_pct": "10.00" }
}
```

`counter_offer` is `null` for `ALLOW`, `DENY` and `NEEDS_APPROVAL`. `Decimal`
values serialise as JSON strings.

### Errors

| status | when | body |
|---|---|---|
| `422` | request fails schema validation (bad UUID, float money, unknown field, quantity < 1) | FastAPI validation error |
| `404` | `agent_id` or `product_id` does not exist | `{"detail": {"code": "AGENT_NOT_FOUND" \| "PRODUCT_NOT_FOUND", "message": "..."}}` |
| `500` | unexpected server/infrastructure failure | generic error; the transaction is rolled back — no partial state |

## Verdict semantics

`ALLOW`, `DENY`, `NEEDS_APPROVAL`, `COUNTER_OFFER` are **all successful policy
evaluations** and all return `200`. In particular:

- **`DENY` is not an HTTP error.** The system evaluated the request and the
  answer is "no". Returning `403` would conflate "policy said no" with "you may
  not call this endpoint".
- **`NEEDS_APPROVAL` is not an error.** The request is commercially plausible but
  above the agent's delegated authority. Phase 6 records the verdict; it does not
  run any approval workflow.
- **`COUNTER_OFFER` is a normal result.** `counter_offer.price` is the
  deterministic floor from `app.counter_offer`; no LLM produces or adjusts it.

## Unknown agent / product → 404, nothing persisted

The endpoint returns `404` and writes **no** `action_request`, `decision`, or
audit event. Reasons:

- `action_request.agent_id` is a `NOT NULL` foreign key. A "rejected request" row
  for a non-existent agent is structurally impossible without dropping
  referential integrity, which would be a worse outcome than a 404.
- The request never reached policy evaluation, so there is no *decision* to audit.
- Unbounded audit writes for unauthenticated garbage input would be a denial-of-
  service vector against the append-only chain.

A `404` is unambiguously not an `ALLOW`, so the "never silently allow an unknown
resource" rule holds.

## End-to-end flow

```
HTTP request (Pydantic-validated)
   │
   ▼
load Agent  (404 if missing)                 ← authoritative
load Product + its Merchant (404 if missing) ← authoritative
   │
   ▼
INSERT action_request  (status RECEIVED)
   │
   ▼
append_audit_event  ACTION_REQUEST_RECEIVED
   │
   ▼
build PolicyInput  (DB agent/product fields + request fields)
   │
   ▼
evaluate(PolicyInput)          ← app.policy, unchanged
   │
   ▼
INSERT decision ; action_request.status = DECIDED
   │
   ▼
append_audit_event  POLICY_EVALUATED
   │
   ▼
COMMIT   ← the single transaction boundary
   │
   ▼
ActionDecisionResponse
```

`ActionRequestStatus` moves `RECEIVED → DECIDED`. `PARSED` / `VALIDATED` /
`INVALID` stay reserved for the Phase 9 natural-language path; this endpoint
receives already-structured input, so it skips them. `raw_input` and
`confidence` are left null; `parsed_payload` records the structured request that
fed the policy input (`{"source": "http", ...}`).

## Authority boundaries

| Data | Source |
|---|---|
| which agent, which product, action type, quantity, requested discount, proposed price | **client request** (Pydantic-validated) |
| agent `status`, `allowed_actions`, `max_transaction_amount` | **database** (`agent` row) |
| product `price`, `stock`, `max_discount_pct`, `min_margin_price`, merchant `policy_version` | **database** (`product` + `merchant` rows) |
| verdict, rule id, reason, counter-offer price & discount % | **`app.policy.evaluate()`** — deterministic, never the client, never an LLM |

`PolicyInput` is a plain frozen object; the policy engine never sees an ORM
instance (Phase 4's DB-independence is preserved). It is built in
`app/action_requests/service.py::_build_policy_input`, outside `app/policy/`.

## Transaction boundary & atomicity

The route's `get_db` dependency yields one session for the whole request and
rolls it back if the handler raises. `app/action_requests/service.py` does every
write — `action_request`, `decision`, both audit events — and calls
`session.commit()` **once, as its last step**. `append_audit_event()` never
commits (Phase 5 contract).

So the set `{action_request, decision, ACTION_REQUEST_RECEIVED,
POLICY_EVALUATED}` commits together or not at all. If any step raises before the
final commit (a DB error, an audit failure), nothing is persisted. This is
covered by `test_atomicity_rollback_when_audit_write_fails`, which patches
`append_audit_event` to fail on the second call (after `action_request` and
`decision` are already flushed) and asserts all three tables are empty
afterwards — production behaviour is not weakened, only the boundary is
exercised.

## Audit events

Two events per successful request, both `ref_type="action_request"`,
`ref_id=<action_request id>` (so the whole story of one request is one
`WHERE ref_id = ...` query):

| event | payload |
|---|---|
| `ACTION_REQUEST_RECEIVED` | `action_request_id`, `agent_id`, `product_id`, `action_type`, `quantity`, `requested_discount_pct`, `proposed_price` |
| `POLICY_EVALUATED` | `decision_id`, `action_request_id`, `verdict`, `rule_id`, `reason`, `policy_version`, `counter_offer_price`, `counter_offer_discount_pct` |

Both are appended in the same transaction. `append_audit_event`'s advisory lock
is re-entrant within one transaction, so the second call does not deadlock, and
it reads the first (flushed) event as the chain head — `POLICY_EVALUATED.prev_hash
== ACTION_REQUEST_RECEIVED.hash`. `verify_audit_chain()` passes after every
successful request. Constants come from `app.audit.events`; no string literals
are duplicated.

## What Phase 6 does NOT do

- **No LLM.** The endpoint works with no `ANTHROPIC_API_KEY`. Natural-language
  parsing and the AI buyer agent are Phase 9/10.
- **No Razorpay execution.** An `ALLOW` means *policy permits the action* — no
  order, no payment link, no charge. Payment execution is a later phase.
- **No approval resolution.** `NEEDS_APPROVAL` is recorded; approving/rejecting
  it is Phase 7.
- **No idempotency key on `/actions`.** There is no payment object to
  de-duplicate here; idempotency arrives with Razorpay execution.
