"""
Audit event-type identifiers.

Plain string constants, not a closed enum. Reasons:

* Every future phase adds a few event types; a DB enum would mean an
  `ALTER TYPE` (or a data migration) each time, and the `event_type` column is
  already a plain `VARCHAR(60)`.
* The audit module must not import from every feature module, and an enum
  centralising all names invites exactly that coupling.

The append service still validates shape (`^[A-Z][A-Z0-9_]{2,59}$`), so typos
and empty strings are rejected — it is "validated strings", not "anything goes".

`ALL` is provided for tests and docs; it is not an allow-list the service
enforces.
"""
from __future__ import annotations

# Phase 5 defines the names; later phases wire the call sites.
ACTION_REQUEST_RECEIVED = "ACTION_REQUEST_RECEIVED"
ACTION_PARSED = "ACTION_PARSED"
POLICY_EVALUATED = "POLICY_EVALUATED"
COUNTER_OFFER_CREATED = "COUNTER_OFFER_CREATED"
APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
APPROVAL_RESOLVED = "APPROVAL_RESOLVED"
PAYMENT_EXECUTION_STARTED = "PAYMENT_EXECUTION_STARTED"       # attempt row created, Razorpay not yet called
PAYMENT_EXECUTION_CREATED = "PAYMENT_EXECUTION_CREATED"       # Razorpay object confirmed created
PAYMENT_EXECUTION_SUCCEEDED = "PAYMENT_EXECUTION_SUCCEEDED"   # payment captured
PAYMENT_EXECUTION_FAILED = "PAYMENT_EXECUTION_FAILED"         # Razorpay creation or the payment failed
PAYMENT_STATUS_UPDATED = "PAYMENT_STATUS_UPDATED"             # a webhook / reconcile changed the local status
WEBHOOK_RECEIVED = "WEBHOOK_RECEIVED"
WEBHOOK_DUPLICATE_IGNORED = "WEBHOOK_DUPLICATE_IGNORED"
AI_PROVIDER_FAILED = "AI_PROVIDER_FAILED"

ALL: frozenset[str] = frozenset(
    {
        ACTION_REQUEST_RECEIVED,
        ACTION_PARSED,
        POLICY_EVALUATED,
        COUNTER_OFFER_CREATED,
        APPROVAL_REQUESTED,
        APPROVAL_RESOLVED,
        PAYMENT_EXECUTION_STARTED,
        PAYMENT_EXECUTION_CREATED,
        PAYMENT_EXECUTION_SUCCEEDED,
        PAYMENT_EXECUTION_FAILED,
        PAYMENT_STATUS_UPDATED,
        WEBHOOK_RECEIVED,
        WEBHOOK_DUPLICATE_IGNORED,
        AI_PROVIDER_FAILED,
    }
)
