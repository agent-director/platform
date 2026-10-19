"""Shared Pydantic v2 contract schemas across Agent Director services."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ok"] = "ok"
    service: str = "sandbox-exec"


class HydrateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    repo_url: str = Field(min_length=1)
    ref: str = Field(default="main", min_length=1)

    @field_validator("repo_url", "ref")
    @classmethod
    def _validate_git_argument(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Value must not be blank or whitespace-only")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("Value must not contain control characters")
        if value.lstrip().startswith("-"):
            raise ValueError("Value must not start with '-'")
        if "\\" in value or ".." in value.split("/"):
            raise ValueError("Value must not contain path traversal segments")
        return value


class HydrateResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: str
    hydrated: bool
    checkout_path: str
