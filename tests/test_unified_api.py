import os
from unittest.mock import mock_open, patch

import pytest
from fastapi.testclient import TestClient

import gateway.unified_api
from gateway.unified_api import app

# Ensure ENV is not 'dev' so we can test headers
os.environ.pop("ENV", None)


@pytest.fixture(autouse=True)
def clear_allowlist_cache():
    gateway.unified_api._allowlist_cache = {}
    yield


client = TestClient(app)


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


def test_verify_admin_success():
    allowlist_yaml = """
admins:
  - admin@example.com
"""
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Tailscale-User-Login": "admin@example.com",
    }
    with patch("builtins.open", mock_open(read_data=allowlist_yaml)):
        response = client.get("/api/config", headers=headers)
        assert response.status_code == 200


def test_verify_admin_forbidden():
    allowlist_yaml = """
admins:
  - admin@example.com
"""
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Tailscale-User-Login": "user@example.com",
    }
    with patch("builtins.open", mock_open(read_data=allowlist_yaml)):
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
        def __init__(self):
            self.status_code = 200
            self.headers = {}

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


def test_chat_proxy_payload_too_large():
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Tailscale-User-Login": "dev@example.com",
        "Content-Length": str((10 * 1024 * 1024) + 1),
    }
    response = client.post(
        "/v1/chat/completions",
        content=b"a" * int(headers["Content-Length"]),
        headers=headers,
    )
    assert response.status_code == 413
    assert response.json()["detail"] == "Payload too large"


def test_chat_proxy_streaming_validator():
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Tailscale-User-Login": "dev@example.com",
    }

    # A malicious string split across multiple chunks
    def stream_malicious_payload():
        yield b'{"mess'
        yield b'ages": [{"role": "user", '
        yield b'"content": "sys'
        yield b'tem(foo)"}]}'

    class MockResponse:
        def __init__(self):
            self.status_code = 200
            self.headers = {}

        async def aiter_raw(self):
            yield b"data: test\n\n"

    with patch("httpx.AsyncClient.send") as mock_send:
        mock_send.return_value = MockResponse()

        # But wait, if mock_send doesn't consume the stream, the HTTPException won't be raised!
        # Let's write a mock that consumes the stream.
        async def mock_send_coro(req, **kwargs):
            async for _ in req.stream:
                pass
            return MockResponse()

        mock_send.side_effect = mock_send_coro

        response = client.post(
            "/v1/chat/completions", content=stream_malicious_payload(), headers=headers
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Malicious input detected."


def test_update_config_valid_yaml():
    allowlist_yaml = "admins:\n  - admin@example.com"
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Tailscale-User-Login": "admin@example.com",
    }
    payload = {"config_yaml": "model_list: []\nrouter_settings: {}"}

    with (
        patch("builtins.open", mock_open(read_data=allowlist_yaml)),
        patch.dict(os.environ, {"GITHUB_TOKEN": "mock"}),
        patch("httpx.AsyncClient.get") as mock_get,
    ):

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
            from unittest.mock import MagicMock

            mock_post.return_value.status_code = 201
            mock_post.return_value.json = MagicMock(
                return_value={"html_url": "http://test.com/pr/1"}
            )
            with patch("httpx.AsyncClient.put") as mock_put:
                mock_put.return_value.status_code = 200
                response = client.post("/api/config", json=payload, headers=headers)
                assert response.status_code == 200
                assert response.json()["status"] == "pr_created"


def test_update_config_conflict():
    allowlist_yaml = "admins:\n  - admin@example.com"
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Tailscale-User-Login": "admin@example.com",
    }
    payload = {"config_yaml": "model_list: []\nrouter_settings: {}"}

    class MockResponse:
        def __init__(self, json_data, status_code=200):
            self._json = json_data
            self.status_code = status_code

        def json(self):
            return self._json

    with (
        patch("builtins.open", mock_open(read_data=allowlist_yaml)),
        patch.dict(os.environ, {"GITHUB_TOKEN": "mock"}),
        patch("httpx.AsyncClient.get") as mock_get,
    ):

        def mock_get_side_effect(url, **kwargs):
            if "page=1" in url:
                return MockResponse([{"head": {"ref": "config-update-abc12345"}}], 200)
            return MockResponse([], 200)

        mock_get.side_effect = mock_get_side_effect
        response = client.post("/api/config", json=payload, headers=headers)
        assert response.status_code == 409
        assert "A configuration PR is already open" in response.json()["detail"]


