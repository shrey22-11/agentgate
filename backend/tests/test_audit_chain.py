"""
Phase 5 — audit chain against PostgreSQL: append, verify, tamper detection,
append-only enforcement, and concurrent-append safety.

Corruption tests deliberately do NOT weaken the production triggers. They
either (a) attempt a normal UPDATE/DELETE/TRUNCATE and assert PostgreSQL
rejects it, or (b) simulate a privileged attacker by `ALTER TABLE ... DISABLE
TRIGGER USER` inside the test's own rolled-back transaction — exactly the
threat-model boundary documented in docs/audit.md.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.audit import (
    GENESIS_PREV_HASH,
    AuditVerificationResult,
    append_audit_event,
    events,
    verify_audit_chain,
)
from app.audit.hashing import compute_event_hash
from app.core.config import get_settings
from decimal import Decimal

_UTC = dt.timezone.utc


async def _append(session, event_type: str = events.POLICY_EVALUATED, **payload):
    return await append_audit_event(
        session,
        ref_type="action_request",
        ref_id=uuid4(),
        event_type=event_type,
        payload=payload or {"k": "v"},
    )


async def _corrupt(session, sql: str, **params) -> None:
    """Mutate audit_event with USER triggers briefly disabled (rolled back at teardown)."""
    await session.execute(text("ALTER TABLE audit_event DISABLE TRIGGER USER"))
    await session.execute(text(sql), params)
    await session.execute(text("ALTER TABLE audit_event ENABLE TRIGGER USER"))


# --- normal chain --------------------------------------------------------
async def test_first_event_uses_genesis_prev_hash(db_session) -> None:
    e1 = await _append(db_session)
    assert e1.prev_hash == GENESIS_PREV_HASH
    assert len(e1.hash) == 64


async def test_events_link_and_seq_is_monotonic(db_session) -> None:
    e1 = await _append(db_session)
    e2 = await _append(db_session)
    e3 = await _append(db_session)
    assert (e2.prev_hash, e3.prev_hash) == (e1.hash, e2.hash)
    assert e1.seq < e2.seq < e3.seq


async def test_verify_passes_for_valid_chain(db_session) -> None:
    for _ in range(4):
        await _append(db_session)
    db_session.expire_all()  # force a real reload from JSONB / timestamptz
    result = await verify_audit_chain(db_session)
    assert result == AuditVerificationResult(valid=True, checked_events=4)


async def test_verify_passes_for_empty_chain(db_session) -> None:
    result = await verify_audit_chain(db_session)
    assert result.valid and result.checked_events == 0


async def test_decimal_payload_survives_jsonb_round_trip(db_session) -> None:
    await _append(db_session, events.COUNTER_OFFER_CREATED, price=Decimal("9000.00"))
    await _append(db_session, events.POLICY_EVALUATED, amount=Decimal("45000"))
    db_session.expire_all()
    assert (await verify_audit_chain(db_session)).valid


# --- tamper detection -------------------------------------------------
async def test_payload_tamper_is_detected(db_session) -> None:
    await _append(db_session)
    e2 = await _append(db_session, events.COUNTER_OFFER_CREATED, price=Decimal("9000.00"))
    await _append(db_session)
    await _corrupt(
        db_session,
        "UPDATE audit_event SET payload = CAST(:p AS jsonb) WHERE id = :id",
        p=json.dumps({"price": {"__decimal__": "1000"}}),
        id=str(e2.id),
    )
    db_session.expire_all()
    result = await verify_audit_chain(db_session)
    assert not result.valid
    assert result.failure == "HASH_MISMATCH"
    assert result.event_id == str(e2.id)


async def test_hash_tamper_is_detected(db_session) -> None:
    await _append(db_session)
    e2 = await _append(db_session)
    await _corrupt(
        db_session,
        "UPDATE audit_event SET hash = :h WHERE id = :id",
        h="d" * 64,
        id=str(e2.id),
    )
    db_session.expire_all()
    result = await verify_audit_chain(db_session)
    assert result.failure == "HASH_MISMATCH"
    assert result.event_id == str(e2.id)


async def test_prev_hash_only_tamper_is_detected(db_session) -> None:
    # prev_hash is inside the hash body, so changing it alone breaks the recompute.
    await _append(db_session)
    e2 = await _append(db_session)
    await _corrupt(
        db_session,
        "UPDATE audit_event SET prev_hash = :p WHERE id = :id",
        p="e" * 64,
        id=str(e2.id),
    )
    db_session.expire_all()
    assert (await verify_audit_chain(db_session)).failure == "HASH_MISMATCH"


async def test_sophisticated_prev_hash_tamper_is_detected(db_session) -> None:
    # Attacker also recomputes the event's own hash consistently; the link to
    # the real predecessor still breaks.
    await _append(db_session)
    e2 = await _append(db_session)
    e3 = await _append(db_session)
    forged_prev = "f" * 64
    forged_hash = compute_event_hash(
        event_id=e3.id,
        ref_type=e3.ref_type,
        ref_id=e3.ref_id,
        event_type=e3.event_type,
        created_at=e3.created_at,
        payload=e3.payload,
        prev_hash=forged_prev,
    )
    await _corrupt(
        db_session,
        "UPDATE audit_event SET prev_hash = :p, hash = :h WHERE id = :id",
        p=forged_prev,
        h=forged_hash,
        id=str(e3.id),
    )
    db_session.expire_all()
    result = await verify_audit_chain(db_session)
    assert result.failure == "PREV_HASH_MISMATCH"
    assert result.event_id == str(e3.id)


async def test_broken_genesis_is_detected(db_session) -> None:
    e1 = await _append(db_session)
    await _append(db_session)
    forged_hash = compute_event_hash(
        event_id=e1.id,
        ref_type=e1.ref_type,
        ref_id=e1.ref_id,
        event_type=e1.event_type,
        created_at=e1.created_at,
        payload=e1.payload,
        prev_hash="1" * 64,
    )
    await _corrupt(
        db_session,
        "UPDATE audit_event SET prev_hash = :p, hash = :h WHERE id = :id",
        p="1" * 64,
        h=forged_hash,
        id=str(e1.id),
    )
    db_session.expire_all()
    result = await verify_audit_chain(db_session)
    assert result.failure == "BROKEN_GENESIS"
    assert result.event_id == str(e1.id)


async def test_missing_middle_event_is_detected(db_session) -> None:
    await _append(db_session)
    e2 = await _append(db_session)
    e3 = await _append(db_session)
    await _corrupt(db_session, "DELETE FROM audit_event WHERE id = :id", id=str(e2.id))
    db_session.expire_all()
    result = await verify_audit_chain(db_session)
    assert not result.valid
    assert result.failure == "PREV_HASH_MISMATCH"
    assert result.event_id == str(e3.id)


async def test_reordered_events_are_detected(db_session) -> None:
    e1 = await _append(db_session)
    e2 = await _append(db_session)
    e3 = await _append(db_session)

    async def _row(event_id):
        r = await db_session.execute(
            text(
                "SELECT id, seq, ref_type, ref_id, event_type, payload, "
                "prev_hash, hash, created_at FROM audit_event WHERE id = :id"
            ),
            {"id": str(event_id)},
        )
        return dict(r.mappings().one())

    a, b = await _row(e2.id), await _row(e3.id)
    ins = text(
        "INSERT INTO audit_event (id, seq, ref_type, ref_id, event_type, payload, "
        "prev_hash, hash, created_at) OVERRIDING SYSTEM VALUE VALUES "
        "(:id, :seq, :ref_type, :ref_id, :event_type, CAST(:payload AS jsonb), "
        ":prev_hash, :hash, :created_at)"
    )
    await db_session.execute(text("ALTER TABLE audit_event DISABLE TRIGGER USER"))
    await db_session.execute(
        text("DELETE FROM audit_event WHERE id IN (:a, :b)"),
        {"a": str(e2.id), "b": str(e3.id)},
    )
    await db_session.execute(ins, {**a, "seq": b["seq"], "payload": json.dumps(a["payload"])})
    await db_session.execute(ins, {**b, "seq": a["seq"], "payload": json.dumps(b["payload"])})
    await db_session.execute(text("ALTER TABLE audit_event ENABLE TRIGGER USER"))

    db_session.expire_all()
    result = await verify_audit_chain(db_session)
    assert not result.valid
    assert result.failure == "PREV_HASH_MISMATCH"
    # e3 now sits in e2's slot; it links to e2, not to e1.
    assert result.event_id == str(e3.id)


async def test_forked_chain_fails_verification(db_session) -> None:
    """What a missing advisory lock would produce: two events off one parent."""
    e1 = await _append(db_session)
    await _append(db_session)  # e2, legitimately links e1
    forged_id = uuid4()
    created = dt.datetime.now(_UTC)
    forged_hash = compute_event_hash(
        event_id=forged_id,
        ref_type="action_request",
        ref_id=forged_id,
        event_type="X_FORK",
        created_at=created,
        payload={},
        prev_hash=e1.hash,  # also claims e1 as parent
    )
    await db_session.execute(
        text(
            "INSERT INTO audit_event (id, ref_type, ref_id, event_type, payload, "
            "prev_hash, hash, created_at) VALUES (:id, 'action_request', :rid, "
            "'X_FORK', '{}'::jsonb, :ph, :h, :ca)"
        ),
        {"id": str(forged_id), "rid": str(forged_id), "ph": e1.hash, "h": forged_hash, "ca": created},
    )
    db_session.expire_all()
    result = await verify_audit_chain(db_session)
    assert result.failure == "PREV_HASH_MISMATCH"
    assert result.event_id == str(forged_id)


# --- database-level append-only enforcement -------------------------
async def test_update_is_rejected_by_postgres(db_session) -> None:
    e1 = await _append(db_session)
    with pytest.raises(DBAPIError) as exc:
        await db_session.execute(
            text("UPDATE audit_event SET event_type = 'X' WHERE id = :id"),
            {"id": str(e1.id)},
        )
    assert "append-only" in str(exc.value)
    await db_session.rollback()


async def test_delete_is_rejected_by_postgres(db_session) -> None:
    e1 = await _append(db_session)
    with pytest.raises(DBAPIError) as exc:
        await db_session.execute(
            text("DELETE FROM audit_event WHERE id = :id"), {"id": str(e1.id)}
        )
    assert "append-only" in str(exc.value)
    await db_session.rollback()


async def test_truncate_is_rejected_by_postgres(db_session) -> None:
    await _append(db_session)
    with pytest.raises(DBAPIError) as exc:
        await db_session.execute(text("TRUNCATE audit_event"))
    assert "append-only" in str(exc.value)
    await db_session.rollback()


async def test_seq_column_cannot_be_updated(db_session) -> None:
    """The walk-order column is GENERATED ALWAYS — PostgreSQL rejects updates to it."""
    e1 = await _append(db_session)
    await db_session.execute(text("ALTER TABLE audit_event DISABLE TRIGGER USER"))
    with pytest.raises(DBAPIError):
        await db_session.execute(
            text("UPDATE audit_event SET seq = seq + 100 WHERE id = :id"),
            {"id": str(e1.id)},
        )
    await db_session.rollback()


# --- input validation ------------------------------------------------
async def test_append_rejects_bad_event_type(db_session) -> None:
    with pytest.raises(ValueError):
        await append_audit_event(
            db_session, ref_type="x", ref_id=uuid4(), event_type="lower case", payload={}
        )


async def test_append_rejects_float_payload(db_session) -> None:
    with pytest.raises(TypeError):
        await append_audit_event(
            db_session,
            ref_type="x",
            ref_id=uuid4(),
            event_type="X_FLOAT",
            payload={"amount": 1.5},
        )


async def test_append_rejects_non_uuid_ref_id(db_session) -> None:
    with pytest.raises(TypeError):
        await append_audit_event(
            db_session, ref_type="x", ref_id="not-a-uuid", event_type="X_REF", payload={}
        )


async def test_append_does_not_mutate_callers_payload(db_session) -> None:
    nested = {"x": 1}
    payload = {"price": Decimal("9000.00"), "nested": nested}
    await append_audit_event(
        db_session, ref_type="x", ref_id=uuid4(), event_type="X_IMMUT", payload=payload
    )
    # caller's dict and its nested dict are untouched
    assert payload["price"] == Decimal("9000.00")
    assert payload["nested"] is nested and nested == {"x": 1}


# --- concurrency ---------------------------------------------------
async def _hard_reset_audit(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("ALTER TABLE audit_event DISABLE TRIGGER USER")
        await conn.exec_driver_sql("TRUNCATE audit_event RESTART IDENTITY")
        await conn.exec_driver_sql("ALTER TABLE audit_event ENABLE TRIGGER USER")
    await engine.dispose()


async def test_advisory_lock_serialises_concurrent_appends() -> None:
    """
    Two real connections. The second append must block on the chain lock until
    the first transaction ends, and the resulting chain must not fork.
    """
    url = get_settings().database_url
    engine_a = create_async_engine(url, poolclass=NullPool)
    engine_b = create_async_engine(url, poolclass=NullPool)
    make_a = async_sessionmaker(engine_a, expire_on_commit=False)
    make_b = async_sessionmaker(engine_b, expire_on_commit=False)
    try:
        async with make_a() as sa, make_b() as sb:
            e1 = await append_audit_event(
                sa, ref_type="x", ref_id=uuid4(), event_type="X_ONE", payload={}
            )
            # sa holds pg_advisory_xact_lock and has not committed.
            second = asyncio.create_task(
                append_audit_event(
                    sb, ref_type="x", ref_id=uuid4(), event_type="X_TWO", payload={}
                )
            )
            await asyncio.sleep(0.5)
            assert not second.done(), "second append should be blocked on the chain lock"

            await sa.commit()  # releases the lock, persists e1
            e2 = await asyncio.wait_for(second, timeout=5)
            await sb.commit()

            assert e2.prev_hash == e1.hash  # chained off e1, not forked

        async with make_a() as s:
            result = await verify_audit_chain(s)
            assert result.valid and result.checked_events == 2
    finally:
        await _hard_reset_audit(url)
        await engine_a.dispose()
        await engine_b.dispose()
