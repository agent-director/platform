# Testing Contract

The suite separates local logic/tooling contracts from live Kubernetes integration. Passing a static render or a stub API response is not proof of deployed workflow execution.

## Local unit and tooling tests

```sh
make setup
make typecheck
make pytest-unit PYTEST_ARGS='-n 0'
```

`tests/unit/` covers API/configuration logic, worker supervision, toolchain/lock consistency, script safety/failure cleanup, independently asserted Helm profiles and native scanner positive controls. It does not create a live Kubernetes cluster or build/pull Docker images. Workflow tests may use the SDK's local time-skipping test server; Helm and scanner controls use their managed native binaries. Initial tool/dependency preparation can require network access.

API tests inject settings and use dependency overrides. Cache/state fixtures are isolated; tests must not share a live deployment or mutable allowlist state accidentally. Source discovery failures must fail rather than silently fall back to less coverage.

Tests requiring native scanner controls run serially (`-n 0`) so environment/database preparation and cleanup fixtures do not collide. Individual failures should be rerun directly after a fix; do not rerun unrelated suites unnecessarily.

## Kind integration

Choose a fresh name and keep the same explicit name throughout:

```sh
make cluster-up CLUSTER_NAME=platform-review
make test-integration CLUSTER_NAME=platform-review
make teardown CLUSTER_NAME=platform-review
```

Integration consumes an existing selected cluster; it does not create or destroy one itself. Creation/deletion use a dedicated `.cache/clusters/<name>/kubeconfig` and verified worktree ownership. Existing unrelated clusters and the user's global kubeconfig are not reset. Reusing an existing cluster does not grant destruction ownership.

The default `kind` profile runs the pinned Temporal CLI dev server **inside Kubernetes**. Workload addresses/ports come from the deployed lifecycle ConfigMap and Helm helpers. Session fixtures own random loopback forwards, wait for functional readiness and clean only their own children on success, failure and interruption.

The successful read-only database readiness connection is retained until session teardown. Each SQL test gets its own scoped forward within the shared signal-aware session registry: libpq TLS connection closure can terminate a shared kubectl tunnel ([upstream issue](https://github.com/kubernetes/kubectl/issues/1746)). TLS remains required, and repeated SQL cases assert the server's actual TLS state. Failed driver connections suppress credential-bearing tracebacks.

Coverage includes authenticated database queries, real Temporal SDK execution against the deployed worker, API health, standalone frontend rendering/static assets, deployment discovery and validated substrate TLS identity. The concurrent HTTP workflow-creation test still exercises an API prototype response; a separate SDK workflow test is what proves worker execution.

Container/Caddy validation uses the built plugin-capable image. It must not pull a standard-image fallback after a custom build fails. Tailnet authentication requires an explicitly configured suitable environment; the normal kind profile disables real-tailnet ingress and does not claim that authentication path is tested.

## Full Temporal profile

`PROFILE=full` selects PostgreSQL-backed Temporal. Its first deployment is staged: provider/CRDs and database prerequisites, authenticated database readiness, server/schema/namespace hooks, then workers. A retry of an incomplete bootstrap is different from upgrading a healthy full deployment. Implicit profile/Temporal namespace migration is rejected, not silently treated as a fresh install.

The full profile is rendered and validated separately from kind/bootstrap/default resources. Render/lint success does not establish runtime deployment, authentication or migration safety. Report those results separately.

## Enforcement and positive controls

- `make check-toolchain`: root selections, native projections, compatibility constraints and subdirectory overrides.
- `make typecheck`: locked Pyrefly over runtime code, scripts and tests using the real project environment.
- `make format`: pinned Prek formatting only; may modify files.
- `make lint`: eligible commit gates, including formatting/fixers and unit tests.
- `make lint-kubernetes`, `make check-schema`: separate raw profile manifests and version-matched custom schemas.
- `make scan-secrets`, `make scan-iac`: real current-tree secret and misconfiguration enforcement.

Synthetic positive controls prove that scanners detect violations, not merely that commands exit successfully. The operator-only KubeLinter profile has exactly two documented exceptions tied to the selected upstream artifact; application/substrate profiles keep the normal baseline. Trivy scans the operator dependency with exact path/check exceptions in `.trivyignore.yaml`, guarded against operator or scanner version changes. Native controls prove the operator was scanned, identical checks still fail on first-party templates, other checks fail on excepted files, and the substrate's inline network-policy exception does not hide a neighboring rule. Skipping the platform lifecycle-name projection does not hide its workload scan. Checkov deliberately retains the user-deferred `charts/platform` exclusion.

## Determinism and local artifacts

Flaky tests must be fixed or removed; there is no unowned quarantine list. Failures, skipped checks and unavailable prerequisites must be reported faithfully.

Project-local caches, temp files, scan logs and frontend outputs live under `.cache`; `.venv` stays separate. Tracked `.schemas` and CRDs are reproducible validation inputs, not disposable caches. Tear down an owned cluster before removing its cached kubeconfig/ownership record.
