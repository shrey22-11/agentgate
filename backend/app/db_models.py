"""
Single import point for every ORM model in the app.

Importing this module has the side effect of registering all tables on
`app.core.db.Base.metadata`. Both Alembic's `env.py` (for autogenerate) and the
test suite import it so nothing has to remember the full list of feature
packages.

Add new models' modules here as feature packages gain them.
"""
from app.action_requests.models import ActionRequest
from app.agents.models import Agent
from app.approvals.models import Approval
from app.audit.models import AuditEvent
from app.catalog.models import Merchant, Product
from app.policy.models import Decision
from app.razorpay.models import PaymentAttempt
from app.webhooks.models import WebhookEvent

__all__ = [
    "ActionRequest",
    "Agent",
    "Approval",
    "AuditEvent",
    "Merchant",
    "Product",
    "Decision",
    "PaymentAttempt",
    "WebhookEvent",
]
