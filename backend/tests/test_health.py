"""
Phase 2 test: verifies the app boots and /health responds.

This is deliberately the only test right now. Section 20 of the
architecture-freeze doc puts real weight on tests for the policy engine —
those arrive in Phase 3 alongside the policy module itself. This file
exists so `pytest` has something real to run from commit one, and CI is
proven working before any feature code lands.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_health_endpoint_responds() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code in (200, 503)
    body = response.json()
    assert "status" in body
    assert "database" in body
