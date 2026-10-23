# Contributing Guidelines

## Commit Standards

This repository follows [Conventional Commits](https://www.conventionalcommits.org/) format:

```
type(scope): description
```

### Types

| Type | When to use |
|------|-------------|
| `feat` | New feature or capability |
| `fix` | Bug fix |
| `docs` | Documentation only |
| `chore` | Maintenance, dependencies, config |
| `refactor` | Code restructure (no behavior change) |
| `test` | Tests only |
| `perf` | Performance improvement |
| `style` | Formatting only (no logic change) |
| `build` | Build system, CI |
| `ci` | CI/CD config only |

Breaking changes: append `!` after type/scope — `feat(gateway)!: renames route endpoint`

### Scopes
- `workspace`: Python `uv` workspaces, `pnpm` workspace configuration, lockfiles
- `layout`: Repository directory structure and package relocations
- `nx`: Nx build graph (`nx.json`, `project.json`), task orchestration, `nx-mcp`
- `dx`: Developer experience, Graphify code intelligence, editor/MCP tooling
- `core`: Control-plane Python package (`apps/core`: `unified_api`, `orchestrator`, `director`)
- `ui`: Next.js frontend application (`apps/ui`), Biome linting, MSW mocks
- `sandbox-exec`: In-sandbox Robyn execution daemon (`apps/sandbox-exec`)
- `contracts`: Shared Pydantic schemas (`libs/contracts`)
- `api`: Unified API, API configuration, request handling
- `gateway`: LiteLLM gateway and model routing
- `orchestrator`: Temporal workers, workflows, activities
- `charts`: Helm chart templates, values, dependencies
- `k8s`: Kubernetes manifests, MCP relay configs
- `scripts`: Repository tooling, schema maintenance and local lifecycle (`tools/scripts`)
- `tests`: Test suites, fixtures, mock harnesses
- `git`: Repository plumbing, workflow configurations
- `platform`: Cross-cutting orchestrator, gateway, and infrastructure changes

### Rules

- Use present indicative tense: "adds feature" not "add feature" or "added feature"
- Keep the subject line under 72 characters
- No trailing period
- NEVER include `Signed-off-by`, `Assisted-by`, or `Co-Authored-By` trailers
- Use HEREDOC format for multi-word commit messages

## Branch Workflow

- Mainline branch: `main`
- Create all feature branches from `main`:
  ```bash
  git fetch origin main
  git switch -c <type>/<description> origin/main
  ```
- Branch naming: kebab-case descriptive names prefixed with conventional commit types (`feat/`, `fix/`, `chore/`, `refactor/`, `test/`).
- Never stack branches — every feature branch is independent.


## Architecture & Monorepo Structure

The repository is structured as a **SOTA Hybrid Nx Modular Monorepo**:
- `apps/core/`: Control-plane Python package (`agent-director-core`) containing `unified_api/`, `orchestrator/`, `director/`, and co-located unit tests (`apps/core/tests/unit/`).
- `apps/ui/`: Next.js 16 frontend application (`platform-ui` in the root `pnpm` workspace) with Biome (`apps/ui/biome.json`) and MSW contract mocks & tests (`apps/ui/src/mocks/`).
- `apps/sandbox-exec/`: Isolated Robyn in-sandbox execution daemon (`agent-director-sandbox-exec`) and unit tests (`apps/sandbox-exec/tests/unit/`).
- `libs/contracts/`: Shared Pydantic v2 contract schemas (`agent-director-contracts`) and unit tests (`libs/contracts/tests/unit/`).
- `tools/scripts/`: Operational and CI scripts (`deploy.sh`, `teardown.sh`, `test-integration.sh`, `scan-images.sh`, `fetch-graphify-brain.sh`, `images.py`, `update-crds.py`).
- `charts/`, `images/`, `tests/`: Helm charts (`charts/platform`, `charts/agent-substrate`), multi-stage container `Dockerfile`s, cross-cutting unit contract tests (`tests/unit/`), and live kind integration tests (`tests/integration/`).

Root `mise.toml` and `mise.lock` select the toolchain (including `biome`). Static import and dependency boundaries between Python workspace packages are enforced by Ruff `TID251` (`banned-api`) and `tests/unit/test_module_boundaries.py`.

## Tooling & Execution

Use `mise exec --` (or `make` targets) so commands have the same selected tools, interpreter and `.cache` environment as CI. `uv` owns the Python workspace (`pyproject.toml` and `uv.lock`):
- Setup all workspaces and hooks: `make setup`
- Run scripts: `mise exec -- uv run --no-project tools/scripts/images.py --format matrix`
- Validate: `make typecheck`, `make lint`, `make test`, `make pytest-unit`, `make nx-affected`, `make scan-iac`, `make scan-secrets`
- Format only: `make format` (pinned Prek formatting hook)
- Code intelligence: `make graphify` (or `make graphify-fetch`)

`prek.toml` configures 8 `pre-commit-hooks` (`trailing-whitespace`, `end-of-file-fixer`, `check-yaml`, `check-json`, `check-toml`, `check-added-large-files`, `check-merge-conflict`, `detect-private-key`) and 16 `local` hooks (`actions-lock`, `check-helm-deps`, `ruff`, `ruff-format`, `biome`, `gitleaks`, `actionlint`, `hadolint`, `checkov`, `kube-linter`, `typecheck`, `helm-lint`, `kubeconform`, `trivy-fs`, `trivy-ci-parity`, `pytest`) — 24 configured hooks total (22 on the default `pre-commit` stage and 2, `trivy-fs` and `trivy-ci-parity`, on the `pre-push`/`manual` stage) across parallel priority tiers (`0`: `trailing-whitespace`, `end-of-file-fixer`, `actions-lock`, `check-helm-deps`, `ruff`; `1`: `ruff-format`, `biome`, `check-yaml`, `check-json`, `check-toml`, `check-added-large-files`, `check-merge-conflict`, `detect-private-key`, `actionlint`, `hadolint`, `typecheck`, `gitleaks`, `checkov`, `kube-linter`, `helm-lint`, `kubeconform`, `trivy-fs`, `trivy-ci-parity`, `pytest`).

Use `pnpm` from the repository root (`package.json`, `pnpm-workspace.yaml`, and `pnpm-lock.yaml`) for Nx orchestration and frontend dependencies. Local AI/editor configuration (`.claude/`, `.mcp.json`) is gitignored in the public repository and hydrated from `hack/ai/` when the private Shadow Context checkout is present. Normal setup builds Helm dependencies from committed locks.

### Running Tests

We strictly separate our tests across co-located package unit suites (`apps/core/tests/unit/`, `apps/sandbox-exec/tests/unit/`, `libs/contracts/tests/unit/`), cross-cutting repository contract tests (`tests/unit/`), and live cluster integration tests (`tests/integration/`). See [`tests/README.md`](tests/README.md) for the full testing contract and instructions on how to run them locally and in CI.
## Developer Guidelines

### Writing Temporal Activities (LLM Invocations)
Due to the inherent non-determinism of LLMs, all LLM invocations **must** be isolated inside Temporal Activities.
- Never call the Gateway or LiteLLM directly from a Temporal Workflow.
- Ensure Activities are strictly idempotent where possible, or rely on Temporal's at-least-once execution guarantees to handle failures.
- Pass necessary state via LangGraph checkpoint serialization.

### Local Bring-Up & Helm Deployment
Local integration uses a kind cluster bound to `127.0.0.1`, the upstream PostgreSQL operator release, the platform chart and the substrate chart. See the chart and testing READMEs for profiles.
- `make cluster-up` creates a worktree-owned cluster (default `CLUSTER_NAME=platform-dev`) with isolated kubeconfig.
- `make test-integration` builds images, deploys, and runs the integration suite against that cluster.
- `make teardown` destroys only a cluster with verified local creation ownership.

### Agent Substrate Sandbox Capabilities
Temporal workers are control-plane workloads, not the sandbox itself. Agent Substrate manages sandbox execution. When implementing sandbox-bound work:
- Assume the execution environment drops all elevated capabilities (e.g., `CAP_SYS_ADMIN`).
- Sandboxes operate on a read-only root filesystem.
- Network routes to cluster-internal control plane services are strictly dropped. Allowlist required MCP endpoints explicitly.
