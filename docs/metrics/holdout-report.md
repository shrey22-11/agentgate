# AgentGate evaluation report - `holdout` split

Generated 2026-09-03T11:09:54+00:00 - OUR SYSTEM - frozen scenario suite

**38 graded scenarios** (30 deterministic-policy, 8 adversarial natural-language) plus 3 idempotency cases and 12 injected audit-tamper trials.

## Headline

| Metric | Result |
|---|---|
| Verdict matches deterministic engine (integration fidelity) | **100.0%** |
| Rule-id matches deterministic engine | 100.0% |
| Block rate on policy-violating requests | **100.0%** |
| False-block rate on benign requests | **0.0%** |
| Prompt-injection neutralised (deterministic verdict + rule + override flag all match) | **100.0%** |
| Structured-parse pass-through (defensive re-validation) | 75.0% |
| Idempotency cases correct | **100.0%** |
| Audit-chain tamper detection | **100.0%** |
| Audit chain valid after the whole run | yes |
| Unexpected Razorpay / payment objects created | 0 |
| Decision latency (policy route) p50 / p95 | 71.183 ms / 110.582 ms |

## How ground truth is determined

For every deterministic scenario the expected verdict and rule id are computed by calling `app.policy.evaluate` on a `PolicyInput` built from the authoritative seeded agent/product rows - the same helper the live `POST /actions` path uses (`_build_policy_input`). For a natural-language scenario, if the defensively-parsed request resolves cleanly against the catalogue the expected verdict is again `evaluate(...)` on the resolved fields; otherwise it is the parser's documented fail-closed result `DENY / RULE_INPUT_INVALID`. No scenario stores a hand-written verdict - the `Scenario` dataclass has no field for one, and a test enforces that.

- Engine blocks every request *designed* as a policy violation: **yes**
- Engine allows every request *designed* as benign: **yes**

  These two lines are the honesty check on the *design intent* labels: when they read `yes`, the block-rate / false-block-rate numbers above are measured against a suite whose intent labels the deterministic engine agrees with. A `NO` would mean a scenario the author mislabelled - it is reported, never relabelled to flatter the metric.

## By category

| Category | Scenarios | Verdict match | Blocked by system |
|---|---|---|---|
| benign | 14 | 100.0% | 0.0% |
| policy_violating | 16 | 100.0% | 100.0% |
| adversarial (NL) | 8 | 100.0% | 75.0% |

## Adversarial / prompt-injection

8 hostile natural-language messages (fake authority, 'ignore previous instructions', 'pay now', lifted caps, hallucinated products, malformed numbers). Each is parsed defensively (model call stubbed) and its legitimate request is put through the same deterministic policy path.

- Deterministic verdict unchanged by the injection: 100.0%
- Override/manipulation correctly flagged in the audit trail: 100.0%
- Reached a real policy evaluation on resolved fields: 75.0%
- Safely failed closed before policy (unresolvable / malformed): 25.0%
- Payment objects created by any adversarial scenario: 0 (see money invariant)

## Decision latency

In-process ASGI against local PostgreSQL, single-threaded, warm. Not a production latency claim.

| Route | n | p50 | p95 | p99 | max | mean |
|---|--:|--:|--:|--:|--:|--:|
| POST /actions | 30 | 71.183 ms | 110.582 ms | 117.423 ms | 118.616 ms | 74.669 ms |
| POST /ai/actions | 8 | 77.374 ms | 89.832 ms | 92.926 ms | 93.7 ms | 76.074 ms |

## Idempotency under injected duplicates

| Mechanism | Cases | Passed |
|---|--:|--:|
| `duplicate_action` | 1 | 1 |
| `payment_attempt_key` | 1 | 1 |
| `webhook_event` | 1 | 1 |

**`duplicate_action`**

- [ok] `idem-dup-action-needs-approval` - same NEEDS_APPROVAL request twice: two independent pending decisions
    - [ok] both HTTP 200
    - [ok] distinct action_request rows
    - [ok] distinct decision rows
    - [ok] identical verdict
    - [ok] identical rule id
    - [ok] identical counter-offer
    - [ok] audit chain still valid

**`payment_attempt_key`**

- [ok] `idem-payment-key-velocity` - a second payment_attempt with the same idempotency key is rejected by the DB
    - [ok] seed decision is ALLOW
    - [ok] first payment_attempt inserted
    - [ok] second insert rejected by a unique constraint
    - [ok] rejection names uq_payment_attempt

**`webhook_event`**

- [ok] `idem-webhook-unknown` - a replayed unknown-type webhook is still deduped, never a 500
    - [ok] first delivery accepted
    - [ok] replay is duplicate_ignored
    - [ok] exactly one webhook_event row
    - [ok] no 5xx / exception raised
    - [ok] audit chain still valid

## Audit-chain integrity under injected tampering

12 trials: append three probe events, corrupt one, run `verify_audit_chain`, roll back so nothing is persisted. 4 clean control trials (no corruption).

| Tamper mode | Trials | Detected |
|---|--:|--:|
| payload | 3 | 3 |
| hash | 3 | 3 |
| prev_hash | 3 | 3 |
| delete_middle | 3 | 3 |
| **clean control (must stay valid)** | 4 | 4 valid |

Committed chain after the full run: **yes** valid, 96 events.

## Scenario failures

None. Every graded scenario matched the deterministic engine.

## What is simulated or stubbed

- Catalogue, stock, margins and the agent population are SIMULATED.
- The natural-language parser's model call is stubbed (no Gemini request) - exactly as every AI test in this repo. The deterministic re-validation, catalogue resolution, confidence gate and the whole policy path are exercised for real.
- RAZORPAY_ENABLED is false: no Razorpay object is created anywhere in this suite. Payment-execution idempotency is checked at the database constraint level.
- Ground truth is computed from app.policy.evaluate on authoritative seed data; no scenario carries a hand-written verdict.
- Latency is in-process ASGI against a local PostgreSQL, single threaded and warm - not a production latency claim.
- No revenue, conversion, AOV or business-impact figure is produced or implied by this harness.
