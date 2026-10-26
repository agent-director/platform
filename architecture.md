# Architecture: Agent Control Plane

## Current state versus planned product

This checkout is a control-plane prototype structured as a **SOTA Hybrid Nx Modular Monorepo** (`apps/core`, `apps/ui`, `apps/sandbox-exec`, `libs/contracts`, `tools/scripts`, and root `charts/`, `images/`, `tests/`), not a completed agent orchestration product. Runtime components share Kubernetes while preserving workload, credential, dependency, and network boundaries. Durable platform data models, richer workflow APIs, authenticated local runners, and production Temporal authorization remain planned roadmap work.

The deployed Python API is `unified_api.main:app` (in `apps/core/unified_api/`); LiteLLM is a separate model-proxy workload. Temporal workflows (`apps/core/orchestrator/`) remain deterministic and non-deterministic work belongs in activities. Existing example workflows/HTTP workflow endpoints include stub behavior, which tests must not describe as proof of a complete task execution engine.

## Runtime components

- **Unified API (`apps/core/unified_api/`):** FastAPI request/configuration surface with validated injected settings (`agent-director-core`). Its health endpoint reports process availability, not proof that every backing service is healthy.
- **Temporal worker (`apps/core/orchestrator/`):** polls the configured namespace/task queue, supervises startup/shutdown, and exposes process health plus SDK-backed readiness. Integration tests execute a real SDK workflow against this worker.
- **Sandbox Execution Daemon (`apps/sandbox-exec/`):** isolated Robyn (`robyn==0.88.0`) package (`agent-director-sandbox-exec`) sharing typed Pydantic schemas via `libs/contracts` (`agent-director-contracts`) and enforcing constant-time Bearer token authentication (`SANDBOX_AUTH_TOKEN`, `hmac.compare_digest`, fail-closed when unset/empty) on `POST /hydrate`, while `/readyz` serves as the unauthenticated readiness probe. Its container image (`images/sandbox/Dockerfile`) remains on a non-listening stub entrypoint until Tier 2.2 runtime secret injection is wired.
- **Frontend (`apps/ui/`):** Next.js 16 application (`platform-ui`) in the root `pnpm` workspace (`strictDepBuilds: true`, `minimumReleaseAge: 2880`, `allowBuilds: { nx: false, "nx-mcp": false, msw: false }`), checked by `mise`-pinned Biome (`2.5.15`, `lint/security`) and equipped with MSW contract mocks and tests (`apps/ui/src/mocks/`) verified against the Unified API OpenAPI schema. Browser requests use same-origin `/api` and `/v1` routes rather than a runtime `NEXT_PUBLIC_API_URL` that would already have been frozen at build time.
- **LiteLLM:** separately deployed model proxy; the API receives its actual Service URL from chart configuration.
- **Agent Substrate:** separately deployed `v0.4.0` API/controller (`ateapi` and `atecontroller`) with scoped database credentials, explicit TLS bundle inputs, matching Service/SAN/server-name wiring, and live HTTP `/metrics` probes on `:8080` for the controller.

## Toolchain and artifact ownership

Root `mise.toml` and `mise.lock` select the CLI and runtime toolchain (including `biome=2.5.15`) and set `NX_NO_CLOUD=true`, `NX_DAEMON=false`, `NX_CACHE_DIRECTORY`, and `NEXT_TELEMETRY_DISABLED=1`. `uv` owns Python workspace packaging (`[tool.uv.workspace]` members `apps/core`, `apps/sandbox-exec`, and `libs/contracts`) plus the `graphify` dependency group (`graphifyy[sql]==0.9.92`), lock resolution (`uv.lock`), and `.venv`; `pnpm` owns root workspace tooling (`nx`, `nx-mcp`), `apps/ui/` dependencies, and root `pnpm-lock.yaml`; and Nx (`nx.json`, `.nxignore`, and 7 `project.json` files) orchestrates build-graph target caching. Static import boundaries, per-package `pyproject.toml` dependency hermeticity, and OpenAPI ↔ MSW contract alignment are enforced by Ruff `TID251` (`flake8-tidy-imports.banned-api`) and `tests/unit/test_module_boundaries.py`. Container Dockerfiles pin their own `FROM <repo>:<tag>@sha256:<digest>` images directly and install only their target workspace package (`uv sync --locked --package <member> --no-install-workspace --no-dev` or `pnpm install --frozen-lockfile --filter platform-ui...`) so standalone builds and Renovate's `dockerfile` manager work without wrapper scripts.

