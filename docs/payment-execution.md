# Payment execution (Phase 8)

**REAL RAZORPAY** (test mode) behind a clean boundary. Deterministic policy
still decides *whether* money may move; this layer is the only thing that makes
a Razorpay payment object exist.

## Execution eligibility

`app.razorpay.eligibility.can_execute(session, decision)` is the single
authoritative answer. Nothing else re-implements it.

| Decision state | Eligible? | reason code |
|---|---|---|
| `verdict = ALLOW` | ✅ | `ALLOW_VERDICT` |
| `verdict = NEEDS_APPROVAL` + an `Approval` with `outcome = APPROVED` | ✅ | `NEEDS_APPROVAL_APPROVED` |
| `verdict = NEEDS_APPROVAL`, no `Approval` row | ❌ | `NEEDS_APPROVAL_NOT_APPROVED` |
| `verdict = NEEDS_APPROVAL` + `Approval` with `outcome = REJECTED` | ❌ | `NEEDS_APPROVAL_REJECTED` |
| `verdict = DENY` or `COUNTER_OFFER` | ❌ | `VERDICT_NOT_EXECUTABLE` |

### Why `ALLOW` and approved `NEEDS_APPROVAL` both execute

`ALLOW` means the request was inside every policy and authority boundary — no
human needed. `NEEDS_APPROVAL` means the request was commercially fine but above
the *agent's* delegated authority; a human approving it supplies exactly the
authority that was missing. In both cases there is deterministic evidence that
*this specific transaction, as evaluated,* is authorised — and the amount
charged is the one the policy engine computed at decision time, not anything a
caller supplies.

## The schema invariant (execution authorisation)

**Before Phase 8**, `payment_attempt` had `CHECK (decision_verdict = 'ALLOW')` —
an approved `NEEDS_APPROVAL` decision could never get a payment row.

**Migration `afa4f259ba3b`** replaces that with a rule the database enforces in
full, no trigger:

- `approval` gains `UNIQUE (id, outcome)` — a valid composite-FK target.
- `payment_attempt` gains nullable `approval_id`, `approval_outcome`.
- `ck_payment_attempt_verdict_is_allow` → **`ck_payment_attempt_executable`**:

  ```
  (decision_verdict = 'ALLOW'
       AND approval_id IS NULL AND approval_outcome IS NULL)
  OR
  (decision_verdict = 'NEEDS_APPROVAL'
       AND approval_id IS NOT NULL AND approval_outcome = 'APPROVED')
  ```

- **`fk_payment_attempt_approved_approval`**: `(approval_id, approval_outcome)`
  → `approval(id, outcome)`. So `approval_outcome = 'APPROVED'` cannot be
  written unless an `approval` row with that id *actually* has `outcome =
  'APPROVED'`.
- `fk_payment_attempt_allow_decision` (`(decision_id, decision_verdict)` →
  `decision(id, verdict)`) is unchanged — the decision must exist with the
  claimed verdict.

### What remains impossible (DB level, verified by tests)

| Attempted | Blocked by |
|---|---|
| payment for a `DENY` / `COUNTER_OFFER` decision | `ck_payment_attempt_executable` (neither branch matches) |
| payment for `NEEDS_APPROVAL` with `approval_id IS NULL` | `ck_payment_attempt_executable` (branch 2 needs it NOT NULL) |
| payment for `NEEDS_APPROVAL` citing a `REJECTED` approval's id as `'APPROVED'` | `fk_payment_attempt_approved_approval` (no `(id, 'APPROVED')` row) |
| payment for `ALLOW` with an `approval_id` attached | `ck_payment_attempt_executable` (branch 1 needs it NULL) |
| a second `payment_attempt` for one decision | `uq_payment_attempt_decision` |
| a second row with the same idempotency key | `uq_payment_attempt_idempotency_key` |

Application code enforces the same via `can_execute`; the constraints are the
backstop for any future bug that tries to bypass the service.

## Chosen Razorpay primitive: Payment Links

