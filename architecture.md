# Architecture: Agent Control Plane

## Current state versus planned product

This checkout is a control-plane prototype, not a completed agent orchestration product. Runtime components share Kubernetes while preserving workload, credential and network boundaries. Future repository/Nx reorganization, durable platform data models, richer workflow APIs, authenticated local runners and production Temporal authorization are not implemented by this tooling change.

The deployed Python API is `unified_api.main:app`; LiteLLM is a separate model-proxy workload. Temporal workflows remain deterministic and non-deterministic work belongs in activities. Existing example workflows/HTTP workflow endpoints include stub behavior, which tests must not describe as proof of a complete task execution engine.

## Runtime components

- **Unified API:** FastAPI request/configuration surface with validated injected settings. Its health endpoint reports process availability, not proof that every backing service is healthy.
- **Temporal worker:** polls the configured namespace/task queue, supervises startup/shutdown, and exposes process health plus SDK-backed readiness. Integration tests execute a real SDK workflow against this worker.
- **Frontend:** Next.js application in `frontend/` managed by `pnpm`. Browser requests use same-origin `/api` and `/v1` routes rather than a runtime `NEXT_PUBLIC_API_URL` that would already have been frozen at build time.
- **LiteLLM:** separately deployed model proxy; the API receives its actual Service URL from chart configuration.
- **Agent Substrate:** separately deployed `v0.4.0` API/controller (`ateapi` and `atecontroller`) with scoped database credentials, explicit TLS bundle inputs, matching Service/SAN/server-name wiring, and live HTTP `/metrics` probes on `:8080` for the controller.

## Toolchain and artifact ownership

Root `mise.toml` and `mise.lock` select the CLI and runtime toolchain. `uv` owns Python packaging, lock resolution and `.venv`; `pnpm` owns `frontend/` dependencies and `frontend/pnpm-lock.yaml`. Container Dockerfiles pin their own `FROM <repo>:<tag>@sha256:<digest>` images directly so standalone builds and Renovate's `dockerfile` manager work without wrapper scripts.

Helm owns cluster names, namespaces, Services and port configuration. Runtime code consumes validated environment settings rather than deriving Helm names. Test fixtures discover the deployed lifecycle contract and own their temporary host access endpoints.

Shared image discovery (`scripts/images.py`) identifies exported Docker targets for CI, kind loading and scans. The API/worker Dockerfiles use their own runtime builds; no common Python dependency-filled base is introduced. Frontend containers copy the distroless Node runtime into the `pnpm` builder stage so both build and runtime execute the same hardened Node binary.

## Backing-service lifecycle

This change preserves the current chart layout. The upstream Zalando `postgres-operator` (`2.0.3`) is installed directly from its Helm repository using `charts/postgres-operator-values.yaml`. The platform chart owns PostgreSQL/RustFS, application workloads, Langfuse, and the selected Temporal profile. Planned infrastructure extraction is not performed here.

Provider versions, packaged CRDs and cached validation schemas must agree (`make update-schemas`). Normal setup/CI materializes committed locks; dependency updates are deliberate maintenance operations, not automatic setup side effects.

Both `PROFILE=kind` and `PROFILE=full` converge in a single Helm pass:

1. Install the upstream `postgres-operator` release and wait for the `postgresqls.acid.zalan.do` CRD.
2. Install `charts/platform` (`temporal.enabled: true`, `workers.enabled: true`, and `langfuse.enabled: true` by default in `values.yaml`, with non-hook schema/namespace Jobs via `useHelmHooks: false`; or `temporalDev.enabled: true` in `values-kind.yaml`).
3. Verify required credentials, trust material, authenticated SQL readiness, and platform Deployment rollouts.
4. Copy scoped database/RustFS secrets and CA trust into `agent-substrate` and install `charts/agent-substrate`.

Production deployment or data migration is not implied by local validation.

## Temporal execution profiles

- SDK time-skipping tests exercise workflow logic without kind.
- The kind profile runs a pinned Temporal CLI dev server as a Kubernetes Deployment/Service with scratch storage, health checks and restricted peers.
- The full profile uses the locked upstream Temporal chart and PostgreSQL persistence.

The CLI dev server is not production-safe and does not establish authentication, persistence or full-server behavior. Rendering the full profile likewise does not prove it deploys successfully; runtime validation must be reported separately.

## Ingress and security boundaries

The Tailscale-enabled Caddy ingress authenticates tailnet identity and routes API/model paths to the API without stripping their prefixes; remaining traffic goes to the frontend. NetworkPolicies scope peers/ports and prevent treating arbitrary network reachability as authorization. Kind validation disables the real-tailnet ingress unless an explicit suitable environment is supplied.

Workload hardening, real probes/resources, namespace-scoped credential delivery and public CA projection are separate from application authorization. Database consumers do not receive the server private key or a substrate superuser credential. An OpenShift serving-certificate Secret alone is not the upstream substrate credential-bundle contract; that mode requires an explicitly prepared bundle.

The unused standalone filesystem-worker prototype and Tailscale Serve JSON are removed. A future host runner still requires its own authentication, namespace/consent and visibility boundaries; no such host-control feature is shipped here.

## Verification boundaries

Unit tests cover Python logic, local tooling/script contracts, Helm profiles and native scanner positive controls without a live Kubernetes cluster. Integration tests separately prove backing-service access and runtime wiring through owned loopback forwards. They must fail on failed discovery rather than reduce coverage silently.

Complementary gates do not substitute for one another. KubeLinter enables all built-in checks (`addAllBuiltIn: true`) with an explicit opt-out list in `.kube-linter.yaml`. Gitleaks runs in `git` mode (`--pre-commit --staged` for pre-commit and `--log-opts="HEAD"` for branch history). Trivy enforces vulnerability, misconfiguration, and secret scanning via `trivy.yaml`. The deferred `charts/platform` Checkov exclusion is preserved and disclosed.

Local teardown deletes only an explicitly named, verified owned kind cluster. It is not an application uninstall, production purge or operator-removal process.
