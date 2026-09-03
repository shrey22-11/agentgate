"""
Single database, single connection pool — deliberately no second
datastore, no cache layer, no queue. See Section P: "no second
database" is a frozen constraint, not an oversight.
"""
from __future__ import annotations

import datetime as _dt
import enum as _enum
import uuid as _uuid
from collections.abc import AsyncGenerator

from sqlalchemy import DateTime, Enum as SAEnum, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    pool_size=5,
    max_overflow=5,
    pool_pre_ping=True,  # avoids stale-connection errors after idle periods
)

AsyncSessionLocal = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


class Base(DeclarativeBase):
    pass


# --- Shared column helpers -------------------------------------------------
# Kept here so every feature module's models.py builds tables the same way:
# UUID primary keys (no cross-table id ambiguity for audit_event.ref_id, and
# no row-count leakage), timezone-aware timestamps, and string-backed enums.


def uuid_pk() -> Mapped[_uuid.UUID]:
    """Primary key column: application-generated UUIDv4, stored as native uuid."""
    return mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=_uuid.uuid4
    )


def str_enum(enum_cls: type[_enum.Enum], **kw):
    """
    An enum column stored as VARCHAR + CHECK constraint, never a native
    PostgreSQL ENUM type. Deliberate: native enums turn every future value
    change into an `ALTER TYPE` migration and buy us nothing here.
    """
    return mapped_column(
        SAEnum(
            enum_cls,
            native_enum=False,
            validate_strings=True,
            length=32,
            # store the value ("ALLOW"), not the member name
            values_callable=lambda e: [m.value for m in e],
        ),
        **kw,
    )


class TimestampMixin:
    """`created_at` / `updated_at`, both server-side, both timezone-aware."""

    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Request-scoped session. The route handler and the services it calls share
    this one session and do all their work inside its single transaction; a
    service performs the final `commit()`. If the handler raises, this
    dependency rolls back so nothing partial is left behind.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def db_is_reachable() -> bool:
    """Used by the /health endpoint. Deliberately does the simplest
    possible query — this is a liveness check, not a schema check."""
    from sqlalchemy import text

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
