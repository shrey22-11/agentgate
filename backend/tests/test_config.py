"""
Phase 13 - deployment-facing settings behaviour.

`normalize_database_url` lets a managed provider's connection string
(`postgres://` on Fly, `postgresql://` on Render, sometimes with libpq-only
query params) be used unchanged as `DATABASE_URL`. A URL that is already in the
app's own form must pass through byte-for-byte so local / test / compose
behaviour never changes.
"""
from __future__ import annotations

import pytest

from app.core.config import normalize_database_url

_CANONICAL = "postgresql+asyncpg://u:p@h:5432/db"


@pytest.mark.parametrize(
    "raw, expected",
    [
        # already canonical -> untouched
        (_CANONICAL, _CANONICAL),
        (
            "postgresql+asyncpg://agentgate:agentgate_local_dev@db:5432/agentgate",
            "postgresql+asyncpg://agentgate:agentgate_local_dev@db:5432/agentgate",
        ),
        # Render hands out postgresql://
        ("postgresql://u:p@h:5432/db", _CANONICAL),
        # Fly hands out postgres://
        ("postgres://u:p@h:5432/db", _CANONICAL),
        # libpq-only params asyncpg would reject are stripped
        ("postgres://u:p@h:5432/db?sslmode=disable", _CANONICAL),
        (
            "postgresql://u:p@h:5432/db?sslmode=require&channel_binding=prefer",
            _CANONICAL,
        ),
        # an unrelated query param is preserved
        (
            "postgresql://u:p@h:5432/db?application_name=agentgate",
            _CANONICAL + "?application_name=agentgate",
        ),
        (
            "postgres://u:p@h/db?sslmode=require&application_name=x",
            "postgresql+asyncpg://u:p@h/db?application_name=x",
        ),
        # surrounding whitespace (copy/paste from a dashboard) is trimmed
        ("  postgresql://u:p@h:5432/db  ", _CANONICAL),
    ],
)
def test_normalize_database_url(raw: str, expected: str) -> None:
    assert normalize_database_url(raw) == expected


def test_settings_accepts_a_bare_managed_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Settings model normalises DATABASE_URL through the field validator."""
    from app.core.config import Settings

    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h:5432/db?sslmode=require")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_x")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "x")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "x")
    # a fresh instance (not the lru_cached get_settings) so env changes apply
    s = Settings(_env_file=None)
    assert s.database_url == "postgresql+asyncpg://u:p@h:5432/db"
