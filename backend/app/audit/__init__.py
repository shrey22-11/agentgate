"""
Tamper-evident, append-only audit trail (OUR SYSTEM).

Public surface:
    append_audit_event(session, ...)   the only writer
    verify_audit_chain(session)        recompute + link-check the whole chain
    AuditVerificationResult            diagnostic result type
    GENESIS_PREV_HASH                  prev_hash of the first event
    events                             event-type string constants
"""
from app.audit import events
from app.audit.hashing import GENESIS_PREV_HASH
from app.audit.service import append_audit_event
from app.audit.verify import AuditVerificationResult, verify_audit_chain

__all__ = [
    "append_audit_event",
    "verify_audit_chain",
    "AuditVerificationResult",
    "GENESIS_PREV_HASH",
    "events",
]
