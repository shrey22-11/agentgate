# Approval flow (Phase 7)

**OUR SYSTEM.** A human gate over a `NEEDS_APPROVAL` decision. Deterministic,
auditable, atomic. It is **not** a second policy engine.

## What a `NEEDS_APPROVAL` verdict means

The requested action is policy-valid enough to continue, but its transaction
amount exceeds the authority delegated to the requesting agent
(`RULE_TRANSACTION_CAP`). A human may let it proceed — or not.

## What approval does NOT do

Approval never:

- turns a `DENY` (or `ALLOW`, or `COUNTER_OFFER`) into an approvable item —
  only `NEEDS_APPROVAL` decisions can be resolved;
- overrides stock, discount, or the deterministic price floor;
- changes the product, the requested amount, or the request;
- mutates `Decision.verdict`, `Decision.policy_rule_id`, `Decision.reason`, or
  `Decision.policy_version` — the deterministic decision stays historical truth;
- creates a `PaymentAttempt` — payment execution is a later phase;
- calls Razorpay or an LLM.

The `Approval` row is a **separate human resolution layered on top of** the
decision, not an edit of it.

## Why "pending" is not a stored state

Inspecting the schema: the `Approval` model requires `outcome` (`APPROVED` /
`REJECTED`) `NOT NULL`, and has `UniqueConstraint(decision_id)`. So:

- **Pending** = a `Decision` with `verdict = NEEDS_APPROVAL` and **no** `Approval`
  row. No extra column, no parallel state machine.
- The `Approval` row is created **only at resolution**, carrying the outcome.
- `uq_approval_decision` makes a second resolution impossible at the database
  level — the strongest possible guarantee for "resolve once".

No migration was needed for Phase 7; the Phase 3 schema already supports it.

## Lifecycle

```
POST /actions
   │  (agent's transaction amount over its cap)
   ▼
Decision(verdict = NEEDS_APPROVAL)  ──────────────►  audit APPROVAL_REQUESTED
   │        (no Approval row yet = "pending")        (written in the same
   │                                                  transaction as the Decision)
   ▼
GET /approvals/pending  ── shows this decision with agent/product/rule context
   │
   ▼
POST /approvals/{decision_id}/approve   or   /reject   { "approver": "...", "reason": "..." }
   │
   ├─ load Decision  FOR UPDATE        (404 if missing)
   ├─ verdict must be NEEDS_APPROVAL   (409 DECISION_NOT_PENDING_APPROVAL)
   ├─ no Approval row may exist yet    (409 APPROVAL_ALREADY_RESOLVED)
   ├─ INSERT approval(outcome, approver, reason)   [uq_approval_decision backstop]
   ├─ audit APPROVAL_RESOLVED
   └─ COMMIT once
   │
   ▼
Decision leaves the pending queue. Decision row is unchanged.
```

`ActionRequestStatus` stays `DECIDED` throughout — approval does not advance it.

## API

### `GET /approvals/pending`

Returns `NEEDS_APPROVAL` decisions with no `Approval` row, oldest first. Each
item (never exposing raw internal columns):

| field | source |
|---|---|
| `decision_id`, `action_request_id`, `policy_version` | `decision` |
| `original_rule_id`, `original_reason` | `decision` (the reason string already contains the transaction amount) |
| `decision_created_at` | `decision.created_at` |
| `agent_id`, `agent_name` | `agent` |
| `product_id`, `product_name`, `product_price` | `product` |
| `action_type`, `quantity`, `requested_discount_pct`, `proposed_price` | `action_request` |

No pagination — the pending queue for one merchant is small by construction.

### `POST /approvals/{decision_id}/approve`
### `POST /approvals/{decision_id}/reject`

Body (`extra = "forbid"`):

```json
{ "approver": "ops@merchant", "reason": "budget approved" }
```

- `approver` — required, 1–120 chars, not blank (whitespace-only is rejected). No
  authentication system exists yet, so the resolver's identity is an explicit
  validated field, not a hardcoded user.
- `reason` — optional, ≤ 2000 chars, whitespace-trimmed (blank → `null`).

**200** — resolved:

```json
{
  "approval_id": "dbf5cd75-...",
  "decision_id": "4d4e2ff8-...",
  "action_request_id": "89378e7b-...",
  "outcome": "APPROVED",
  "approver": "ops@merchant",
  "reason": "budget approved",
  "resolved_at": "2026-09-03T06:35:55.306038Z"
}
```

`resolved_at` is the `Approval` row's `created_at` (the row is created at
resolution).

### Status codes

