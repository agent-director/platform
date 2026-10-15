# Testing Contract

The suite separates local logic/tooling contracts from live Kubernetes integration. Passing a static render or a stub API response is not proof of deployed workflow execution.

## Local unit and tooling tests

```sh
make setup
make typecheck
make pytest-unit PYTEST_ARGS='-n 0'
```

`tests/unit/` covers API/configuration logic, worker supervision, script safety/failure cleanup, independently asserted Helm profiles and native scanner positive controls. It does not create a live Kubernetes cluster or build/pull Docker images.

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
- `make lint`: all pre-commit hooks running in parallel priority tiers (`0` fixers, `1` formatters, `2` linters/unit tests).
- `make lint-kubernetes`, `make check-schema`, `make lint-checkov`: rendered profile validation against KubeLinter, Kubeconform (`.schemas/`), and Checkov.
- `make scan-secrets`, `make scan-iac`, `make scan-local`: Gitleaks (`git` mode) and Trivy IaC/image enforcement.
