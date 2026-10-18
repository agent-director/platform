"""Unified API BFF; workflow endpoints currently return placeholder responses."""

import base64
import re
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, cast

import httpx
import yaml
from cachetools.func import ttl_cache
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, JsonValue, TypeAdapter
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from unified_api.settings import Settings

MALICIOUS_REGEX = re.compile(r"\.\./|<script>|system\(|exec\(", re.IGNORECASE)
MAX_BODY_SIZE = 10 * 1024 * 1024
MAX_YAML_NODES = 10_000
MAX_YAML_DEPTH = 64
JSON_VALUE = TypeAdapter(JsonValue)
router = APIRouter()


def get_settings() -> Settings:
    return Settings.from_environment()


def get_http_client(request: Request) -> httpx.AsyncClient:
    return cast(httpx.AsyncClient, request.app.state.http_client)


async def verify_csrf_header(
    request: Request, call_next: RequestResponseEndpoint
) -> Response:
    if (
        request.method in ["POST", "PUT", "PATCH", "DELETE"]
        and request.headers.get("X-Requested-With") != "XMLHttpRequest"
    ):
        return JSONResponse(
            status_code=403,
            content={
                "detail": "CSRF verification failed: Missing X-Requested-With header"
            },
        )
    return await call_next(request)


def get_current_user(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    tailscale_user_login: Annotated[str | None, Header()] = None,
) -> str:
    if tailscale_user_login == settings.internal_service_user:
        auth_header = request.headers.get("Authorization", "")
        if (
            not settings.internal_token
            or auth_header != f"Bearer {settings.internal_token}"
        ) and settings.environment != "dev":
            raise HTTPException(
                status_code=401, detail="Invalid internal service token"
            )
        return settings.internal_service_user
    if not tailscale_user_login:
        if settings.environment == "dev":
            return "dev@example.com"
        raise HTTPException(
            status_code=401, detail="Missing Tailscale-User-Login header"
        )
    return tailscale_user_login


def parse_config_yaml(source: str) -> JsonValue:
    """Bound the composed alias graph before merge construction or JSON conversion."""
    memo: dict[int, tuple[int, int]] = {}
    active: set[int] = set()

    def measure(node: Node, depth: int) -> tuple[int, int]:
        if depth > MAX_YAML_DEPTH:
            raise ValueError("YAML nesting limit exceeded")
        if isinstance(node, ScalarNode):
            return 1, 0
        identity = id(node)
        if identity in active:
            raise ValueError("Recursive YAML aliases are not supported")
        if identity in memo:
            count, height = memo[identity]
            if depth + height > MAX_YAML_DEPTH:
                raise ValueError("YAML nesting limit exceeded")
            return count, height
        if isinstance(node, MappingNode):
            if len(node.value) * 2 > MAX_YAML_NODES:
                raise ValueError("YAML expanded-node limit exceeded")
            children = [child for pair in node.value for child in pair]
        elif isinstance(node, SequenceNode):
            children = node.value
        else:
            raise TypeError("Unsupported YAML node")
        if len(children) > MAX_YAML_NODES:
            raise ValueError("YAML expanded-node limit exceeded")
        active.add(identity)
        count = 1
        height = 0
        for child in children:
            child_count, child_height = measure(child, depth + 1)
            count += child_count
            height = max(height, child_height + 1)
            if count > MAX_YAML_NODES:
                raise ValueError("YAML expanded-node limit exceeded")
        active.remove(identity)
        memo[identity] = count, height
        return count, height

    loader = yaml.SafeLoader(source)
    try:
        node = loader.get_single_node()
        if node is None:
            return None
        measure(node, 0)
        return JSON_VALUE.validate_python(loader.construct_document(node), strict=True)
    except RecursionError as exc:
        raise ValueError("YAML nesting limit exceeded") from exc
    finally:
        loader.dispose()


@ttl_cache(maxsize=10, ttl=300.0)
def get_cached_yaml(path: str) -> JsonValue:
    with open(path) as file:
        return parse_config_yaml(file.read())


