import json
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import cast

import pytest
from robyn import Request, Response

from contracts import HealthResponse, HydrateResponse
from sandbox_exec.daemon import (
    build_readyz_response,
    handle_hydrate,
    hydrate,
    readyz,
    verify_bearer_token,
)


def test_verify_bearer_token_fails_closed_on_empty_or_whitespace_token() -> None:
    assert not verify_bearer_token("Bearer secret-token", "")
    assert not verify_bearer_token("Bearer secret-token", "   ")
    assert not verify_bearer_token("Bearer   ", "secret-token")
    assert not verify_bearer_token("Bearer ", "secret-token")
    assert not verify_bearer_token("Bearer", "secret-token")
    assert not verify_bearer_token("bearer secret-token", "secret-token")
    assert not verify_bearer_token(None, "secret-token")
    assert not verify_bearer_token("Basic secret-token", "secret-token")


def test_verify_bearer_token_constant_time_match() -> None:
    assert verify_bearer_token("Bearer valid-token-123", "valid-token-123")
    assert verify_bearer_token("Bearer valid-token-123", "  valid-token-123\n")
    assert not verify_bearer_token("Bearer wrong-token", "valid-token-123")


@pytest.mark.asyncio
async def test_readyz_returns_health_response() -> None:
    response = build_readyz_response()
    assert response.status_code == 200
    parsed = HealthResponse.model_validate_json(response.description)
    assert parsed.status == "ok"
    assert parsed.service == "sandbox-exec"

    readyz_fn = cast(Callable[[], Awaitable[Response]], readyz)
    route_response = await readyz_fn()
    assert route_response.status_code == 200
    assert HealthResponse.model_validate_json(route_response.description) == parsed


def test_hydrate_rejects_unauthenticated_or_unset_token() -> None:
    body = json.dumps(
        {"workspace_id": "ws-1", "repo_url": "https://example.com/repo.git"}
    )
    status_missing_env, _ = handle_hydrate(
        body,
        {"Authorization": "Bearer secret"},
        environ={},
    )
    assert status_missing_env == 401

    status_whitespace_env, _ = handle_hydrate(
        body,
        {"Authorization": "Bearer secret"},
        environ={"SANDBOX_AUTH_TOKEN": "   "},
    )
    assert status_whitespace_env == 401

    status_bad_token, _ = handle_hydrate(
        body,
        {"Authorization": "Bearer wrong"},
        environ={"SANDBOX_AUTH_TOKEN": "secret"},
    )
    assert status_bad_token == 401

    status_no_header, _ = handle_hydrate(
        body,
        {},
        environ={"SANDBOX_AUTH_TOKEN": "secret"},
    )
    assert status_no_header == 401


@pytest.mark.asyncio
async def test_hydrate_validates_request_and_returns_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = {"SANDBOX_AUTH_TOKEN": "sandbox-secret-token"}
    headers = {"Authorization": "Bearer sandbox-secret-token"}

    bad_status, _ = handle_hydrate('{"workspace_id": ""}', headers, environ=env)
    assert bad_status == 422

    malformed_status, _ = handle_hydrate(b"{not-valid-json", headers, environ=env)
    assert malformed_status == 422

    valid_body = json.dumps(
        {
            "workspace_id": "ws-alpha",
            "repo_url": "https://github.com/agent-director/platform.git",
            "ref": "main",
        }
    )
    ok_status, payload = handle_hydrate(
        valid_body.encode("utf-8"),
        {"authorization": "Bearer sandbox-secret-token"},
        environ=env,
    )
    assert ok_status == 200
    hydrated = HydrateResponse.model_validate_json(payload)
    assert hydrated.workspace_id == "ws-alpha"
    assert hydrated.hydrated is True
    assert hydrated.checkout_path == "/workspace/ws-alpha"

    monkeypatch.setenv("SANDBOX_AUTH_TOKEN", "sandbox-secret-token")
    fake_request = cast(
        Request,
        SimpleNamespace(
            headers={"authorization": "Bearer sandbox-secret-token"},
            body=valid_body,
        ),
    )
    hydrate_fn = cast(Callable[[Request], Awaitable[Response]], hydrate)
    route_response = await hydrate_fn(fake_request)
    assert route_response.status_code == 200
    assert HydrateResponse.model_validate_json(route_response.description) == hydrated
