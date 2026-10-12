import asyncio

import httpx
import pytest

API_URL = "http://127.0.0.1:8000"


@pytest.mark.asyncio
async def test_concurrent_workflow_creation():
    """
    Simulate a basic load test: creating multiple workflows concurrently
    against the running cluster to ensure the unified API and temporal workers
    handle concurrent requests smoothly without crashing or dropping connections.
    """
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Tailscale-User-Login": "load-tester@example.com",
    }

    async with httpx.AsyncClient(base_url=API_URL, timeout=10.0) as async_client:
        tasks = []
        for i in range(50):
            payload = {"name": f"test_workflow_{i}", "args": {"iteration": i}}
            tasks.append(
                async_client.post("/api/workflows", json=payload, headers=headers)
            )

        responses = await asyncio.gather(*tasks, return_exceptions=True)

        assert len(responses) == 50
        for resp in responses:
            if isinstance(resp, Exception):
                pytest.fail(f"Request failed with exception: {resp}")
            assert resp.status_code == 200
            assert resp.json()["id"] == "wf_12345"
            assert resp.json()["status"] == "started"


@pytest.mark.asyncio
async def test_chaos_missing_headers():
    """
    Security/Chaos test: Ensure missing headers reliably return 401/403
    and do not cause unhandled exceptions in the actual deployed gateway.
    """
    async with httpx.AsyncClient(base_url=API_URL, timeout=5.0) as client:
        # Missing CSRF
        response = await client.post(
            "/api/workflows",
            json={"name": "test", "args": {}},
            headers={"Tailscale-User-Login": "user@example.com"},
        )
        assert response.status_code == 403

        # Missing Auth
        response = await client.post(
            "/api/workflows",
            json={"name": "test", "args": {}},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert response.status_code == 401

        # Missing Both
        response = await client.post(
            "/api/workflows", json={"name": "test", "args": {}}
        )
        assert response.status_code == 403  # CSRF evaluated first


@pytest.mark.asyncio
async def test_security_admin_endpoint_no_bypass():
    """
    Security test: Ensure the /api/config endpoint cannot be bypassed
    by standard users or missing auth in the real deployed environment.
    Note: We do not mock the allowlist here. We test the actual default rejection.
    """
    headers_user = {
        "X-Requested-With": "XMLHttpRequest",
        "Tailscale-User-Login": "hacker@example.com",
    }

    async with httpx.AsyncClient(base_url=API_URL, timeout=5.0) as client:
        response = await client.get("/api/config", headers=headers_user)
        assert response.status_code == 403
        assert "Admin privileges required" in response.json()["detail"]

        # Missing Auth entirely
        response = await client.get("/api/config")
        assert response.status_code == 401