`client.payment_link.create(...)` returns a `plink_...` id and a `short_url`
that is a **complete real test-mode payment page** hosted by Razorpay — a payer
opens it, pays with a test card, and Razorpay fires `payment_link.paid` /
`payment.captured` webhooks. This gives the strongest end-to-end demonstration
with no frontend work (Orders + Standard Checkout needs the Razorpay Checkout JS
widget, which belongs to a later UI phase). `razorpay_order_id` is kept on the
model for a possible future Orders path but is unused in Phase 8.

## Amount derivation

Never from the request. `POST /payments/{decision_id}/execute` has no body. The
charge amount is `decision.executable_amount`, written by the Action API at
decision time via `app.policy.effective_transaction_amount(policy_input)` — the
same pure helpers the engine uses in `_build_ctx`, so it can never drift from
what policy evaluated. It is `NULL` for `DENY` / `COUNTER_OFFER`. The SDK is
called with `amount = int(executable_amount * 100)` paise.

## Execution lifecycle

```
ALLOW                                   NEEDS_APPROVAL
  │                                       │
  │                                     APPROVED   (human)
  │                                       │
  └──────────────┬────────────────────────┘
                 ▼
        POST /payments/{decision_id}/execute
                 ▼
   Txn 1  (row lock on the decision)
     can_execute() -> eligible?
     no prior payment_attempt?
     INSERT payment_attempt (status = CREATED)
     audit PAYMENT_EXECUTION_STARTED
     COMMIT                                  ← lock released
                 ▼
   Razorpay: payment_link.create   (no DB transaction held)
        success ──────────────┐        failure ──────────────┐
                              ▼                               ▼
   Txn 2                                     Txn 2b
     store plink id + short_url                status = FAILED
     status = PENDING                          audit PAYMENT_EXECUTION_FAILED
     audit PAYMENT_EXECUTION_CREATED           COMMIT  →  HTTP 502
     COMMIT  →  HTTP 200 (PENDING)
                 ▼
   webhook  payment_link.paid / payment.captured
     status = PAID
     audit PAYMENT_STATUS_UPDATED + PAYMENT_EXECUTION_SUCCEEDED
                 │
   webhook  payment_link.expired  → status = EXPIRED
   webhook  payment_link.cancelled / payment.failed → status = FAILED
```

### Payment status transitions

`CREATED → PENDING → PAID` · `CREATED → FAILED` · `PENDING → FAILED / EXPIRED`.
Terminal: `PAID`, `FAILED`, `EXPIRED`. A webhook or reconcile that would move a
terminal attempt is recorded but applies no transition.

## Idempotency and concurrency

- **One `payment_attempt` per decision** (`uq_payment_attempt_decision`) and a
  deterministic `idempotency_key = "decision:<decision_id>"`
  (`uq_payment_attempt_idempotency_key`). There is no way to get two rows, so no
  way to create two Razorpay objects through the service.
- **Sequential duplicate execute**: the second call finds the existing attempt.
  `PENDING` / `PAID` → returns it unchanged (`already_existed: true`, HTTP 200).
  `CREATED` → `409 EXECUTION_IN_PROGRESS` (a prior call crashed mid-flight — run
  reconcile). `FAILED` / `EXPIRED` → `409 EXECUTION_TERMINAL_FAILED` (a failed
  attempt is terminal for that decision).
- **Concurrent execute**: `SELECT ... FOR UPDATE` on the `decision` row
  serialises the two Txn-1s. The first inserts the attempt and commits; the
  second acquires the lock, sees the `CREATED` attempt, and returns
  `409 EXECUTION_IN_PROGRESS`. Exactly one attempt, exactly one Razorpay object.
  (Test: `test_concurrent_execute_creates_one_object`.)
- **Idempotency is local.** Razorpay's Payment Links API (SDK 2.0.1) has no
  merchant-supplied idempotency-key header, so we do not claim exactly-once at
  the Razorpay boundary. We rely on the unique constraints above plus a unique
  `reference_id = str(payment_attempt.id)` on the payment link, which lets
  reconciliation find an object created just before a crash.

## Transaction / crash safety

The execution service commits **more than once per request** — deliberately, and
it is the only service that does. Holding a PostgreSQL transaction open across a
slow Razorpay HTTP call would be worse.

