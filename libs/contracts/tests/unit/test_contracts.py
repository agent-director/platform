import pytest
from pydantic import ValidationError

from contracts import HealthResponse, HydrateRequest, HydrateResponse


def test_health_response_defaults_and_round_trip() -> None:
    health = HealthResponse()
    assert health.status == "ok"
    assert health.service == "sandbox-exec"
    restored = HealthResponse.model_validate_json(health.model_dump_json())
    assert restored == health

    schema = HealthResponse.model_json_schema()
    assert "status" in schema["properties"]
    assert "service" in schema["properties"]


def test_health_response_rejects_invalid_status_extra_and_mutation() -> None:
    with pytest.raises(ValidationError):
        HealthResponse.model_validate({"status": "degraded"})
    with pytest.raises(ValidationError):
        HealthResponse.model_validate({"status": "ok", "unexpected": "field"})
    health = HealthResponse()
    with pytest.raises(ValidationError):
        health.__setattr__("service", "other")


def test_hydrate_request_validation_and_schema() -> None:
    req = HydrateRequest(
        workspace_id="ws-123",
        repo_url="https://github.com/agent-director/platform.git",
    )
    assert req.ref == "main"
    restored = HydrateRequest.model_validate_json(req.model_dump_json())
    assert restored == req

    schema = HydrateRequest.model_json_schema()
    assert "workspace_id" in schema["properties"]
    assert "repo_url" in schema["properties"]
    assert "ref" in schema["properties"]

    with pytest.raises(ValidationError):
        HydrateRequest(workspace_id="", repo_url="https://example.com/repo.git")
    with pytest.raises(ValidationError):
        HydrateRequest(workspace_id="ws-123", repo_url="")
    with pytest.raises(ValidationError):
        HydrateRequest(
            workspace_id="ws-123",
            repo_url="https://example.com/repo.git",
            ref="",
        )
    with pytest.raises(ValidationError):
        HydrateRequest.model_validate(
            {
                "workspace_id": "ws-123",
                "repo_url": "https://example.com/repo.git",
                "extra_field": True,
            }
        )


@pytest.mark.parametrize(
    ("kwargs"),
    [
        {"workspace_id": "../../etc/passwd", "repo_url": "https://example.com/r.git"},
        {"workspace_id": "   ", "repo_url": "https://example.com/r.git"},
        {"workspace_id": ".hidden", "repo_url": "https://example.com/r.git"},
        {"workspace_id": "ws-123", "repo_url": "-oProxyCommand=evil"},
        {"workspace_id": "ws-123", "repo_url": "   "},
        {"workspace_id": "ws-123", "repo_url": "https://example.com/..\n/r.git"},
        {"workspace_id": "ws-123", "repo_url": "https://example.com/../r.git"},
        {
            "workspace_id": "ws-123",
            "repo_url": "https://example.com/r.git",
            "ref": "--upload-pack=evil",
        },
        {
            "workspace_id": "ws-123",
            "repo_url": "https://example.com/r.git",
            "ref": "   ",
        },
        {
            "workspace_id": "ws-123",
            "repo_url": "https://example.com/r.git",
            "ref": "refs/heads/../main",
        },
    ],
)
def test_hydrate_request_rejects_traversal_and_option_injection(
    kwargs: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        HydrateRequest.model_validate(kwargs)


def test_hydrate_response_round_trip_and_schema() -> None:
    resp = HydrateResponse(
        workspace_id="ws-123",
        hydrated=True,
        checkout_path="/workspace/ws-123",
    )
    restored = HydrateResponse.model_validate_json(resp.model_dump_json())
    assert restored == resp

    schema = HydrateResponse.model_json_schema()
    assert set(schema["required"]) == {"workspace_id", "hydrated", "checkout_path"}

    with pytest.raises(ValidationError):
        HydrateResponse.model_validate(
            {
                "workspace_id": "ws-123",
                "hydrated": True,
                "checkout_path": "/workspace/ws-123",
                "extra": "forbidden",
            }
        )
