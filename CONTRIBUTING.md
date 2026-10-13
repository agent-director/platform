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
- `api`: Unified API, API configuration, request handling
- `gateway`: LiteLLM gateway and model routing
- `orchestrator`: Temporal workers, workflows, activities
- `charts`: Helm chart templates, values, dependencies
- `k8s`: Kubernetes manifests, MCP relay configs
- `scripts`: Repository tooling, schema maintenance and local lifecycle
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

The current checkout contains `unified_api/`, `orchestrator/`, `director/`, `frontend/`, `charts/`, `images/` and repository maintenance `scripts/`. It uses one Python project and a minimal pnpm workspace; it is not yet an Nx/apps/libs/tools workspace. Keep deployable runtimes distinct from maintenance tooling, and do not introduce the planned directory/repository migration as an incidental cleanup.

Root `mise.toml` and `mise.lock` select the toolchain. Subpackages inherit those selections; unapproved child selectors and conflicting native metadata fail the consistency gate. Python `requires-python` and Node `engines` describe compatibility, not another development-version authority.
## Tooling & Execution

Use the root mise launcher so direct commands have the same selected tools, interpreter and cache environment as Make and CI. uv remains the Python package/environment manager:
- Run scripts: `./scripts/mise.sh exec -- uv run script.py`
- Add dependencies: `./scripts/mise.sh exec -- uv add <package>`
- Validate: `make check-toolchain`, `make typecheck`, `make lint`
- Format only: `make format` (pinned Prek formatting hook)

Use pnpm and its frozen workspace lock for frontend dependencies. Normal setup builds Helm dependencies from committed locks; intentionally changing selected dependencies is a separate maintenance operation. Do not maintain independent child tool pins or install another package-manager/runtime version to bypass a failed consistency gate.

### Running Tests

We strictly separate our tests to ensure fast feedback and reliable infrastructure validation. See [`tests/README.md`](tests/README.md) for the full testing contract and instructions on how to run them locally and in CI.
## Developer Guidelines

### Writing Temporal Activities (LLM Invocations)
Due to the inherent non-determinism of LLMs, all LLM invocations **must** be isolated inside Temporal Activities.
- Never call the Gateway or LiteLLM directly from a Temporal Workflow.
- Ensure Activities are strictly idempotent where possible, or rely on Temporal's at-least-once execution guarantees to handle failures.
- Pass necessary state via LangGraph checkpoint serialization.

### Local Bring-Up & Helm Deployment
Local integration uses an explicitly selected kind cluster, the locked PostgreSQL operator wrapper, the existing platform chart and the substrate chart. See the chart and testing READMEs for prerequisite staging and profiles.
- `make cluster-up CLUSTER_NAME=<unique>` creates a worktree-owned cluster with isolated kubeconfig.
- `make test-integration CLUSTER_NAME=<same>` uses it; the Temporal CLI runs inside kind.
- `make teardown CLUSTER_NAME=<same>` destroys only verified local creation ownership, never production operators/releases/namespaces.
- Full Temporal/database migration and infrastructure-release extraction have distinct lifecycle requirements; local teardown is not a production uninstall.

### Agent Substrate Sandbox Capabilities
Temporal workers are control-plane workloads, not the sandbox itself. Agent Substrate manages sandbox execution. When implementing sandbox-bound work:
- Assume the execution environment drops all elevated capabilities (e.g., `CAP_SYS_ADMIN`).
- Sandboxes operate on a read-only root filesystem.
- Network routes to cluster-internal control plane services are strictly dropped. Allowlist required MCP endpoints explicitly.
