import asyncio
import base64
import logging
import os
import re
import time
import uuid
from typing import Any

import httpx
import yaml  # type: ignore
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

# Pre-compiled at module scope
MALICIOUS_REGEX = re.compile(r"\.\./|<script>|system\(|exec\(", re.IGNORECASE)
MAX_BODY_SIZE = 10 * 1024 * 1024

logger = logging.getLogger(__name__)

app = FastAPI(title="Unified API BFF")

http_client = httpx.AsyncClient(timeout=30.0)


@app.on_event("startup")
async def startup_event():
    pass


@app.on_event("shutdown")
async def shutdown_event():
    await http_client.aclose()


allowed_origins = os.environ.get(
    "ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_allowlist_path():
    return os.environ.get(
        "ADMIN_ALLOWLIST_PATH",
        "/etc/unified-api/allowlist.yaml",
    )


def get_config_path():
    return os.environ.get(
        "LITELLM_CONFIG_PATH",
        "/app/gateway/litellm_config.yaml"
        if os.path.exists("/app/gateway/litellm_config.yaml")
        else "gateway/litellm_config.yaml",
    )


LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm:4000")


@app.middleware("http")
async def verify_csrf_header(request: Request, call_next):
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
    request: Request, tailscale_user_login: str | None = Header(None)
) -> str:
    internal_user = os.environ.get("INTERNAL_SERVICE_USER", "orchestrator@internal")
    if tailscale_user_login == internal_user:
        auth_header = request.headers.get("Authorization", "")
        expected_token = os.environ.get("INTERNAL_TOKEN") or os.environ.get(
            "CHECKPOINT_AUTH_TOKEN"
        )
        if (not expected_token or auth_header != f"Bearer {expected_token}") and os.environ.get("ENV") != "dev":
                raise HTTPException(
                    status_code=401, detail="Invalid internal service token"
                )
        return internal_user

    if not tailscale_user_login:
        if os.environ.get("ENV") == "dev":
            return "dev@example.com"
        raise HTTPException(
            status_code=401, detail="Missing Tailscale-User-Login header"
        )
    return tailscale_user_login


_allowlist_cache: dict[str, Any] = {}
_ALLOWLIST_TTL = 300


def verify_admin(user: str = Depends(get_current_user)) -> str:
    global _allowlist_cache
    current_time = time.time()
    if (
        "data" in _allowlist_cache
        and current_time - _allowlist_cache["time"] < _ALLOWLIST_TTL
    ):
        allowlist = _allowlist_cache["data"]
    else:
        try:
            with open(get_allowlist_path(), "r") as f:
                allowlist = yaml.safe_load(f)
                _allowlist_cache = {"data": allowlist, "time": current_time}
        except FileNotFoundError:
            raise HTTPException(status_code=500, detail="Admin allowlist not found")

    if not isinstance(allowlist, dict):
        raise HTTPException(status_code=500, detail="Malformed Admin allowlist")

    if "data" in allowlist and "allowlist.yaml" in allowlist["data"]:
        sub_allowlist = yaml.safe_load(allowlist["data"]["allowlist.yaml"])
        if isinstance(sub_allowlist, dict):
            allowlist = sub_allowlist

    admins = allowlist.get("admins", [])
    if user not in admins:
        raise HTTPException(status_code=403, detail="Admin privileges required")

    return user


@app.post("/v1/chat/completions")
async def chat_proxy(request: Request, user: str = Depends(get_current_user)):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_BODY_SIZE:
                raise HTTPException(status_code=413, detail="Payload too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header")

    async def validated_stream():
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

    body = validated_stream()

    headers = dict(request.headers)
    for h in ["host", "content-length", "x-requested-with"]:
        headers.pop(h, None)
    headers["x-litellm-user"] = user

    req = http_client.build_request(
        method="POST",
        url=f"{LITELLM_URL}/v1/chat/completions",
        content=body,
        headers=headers,
    )

    response = await http_client.send(req, stream=True)

    async def stream_generator():
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            if hasattr(response, "aclose"):
                await response.aclose()

    return StreamingResponse(
        stream_generator(),
        status_code=response.status_code,
        headers={
            k: v
            for k, v in response.headers.items()
            if k.lower()
            not in ["content-encoding", "content-length", "transfer-encoding"]
        },
    )


class WorkflowRequest(BaseModel):
    name: str
    args: dict[str, Any]


@app.post("/api/workflows")
async def create_workflow(req: WorkflowRequest, user: str = Depends(get_current_user)):
    # Submit Temporal workflow (Stub)
    return {"id": "wf_12345", "status": "started", "user": user}


