"""
Raw DDL for the `audit_event` append-only protection.

Single source of truth so the Alembic migration and the test-suite schema
builder (`tests/conftest.py`, which builds from ORM metadata, not migrations)
apply exactly the same triggers. Each entry is one top-level statement —
SQLAlchemy's asyncpg dialect uses the extended query protocol and will not run
multiple statements in one string (the `;` inside the dollar-quoted function
body is fine, it is not a top-level separator).
"""
from __future__ import annotations

_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION agentgate_audit_event_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit_event is append-only; % is not permitted', TG_OP;
    RETURN NULL;
END;
$$
""".strip()

CREATE_APPEND_ONLY_SQL: tuple[str, ...] = (
    _IMMUTABLE_FN,
    "DROP TRIGGER IF EXISTS audit_event_no_update ON audit_event",
    "CREATE TRIGGER audit_event_no_update BEFORE UPDATE ON audit_event "
    "FOR EACH ROW EXECUTE FUNCTION agentgate_audit_event_immutable()",
    "DROP TRIGGER IF EXISTS audit_event_no_delete ON audit_event",
    "CREATE TRIGGER audit_event_no_delete BEFORE DELETE ON audit_event "
    "FOR EACH ROW EXECUTE FUNCTION agentgate_audit_event_immutable()",
    "DROP TRIGGER IF EXISTS audit_event_no_truncate ON audit_event",
    "CREATE TRIGGER audit_event_no_truncate BEFORE TRUNCATE ON audit_event "
    "FOR EACH STATEMENT EXECUTE FUNCTION agentgate_audit_event_immutable()",
)

DROP_APPEND_ONLY_SQL: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS audit_event_no_update ON audit_event",
    "DROP TRIGGER IF EXISTS audit_event_no_delete ON audit_event",
    "DROP TRIGGER IF EXISTS audit_event_no_truncate ON audit_event",
    "DROP FUNCTION IF EXISTS agentgate_audit_event_immutable()",
)
