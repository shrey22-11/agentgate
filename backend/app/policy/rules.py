"""
Stable rule identifiers and the evaluation precedence.

Rule IDs are part of the audit contract: they are written to `decision.policy_rule_id`
and shown in the UI, so they must not change once emitted. Add new ones; do not
rename existing ones.

Evaluation precedence (highest priority first)
---------------------------------------------
0. RULE_INPUT_INVALID   — structured input is missing fields or self-contradictory
1. RULE_AGENT_ACTIVE    — the agent's status must be ACTIVE
2. RULE_ACTION_PERMISSION — the action type must be in the agent's allow-list
3. RULE_TRANSACTION_CAP — requested amount over the agent's per-transaction limit
4. RULE_STOCK_AVAILABLE — requested quantity over available stock
5. RULE_DISCOUNT_POLICY / RULE_PRICE_FLOOR — requested price below the deterministic floor
6. RULE_OK              — nothing above fired; the request is allowed

The first rule that fires decides the verdict; later rules are not consulted.
This ordering is deliberate:

* Agent identity and authority (1-3) are checked before anything about the
  merchant's catalogue. An inactive or unauthorised agent is denied even if the
  commercial terms would have been fine.
* The transaction cap (3) is an agent-authority question, so it is checked
  before stock (4), which is a merchant-fulfilment question. A large order from
  an out-of-authority agent is routed to approval, not denied for stock.
* Commercial-terms checks (5) run last, so an out-of-stock request is denied
  outright and never produces a COUNTER_OFFER for inventory that does not exist.
"""
from __future__ import annotations

RULE_INPUT_INVALID = "RULE_INPUT_INVALID"
RULE_AGENT_ACTIVE = "RULE_AGENT_ACTIVE"
RULE_ACTION_PERMISSION = "RULE_ACTION_PERMISSION"
RULE_TRANSACTION_CAP = "RULE_TRANSACTION_CAP"
RULE_STOCK_AVAILABLE = "RULE_STOCK_AVAILABLE"
RULE_DISCOUNT_POLICY = "RULE_DISCOUNT_POLICY"
RULE_PRICE_FLOOR = "RULE_PRICE_FLOOR"
RULE_OK = "RULE_OK"

ALL_RULE_IDS = frozenset(
    {
        RULE_INPUT_INVALID,
        RULE_AGENT_ACTIVE,
        RULE_ACTION_PERMISSION,
        RULE_TRANSACTION_CAP,
        RULE_STOCK_AVAILABLE,
        RULE_DISCOUNT_POLICY,
        RULE_PRICE_FLOOR,
        RULE_OK,
    }
)
