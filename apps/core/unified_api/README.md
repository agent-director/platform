# Unified API

The `unified_api` package (`apps/core/unified_api/` in the `agent-director-core` workspace package) is the platform's FastAPI backend-for-frontend. Its ASGI entrypoint is `unified_api.main:app`.

## Current behavior

- `/v1/chat/completions` streams requests to the configured LiteLLM service and closes upstream responses after streaming.
- `/api/config` reads the mounted LiteLLM configuration for allowlisted administrators. Configuration updates propose a GitHub pull request using `GITHUB_TOKEN`, `GITHUB_REPO` and `GITHUB_CONFIG_PATH`; a downstream GitOps integration must consume that file.
- `/api/workflows`, workflow state and signal endpoints return placeholder responses. They do not submit or query Temporal workflows.
- `/health` reports API process health, not backing-service readiness.

## Configuration

`unified_api.settings.Settings` validates configuration; `create_app(settings)` and the `get_settings` dependency support explicit injection in tests and embedding applications. Without injection, startup requires `LITELLM_URL`. There is no built-in cluster hostname.

| Variable | Contract |
|---|---|
| `LITELLM_URL` | Required HTTP(S) service URL; optional `/v1` suffix is normalized |
| `UVICORN_PORT` | Listener port consumed by Uvicorn; Helm injects the matching Service port |
| `LITELLM_CONFIG_PATH` | Mounted configuration path, default `/app/unified_api/litellm_config.yaml` |
| `ADMIN_ALLOWLIST_PATH` | Default `/etc/unified-api/allowlist.yaml`; YAML `admins` list |
| `ALLOWED_ORIGINS` | Comma-separated explicit HTTP(S) origins; local frontend origins by default |
| `INTERNAL_SERVICE_USER`, `INTERNAL_TOKEN` | Internal identity and its required bearer token outside `ENV=dev` |
| `GITHUB_TOKEN`, `GITHUB_REPO`, `GITHUB_CONFIG_PATH` | Config-update credentials/destination; default repo-relative path `apps/core/unified_api/litellm_config.yaml` |

Helm owns in-cluster names, namespace and ports and injects endpoints. Local execution uses the host endpoints from `.env.example`, for example `mise exec -- uv run --locked uvicorn unified_api.main:app --host 127.0.0.1` after setting `LITELLM_URL`. Unit tests live in `apps/core/tests/unit/test_unified_api.py` and run via `make pytest-unit` or `mise exec -- uv run --locked pytest apps/core/tests/unit/test_unified_api.py`.

## Security boundary

The ingress must overwrite `Tailscale-User-Login` with the authenticated identity. Direct public access would allow identity spoofing; the API assumes that trusted ingress boundary. State-changing routes require `X-Requested-With: XMLHttpRequest`; administrator routes additionally check the configured allowlist. YAML is parsed safely and validated as JSON-compatible configuration, with a 10,000-expanded-node budget and a nesting limit of 64. Small alias and merge graphs are supported; recursive or over-budget graphs are rejected before YAML merge construction and JSON conversion. The existing input blocklist is not a complete prompt-injection defense or a claim of tenant-isolated workflow state.
