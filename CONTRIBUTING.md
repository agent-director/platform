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
- `gateway`: Unified API, LiteLLM gateway, routing
- `orchestrator`: Temporal workers, workflows, activities
- `charts`: Helm chart templates, values, dependencies
- `k8s`: Kubernetes manifests, MCP relay configs
- `scripts`: Developer utilities, local worker scripts
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

We use an **Nx-managed Modular Monorepo**. To ensure isolated dependencies, cacheable CI runs, and strict architectural boundaries, code MUST be placed in the correct directory:

- **`apps/` (Deployable Runtimes):** Containers, web servers, and UI bundles that get executed or deployed. They should remain "thin" and mostly wire up shared libraries.
  - Examples: `apps/core` (API), `apps/ui` (React), `apps/sandbox-exec` (Daemon), `apps/charts` (Helm).
- **`libs/` (Shared Code & Contracts):** The bulk of the codebase. Reusable Python modules, UI components, and API schemas.
  - Examples: `libs/contracts` (Shared OpenAPI/Types).
- **`tools/` (Generators & Automation):** Scripts, Copier templates, and DX improvements used to maintain the codebase, not shipped to users.
  - Examples: `tools/toolkit-template` (Scaffolding), `tools/scripts` (BATS/Bash).
## Tooling & Execution

Always use `uv` for Python execution and dependency management:
- Run scripts: `uv run script.py`
- Run tests: `uv run pytest tests/`
- Add dependencies: `uv add <package>`

## Developer Guidelines

### Writing Temporal Activities (LLM Invocations)
Due to the inherent non-determinism of LLMs, all LLM invocations **must** be isolated inside Temporal Activities.
- Never call the Gateway or LiteLLM directly from a Temporal Workflow.
- Ensure Activities are strictly idempotent where possible, or rely on Temporal's at-least-once execution guarantees to handle failures.
- Pass necessary state via LangGraph checkpoint serialization.

### Local Bring-Up & Helm Deployment
Deploying the base infrastructure (PostgreSQL, Temporal, LiteLLM proxy, Agent Substrate workers) relies on the `charts/platform` Helm chart.
- Local environment bring-up requires Kubernetes (e.g., Docker Desktop, Minikube, or kind).
- Follow the infrastructure guides (TBD) for deploying the chart and establishing the required sandbox configurations.

### Agent Substrate Sandbox Capabilities
The `temporal-worker` pod acts as the Agent Substrate Sandbox. When implementing workflows:
- Assume the execution environment drops all elevated capabilities (e.g., `CAP_SYS_ADMIN`).
- Sandboxes operate on a read-only root filesystem.
- Network routes to cluster-internal control plane services are strictly dropped. Allowlist required MCP endpoints explicitly.