@app.get("/api/workflows/{workflow_id}/state")
async def get_workflow_state(workflow_id: str, user: str = Depends(get_current_user)):
    # Retrieve state (Stub)
    return {"id": workflow_id, "state": "running"}


@app.post("/api/workflows/{workflow_id}/signal")
async def signal_workflow(
    workflow_id: str, payload: dict, user: str = Depends(get_current_user)
):
    # Dispatch signal (Stub)
    return {"id": workflow_id, "signaled": True}


@app.get("/api/config")
async def get_config(user: str = Depends(verify_admin)):
    def _read_config():
        try:
            with open(get_config_path(), "r") as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            return {"model_list": []}
        except yaml.YAMLError:
            return {"model_list": []}

    return await asyncio.to_thread(_read_config)


class ConfigUpdateRequest(BaseModel):
    config_yaml: str


@app.post("/api/config")
async def update_config(req: ConfigUpdateRequest, user: str = Depends(verify_admin)):
    try:
        data = yaml.safe_load(req.config_yaml)
        if not isinstance(data, dict):
            raise TypeError("YAML must be a dictionary")
        if "model_list" not in data or not isinstance(data["model_list"], list):
            raise ValueError("Configuration must contain a valid 'model_list'")
    except (yaml.YAMLError, ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML provided: {e!s}")

    github_token = os.environ.get("GITHUB_TOKEN")

    if not github_token:
        raise HTTPException(status_code=500, detail="GITHUB_TOKEN not configured")

    repo = os.environ.get("GITHUB_REPO", "gordon-control-plane/platform")

    async with httpx.AsyncClient() as client:
        # Check for open config PRs
        headers = {
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        f"https://api.github.com/repos/{repo}/pulls?state=open&head={repo.split('/')[0]}:config-update-"

        config_prs = []
        for page in range(1, 10):
            page_url = f"https://api.github.com/repos/{repo}/pulls?state=open&per_page=100&page={page}"
            page_resp = await client.get(page_url, headers=headers)
            if page_resp.status_code != 200:
                break
            page_prs = page_resp.json()
            if not page_prs:
                break
            matching_prs = [
                pr
                for pr in page_prs
                if pr.get("head", {}).get("ref", "").startswith("config-update-")
            ]
            if matching_prs:
                config_prs.extend(matching_prs)
                break
            if len(page_prs) < 100:
                break

        if config_prs:
            raise HTTPException(
                status_code=409, detail="A configuration PR is already open"
            )

        # Real GitOps PR Implementation
        branch_name = f"config-update-{uuid.uuid4().hex[:8]}"

        # 1. Get default branch SHA
        repo_url = f"https://api.github.com/repos/{repo}"
        repo_info = await client.get(repo_url, headers=headers)
        if repo_info.status_code != 200:
            raise HTTPException(status_code=500, detail="Failed to fetch repo info")
        default_branch = repo_info.json().get("default_branch", "main")

        ref_url = f"https://api.github.com/repos/{repo}/git/refs/heads/{default_branch}"
        ref_resp = await client.get(ref_url, headers=headers)
        if ref_resp.status_code != 200:
            raise HTTPException(
                status_code=500, detail="Failed to fetch default branch"
            )
        base_sha = ref_resp.json()["object"]["sha"]

        # 2. Create new branch
        create_ref_url = f"https://api.github.com/repos/{repo}/git/refs"
        await client.post(
            create_ref_url,
            headers=headers,
            json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
        )

        # 3. Fetch existing file SHA
        file_path = "gateway/litellm_config.yaml"
        content_url = f"https://api.github.com/repos/{repo}/contents/{file_path}?ref={branch_name}"
        content_resp = await client.get(content_url, headers=headers)
        file_sha = None
        if content_resp.status_code == 200:
            file_sha = content_resp.json()["sha"]

        # 4. Update file
        content_b64 = base64.b64encode(req.config_yaml.encode("utf-8")).decode("utf-8")
        update_data = {
            "message": "chore(config): update litellm configuration via Unified API",
            "content": content_b64,
            "branch": branch_name,
        }
        if file_sha:
            update_data["sha"] = file_sha

        update_resp = await client.put(content_url, headers=headers, json=update_data)
        if update_resp.status_code not in [200, 201]:
            raise HTTPException(status_code=500, detail="Failed to commit file update")

        # 5. Create PR
        pr_url = f"https://api.github.com/repos/{repo}/pulls"
        pr_resp = await client.post(
            pr_url,
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

        return {"status": "pr_created", "url": pr_resp.json()["html_url"]}


@app.get("/health")
async def health():
    return {"status": "ok"}
