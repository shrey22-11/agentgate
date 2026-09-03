"""
Deterministic policy engine (OUR SYSTEM).

Public surface:
    evaluate(PolicyInput) -> PolicyDecision
    PolicyInput   — the validated in-memory snapshot to evaluate
    PolicyDecision — the immutable result (verdict + rule id + reason + ...)
    rules         — stable rule-id constants and the documented precedence
"""
from app.policy import rules
from app.policy.decision import PolicyDecision
from app.policy.engine import effective_transaction_amount, evaluate
from app.policy.input import PolicyInput

__all__ = [
    "evaluate",
    "effective_transaction_amount",
    "PolicyInput",
    "PolicyDecision",
    "rules",
]
