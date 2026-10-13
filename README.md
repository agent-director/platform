# Agent Director

Agent Director is a Kubernetes-hosted control-plane prototype with a FastAPI Unified API, Temporal workers, a Next.js frontend, a LiteLLM proxy and Agent Substrate integration.

The API currently proxies model requests and manages its existing configuration surface. Workflow HTTP endpoints still return prototype responses; the integration suite separately exercises a real workflow through the Temporal SDK and deployed worker. Neither those stubs nor chart rendering establishes a completed orchestration product or production authentication rollout.

## Repository layout

- `unified_api/`: API runtime, served as `unified_api.main:app`.
- `orchestrator/`: Temporal workflows, activities and worker health endpoint.
- `director/`: installed Python CLI.
- `frontend/`: pnpm workspace application.
- `charts/platform/`: application and backing-service chart, with kind/bootstrap/full Temporal profiles.
- `charts/postgres-operator/`: locked wrapper for the separately installed database operator.
- `charts/agent-substrate/`: substrate deployment and connection contracts.
- `images/`: runtime Dockerfiles; API/worker builds remain separate, without a new shared Python base.
- `scripts/`: repository tooling, schema maintenance and guarded local lifecycle.

The checkout does not yet implement the planned Nx/apps/libs/tools migration. See [architecture.md](architecture.md) for current boundaries and limitations.

## Toolchain and dependencies

`mise.toml` and `mise.lock` select the toolchain. The checksum-verified project launcher bootstraps the pinned mise binary without changing global configuration. uv owns Python dependencies, `uv.lock`, editable package installation and `.venv`; pnpm owns the frontend workspace and `pnpm-lock.yaml`.

From the checkout root:

```sh
make setup
make check-toolchain
make typecheck
make format
make pytest-unit
make lint
```

`make format` invokes the pinned Prek formatter only. `make lint` also runs validators and tests; its formatting/fixer hooks may modify files. Setup installs pre-commit and pre-push hooks locally, but skips hook installation when `CI=true`.

For direct commands, use the project launcher so tool selection and cache settings match Make and CI:

```sh
./scripts/mise.sh exec -- uv run --locked director --help
./scripts/mise.sh exec -- pnpm --filter platform-ui typecheck
./scripts/mise.sh exec -- pnpm --filter platform-ui build
```

Run the launcher from a project subdirectory using its relative path, for example `../scripts/mise.sh` from `frontend/`. Subpackages inherit the root toolchain; conflicting child selectors are rejected. Package compatibility ranges remain distinct from exact development pins. `.python-version` and required native package-manager/build metadata are checked projections, not independent version authorities.

## Local kind validation

Choose an explicit cluster name that is not already in use:

```sh
make cluster-up CLUSTER_NAME=platform-review
make test-integration CLUSTER_NAME=platform-review
make teardown CLUSTER_NAME=platform-review
```

Creation uses a worktree-local kubeconfig. Integration builds and loads the exported images, deploys the selected profile, and uses fixture-owned loopback forwards with dynamically selected ports. The Temporal CLI dev server runs **inside kind** behind a Service; there is no external Docker-IP injection.

Teardown destroys only a cluster with verified creation ownership for this worktree. It never uninstalls production releases/operators or deletes a namespace. Explicit reuse (`REUSE_CLUSTER=true`) does not create new destruction ownership. Do not delete `.cache` while an owned cluster is still needed: it contains its isolated kubeconfig and ownership record.

See [tests/README.md](tests/README.md) and [charts/platform/README.md](charts/platform/README.md) for test and deployment profiles.

## Validation and caches

Project-local caches, temporary artifacts, scan logs and frontend outputs live under `.cache/`; `.venv` remains separate. Installed interpreters remain outside disposable cache storage so cache cleanup does not break `.venv`. Tracked `.schemas/` and CRDs are versioned validation inputs.

Security gates include current-tree Gitleaks detection, enabled Trivy misconfiguration scanning, KubeLinter profiles, schema validation, and the existing Checkov scope. The `charts/platform` Checkov exclusion is intentionally retained for its separately planned compliance work; a passing Checkov run is not a claim that it covers that chart.
