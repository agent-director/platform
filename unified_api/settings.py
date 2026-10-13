"""Validated, injectable configuration for the Unified API."""

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, field_validator


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    litellm_url: str
    litellm_config_path: Path = Path("/app/unified_api/litellm_config.yaml")
    admin_allowlist_path: Path = Path("/etc/unified-api/allowlist.yaml")
    allowed_origins: tuple[str, ...] = (
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    )
    environment: str = "production"
    internal_service_user: str = "orchestrator@internal"
    internal_token: str | None = None
    github_token: str | None = None
    github_repo: str = "agent-director/platform"
    github_config_path: str = "unified_api/litellm_config.yaml"

    @field_validator("litellm_url")
    @classmethod
    def validate_litellm_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if (
            any(char.isspace() for char in value)
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "?" in value
            or "#" in value
            or parsed.path not in {"", "/v1"}
        ):
            raise ValueError(
                "LITELLM_URL must be an HTTP(S) service URL, optionally ending in /v1"
            )
        # Accessing port also validates malformed/out-of-range ports.
        if parsed.port == 0:
            raise ValueError("LITELLM_URL port must be greater than zero")
        try:
            return str(httpx.URL(value).copy_with(path=""))
        except httpx.InvalidURL as exc:
            raise ValueError("LITELLM_URL must be a valid HTTP(S) service URL") from exc

    @field_validator("allowed_origins")
    @classmethod
    def validate_origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            parsed = urlsplit(value)
            if (
                any(char.isspace() for char in value)
                or parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.path not in {"", "/"}
                or "?" in value
                or "#" in value
                or parsed.username is not None
            ):
                raise ValueError(
                    "ALLOWED_ORIGINS must contain explicit HTTP(S) origins"
                )
            if parsed.port == 0:
                raise ValueError("ALLOWED_ORIGINS port must be greater than zero")
        try:
            return tuple(str(httpx.URL(value).copy_with(path="")) for value in values)
        except httpx.InvalidURL as exc:
            raise ValueError(
                "ALLOWED_ORIGINS must contain valid HTTP(S) origins"
            ) from exc

    @classmethod
    def origins_from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> tuple[str, ...]:
        env = os.environ if environ is None else environ
        return cls.validate_origins(
            tuple(
                value.strip()
                for value in env.get(
                    "ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
                ).split(",")
                if value.strip()
            )
        )

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        endpoint = env.get("LITELLM_URL")
        if not endpoint:
            raise ValueError("LITELLM_URL is required")
        origins = cls.origins_from_environment(env)
        return cls(
            litellm_url=endpoint,
            litellm_config_path=Path(
                env.get("LITELLM_CONFIG_PATH", "/app/unified_api/litellm_config.yaml")
            ),
            admin_allowlist_path=Path(
                env.get("ADMIN_ALLOWLIST_PATH", "/etc/unified-api/allowlist.yaml")
            ),
            allowed_origins=origins,
            environment=env.get("ENV", "production"),
            internal_service_user=env.get(
                "INTERNAL_SERVICE_USER", "orchestrator@internal"
            ),
            internal_token=env.get("INTERNAL_TOKEN")
            or env.get("CHECKPOINT_AUTH_TOKEN"),
            github_token=env.get("GITHUB_TOKEN"),
            github_repo=env.get("GITHUB_REPO", "agent-director/platform"),
            github_config_path=env.get(
                "GITHUB_CONFIG_PATH", "unified_api/litellm_config.yaml"
            ),
        )
