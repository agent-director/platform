# Agent Director

Agent Director is a Kubernetes-hosted control-plane prototype with a FastAPI Unified API, Temporal workers, a Next.js frontend, a LiteLLM proxy and Agent Substrate integration.

The API currently proxies model requests and manages its existing configuration surface. Workflow HTTP endpoints still return prototype responses; the integration suite separately exercises a real workflow through the Temporal SDK and deployed worker. Neither those stubs nor chart rendering establishes a completed orchestration product or production authentication rollout.

## Repository layout

| Path | Responsibility |
|---|---|
| `apps/core/` | Control-plane Python package (`agent-director-core`): FastAPI `unified_api` (`unified_api.main:app`), Temporal `orchestrator` workflows/activities/worker, `director` CLI, and unit tests (`apps/core/tests/unit/`). |
| `apps/ui/` | Next.js 16 application (`platform-ui` in the root `pnpm` workspace), Biome (`lint/security`) configuration, and MSW contract mocks & tests (`apps/ui/src/mocks/`). |
| `apps/sandbox-exec/` | Isolated Robyn (`robyn==0.88.0`) in-sandbox execution daemon (`agent-director-sandbox-exec`) with constant-time Bearer token authentication (`SANDBOX_AUTH_TOKEN`) and unit tests (`apps/sandbox-exec/tests/unit/`). |
| `libs/contracts/` | Shared Pydantic v2 contract schemas (`agent-director-contracts`) and unit tests (`libs/contracts/tests/unit/`). |
| `tools/scripts/` | Repository operational scripts, CRD/schema maintenance (`update-crds.py`), image discovery (`images.py`), container vulnerability scanning (`scan-images.sh`), Graphify bundle hydration (`fetch-graphify-brain.sh`), and guarded local kind lifecycle (`deploy.sh`, `teardown.sh`, `test-integration.sh`). |
| `charts/` | `charts/platform/` (application and backing-service chart with full and kind Temporal profiles) and `charts/agent-substrate/` (Agent Substrate v0.4.0 deployment and connection contracts). |
| `images/` | Multi-stage runtime Dockerfiles isolated per `uv`/`pnpm` workspace member. |
| `tests/` | Cross-cutting repository contract unit tests (`tests/unit/`) and live kind integration suite (`tests/integration/`). |

See [architecture.md](architecture.md) for workspace boundaries, import enforcement, and current runtime limitations.

## Toolchain and dependencies

`mise.toml` and `mise.lock` select the toolchain (including `biome`) and `.cache` environment paths (`NX_NO_CLOUD=true`, `NX_DAEMON=false`). `uv` owns Python workspace resolution (`apps/core`, `apps/sandbox-exec`, `libs/contracts`) and the `graphify` tool group (`graphifyy[sql]==0.9.92`) in `pyproject.toml` and `uv.lock`; `pnpm` owns the root workspace (`package.json`, `pnpm-workspace.yaml`, `pnpm-lock.yaml`) and `apps/ui/`; Nx (`nx.json` and 7 `project.json` descriptors) orchestrates polyglot task caching; and Graphify generates AST code-intelligence graphs (with optional `GRAPHIFY_BACKEND` LLM semantic extraction and community naming when an LLM provider is configured).

From the checkout root:

```sh
make setup
make typecheck
make build-frontend
make format
make test
make pytest-unit
make lint
make nx-graph
make nx-affected
make graphify
make graphify-fetch
```

`make format` invokes the pinned Prek formatter only. `prek.toml` defines 8 `pre-commit-hooks` and 16 `local` hooks (24 configured hooks total: 22 on the default `pre-commit` stage and 2 on the `pre-push`/`manual` stage); `make lint` runs all 22 default-stage hooks in parallel priority tiers (`0` and `1`), and its formatting/fixer hooks may modify files. `make nx-graph` prints the Nx project graph JSON, `make nx-affected` runs affected `lint,typecheck,test,build` targets across the 7 workspace projects, `make graphify` extracts and labels the repository graph into `graphify-out/` (respecting `.graphifyignore`), and `make graphify-fetch` hydrates `graphify-out/` from the `graph-latest` release bundle. Setup installs pre-commit and pre-push hooks locally, and skips hook installation when `CI=true`.

For direct commands, use `mise exec --` so tool selection and cache settings match Make and CI:

```sh
mise exec -- uv run --locked director --help
mise exec -- pnpm run typecheck
mise exec -- pnpm run test
mise exec -- pnpm run build
mise exec -- biome check --config-path apps/ui/biome.json apps/ui/src
```

## Local kind validation

`CLUSTER_NAME` defaults to `platform-dev` and binds the local kind API server strictly to `127.0.0.1`:

```sh
make cluster-up
make test-integration
make teardown
```

You can also pass an explicit `CLUSTER_NAME=<unique-name>` when running multiple clusters concurrently. Creation uses a worktree-local kubeconfig under `.cache/clusters/<name>/kubeconfig`. Integration builds and loads the exported images, deploys the selected profile, and uses fixture-owned loopback forwards with dynamically selected ports. The Temporal CLI dev server runs **inside kind** behind a Service.

Teardown destroys only a cluster with verified creation ownership for this worktree. Explicit reuse (`REUSE_CLUSTER=true`) does not create new destruction ownership. Do not delete `.cache` while an owned cluster is still needed: it contains its isolated kubeconfig and ownership record.

See [tests/README.md](tests/README.md) and [charts/platform/README.md](charts/platform/README.md) for test and deployment profiles.

## Validation and caches

Project-local tool caches, Nx task cache (`.cache/nx`), and cluster state live under `.cache/`; `.venv`, `apps/ui/.next`, and gitignored `graphify-out/` remain separate. Tracked `.schemas/` and CRDs are versioned validation inputs.

Security gates include Gitleaks secret detection (`make scan-secrets`), Trivy misconfiguration and vulnerability scanning (`make scan-iac` and `make scan-local`), KubeLinter (`make lint-kubernetes`), Kubeconform schema validation (`make check-schema`), and Checkov (`make lint-checkov`). The `charts/platform` Checkov exclusion is intentionally retained for its separately planned compliance work.