def get_admin_allowlist(
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, JsonValue]:
    try:
        allowlist = get_cached_yaml(str(settings.admin_allowlist_path))
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=500, detail="Admin allowlist not found"
        ) from exc
    except (yaml.YAMLError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=500, detail="Malformed Admin allowlist"
        ) from exc
    if not isinstance(allowlist, dict) or not isinstance(allowlist.get("admins"), list):
        raise HTTPException(status_code=500, detail="Malformed Admin allowlist")
    return allowlist


def verify_admin(
    allowlist: Annotated[dict[str, JsonValue], Depends(get_admin_allowlist)],
    user: Annotated[str, Depends(get_current_user)],
) -> str:
    admins = allowlist.get("admins", [])
    if not isinstance(admins, list) or user not in admins:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return user


@router.post("/v1/chat/completions")
async def chat_proxy(
    request: Request,
    user: Annotated[str, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> StreamingResponse:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_BODY_SIZE:
                raise HTTPException(status_code=413, detail="Payload too large")
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid Content-Length header"
            ) from exc

    async def validated_stream() -> AsyncIterator[bytes]:
        total_bytes = 0
        buffer = ""
        async for chunk in request.stream():
            total_bytes += len(chunk)
            if total_bytes > MAX_BODY_SIZE:
                raise HTTPException(status_code=413, detail="Payload too large")
            buffer += chunk.decode("utf-8", errors="ignore")
            if MALICIOUS_REGEX.search(buffer):
                raise HTTPException(status_code=400, detail="Malicious input detected.")
            buffer = buffer[-20:]
            yield chunk

    headers = dict(request.headers)
    for header in ["host", "content-length", "x-requested-with"]:
        headers.pop(header, None)
    headers["x-litellm-user"] = user
    upstream_request = client.build_request(
        method="POST",
        url=f"{settings.litellm_url}/v1/chat/completions",
        content=validated_stream(),
        headers=headers,
    )
    response = await client.send(upstream_request, stream=True)

    async def stream_generator() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()

    return StreamingResponse(
        stream_generator(),
        status_code=response.status_code,
        headers={
            key: value
            for key, value in response.headers.items()
            if key.lower()
            not in ["content-encoding", "content-length", "transfer-encoding"]
        },
    )


class WorkflowRequest(BaseModel):
    name: str
    args: dict[str, JsonValue]


@router.post("/api/workflows")
async def create_workflow(
    req: WorkflowRequest, user: Annotated[str, Depends(get_current_user)]
) -> dict[str, str]:
    # Placeholder only: this does not submit a Temporal workflow.
    return {"id": "wf_12345", "status": "started", "user": user}


@router.get("/api/workflows/{workflow_id}/state")
async def get_workflow_state(
    workflow_id: str, user: Annotated[str, Depends(get_current_user)]
) -> dict[str, str]:
    return {"id": workflow_id, "state": "running"}


@router.post("/api/workflows/{workflow_id}/signal")
async def signal_workflow(
    workflow_id: str,
    payload: dict[str, JsonValue],
    user: Annotated[str, Depends(get_current_user)],
) -> dict[str, str | bool]:
    return {"id": workflow_id, "signaled": True}


@router.get("/api/config")
def get_config(
    user: Annotated[str, Depends(verify_admin)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> JsonValue:
    try:
        return get_cached_yaml(str(settings.litellm_config_path)) or {}
    except (FileNotFoundError, yaml.YAMLError, ValueError, TypeError):
        return {"model_list": []}


class ConfigUpdateRequest(BaseModel):
    config_yaml: str


class PullHead(BaseModel):
    ref: str = ""


class PullInfo(BaseModel):
    head: PullHead


class RepoInfo(BaseModel):
    default_branch: str = "main"


class GitObject(BaseModel):
    sha: str


class GitRef(BaseModel):
    object: GitObject


class PullCreated(BaseModel):
    html_url: str


@router.post("/api/config")
async def update_config(
    req: ConfigUpdateRequest,
    user: Annotated[str, Depends(verify_admin)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    try:
        data = parse_config_yaml(req.config_yaml)
        if not isinstance(data, dict):
            raise TypeError("YAML must be a dictionary")
        if "model_list" not in data or not isinstance(data["model_list"], list):
            raise ValueError("Configuration must contain a valid 'model_list'")
    except (yaml.YAMLError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid YAML provided: {exc!s}"
        ) from exc
    if not settings.github_token:
        raise HTTPException(status_code=500, detail="GITHUB_TOKEN not configured")
    repo = settings.github_repo
    async with httpx.AsyncClient() as client:
        headers = {
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        for page in range(1, 10):
            page_url = f"https://api.github.com/repos/{repo}/pulls?state=open&per_page=100&page={page}"
            page_resp = await client.get(page_url, headers=headers)
            if page_resp.status_code != 200:
                raise HTTPException(
                    status_code=500, detail="Failed to fetch open configuration PRs"
                )
            page_prs = TypeAdapter(list[PullInfo]).validate_python(page_resp.json())
            if any(pr.head.ref.startswith("config-update-") for pr in page_prs):
                raise HTTPException(
                    status_code=409, detail="A configuration PR is already open"
                )
            if len(page_prs) < 100:
                break
        branch_name = f"config-update-{uuid.uuid4().hex[:8]}"
        repo_url = f"https://api.github.com/repos/{repo}"
        repo_info = await client.get(repo_url, headers=headers)
        if repo_info.status_code != 200:
            raise HTTPException(status_code=500, detail="Failed to fetch repo info")
        default_branch = RepoInfo.model_validate(repo_info.json()).default_branch
        ref_url = f"https://api.github.com/repos/{repo}/git/refs/heads/{default_branch}"
        ref_resp = await client.get(ref_url, headers=headers)
        if ref_resp.status_code != 200:
            raise HTTPException(
                status_code=500, detail="Failed to fetch default branch"
            )
        base_sha = GitRef.model_validate(ref_resp.json()).object.sha
        create_ref_url = f"https://api.github.com/repos/{repo}/git/refs"
        create_ref_resp = await client.post(
            create_ref_url,
            headers=headers,
            json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
        )
        if create_ref_resp.status_code != 201:
            raise HTTPException(
                status_code=500, detail="Failed to create configuration branch"
            )
        content_url = f"https://api.github.com/repos/{repo}/contents/{settings.github_config_path}?ref={branch_name}"
        content_resp = await client.get(content_url, headers=headers)
        update_data = {
            "message": "chore(config): update litellm configuration via Unified API",
            "content": base64.b64encode(req.config_yaml.encode()).decode(),
            "branch": branch_name,
        }
        if content_resp.status_code == 200:
            update_data["sha"] = GitObject.model_validate(content_resp.json()).sha
        elif content_resp.status_code != 404:
            raise HTTPException(
                status_code=500, detail="Failed to fetch configuration file"
            )
        update_resp = await client.put(content_url, headers=headers, json=update_data)
        if update_resp.status_code not in [200, 201]:
            raise HTTPException(status_code=500, detail="Failed to commit file update")
        pr_resp = await client.post(
            f"https://api.github.com/repos/{repo}/pulls",
            headers=headers,
            json={
                "title": "Config: Update LiteLLM Configuration",
                "body": "Automated configuration update generated from the Unified API dashboard.",
                "head": branch_name,
                "base": default_branch,
            },
        )
        if pr_resp.status_code != 201:
            raise HTTPException(status_code=500, detail="Failed to create Pull Request")
        return {
            "status": "pr_created",
            "url": PullCreated.model_validate(pr_resp.json()).html_url,
        }


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def configured_cors(application: ASGIApp, settings: Settings | None) -> ASGIApp:
    # Starlette constructs middleware at ASGI startup, not module import.
    origins = (
        settings.allowed_origins
        if settings is not None
        else Settings.origins_from_environment()
    )
    return CORSMiddleware(
        application,
        allow_origins=list(origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
        # Fail startup for missing/invalid endpoints rather than inventing cluster names.
        if settings is None:
            Settings.from_environment()
        async with httpx.AsyncClient(timeout=30.0) as client:
            application.state.http_client = client
            yield

    application = FastAPI(title="Unified API BFF", lifespan=lifespan)
    if settings is not None:
        application.dependency_overrides[get_settings] = lambda: settings
    application.add_middleware(configured_cors, settings=settings)
    application.middleware("http")(verify_csrf_header)
    application.include_router(router)
    return application


app = create_app()