- **Crash after Txn 1, before the Razorpay call** — a `CREATED` attempt with no
  Razorpay id. `POST /payments/{decision_id}/reconcile` looks for a payment link
  with `reference_id = str(attempt.id)`; none exists → marks the attempt
  `FAILED` (safe: nothing was created).
- **Crash after the Razorpay call, before Txn 2** — a `CREATED` attempt, but a
  payment link *does* exist at Razorpay with our `reference_id`. Reconcile finds
  it, adopts its id/status, and audits `PAYMENT_EXECUTION_CREATED` (recovered)
  + `PAYMENT_STATUS_UPDATED`.
- **Txn 2b fails to commit** — the attempt stays `CREATED`; same recovery path.
- **Razorpay call fails outright** — Txn 2b marks `FAILED` and audits it; HTTP
  502. Not retryable for that decision (terminal); a fresh action request is
  required.

Known limitation: there is a tiny window where Razorpay created the object and
our process died before *any* DB record of the id — recovery depends on the
`reference_id` lookup, which requires Razorpay to have accepted the create. This
is honest at-least-once with idempotent reconciliation, not distributed
exactly-once.

## Webhook security

- **Endpoint**: `POST /webhooks/razorpay`.
- **Raw body**: the handler reads `await request.body()` (bytes) and never
  parses-then-reserialises before verifying.
- **Signature**: HMAC-SHA256 hex of the raw body with the **webhook secret**
  (`RAZORPAY_WEBHOOK_SECRET`, distinct from `RAZORPAY_KEY_SECRET`), constant-time
  compared to `X-Razorpay-Signature`. `SdkRazorpayClient` delegates to
  `client.utility.verify_webhook_signature(...)`; the test fake replicates the
  identical HMAC.
