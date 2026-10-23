import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.types import Message, Scope
from typing_extensions import override

from unified_api.main import (
    app,
    chat_proxy,
    create_app,
    get_cached_yaml,
    get_settings,
    parse_config_yaml,
)
from unified_api.settings import Settings

USER_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Tailscale-User-Login": "user@example.com",
}
ADMIN_HEADERS = {**USER_HEADERS, "Tailscale-User-Login": "admin@example.com"}
CONFIG_PAYLOAD = {"config_yaml": "model_list: []\nrouter_settings: {}"}


class EventStream(httpx.AsyncByteStream):
    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"data: test\n\n"


def event_response() -> httpx.Response:
    return httpx.Response(200, stream=EventStream())


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_csrf_middleware_missing_header(client: TestClient) -> None:
    response = client.post(
        "/api/workflows",
        json={"name": "test", "args": {}},
        headers={"Tailscale-User-Login": "user@example.com"},
    )
    assert response.status_code == 403
    assert "CSRF verification failed" in response.json()["detail"]


def test_csrf_middleware_present_header_returns_placeholder(client: TestClient) -> None:
    response = client.post(
        "/api/workflows", json={"name": "test", "args": {}}, headers=USER_HEADERS
    )
    assert response.status_code == 200
    assert response.json()["id"] == "wf_12345"


def test_auth_missing_header(client: TestClient) -> None:
    response = client.get("/api/workflows/wf_123/state")
    assert response.status_code == 401
    assert "Missing Tailscale-User-Login header" in response.json()["detail"]


def test_auth_with_dev_env(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "dev")
    response = client.get("/api/workflows/wf_123/state")
    assert response.status_code == 200
    assert response.json()["state"] == "running"


def test_internal_service_requires_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = {"Tailscale-User-Login": "orchestrator@internal"}
    assert (
        client.get(
            "/api/workflows/wf_123/state",
            headers={**headers, "Authorization": "Bearer unit-test-token"},
        ).status_code
        == 401
    )

    monkeypatch.setenv("INTERNAL_TOKEN", "   ")
    assert (
        client.get(
            "/api/workflows/wf_123/state",
            headers={**headers, "Authorization": "Bearer    "},
        ).status_code
        == 401
    )

    monkeypatch.setenv("INTERNAL_TOKEN", "unit-test-token")
    assert client.get("/api/workflows/wf_123/state", headers=headers).status_code == 401
    assert (
        client.get(
            "/api/workflows/wf_123/state",
            headers={**headers, "Authorization": "Bearer wrong-token"},
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/workflows/wf_123/state",
            headers=[
                (b"tailscale-user-login", b"orchestrator@internal"),
                (b"authorization", b"Bearer \xe9"),
            ],
        ).status_code
        == 401
    )
    headers["Authorization"] = "Bearer unit-test-token"
    assert client.get("/api/workflows/wf_123/state", headers=headers).status_code == 200


def test_verify_admin_success(client: TestClient, mock_admin_allowlist: None) -> None:
    response = client.get("/api/config", headers=ADMIN_HEADERS)
    assert response.status_code == 200


def test_verify_admin_forbidden(client: TestClient, mock_admin_allowlist: None) -> None:
    response = client.get("/api/config", headers=USER_HEADERS)
    assert response.status_code == 403
    assert "Admin privileges required" in response.json()["detail"]


