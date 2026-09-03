"""
AuditEvent — append-only, hash-chained record of every decision-relevant event.

Chain rule (implemented in `app.audit.hashing` / `app.audit.service`):

    hash = SHA256(canonical_json({
        id, ref_type, ref_id, event_type, created_at, payload, prev_hash
    }))

`prev_hash` is the previous event's `hash`; the first event uses the genesis
sentinel (64 zeros). `verify_audit_chain()` walks the rows in `seq` order,
recomputes each hash, and reports the first row where the chain breaks.

`seq` (a monotonic identity column) — not `created_at` — is the canonical
walk order: timestamps can tie, the sequence cannot. `created_at` is assigned
by the append service (not the database) so it is known before the hash is
computed and is part of the hash contract.

This table has no update or delete path: a PostgreSQL trigger added in the
`audit append-only protection` migration rejects UPDATE / DELETE / TRUNCATE,
and the audit service exposes no mutating method. It deliberately does NOT use
`TimestampMixin` — an `updated_at` column is meaningless on an append-only log.

This is a hash-chained, tamper-evident, append-only audit log. It is not a
blockchain and is never described as one.
"""
from __future__ import annotations

import datetime as _dt
import uuid as _uuid

from sqlalchemy import BigInteger, DateTime, Identity, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, uuid_pk


class AuditEvent(Base):
    __tablename__ = "audit_event"

    id: Mapped[_uuid.UUID] = uuid_pk()

    # Canonical chain walk order. DB-assigned, strictly monotone. Gaps are
    # possible (a rolled-back INSERT consumes a value) and are not tampering.
    seq: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), unique=True, nullable=False
    )

    # What this event is about, e.g. ref_type="action_request", ref_id=<uuid>.
    ref_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    ref_id: Mapped[_uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)

    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    # App-assigned (see module docstring). server_default is a defensive
    # fallback only; the service always passes an explicit value.
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
