"""
Test configuration.

Tests must be hermetic. Two things are arranged here before anything imports
`app.*`:

1. Deterministic settings are pushed into the environment so the app's Pydantic
   settings model never depends on a developer's real `.env` (which holds
   live-ish secrets). Real env vars take precedence over the `.env` file in
   pydantic-settings, so this cleanly overrides whatever is on disk.

2. The database is pointed at a dedicated `agentgate_test` database, separate
   from the `agentgate` dev database, so the test run never touches dev/seed
   data. `_prepare_database` creates it if needed and builds the schema from
   the ORM metadata, then applies the `audit_event` append-only triggers from
   `app.audit.ddl` (the same statements the Alembic migration runs — triggers
   are not in ORM metadata). Alembic itself is exercised by the docker-compose
   / migration checks, not by unit tests.

Async DB fixtures use a fresh `NullPool` engine per test rather than the app's
module-level pooled engine, so no connection is ever reused across the
per-test event loops that pytest-asyncio creates.
"""
from __future__ import annotations

import asyncio
import os

# --- (1) settings, before any app import -----------------------------------
_DEV_DB = "postgresql+asyncpg://agentgate:agentgate_local_dev@localhost:5544/agentgate"
_TEST_DB = _DEV_DB.rsplit("/", 1)[0] + "/agentgate_test"

os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", _TEST_DB)
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key-not-real")
os.environ.setdefault("AI_MODEL", "gemini-2.5-flash")
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test_dummy")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "dummy_secret")
os.environ.setdefault("RAZORPAY_WEBHOOK_SECRET", "dummy_webhook_secret")
# Hard-set (not setdefault): the suite runs entirely against fakes and must
# never make a real Gemini / Razorpay call, even when a developer's repo-root
# .env has AI_ENABLED=true / RAZORPAY_ENABLED=true for live testing. Real env
# vars take precedence over the .env file in pydantic-settings, so this cleanly
# overrides it. Tests that need the enabled path build a Settings instance
# explicitly.
os.environ["AI_ENABLED"] = "false"
os.environ["RAZORPAY_ENABLED"] = "false"

# --- fixtures -------------------------------------------------------------
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.db import Base  # noqa: E402
import app.db_models  # noqa: E402,F401  (registers every table on Base.metadata)


def _test_engine():
    return create_async_engine(get_settings().database_url, poolclass=NullPool)


async def _ensure_test_database_exists() -> None:
    import asyncpg

    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    admin_dsn = dsn.rsplit("/", 1)[0] + "/agentgate"
    target_db = dsn.rsplit("/", 1)[1]
    conn = await asyncpg.connect(admin_dsn)
    try:
        if not await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", target_db
        ):
            await conn.execute(f'CREATE DATABASE "{target_db}"')
    finally:
        await conn.close()


async def _setup_schema() -> None:
    from app.audit.ddl import CREATE_APPEND_ONLY_SQL

    await _ensure_test_database_exists()
    engine = _test_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        # Triggers are not in ORM metadata — apply the exact migration DDL.
        for statement in CREATE_APPEND_ONLY_SQL:
            await conn.exec_driver_sql(statement)
    await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _prepare_database():
    # Sync fixture: asyncio.run owns and closes its own loop, so the engine
    # created inside never leaks loop affinity into the test loops.
    asyncio.run(_setup_schema())
    yield


@pytest_asyncio.fixture
async def db_session():
    """
    A session whose work is always rolled back. Tests use `flush()` to force
    constraint checks; teardown rolls back, so tests stay independent without
    per-test cleanup code.
    """
    engine = _test_engine()
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        try:
            yield session
        finally:
            await session.rollback()
    await engine.dispose()