def test_chat_proxy_uses_injected_endpoint_and_closes_stream(
    client: TestClient,
) -> None:
    settings = Settings(litellm_url="https://custom-release.example:4141/v1/")
    app.dependency_overrides[get_settings] = lambda: settings
    upstream = event_response()
    try:
        with patch("httpx.AsyncClient.send", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = upstream
            response = client.post(
                "/v1/chat/completions",
                json={"model": "test", "messages": []},
                headers=USER_HEADERS,
            )
        assert response.status_code == 200
        assert response.text == "data: test\n\n"
        request = mock_send.call_args.args[0]
        assert (
            str(request.url)
            == "https://custom-release.example:4141/v1/chat/completions"
        )
        assert request.headers["x-litellm-user"] == "user@example.com"
        assert upstream.is_closed
    finally:
        app.dependency_overrides.pop(get_settings)


def test_chat_proxy_payload_too_large(client: TestClient) -> None:
    headers = {**USER_HEADERS, "Content-Length": str((10 * 1024 * 1024) + 1)}
    response = client.post("/v1/chat/completions", content=b"payload", headers=headers)
    assert response.status_code == 413
    assert response.json()["detail"] == "Payload too large"


def test_chat_proxy_invalid_content_length(client: TestClient) -> None:
    response = client.post(
        "/v1/chat/completions",
        content=b"payload",
        headers={**USER_HEADERS, "Content-Length": "invalid"},
    )
    assert response.status_code == 400


def test_chat_proxy_malicious_content_rejected(client: TestClient) -> None:
    def stream_malicious_payload() -> Iterator[bytes]:
        yield b'{"mess'
        yield b'ages": [{"role": "user", '
        yield b'"content": "sys'
        yield b'tem(foo)"}]}'

    async def consume_request(
        request: httpx.Request, *, stream: bool
    ) -> httpx.Response:
        await request.aread()
        return event_response()

    with patch("httpx.AsyncClient.send", new_callable=AsyncMock) as mock_send:
        mock_send.side_effect = consume_request
        response = client.post(
            "/v1/chat/completions",
            content=stream_malicious_payload(),
            headers=USER_HEADERS,
        )
    assert response.status_code == 400
    assert response.json()["detail"] == "Malicious input detected."


def github_get(url: str, **kwargs: object) -> httpx.Response:
    if "pulls?state=open" in url:
        return httpx.Response(200, json=[])
    if "git/refs/heads" in url:
        return httpx.Response(200, json={"object": {"sha": "123"}})
    if "contents" in url:
        return httpx.Response(200, json={"sha": "abc456"})
    return httpx.Response(200, json={"default_branch": "main"})


def test_update_config_valid_yaml(
    client: TestClient, mock_admin_allowlist: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "mock")
    monkeypatch.setenv("GITHUB_CONFIG_PATH", "configuration/models.yaml")
    with (
        patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get,
        patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post,
        patch("httpx.AsyncClient.put", new_callable=AsyncMock) as mock_put,
    ):
        mock_get.side_effect = github_get
        mock_post.return_value = httpx.Response(
            201, json={"html_url": "https://test.example/pr/1"}
        )
        mock_put.return_value = httpx.Response(200)
        response = client.post(
            "/api/config", json=CONFIG_PAYLOAD, headers=ADMIN_HEADERS
        )
        assert response.status_code == 200
        assert response.json()["status"] == "pr_created"
        assert (
            "contents/configuration/models.yaml?ref=config-update-"
            in mock_put.call_args.args[0]
        )


def test_update_config_conflict(
    client: TestClient, mock_admin_allowlist: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "mock")
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = httpx.Response(
            200, json=[{"head": {"ref": "config-update-abc12345"}}]
        )
        response = client.post(
            "/api/config", json=CONFIG_PAYLOAD, headers=ADMIN_HEADERS
        )
    assert response.status_code == 409
    assert "A configuration PR is already open" in response.json()["detail"]


def test_update_config_github_upstream_failure(
    client: TestClient, mock_admin_allowlist: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_repo_get(url: str, **kwargs: object) -> httpx.Response:
        if "pulls?state=open" in url:
            return httpx.Response(200, json=[])
        return httpx.Response(500, json={})

    monkeypatch.setenv("GITHUB_TOKEN", "mock")
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = failing_repo_get
        response = client.post(
            "/api/config", json=CONFIG_PAYLOAD, headers=ADMIN_HEADERS
        )
    assert response.status_code == 500
    assert "Failed to fetch repo info" in response.json()["detail"]


def test_update_config_pr_creation_failure(
    client: TestClient, mock_admin_allowlist: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "mock")
    with (
        patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get,
        patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post,
        patch("httpx.AsyncClient.put", new_callable=AsyncMock) as mock_put,
    ):
        mock_get.side_effect = github_get
        mock_post.side_effect = [httpx.Response(201), httpx.Response(500)]
        mock_put.return_value = httpx.Response(200)
        response = client.post(
            "/api/config", json=CONFIG_PAYLOAD, headers=ADMIN_HEADERS
        )
    assert response.status_code == 500
    assert "Failed to create Pull Request" in response.json()["detail"]


def test_update_config_branch_creation_failure(
    client: TestClient, mock_admin_allowlist: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "mock")
    with (
        patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get,
        patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post,
    ):
        mock_get.side_effect = github_get
        mock_post.return_value = httpx.Response(500)
        response = client.post(
            "/api/config", json=CONFIG_PAYLOAD, headers=ADMIN_HEADERS
        )
    assert response.status_code == 500
    assert "Failed to create configuration branch" in response.json()["detail"]


@pytest.mark.parametrize(
    "config_yaml,detail",
    [
        ("some_other_key: []", "Configuration must contain a valid 'model_list'"),
        ("- list_item", "YAML must be a dictionary"),
        ("foo: [unclosed list", "Invalid YAML"),
    ],
)
def test_update_config_invalid_yaml(
    client: TestClient, mock_admin_allowlist: None, config_yaml: str, detail: str
) -> None:
    response = client.post(
        "/api/config", json={"config_yaml": config_yaml}, headers=ADMIN_HEADERS
    )
    assert response.status_code == 400
    assert detail in response.json()["detail"]


@pytest.mark.parametrize(
    "endpoint",
    [
        "litellm:4000",
        "ftp://models.example:4000",
        "http://models.example:70000",
        "http://models.example:0",
        "http://models.example/other",
        "http://models.example/?token=value",
        "http://user:pass@models.example:4000",
        "http://models.example?",
        "http://models.example#",
        "http://models.example/v1?",
        "http://models.example/v1#",
        "http://[v1.models]:4000",
        "http://models" + chr(0x200B) + ".example:4000",
        "http://models\x7f.example:4000",
    ],
)
def test_settings_reject_invalid_litellm_endpoints(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        Settings(litellm_url=endpoint)


def test_settings_require_explicit_litellm_endpoint() -> None:
    with pytest.raises(ValueError, match="LITELLM_URL is required"):
        Settings.from_environment({})


def test_settings_read_nondefault_paths(tmp_path: Path) -> None:
    settings = Settings.from_environment(
        {
            "LITELLM_URL": "http://custom-backend.example:4911",
            "LITELLM_CONFIG_PATH": str(tmp_path / "models.yaml"),
            "ADMIN_ALLOWLIST_PATH": str(tmp_path / "admins.yaml"),
            "ALLOWED_ORIGINS": "https://ui.example:4443",
        }
    )
    assert settings.litellm_config_path == tmp_path / "models.yaml"
    assert settings.admin_allowlist_path == tmp_path / "admins.yaml"
    assert settings.allowed_origins == ("https://ui.example:4443",)
    assert settings.github_config_path == "charts/platform/files/litellm_config.yaml"
    assert (
        Settings(litellm_url="http://custom-backend.example:4911").github_config_path
        == "charts/platform/files/litellm_config.yaml"
    )
    assert (Path(__file__).parents[4] / settings.github_config_path).is_file()


def test_create_app_settings_dependency_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LITELLM_URL", raising=False)
    settings = Settings(litellm_url="http://custom.test:4321", environment="dev")
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/workflows/wf_123/state")
        assert response.status_code == 200


@pytest.mark.parametrize(
    "endpoint",
    [
        None,
        "invalid-endpoint",
        "http://[v1.models]:4000",
        "http://models" + chr(0x200B) + ".example:4000",
        "http://models\x7f.example:4000",
    ],
)
def test_default_app_startup_rejects_missing_or_invalid_endpoint(
    monkeypatch: pytest.MonkeyPatch, endpoint: str | None
) -> None:
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://ui.example")
    if endpoint is None:
        monkeypatch.delenv("LITELLM_URL", raising=False)
    else:
        monkeypatch.setenv("LITELLM_URL", endpoint)
    with pytest.raises(ValueError), TestClient(create_app()):
        pass


def test_import_and_explicit_settings_ignore_invalid_environment_origins() -> None:
    program = (
        "from unified_api.main import create_app\n"
        "from unified_api.settings import Settings\n"
        "from fastapi.testclient import TestClient\n"
        "settings = Settings(litellm_url='http://models.test:4321')\n"
        "with TestClient(create_app(settings)) as client:\n"
        "    assert client.get('/health').status_code == 200\n"
    )
    result = subprocess.run(
        ["uv", "run", "--locked", "--no-sync", "python", "-c", program],
        cwd=Path(__file__).parents[4],
        env={**os.environ, "ALLOWED_ORIGINS": "*", "LITELLM_URL": "invalid-endpoint"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_environment_app_rejects_invalid_origins_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LITELLM_URL", "http://models.test:4321")
    monkeypatch.setenv("ALLOWED_ORIGINS", "*")
    application = create_app()
    with (
        pytest.raises(ValueError, match="ALLOWED_ORIGINS"),
        TestClient(application),
    ):
        pass


@pytest.mark.parametrize("injected", [True, False])
def test_cors_uses_canonical_validated_origin(
    monkeypatch: pytest.MonkeyPatch, injected: bool
) -> None:
    if injected:
        settings = Settings(
            litellm_url="http://models.test:4321",
            allowed_origins=("https://UI.example:443/",),
        )
        application = create_app(settings)
    else:
        monkeypatch.setenv("LITELLM_URL", "http://models.test:4321")
        monkeypatch.setenv("ALLOWED_ORIGINS", "https://UI.example:443/")
        application = create_app()
    with TestClient(application) as client:
        response = client.options(
            "/api/config",
            headers={
                "Origin": "https://ui.example",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://ui.example"


def alias_document(levels: int) -> str:
    lines = ["a0: &a0 [x]"]
    lines.extend(
        f"a{level}: &a{level} [*a{level - 1}, *a{level - 1}]"
        for level in range(1, levels + 1)
    )
    lines.append(f"model_list: *a{levels}")
    return "\n".join(lines)


def merge_alias_document(levels: int) -> str:
    lines = ["a0: &a0 {model: sample}"]
    lines.extend(
        f"a{level}: &a{level} {{<<: [*a{level - 1}, *a{level - 1}]}}"
        for level in range(1, levels + 1)
    )
    lines.append(f"model_list: [*a{levels}]")
    return "\n".join(lines)


def test_config_yaml_rejects_merge_expansion_before_construction() -> None:
    with patch("yaml.SafeLoader.construct_document") as construct:
        with pytest.raises(ValueError, match="expanded-node limit"):
            parse_config_yaml(merge_alias_document(16))
        construct.assert_not_called()


def test_config_yaml_accepts_small_merge_aliases() -> None:
    parsed = parse_config_yaml(
        "defaults: &defaults {model: sample}\nmodel_list: [{<<: *defaults}]"
    )
    assert parsed == {
        "defaults": {"model": "sample"},
        "model_list": [{"model": "sample"}],
    }


def test_config_yaml_accepts_small_alias_graph() -> None:
    parsed = parse_config_yaml("models: &models []\nmodel_list: *models")
    assert parsed == {"models": [], "model_list": []}


@pytest.mark.parametrize(
    "source,detail",
    [
        (alias_document(16), "expanded-node limit"),
        ("model_list: &recursive [*recursive]", "Recursive YAML aliases"),
        ("model_list: " + "[" * 80 + "x" + "]" * 80, "nesting limit"),
    ],
)
def test_config_yaml_rejects_unbounded_graphs(
    tmp_path: Path, source: str, detail: str
) -> None:
    with pytest.raises(ValueError, match=detail):
        parse_config_yaml(source)
    path = tmp_path / "models.yaml"
    path.write_text(source)
    with pytest.raises(ValueError, match=detail):
        get_cached_yaml(str(path))


def test_config_update_rejects_alias_expansion_before_github(
    client: TestClient, mock_admin_allowlist: None
) -> None:
    response = client.post(
        "/api/config", json={"config_yaml": alias_document(16)}, headers=ADMIN_HEADERS
    )
    assert response.status_code == 400
    assert "expanded-node limit" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chunks,expected_status",
    [
        ([b'{"content":"sys', b'tem(foo)"}'], 400),
        ([b"12345678", b"9"], 413),
    ],
)
async def test_chat_proxy_preserves_chunk_validation(
    monkeypatch: pytest.MonkeyPatch, chunks: list[bytes], expected_status: int
) -> None:
    if expected_status == 413:
        monkeypatch.setattr("unified_api.main.MAX_BODY_SIZE", 8)
    messages = iter(chunks)
    deliveries = 0

    async def receive() -> Message:
        nonlocal deliveries
        try:
            chunk = next(messages)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}
        deliveries += 1
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def upstream(request: httpx.Request) -> httpx.Response:
        await request.aread()
        return event_response()

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [],
    }
    request = Request(scope, receive)
    settings = Settings(litellm_url="http://models.test:4321")
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        with pytest.raises(HTTPException) as error:
            await chat_proxy(request, "user@example.com", settings, client)
    assert error.value.status_code == expected_status
    assert deliveries == 2