Helm owns cluster names, namespaces, Services and port configuration. Runtime code consumes validated environment settings rather than deriving Helm names. Test fixtures discover the deployed lifecycle contract and own their temporary host access endpoints.

Shared image discovery (`tools/scripts/images.py`) identifies exported Docker targets for CI, kind loading and scans. The API/worker/sandbox Dockerfiles use their own per-package runtime builds; no common Python dependency-filled base is introduced. Frontend containers copy the distroless Node runtime into the `pnpm` builder stage so both build and runtime execute the same hardened Node binary.

## Backing-service lifecycle

This change preserves the current chart layout. The upstream Zalando `postgres-operator` (`2.0.3`) is installed directly from its Helm repository using `charts/postgres-operator-values.yaml`. The platform chart owns PostgreSQL/RustFS, application workloads, Langfuse, and the selected Temporal profile. Planned infrastructure extraction is not performed here.

Provider versions, packaged CRDs and cached validation schemas must agree (`make update-schemas`). Dependency updates are deliberate maintenance operations, not automatic setup side effects.

Both `PROFILE=kind` and `PROFILE=full` converge in a single Helm pass:

1. Install the upstream `postgres-operator` release and wait for the `postgresqls.acid.zalan.do` CRD.
2. Install `charts/platform` (`temporal.enabled: true`, `workers.enabled: true`, and `langfuse.enabled: true` by default in `values.yaml`, with first-party non-hook schema/namespace Jobs in `charts/platform/templates/temporal.yaml`; or `temporalDev.enabled: true` in `values-kind.yaml`).
3. Verify required credentials, trust material, authenticated SQL readiness, and platform Deployment rollouts.
4. Copy scoped database/RustFS secrets and CA trust into `agent-substrate` and install `charts/agent-substrate`.

Production deployment or data migration is not implied by local validation.

## Temporal execution profiles

- SDK time-skipping tests and `temporalio.worker.Replayer` history replay verification (`apps/core/tests/unit/fixtures/`) exercise workflow logic and determinism without kind, and `make dev-temporal` runs a host-local Temporal CLI dev server (`127.0.0.1:7233`, UI `8233`).
- The kind profile runs a pinned Temporal CLI dev server as a Kubernetes Deployment/Service with scratch storage, health checks and restricted peers.
- The full profile uses first-party Temporal templates (`charts/platform/templates/temporal.yaml`) with `temporalio/server:1.32.0` (`passwordCommand` Secret-backed credential loading), non-hook `temporalio/admin-tools:1.32.0` schema/namespace Jobs, and PostgreSQL persistence, replacing the upstream Temporal subchart.

The CLI dev server is not production-safe and does not establish authentication, persistence or full-server behavior. Rendering the full profile likewise does not prove it deploys successfully; runtime validation must be reported separately.

## Ingress and security boundaries

The Tailscale-enabled Caddy ingress authenticates tailnet identity and routes API/model paths to the API without stripping their prefixes; remaining traffic goes to the frontend. NetworkPolicies scope peers/ports and prevent treating arbitrary network reachability as authorization. Kind validation disables the real-tailnet ingress unless an explicit suitable environment is supplied.

Workload hardening, real probes/resources, namespace-scoped credential delivery and public CA projection are separate from application authorization. Database consumers do not receive the server private key or a substrate superuser credential. An OpenShift serving-certificate Secret alone is not the upstream substrate credential-bundle contract; that mode requires an explicitly prepared bundle.

The unused standalone filesystem-worker prototype and Tailscale Serve JSON are removed. A future host runner still requires its own authentication, namespace/consent and visibility boundaries; no such host-control feature is shipped here.

## Verification boundaries

Unit tests cover Python logic, local tooling/script contracts, Helm profiles and native scanner positive controls without a live Kubernetes cluster. Integration tests separately prove backing-service access and runtime wiring through owned loopback forwards. They must fail on failed discovery rather than reduce coverage silently.

Complementary gates do not substitute for one another. KubeLinter enables all built-in checks (`addAllBuiltIn: true`) with an explicit opt-out list in `.kube-linter.yaml`. Betterleaks (`.betterleaks.toml`) runs in `git` mode (`--pre-commit --staged` for pre-commit and `--log-opts="HEAD"` for branch history). Trivy enforces vulnerability, misconfiguration, and secret scanning via `trivy.yaml`. Checkov scans both `charts/platform` (default and `values-kind.yaml` profiles) and `charts/agent-substrate` with zero `skip-path` exclusions, and `values.schema.json` validates chart inputs before rendering.

Local teardown deletes only an explicitly named, verified owned kind cluster. It is not an application uninstall, production purge or operator-removal process.