| status | when |
|---|---|
| `200` | decision resolved |
| `404` | `{"detail": {"code": "DECISION_NOT_FOUND"}}` — no such decision; nothing persisted |
| `409` | `{"detail": {"code": "DECISION_NOT_PENDING_APPROVAL"}}` — verdict is `ALLOW` / `DENY` / `COUNTER_OFFER` |
| `409` | `{"detail": {"code": "APPROVAL_ALREADY_RESOLVED"}}` — an `Approval` row already exists |
| `422` | body fails validation (missing/blank `approver`, unknown field, over-long `reason`) |
| `500` | unexpected failure — transaction rolled back, nothing persisted |

`409` (Conflict) is used rather than `403`/`422` for "wrong verdict" and
"already resolved": the request is well-formed, it conflicts with the current
state of the resource.

## State invariants and how each is enforced

| Invariant | Enforcement |
|---|---|
| **1.** Only `NEEDS_APPROVAL` decisions can be resolved | `resolve_approval` checks `decision.verdict is Verdict.NEEDS_APPROVAL` → `409` otherwise. Tested for all of `ALLOW`/`DENY`/`COUNTER_OFFER` × approve/reject. |
| **2.** A decision is resolved at most once | (a) `SELECT ... FOR UPDATE` on the decision row serialises concurrent resolvers; (b) an explicit "does an `Approval` row exist?" check → `409`; (c) `uq_approval_decision` unique constraint — a race that beats (a)+(b) hits `IntegrityError`, caught and returned as `409`. Tested: approve→approve, approve→reject, reject→approve, reject→reject, a direct second-row insert (→ `IntegrityError`), and a two-connection concurrency test. |
| **3.** Approval never mutates the decision / action request | `resolve_approval` only `INSERT`s an `Approval` and appends an audit event. It never writes `Decision` or `ActionRequest`. Tested by re-reading both after resolution. |
| **4.** Approval creates no `PaymentAttempt` | No code path here touches `payment_attempt`. Tested: `select(PaymentAttempt)` is empty after approve and after reject. |
| **5.** Resolution + audit event are atomic | One session (`get_db`), one `commit()` at the end of `resolve_approval`; `append_audit_event` never commits. If the audit write fails, nothing persists. Tested by patching `append_audit_event` to raise → `500`, then asserting from a separate session that no `Approval` row and no `APPROVAL_RESOLVED` event exist. |

## Concurrency

Two near-simultaneous resolve calls for the same decision:

1. Both enter `resolve_approval`; the first `SELECT ... FOR UPDATE` on the
   `decision` row takes a row lock.
2. The second blocks on that lock until the first transaction commits.
3. The first commits — an `Approval` row now exists.
4. The second acquires the lock, its "does an `Approval` row exist?" check now
   sees the row → `409 APPROVAL_ALREADY_RESOLVED`.
5. Even if steps 1–4 were somehow bypassed, `uq_approval_decision` rejects the
   second `INSERT` and the `IntegrityError` is translated to `409`.

Row lock + unique constraint. No Redis, no queue. The two-connection test
(`test_concurrent_resolutions_serialise_to_one`) demonstrates the block and the
single surviving resolution.

## Audit events

| event | when | payload |
|---|---|---|
| `APPROVAL_REQUESTED` | in `POST /actions`, same transaction as a `NEEDS_APPROVAL` `Decision` | `decision_id`, `action_request_id`, `agent_id`, `product_id`, `original_rule_id`, `original_reason`, `policy_version` |
| `APPROVAL_RESOLVED` | in the resolve endpoint, same transaction as the `Approval` row | `approval_id`, `decision_id`, `action_request_id`, `outcome`, `approver`, `reason`, `original_verdict`, `original_rule_id`, `policy_version` |

Both use `ref_type = "action_request"`, `ref_id = <action_request id>`, so a
single `WHERE ref_id = ...` query returns the whole story of one request:
`ACTION_REQUEST_RECEIVED → POLICY_EVALUATED → APPROVAL_REQUESTED →
APPROVAL_RESOLVED`. Event names come from `app.audit.events`. Every approval
workflow therefore has an auditable origin (`APPROVAL_REQUESTED`) and an
auditable outcome (`APPROVAL_RESOLVED`), chained into the tamper-evident log.

## Why payment execution is deferred

An approved `NEEDS_APPROVAL` decision means *a human has authorised this
transaction to proceed*. It does **not** mean money moved. Creating a Razorpay
order / payment link, attaching a `PaymentAttempt` (which the schema only allows
against an `ALLOW` decision), webhook handling, and idempotency are a distinct
concern with its own failure modes — that is the next phase. Keeping the human
gate and the payment rail separate keeps each one's authority boundary clean and
independently testable.