- **Invalid or missing signature** → `400 INVALID_SIGNATURE`, **nothing
  persisted**, a secret-free warning is logged. Rationale (same as "unknown
  resource → 404" elsewhere): an unauthenticated request carries no trusted data
  to record, and writing unauthenticated payloads to the append-only audit chain
  or the `webhook_event` table is a DoS vector. A production deployment should
  also rate-limit this route at the edge.
- **Non-JSON body (valid signature)** → `400 MALFORMED_WEBHOOK`, nothing
  persisted.

### Deduplication

Razorpay puts the delivery id in the **`X-Razorpay-Event-Id` header**, not the
JSON body. `webhook_event.event_id` = that header, or — if absent —
`"sha256:" + sha256(raw_body).hexdigest()` (deterministic, documented fallback).
`webhook_event.event_id` is `UNIQUE`: a replay hits the constraint, the
transaction is rolled back, and the endpoint returns `200 {"status":
"duplicate_ignored"}` with no re-processing, no second status transition, and no
extra audit event.

### Unknown / unmatched events

- Event type we don't act on (e.g. a future `payment_link.*`) → recorded,
  audited `WEBHOOK_RECEIVED`, no status change, `200 received_unknown_event`.
  Never a 500.
- Event about a payment object we don't track → recorded, audited
  `WEBHOOK_RECEIVED` (`matched: false`), `200 received_unmatched`.

### Atomicity

The `webhook_event` insert, the `payment_attempt` status change, and all audit
events for one delivery commit together. If any step raises, nothing persists
and Razorpay retries the delivery (which we then process cleanly).

## Audit events (new in Phase 8)

| event | when | key payload |
|---|---|---|
| `PAYMENT_EXECUTION_STARTED` | attempt row created, before the Razorpay call | payment_attempt_id, decision_id, verdict, authorised_via, amount |
| `PAYMENT_EXECUTION_CREATED` | Razorpay payment link confirmed created (or recovered by reconcile) | payment_attempt_id, razorpay_payment_link_id, short_url, amount |
| `PAYMENT_EXECUTION_FAILED` | Razorpay create failed, or reconcile found nothing | payment_attempt_id, error (our message — no secrets, no body) |
| `PAYMENT_STATUS_UPDATED` | a webhook or reconcile changed the local status | payment_attempt_id, new_status, source |
| `PAYMENT_EXECUTION_SUCCEEDED` | status became `PAID` | payment_attempt_id, decision_id |
| `WEBHOOK_RECEIVED` | every accepted (signature-valid, non-duplicate) delivery | razorpay_event_id, event_type, matched |
| `WEBHOOK_DUPLICATE_IGNORED` | *(constant reserved; a duplicate currently returns 200 with no audit event to keep the chain bounded)* | — |

All payment audit events use `ref_type = "action_request"`, `ref_id = <action
request id>` (an unmatched webhook uses `ref_type = "webhook_event"`), so one
`WHERE ref_id = ...` query returns the whole story.

### Expected chains

Successful payment link creation:
```
ACTION_REQUEST_RECEIVED → POLICY_EVALUATED
→ PAYMENT_EXECUTION_STARTED → PAYMENT_EXECUTION_CREATED
→ WEBHOOK_RECEIVED → PAYMENT_STATUS_UPDATED → PAYMENT_EXECUTION_SUCCEEDED
```

Approved high-value transaction, then paid:
```
ACTION_REQUEST_RECEIVED → POLICY_EVALUATED → APPROVAL_REQUESTED → APPROVAL_RESOLVED
→ PAYMENT_EXECUTION_STARTED → PAYMENT_EXECUTION_CREATED
→ WEBHOOK_RECEIVED → PAYMENT_STATUS_UPDATED → PAYMENT_EXECUTION_SUCCEEDED
```

Razorpay creation failure:
```
ACTION_REQUEST_RECEIVED → POLICY_EVALUATED
→ PAYMENT_EXECUTION_STARTED → PAYMENT_EXECUTION_FAILED
```

Duplicate webhook: the second delivery adds nothing (`200 duplicate_ignored`).

`verify_audit_chain()` stays valid across all of these paths (tested).

## Configuration

| var | meaning |
|---|---|
| `RAZORPAY_ENABLED` | `false` (default): the app boots, `/payments/*` and `/webhooks/razorpay` return `503 RAZORPAY_DISABLED`, no DB writes. `true`: real SDK client; startup fails if any of the three secrets is blank or still a `placeholder`. |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | test-mode API key pair |
| `RAZORPAY_WEBHOOK_SECRET` | the webhook signing secret — **different** from `RAZORPAY_KEY_SECRET` |

Secrets are never logged and never placed in audit payloads. `.env.example`
carries placeholders only; `.env` is gitignored.

## API summary

| method + path | success | errors |
|---|---|---|
| `POST /payments/{decision_id}/execute` | `200` `PaymentExecutionResponse` (`status`, `amount`, `razorpay_payment_link_id`, `short_url`, `already_existed`) | `404` decision not found · `409` `VERDICT_NOT_EXECUTABLE` / `NEEDS_APPROVAL_NOT_APPROVED` / `NEEDS_APPROVAL_REJECTED` / `EXECUTION_IN_PROGRESS` / `EXECUTION_TERMINAL_FAILED` · `502` `RAZORPAY_CREATE_FAILED` · `503` `RAZORPAY_DISABLED` |
| `POST /payments/{decision_id}/reconcile` | `200` `PaymentExecutionResponse` | `404` · `409` `NO_PAYMENT_ATTEMPT` · `503` |
| `POST /webhooks/razorpay` | `200` `{status, event_type, payment_status}` | `400` `INVALID_SIGNATURE` / `MALFORMED_WEBHOOK` · `503` |

Reconciliation is a **manually callable endpoint**, not a scheduler — there is
no background polling in Phase 8.

## Known limitations

- No merchant-supplied Razorpay idempotency key (SDK/API limitation) — see
  *Idempotency*. Local uniqueness + `reference_id` reconciliation is the
  mitigation; it is at-least-once with idempotent recovery, not exactly-once.
- A failed `payment_attempt` is terminal for its decision; retrying needs a new
  action request.
- `payment_link.partially_paid` is treated as still `PENDING` (all-or-nothing
  model).
- Tail-truncation of the audit chain is still undetectable by chain
  verification alone (unchanged from Phase 5).
