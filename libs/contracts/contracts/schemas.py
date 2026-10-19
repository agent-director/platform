"""Shared Pydantic v2 contract schemas across Agent Director services."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ok"] = "ok"
    service: str = "sandbox-exec"


class HydrateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: str = Field(min_length=1)
    repo_url: str = Field(min_length=1)
    ref: str = Field(default="main", min_length=1)


class HydrateResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: str
    hydrated: bool
    checkout_path: str
