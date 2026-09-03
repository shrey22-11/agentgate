"""
The only writer for `audit_event`.

    await append_audit_event(session, ref_type=..., ref_id=..., event_type=...,
                             payload=...) -> AuditEvent

The call runs inside the caller's transaction and does NOT commit — so an audit
event is committed atomically with whatever business change it records (e.g. the
POLICY_EVALUATED event lands in the same transaction as the `decision` row).

Concurrency: every append first takes `pg_advisory_xact_lock` on a fixed key,
which serialises the read-head -> insert critical section across connections and
transactions. The lock is transaction-scoped, so it is released automatically on
the caller's commit or rollback. There is no `SELECT ... FOR UPDATE` fallback
because the genesis case (empty table) has no row to lock.

Errors are never swallowed: a bad payload raises `TypeError`, a hashing problem
raises, and a database constraint violation propagates from `flush()`.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.hashing import GENESIS_PREV_HASH, compute_event_hash, to_json_safe
from app.audit.models import AuditEvent

# Fixed advisory-lock key for the audit chain. Derived deterministically from a
# label so it is stable and collision-unlikely; the literal value is recorded in
# docs/audit.md.
_CHAIN_LOCK_KEY: int = int.from_bytes(
    hashlib.blake2b(b"agentgate.audit.chain", digest_size=8).digest(),
    byteorder="big",
    signed=True,
)

_EVENT_TYPE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,59}$")
_REF_TYPE_MAX = 40


def _validate_ref_type(ref_type: str) -> None:
    if not isinstance(ref_type, str) or not ref_type:
        raise TypeError("ref_type must be a non-empty str")
    if len(ref_type) > _REF_TYPE_MAX:
        raise ValueError(f"ref_type must be <= {_REF_TYPE_MAX} chars")


def _validate_event_type(event_type: str) -> None:
    if not isinstance(event_type, str) or not _EVENT_TYPE_RE.match(event_type):
        raise ValueError(
            f"event_type {event_type!r} must match {_EVENT_TYPE_RE.pattern}"
        )


async def append_audit_event(
    session: AsyncSession,
    *,
    ref_type: str,
    ref_id: uuid.UUID,
    event_type: str,
    payload: Mapping[str, Any],
) -> AuditEvent:
    _validate_ref_type(ref_type)
    _validate_event_type(event_type)
    if not isinstance(ref_id, uuid.UUID):
        raise TypeError("ref_id must be a uuid.UUID")
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")

    # Builds a fresh structure — the caller's payload dict is never mutated.
    json_safe_payload = to_json_safe(payload)

    # Serialise appends: only one transaction at a time may read the head and
    # insert its successor.
    await session.execute(select(func.pg_advisory_xact_lock(_CHAIN_LOCK_KEY)))

    head = (
        await session.execute(
            select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(1)
        )
    ).scalar_one_or_none()
    prev_hash = head.hash if head is not None else GENESIS_PREV_HASH

    event_id = uuid.uuid4()
    created_at = datetime.now(timezone.utc)
    event_hash = compute_event_hash(
        event_id=event_id,
        ref_type=ref_type,
        ref_id=ref_id,
        event_type=event_type,
        created_at=created_at,
        payload=json_safe_payload,
        prev_hash=prev_hash,
    )

    event = AuditEvent(
        id=event_id,
        ref_type=ref_type,
        ref_id=ref_id,
        event_type=event_type,
        payload=json_safe_payload,
        prev_hash=prev_hash,
        hash=event_hash,
        created_at=created_at,
    )
    session.add(event)
    await session.flush()  # surface DB errors now; the caller commits
    return event
