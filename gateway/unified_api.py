import asyncio
import base64
import copy
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
import yaml  # type: ignore
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from gateway.shunt_middleware import apply_shunt_middleware

http_client = httpx.AsyncClient(timeout=30.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await http_client.aclose()


app = FastAPI(title="Unified API BFF", lifespan=lifespan)


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
    path = os.environ.get("ADMIN_ALLOWLIST_PATH")
    if path:
        return path
    if os.path.exists("/etc/unified-api/allowlist.yaml"):
        return "/etc/unified-api/allowlist.yaml"
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "..", "admin-allowlist.yaml")


def get_config_path():
    path = os.environ.get("LITELLM_CONFIG_PATH")
    if path:
        return path
    if os.path.exists("/app/gateway/litellm_config.yaml"):
        return "/app/gateway/litellm_config.yaml"
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "litellm_config.yaml")


_CONFIG_TTL = 300


LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm:4000")


@app.middleware("http")
async def verify_csrf_header(request: Request, call_next):
    if request.method in ["POST", "PUT", "PATCH", "DELETE"]:
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "CSRF verification failed: Missing X-Requested-With header"
                },
            )
    return await call_next(request)


async def get_current_user(tailscale_user_login: str | None = Header(None)) -> str:
    if not tailscale_user_login:
        if os.environ.get("ENV") == "dev":
            return "dev@example.com"
        raise HTTPException(
            status_code=401, detail="Missing Tailscale-User-Login header"
        )
    return tailscale_user_login


class AsyncTTLCache:
    def __init__(self, ttl: int):
        self.ttl = ttl
        self._cache = {}
        self._lock = asyncio.Lock()

    async def get_or_load(self, loader):
        if (
            "data" in self._cache
            and time.time() - self._cache.get("time", 0) < self.ttl
        ):
            return copy.deepcopy(self._cache["data"])

        async with self._lock:
            if (
                "data" in self._cache
                and time.time() - self._cache.get("time", 0) < self.ttl
            ):
                return copy.deepcopy(self._cache["data"])

            data = await loader()
            self._cache["data"] = data
            self._cache["time"] = time.time()
            return copy.deepcopy(data)


_allowlist_cache = AsyncTTLCache(ttl=_CONFIG_TTL)
_config_cache = AsyncTTLCache(ttl=_CONFIG_TTL)


async def get_admin_allowlist() -> dict[str, Any]:
    # Returning mapping proxy or deepcopy ensures callers cannot mutate the cache
    async def _load_allowlist():
        try:

            def _read_allowlist():
                with open(get_allowlist_path(), "r") as f:
                    return yaml.safe_load(f)

            allowlist = await asyncio.to_thread(_read_allowlist)
        except FileNotFoundError:
            raise HTTPException(status_code=500, detail="Admin allowlist not found")
        except yaml.YAMLError:
            raise HTTPException(status_code=500, detail="Malformed Admin allowlist")

        if not isinstance(allowlist, dict):
            raise HTTPException(status_code=500, detail="Malformed Admin allowlist")

        if "data" in allowlist and "allowlist.yaml" in allowlist["data"]:
            sub_allowlist = yaml.safe_load(allowlist["data"]["allowlist.yaml"])
            if isinstance(sub_allowlist, dict):
                allowlist = sub_allowlist

        return allowlist

    return await _allowlist_cache.get_or_load(_load_allowlist)


async def verify_admin(
    allowlist: dict = Depends(get_admin_allowlist),
    user: str = Depends(get_current_user),
) -> str:
    admins = allowlist.get("admins", [])
    if user not in admins:
        raise HTTPException(status_code=403, detail="Admin privileges required")

    return user


@app.post("/v1/chat/completions")
async def chat_proxy(request: Request, user: str = Depends(get_current_user)):
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Payload too large")
    raw_body = await request.body()
    if len(raw_body) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Payload too large")

    body = await asyncio.to_thread(apply_shunt_middleware, raw_body, user)

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
    async def _load_config():
        def _read_config():
            try:
                with open(get_config_path(), "r") as f:
                    data = yaml.safe_load(f) or {}
                    return data
            except FileNotFoundError:
                return {"model_list": []}
            except yaml.YAMLError:
                return {"model_list": []}

        return await asyncio.to_thread(_read_config)

    return await _config_cache.get_or_load(_load_config)


class ConfigUpdateRequest(BaseModel):
    config_yaml: str


@app.post("/api/config")
async def update_config(req: ConfigUpdateRequest, user: str = Depends(verify_admin)):
    try:
        data = yaml.safe_load(req.config_yaml)
        if not isinstance(data, dict):
            raise ValueError("YAML must be a dictionary")
        if "model_list" not in data or not isinstance(data["model_list"], list):
            raise ValueError("Configuration must contain a valid 'model_list'")
    except (yaml.YAMLError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML provided: {e!s}")

    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        raise HTTPException(status_code=500, detail="GITHUB_TOKEN not configured")

    repo = os.environ.get("GITHUB_REPO", "agent-director/platform")

    async with httpx.AsyncClient() as client:
        # Check for open config PRs
        headers = {
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        prs_url = f"https://api.github.com/repos/{repo}/pulls?state=open"
        resp = await client.get(prs_url, headers=headers)

        if resp.status_code == 200:
            prs = resp.json()
            config_prs = [
                pr
                for pr in prs
                if pr.get("head", {}).get("ref", "").startswith("config-update-")
            ]
            if config_prs:
                raise HTTPException(
                    status_code=409, detail="A configuration PR is already open"
                )

        # Real GitOps PR Implementation
        import uuid

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
