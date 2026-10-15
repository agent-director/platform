# Agent Director

Agent Director is a Kubernetes-hosted control-plane prototype with a FastAPI Unified API, Temporal workers, a Next.js frontend, a LiteLLM proxy and Agent Substrate integration.

The API currently proxies model requests and manages its existing configuration surface. Workflow HTTP endpoints still return prototype responses; the integration suite separately exercises a real workflow through the Temporal SDK and deployed worker. Neither those stubs nor chart rendering establishes a completed orchestration product or production authentication rollout.

## Repository layout

- `unified_api/`: API runtime, served as `unified_api.main:app`.
- `orchestrator/`: Temporal workflows, activities and worker health endpoint.
- `director/`: installed Python CLI.
- `frontend/`: pnpm workspace application.
- `charts/platform/`: application and backing-service chart, with full and kind Temporal profiles.
- `charts/agent-substrate/`: Agent Substrate v0.4.0 deployment and connection contracts.
- `images/`: runtime Dockerfiles; API/worker builds remain separate, without a shared Python base.
- `scripts/`: repository tooling, schema maintenance and guarded local lifecycle.

The checkout does not yet implement the planned Nx/apps/libs/tools migration. See [architecture.md](architecture.md) for current boundaries and limitations.

## Toolchain and dependencies

`mise.toml` and `mise.lock` select the toolchain and `.cache` environment paths. `uv` owns Python dependencies, `uv.lock`, editable package installation and `.venv`; `pnpm` owns the frontend workspace and `pnpm-lock.yaml`.

From the checkout root:

```sh
make setup
make typecheck
make build-frontend
make format
make pytest-unit
make lint
```

`make format` invokes the pinned Prek formatter only. `make lint` runs all configured hooks in parallel priority tiers; its formatting/fixer hooks may modify files. Setup installs pre-commit and pre-push hooks locally, and skips hook installation when `CI=true`.

For direct commands, use `mise exec --` so tool selection and cache settings match Make and CI:

```sh
mise exec -- uv run --locked director --help
mise exec -- pnpm --filter platform-ui typecheck
mise exec -- pnpm --filter platform-ui build
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

Project-local caches, temporary artifacts, scan logs and frontend outputs live under `.cache/`; `.venv` remains separate. Tracked `.schemas/` and CRDs are versioned validation inputs.

Security gates include Gitleaks secret detection (`make scan-secrets`), Trivy misconfiguration and vulnerability scanning (`make scan-iac` and `make scan-local`), KubeLinter (`make lint-kubernetes`), Kubeconform schema validation (`make check-schema`), and Checkov (`make lint-checkov`). The `charts/platform` Checkov exclusion is intentionally retained for its separately planned compliance work.
