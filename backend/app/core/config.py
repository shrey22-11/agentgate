"""
Typed application settings.

Per the architecture-freeze doc (Section L): environment variables are
validated at startup via this Pydantic settings model. If a required
variable is missing, the process fails fast at boot instead of failing
mysteriously later — this is a deliberate deployment-reliability choice,
not an accident.
"""
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Query params that psycopg/libpq accept but asyncpg does not (asyncpg would
# raise "unexpected keyword argument"). Managed providers append these to the
# external connection string; the internal URL — which is what a same-region
# service should use — normally has none.
_DROP_DB_QUERY_KEYS = {"sslmode", "channel_binding", "target_session_attrs", "gssencmode"}


def normalize_database_url(raw: str) -> str:
    """
    Accept the connection strings managed Postgres providers hand out
    (``postgres://…`` on Fly, ``postgresql://…`` on Render) and return one this
    app can use: the async driver is asyncpg, so the scheme must be
    ``postgresql+asyncpg://``. Also strips libpq-only query params that asyncpg
    rejects. A URL that is already ``postgresql+asyncpg://`` with no such params
    passes through unchanged, so local / test / compose behaviour is identical.
    """
    url = (raw or "").strip()
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]

    if "?" in url:
        base, _, query = url.partition("?")
        kept = [
            part
            for part in query.split("&")
            if part and part.split("=", 1)[0].lower() not in _DROP_DB_QUERY_KEYS
        ]
        url = base + ("?" + "&".join(kept) if kept else "")
    return url


# The repo root holds the single .env (see .env.example). config.py lives at
# backend/app/core/config.py, so the repo root is three parents up. We look
# there first, then fall back to a .env in the current working directory.
# In Docker there is no .env file at all — docker-compose injects the vars as
# real environment variables, which take precedence over any env_file — so a
# missing file here is fine and silently ignored by pydantic-settings.
_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_REPO_ROOT / ".env", ".env"),
        extra="ignore",
    )

    # --- Core ---
    environment: str = Field(default="local")  # local | production
    app_name: str = "AgentGate"

    # --- Database ---
    database_url: str = Field(
        ...,
        description="postgresql+asyncpg://user:pass@host:port/dbname "
        "(a bare postgres:// or postgresql:// URL from a managed provider is "
        "normalised automatically)",
    )

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        return normalize_database_url(value)

    # --- AI provider (single provider: Anthropic Claude, per Section G) ---
    # The app boots without a real key as long as ai_enabled is false. When
    # true the key must be real (not blank, not the .env.example placeholder)
    # or startup fails clearly.
    ai_enabled: bool = Field(default=False)
    anthropic_api_key: str = Field(..., description="Claude API key")
    ai_model: str = Field(default="claude-opus-5")
    ai_request_timeout_seconds: float = Field(default=20.0, gt=0)
    # Deterministic gate (see docs/ai-parsing.md): the natural-language parser
    # emits a confidence in [0, 1] computed from how cleanly the request
    # resolved into a catalog-anchored structured action. Below this, the parse
    # fails closed. It is NOT an LLM self-report.
    ai_parse_confidence_threshold: float = Field(default=0.6, ge=0, le=1)
    # AI buyer agent budget (Phase 10). Hard caps enforced by our loop, not the
    # model: it gets at most this many model turns and this many request_action
    # calls per run.
    ai_buyer_max_steps: int = Field(default=8, ge=1, le=30)
    ai_buyer_max_request_actions: int = Field(default=3, ge=1, le=10)

    # --- Razorpay (test mode only, per Section I) ---
    # The app boots without real credentials as long as execution is disabled.
    # When razorpay_enabled is true the three secrets must be real (not blank,
    # not the .env.example placeholders) or startup fails clearly.
    razorpay_enabled: bool = Field(default=False)
    razorpay_key_id: str = Field(...)
    razorpay_key_secret: str = Field(...)
    razorpay_webhook_secret: str = Field(
        ...,
        description="Distinct from the API key secret — a separate secret "
        "configured in the Razorpay dashboard for this webhook endpoint.",
    )

    # --- Policy defaults (overridable per-merchant in the DB later) ---
    default_max_discount_pct: float = 10.0
    default_approval_threshold_inr: float = 5000.0

    @model_validator(mode="after")
    def _check_razorpay_credentials(self) -> "Settings":
        if not self.razorpay_enabled:
            return self
        missing = [
            name
            for name, value in (
                ("RAZORPAY_KEY_ID", self.razorpay_key_id),
                ("RAZORPAY_KEY_SECRET", self.razorpay_key_secret),
                ("RAZORPAY_WEBHOOK_SECRET", self.razorpay_webhook_secret),
            )
            if not value or "placeholder" in value.lower()
        ]
        if missing:
            raise ValueError(
                "RAZORPAY_ENABLED is true but these are missing or still "
                f"placeholders: {', '.join(missing)}"
            )
        return self

    @model_validator(mode="after")
    def _check_ai_credentials(self) -> "Settings":
        if not self.ai_enabled:
            return self
        key = self.anthropic_api_key or ""
        if not key or "placeholder" in key.lower():
            raise ValueError(
                "AI_ENABLED is true but ANTHROPIC_API_KEY is missing or still a "
                "placeholder"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    # Instantiating here (not at import time) means a missing env var
    # raises a clear validation error the first time settings are
    # actually requested, and the error surfaces at app startup via the
    # health-check dependency chain rather than at random request time.
    return Settings()
