import asyncio

import pytest
from fastapi.testclient import TestClient

from gateway.unified_api import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.mark.asyncio
async def test_concurrent_workflow_creation(client):
    """
    Simulate a basic load test: creating multiple workflows concurrently
    to ensure the API handles concurrent requests smoothly without crashing.
    """
    import httpx

    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Tailscale-User-Login": "load-tester@example.com",
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        tasks = []
        for i in range(50):
            payload = {"name": f"test_workflow_{i}", "args": {"iteration": i}}
            tasks.append(
                async_client.post("/api/workflows", json=payload, headers=headers)
            )

        responses = await asyncio.gather(*tasks)

        assert len(responses) == 50
        for resp in responses:
            assert resp.status_code == 200
            assert resp.json()["id"] == "wf_12345"
            assert resp.json()["status"] == "started"


def test_chaos_missing_headers(client):
    """
    Security/Chaos test: Ensure missing headers reliably return 401/403
    and do not cause unhandled exceptions.
    """
    # Missing CSRF
    response = client.post(
        "/api/workflows",
        json={"name": "test", "args": {}},
        headers={"Tailscale-User-Login": "user@example.com"},
    )
    assert response.status_code == 403

    # Missing Auth
    response = client.post(
        "/api/workflows",
        json={"name": "test", "args": {}},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert response.status_code == 401

    # Missing Both
    response = client.post("/api/workflows", json={"name": "test", "args": {}})
    assert response.status_code == 403  # CSRF evaluated first


def test_security_admin_endpoint_no_bypass(client):
    """
    Security test: Ensure the /api/config endpoint cannot be bypassed
    by standard users or missing auth.
    """
    headers_user = {
        "X-Requested-With": "XMLHttpRequest",
        "Tailscale-User-Login": "hacker@example.com",
    }

    # Standard user without allowlist
    response = client.get("/api/config", headers=headers_user)
    assert response.status_code in [
        403,
        500,
    ]  # 500 if allowlist is missing, 403 if it exists but user not in it

    # Missing Auth entirely
    response = client.get("/api/config")
    assert response.status_code == 401
