import os
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from gateway.unified_api import app, get_admin_allowlist

# Ensure ENV is not 'dev' so we can test headers
os.environ.pop("ENV", None)

client = TestClient(app)


@pytest.fixture
def mock_admin_allowlist():
    app.dependency_overrides[get_admin_allowlist] = lambda: {
        "admins": ["admin@example.com"]
    }
    yield
    app.dependency_overrides.clear()


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_csrf_middleware_missing_header():
    # POST without X-Requested-With should fail
    response = client.post(
        "/api/workflows",
        json={"name": "test", "args": {}},
        headers={"Tailscale-User-Login": "user@example.com"},
    )
    assert response.status_code == 403
    assert "CSRF verification failed" in response.json()["detail"]


def test_csrf_middleware_present_header():
    # POST with X-Requested-With should pass CSRF but might fail auth/admin or succeed
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Tailscale-User-Login": "user@example.com",
    }
    response = client.post(
        "/api/workflows", json={"name": "test", "args": {}}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["id"] == "wf_12345"


def test_auth_missing_header():
    # GET doesn't require CSRF header, but still requires Auth for workflows
    response = client.get("/api/workflows/wf_123/state")
    assert response.status_code == 401
    assert "Missing Tailscale-User-Login header" in response.json()["detail"]


def test_auth_with_dev_env():
    with patch.dict(os.environ, {"ENV": "dev"}):
        response = client.get("/api/workflows/wf_123/state")
        assert response.status_code == 200
        assert response.json()["state"] == "running"


def test_verify_admin_success(mock_admin_allowlist):
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Tailscale-User-Login": "admin@example.com",
    }
    response = client.get("/api/config", headers=headers)
    assert response.status_code == 200


def test_verify_admin_forbidden(mock_admin_allowlist):
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Tailscale-User-Login": "user@example.com",
    }
    response = client.get("/api/config", headers=headers)
    assert response.status_code == 403
    assert "Admin privileges required" in response.json()["detail"]


def test_chat_proxy():
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Tailscale-User-Login": "user@example.com",
    }

    # Mocking httpx.AsyncClient.send to avoid real network call
    class MockResponse:
        status_code = 200
        headers = {}

        async def aiter_raw(self):
            yield b"data: test\\n\\n"

    with patch("httpx.AsyncClient.send") as mock_send:
        mock_send.return_value = MockResponse()
        response = client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": []},
            headers=headers,
        )
        assert response.status_code == 200
        assert response.text == "data: test\\n\\n"


def test_update_config_valid_yaml(mock_admin_allowlist):
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Tailscale-User-Login": "admin@example.com",
    }
    payload = {"config_yaml": "model_list: []\nrouter_settings: {}"}

    with patch.dict(os.environ, {"GITHUB_TOKEN": "mock"}):
        with patch("httpx.AsyncClient.get") as mock_get:

            def mock_get_side_effect(url, **kwargs):
                class MockResponse:
                    def __init__(self, json_data, status_code):
                        self._json = json_data
                        self.status_code = status_code

                    def json(self):
                        return self._json

                if "pulls?state=open" in url:
                    return MockResponse([], 200)
                if "git/refs/heads" in url:
                    return MockResponse({"object": {"sha": "123"}}, 200)
                if "contents" in url:
                    return MockResponse({"sha": "abc456"}, 200)
                return MockResponse({"default_branch": "main"}, 200)

            mock_get.side_effect = mock_get_side_effect

            with patch("httpx.AsyncClient.post") as mock_post:
                mock_post.return_value.status_code = 201
                mock_post.return_value.json = MagicMock(
                    return_value={"html_url": "http://test.com/pr/1"}
                )
                with patch("httpx.AsyncClient.put") as mock_put:
                    mock_put.return_value.status_code = 200
                    response = client.post("/api/config", json=payload, headers=headers)
                    assert response.status_code == 200
                    assert response.json()["status"] == "pr_created"


def test_update_config_invalid_yaml(mock_admin_allowlist):
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Tailscale-User-Login": "admin@example.com",
    }
    # Missing model_list
    payload = {"config_yaml": "some_other_key: []"}

    response = client.post("/api/config", json=payload, headers=headers)
    assert response.status_code == 400
    assert (
        "Configuration must contain a valid 'model_list'" in response.json()["detail"]
    )
