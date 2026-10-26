# Testing Contract

The suite separates local logic/tooling contracts from live Kubernetes integration. Passing a static render or a stub API response is not proof of deployed workflow execution.

## Local unit and tooling tests

```sh
make setup
make typecheck
make pytest-unit
```

Unit tests are organized across four workspace test roots configured in root `pyproject.toml` (`testpaths`) and inherit git-hook sanitization (`_GIT_HOOK_VARS`) and `ROBYN_CLI=true` from root `conftest.py`:
- `apps/core/tests/unit/`: Unified API (`test_unified_api.py`), Temporal worker/workflows (`test_orchestrator.py`) and `temporalio.worker.Replayer` determinism history fixtures (`apps/core/tests/unit/fixtures/`), and `director` CLI (`test_cli.py`).
- `apps/sandbox-exec/tests/unit/`: Robyn in-sandbox execution daemon and constant-time Bearer token auth boundary (`test_daemon.py`).
- `libs/contracts/tests/unit/`: Shared Pydantic v2 contract schemas and round-trip JSON schema serialization (`test_contracts.py`).
- `tests/unit/`: Cross-cutting repository contracts including static AST import boundaries, per-package `pyproject.toml` dependency hermeticity, Nx project-graph layering, and OpenAPI ↔ MSW contract alignment (`test_module_boundaries.py`), lifecycle script safety/failure cleanup (`test_scripts.py`, `test_crd_scripts.py`), Helm chart rendering/profiles, `values.schema.json` contract tests (`test_chart_contracts.py`), ingress, and OLM/substrate schemas (`test_images.py`, `test_ingress.py`, `test_olm_schemas.py`, `test_database_fixtures.py`, `test_substrate_log_safety.py`), and Checkov/Betterleaks/scanner/hook/Nx positive controls (`test_security_gates.py`).

Frontend MSW contract handlers are also exercised directly via Node's native test runner in `apps/ui/src/mocks/handlers.test.ts` (`make test` or `mise exec -- pnpm --filter platform-ui run test`).

The unit suite does not create a live Kubernetes cluster or build/pull Docker images. You can also run per-project targets with isolated `--basetemp` directories via Nx (`make nx-affected` or `mise exec -- pnpm exec nx run-many -t test --parallel=3`).

## Kind integration

`CLUSTER_NAME` defaults to `platform-dev` (or pass an explicit `CLUSTER_NAME=<name>`):

```sh
make cluster-up
make test-integration
make teardown
```

Integration consumes an existing selected cluster; it does not create or destroy one itself. Creation/deletion use a dedicated `.cache/clusters/<name>/kubeconfig`, bind the API server strictly to `127.0.0.1`, and verify worktree ownership.

The default `kind` profile runs the pinned Temporal CLI dev server **inside Kubernetes**. Session fixtures own random loopback forwards, wait for functional readiness and clean only their own children on success, failure and interruption.

The successful read-only database readiness connection is retained until session teardown. Each SQL test gets its own scoped forward within the shared signal-aware session registry: libpq TLS connection closure can terminate a shared kubectl tunnel ([upstream issue](https://github.com/kubernetes/kubectl/issues/1746)).

Coverage includes authenticated database queries, real Temporal SDK execution against the deployed worker, API health, standalone frontend rendering/static assets, deployment discovery and validated substrate TLS identity.

## Enforcement and positive controls

- `make typecheck`: locked Pyrefly over Python code and TypeScript typechecking for the frontend.
- `make format`: pinned Prek formatting only.
- `make lint`: all pre-commit hooks running in two parallel priority tiers (`0` autofixers/materializers, `1` formatters, linters, and parallel unit tests).
- `make lint-kubernetes`, `make check-schema`, `make lint-checkov`: rendered profile validation against KubeLinter, Kubeconform (`.schemas/`), and Checkov.
- `make scan-secrets`, `make scan-iac`, `make scan-local`: Betterleaks (`git` mode) and Trivy IaC/image enforcement.
