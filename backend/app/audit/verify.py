"""
Chain verification. Independent of the append path and side-effect free:
it only reads.

    verify_audit_chain(session) -> AuditVerificationResult

Walks every `audit_event` row in `seq` order and, for each, (1) recomputes the
hash from the stored fields and compares it to the stored `hash`, then
(2) checks `prev_hash` links to the previous row's `hash` (or the genesis
sentinel for the first row). The first failure is reported with a code and the
offending event's id/seq.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.hashing import GENESIS_PREV_HASH, compute_event_hash
from app.audit.models import AuditEvent

# Failure codes.
HASH_MISMATCH = "HASH_MISMATCH"              # recomputed hash != stored hash (payload/field tamper)
BROKEN_GENESIS = "BROKEN_GENESIS"            # first row's prev_hash != genesis sentinel
PREV_HASH_MISMATCH = "PREV_HASH_MISMATCH"    # prev_hash != previous row's hash (link/reorder/mid-delete)


@dataclass(frozen=True)
class AuditVerificationResult:
    valid: bool
    checked_events: int
    failure: str | None = None
    failure_detail: str | None = None
    event_id: str | None = None
    event_seq: int | None = None


async def verify_audit_chain(session: AsyncSession) -> AuditVerificationResult:
    rows = (
        await session.scalars(select(AuditEvent).order_by(AuditEvent.seq.asc()))
    ).all()

    expected_prev = GENESIS_PREV_HASH
    for index, event in enumerate(rows):
        recomputed = compute_event_hash(
            event_id=event.id,
            ref_type=event.ref_type,
            ref_id=event.ref_id,
            event_type=event.event_type,
            created_at=event.created_at,
            payload=event.payload,
            prev_hash=event.prev_hash,
        )
        if recomputed != event.hash:
            return AuditVerificationResult(
                valid=False,
                checked_events=index,
                failure=HASH_MISMATCH,
                failure_detail=(
                    f"event {event.id} (seq {event.seq}): stored hash does not "
                    f"match the hash recomputed from its stored fields"
                ),
                event_id=str(event.id),
                event_seq=event.seq,
            )
        if event.prev_hash != expected_prev:
            is_first = index == 0
            return AuditVerificationResult(
                valid=False,
                checked_events=index,
                failure=BROKEN_GENESIS if is_first else PREV_HASH_MISMATCH,
                failure_detail=(
                    f"event {event.id} (seq {event.seq}): prev_hash "
                    f"{'is not the genesis sentinel' if is_first else 'does not link to the preceding event'}"
                ),
                event_id=str(event.id),
                event_seq=event.seq,
            )
        expected_prev = event.hash

    return AuditVerificationResult(valid=True, checked_events=len(rows))
