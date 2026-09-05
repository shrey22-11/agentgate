"""
AgentGate backend entrypoint.

  - creates the FastAPI app from typed settings
  - exposes /health (liveness + a single DB dependency check)
  - registers every feature router (actions, ai, approvals, payments,
    webhooks, audit, catalog, dashboard)
  - serves the built React SPA same-origin, so no CORS surface exists at all
    (deliberate — see the architecture-freeze doc, Sections H / L).
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

from app.action_requests.router import router as actions_router
from app.ai.router import router as ai_router
from app.approvals.router import router as approvals_router
from app.audit.router import router as audit_router
from app.catalog.router import router as catalog_router
from app.dashboard.router import router as dashboard_router
from app.razorpay.router import router as payments_router
from app.webhooks.router import router as webhooks_router
from app.core.config import get_settings
from app.core.db import db_is_reachable

settings = get_settings()

app = FastAPI(title=settings.app_name)

FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"


@app.get("/health")
async def health() -> JSONResponse:
    """
    Liveness + one dependency check (DB). Deliberately does NOT check
    the AI provider or Razorpay here — those failures should surface as
    fail-closed behavior inside the relevant module (per Section J),
    not as a down health check, or a transient LLM hiccup would take
    the whole app "unhealthy" for no good reason.
    """
    db_ok = await db_is_reachable()
    status_code = 200 if db_ok else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if db_ok else "degraded",
            "database": "reachable" if db_ok else "unreachable",
            "environment": settings.environment,
        },
    )


# Feature routers must be registered before the catch-all SPA mount below,
# which otherwise swallows every path.
app.include_router(actions_router)
app.include_router(ai_router)
app.include_router(approvals_router)
app.include_router(payments_router)
app.include_router(webhooks_router)
app.include_router(audit_router)
app.include_router(catalog_router)
app.include_router(dashboard_router)


# --- Same-origin SPA serving -------------------------------------------------
# Deliberate choice (Section H / L): the built React app is served by this
# same FastAPI process. This makes CORS a non-issue structurally, rather
# than something to configure and maintain.
if FRONTEND_DIST.exists():
    app.mount(
        "/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend"
    )
else:
    @app.get("/")
    async def frontend_not_built() -> dict:
        return {
            "message": (
                "Frontend build not found at "
                f"{FRONTEND_DIST}. Run `npm run build` in /frontend, "
                "or run the frontend dev server separately during local "
                "development (see README)."
            )
        }