def test_update_config_github_upstream_failure():
    allowlist_yaml = "admins:\n  - admin@example.com"
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Tailscale-User-Login": "admin@example.com",
    }
    payload = {"config_yaml": "model_list: []\nrouter_settings: {}"}

    class MockResponse:
        def __init__(self, json_data, status_code=200):
            self._json = json_data
            self.status_code = status_code

        def json(self):
            return self._json

    # Test failure to fetch repo info
    with (
        patch("builtins.open", mock_open(read_data=allowlist_yaml)),
        patch.dict(os.environ, {"GITHUB_TOKEN": "mock"}),
        patch("httpx.AsyncClient.get") as mock_get,
    ):

        def mock_get_side_effect(url, **kwargs):
            if "pulls?state=open" in url:
                return MockResponse([], 200)
            return MockResponse({}, 500)

        mock_get.side_effect = mock_get_side_effect
        response = client.post("/api/config", json=payload, headers=headers)
        assert response.status_code == 500
        assert "Failed to fetch repo info" in response.json()["detail"]


def test_update_config_pr_creation_failure():
    allowlist_yaml = "admins:\n  - admin@example.com"
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Tailscale-User-Login": "admin@example.com",
    }
    payload = {"config_yaml": "model_list: []\nrouter_settings: {}"}

    class MockResponse:
        def __init__(self, json_data, status_code=200):
            self._json = json_data
            self.status_code = status_code

        def json(self):
            return self._json

    with (
        patch("builtins.open", mock_open(read_data=allowlist_yaml)),
        patch.dict(os.environ, {"GITHUB_TOKEN": "mock"}),
        patch("httpx.AsyncClient.get") as mock_get,
    ):

        def mock_get_side_effect(url, **kwargs):
            if "pulls?state=open" in url:
                return MockResponse([], 200)
            if "git/refs/heads" in url:
                return MockResponse({"object": {"sha": "123"}}, 200)
            if "contents" in url:
                return MockResponse({"sha": "abc456"}, 200)
            return MockResponse({"default_branch": "main"}, 200)

        mock_get.side_effect = mock_get_side_effect
        with patch("httpx.AsyncClient.post") as mock_post:
            mock_post.return_value = MockResponse({}, 500)
            with patch("httpx.AsyncClient.put") as mock_put:
                mock_put.return_value = MockResponse({}, 200)
                response = client.post("/api/config", json=payload, headers=headers)
                assert response.status_code == 500
                assert "Failed to create Pull Request" in response.json()["detail"]


def test_update_config_invalid_yaml():
    allowlist_yaml = "admins:\n  - admin@example.com"
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Tailscale-User-Login": "admin@example.com",
    }
    # Missing model_list
    payload = {"config_yaml": "some_other_key: []"}

    with patch("builtins.open", mock_open(read_data=allowlist_yaml)):
        response = client.post("/api/config", json=payload, headers=headers)
        assert response.status_code == 400
        assert (
            "Configuration must contain a valid 'model_list'"
            in response.json()["detail"]
        )

        # Test TypeError (non-dict YAML)
        payload_type = {"config_yaml": "- list_item"}
        response_type = client.post("/api/config", json=payload_type, headers=headers)
        assert response_type.status_code == 400
        assert "YAML must be a dictionary" in response_type.json()["detail"]

        # Test YAMLError (invalid syntax)
        payload_yaml_err = {"config_yaml": "foo: [unclosed list"}
        response_err = client.post(
            "/api/config", json=payload_yaml_err, headers=headers
        )
        assert response_err.status_code == 400
        assert "Invalid YAML" in response_err.json()["detail"]
