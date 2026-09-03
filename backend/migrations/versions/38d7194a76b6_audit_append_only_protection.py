"""audit append-only protection

Revision ID: 38d7194a76b6
Revises: 44a6dc22308b
Create Date: 2026-09-03 11:06:19.751003

Two changes to `audit_event`:

1. Drop `updated_at`. It is meaningless on an append-only table (there is no
   update path) and its `onupdate` was dead behaviour.
2. Add a plpgsql function + BEFORE triggers that reject UPDATE, DELETE and
   TRUNCATE. INSERT stays allowed. The trigger SQL lives in `app.audit.ddl`
   so the test-suite schema builder (which builds from ORM metadata, not
   migrations) applies exactly the same statements.

Honest limitation: a sufficiently privileged role can still `ALTER TABLE
... DISABLE TRIGGER`, drop the function, or rewrite rows. This blocks the
application and ordinary DB operations, not a determined superuser; the hash
chain is what detects tampering after the fact.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.audit.ddl import CREATE_APPEND_ONLY_SQL, DROP_APPEND_ONLY_SQL

revision: str = "38d7194a76b6"
down_revision: Union[str, None] = "44a6dc22308b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("audit_event", "updated_at")
    for statement in CREATE_APPEND_ONLY_SQL:
        op.execute(statement)


def downgrade() -> None:
    for statement in DROP_APPEND_ONLY_SQL:
        op.execute(statement)
    op.add_column(
        "audit_event",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
