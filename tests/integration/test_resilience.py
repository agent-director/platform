import asyncio

import httpx
import pytest


@pytest.mark.asyncio
async def test_concurrent_api_workflow_stub_requests(api_url: str) -> None:
    """The deployed API stub handles concurrent HTTP requests; this is not Temporal execution."""
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Tailscale-User-Login": "load-tester@example.com",
    }
    async with httpx.AsyncClient(base_url=api_url, timeout=10.0) as client:
        requests = [
            client.post(
                "/api/workflows",
                json={"name": f"test_workflow_{index}", "args": {"iteration": index}},
                headers=headers,
            )
            for index in range(50)
        ]
        responses = await asyncio.gather(*requests, return_exceptions=True)
        assert len(responses) == 50
        for response in responses:
            if not isinstance(response, httpx.Response):
                pytest.fail(f"HTTP stub request failed: {response}")
            assert response.status_code == 200
            assert response.json()["id"] == "wf_12345"
            assert response.json()["status"] == "started"


@pytest.mark.asyncio
async def test_missing_security_headers_are_rejected(api_url: str) -> None:
    """Missing auth/CSRF headers fail predictably on the real deployed API."""
    async with httpx.AsyncClient(base_url=api_url, timeout=5.0) as client:
        response = await client.post(
            "/api/workflows",
            json={"name": "test", "args": {}},
            headers={"Tailscale-User-Login": "user@example.com"},
        )
        assert response.status_code == 403
        response = await client.post(
            "/api/workflows",
            json={"name": "test", "args": {}},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert response.status_code == 401
        response = await client.post(
            "/api/workflows", json={"name": "test", "args": {}}
        )
        assert response.status_code == 403  # CSRF is evaluated before auth.


@pytest.mark.asyncio
async def test_admin_endpoint_has_no_auth_bypass(api_url: str) -> None:
    """Non-admin users and unauthenticated requests cannot read deployed configuration."""
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Tailscale-User-Login": "hacker@example.com",
    }
    async with httpx.AsyncClient(base_url=api_url, timeout=5.0) as client:
        response = await client.get("/api/config", headers=headers)
        assert response.status_code == 403
        assert "Admin privileges required" in response.json()["detail"]
        response = await client.get("/api/config")
        assert response.status_code == 401
