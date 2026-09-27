# Architecture: Two-Plane Agent Control Plane

The `agent-director` is designed around a **Two-Plane** architecture to accommodate the differing operational profiles of highly interactive, real-time agent sessions versus long-running, durable automation workflows.

## 1. Two-Plane Execution Model

### The Automation Plane
Powered by **Temporal** and **LangGraph**, the Automation Plane is responsible for durable, long-running agent workflows. These include asynchronous tasks such as CI pipelines, background code reviews, and periodic project management standups.
- **Components:** Temporal Workers (`orchestrator`).
- **Characteristics:** Highly retriable, state-checkpointed, determinant workflow execution with LLM interactions isolated inside Temporal Activities.

### The Interactive Plane
Powered by **Omnigent**, the Interactive Plane supports real-time, low-latency interaction between end-users and agents.
- **Components:** `interactive_plane` interfaces.
- **Characteristics:** Fast feedback loops, direct state mutation, and streaming UI integrations.

## 2. Core Components
### Gateway (LiteLLM + Shunt)
The central intelligence router for the platform. It intercepts all outgoing LLM calls.
- **LiteLLM Base:** Provides vendor-agnostic LLM routing. We use a **GitOps + Kubernetes Secrets** split model for configuration:
  - **Routing (GitOps):** `litellm_config.yaml` is tracked in version control. It defines fallbacks, rate limits, and model identifiers (e.g. mapping `claude-3` to Vertex AI).
  - **Authentication (Vault/Secrets):** API keys (`ANTHROPIC_API_KEY`, GCP Service Accounts for Vertex) are injected into the Gateway pods exclusively as environment variables (via Kubernetes Secrets or ExternalSecrets to Vault). `litellm_config.yaml` natively resolves these via `os.environ/` syntax, completely isolating auth from routing logic without requiring the LiteLLM UI.
- **Shunt Middleware:** A specialized, token-optimizing middleware (speculatively evaluated as in-process Python vs. high-performance Rust/Go proxy) that performs deep inspection, caching, and policy enforcement on LLM requests without blocking high-throughput streaming.

### Centralized MCP (Model Context Protocol)
A centralized manager that aggregates tools and contexts. It acts as the registry, mapping available tools to specific agent capabilities and authorizing execution.
- Serves as the bridge feeding tool definitions into the Gateway and Agent execution environments via JSON-RPC.

### OpenShell Sandbox
Provides the secure, isolated execution environment where agent-generated code and tool actions run.
- Limits network access (e.g., blocking cluster-internal routing and cloud IMDS, allowing only explicitly permitted MCP endpoints).
- Drops capabilities (e.g., `CAP_SYS_ADMIN`) and defaults to read-only root filesystems.

2. **Network Isolation:** OpenShell sandboxes cannot reach internal services (Temporal, Gateway, Postgres) or cloud metadata endpoints.
The base deployment layer defined by the `charts/platform` Helm chart. It manages:
- PostgreSQL (Temporal state, LiteLLM routing rules, LangGraph checkpoints, MCP registry).
- Temporal Cluster.
- LiteLLM Gateway pods.
- OpenShell Worker nodes.

## 3. API Gateway & Ingress

The platform exposes its internal API and Frontend through an unprivileged Tailscale and Caddy ingress bridge, bypassing the need for standard Kubernetes Ingress controllers or public LoadBalancers.

- **Tailscale Integration:** Tailscale is deployed as a **Native Sidecar** (a Kubernetes 1.28+ feature utilizing `initContainers` with `restartPolicy: Always`). This ensures the Tailscale daemon starts before the primary web server (Caddy) and cleanly terminates when the Pod shuts down.
- **State Storage:** Tailscale node state is durably stored using a `ReadWriteOnce` PersistentVolumeClaim (PVC). This provides a resilient identity for the Tailscale node and completely avoids the security risks associated with granting secret-creation RBAC permissions to the pod.
- **Caddy Reverse Proxy:** Caddy handles local routing from the Tailscale interface to internal services (`unified-api` and `frontend`). To protect internal services from resource exhaustion, Caddy enforces specific volumetric limits:
  - `request_body` size is capped at 10MB.
  - `timeouts` are configured to prevent slowloris attacks (`read_header 5s`) while allowing sufficient time for larger payloads (`read_body 120s`).

## 4. Security & Boundary Constraints
To operate agents safely at scale, the architecture enforces multiple layers of security:

1. **Sandbox Escapes:** OpenShell strictly limits path traversal and capabilities.
2. **Network Isolation:** Ambient sandboxes cannot reach internal services (Temporal, Gateway, Postgres) or cloud metadata endpoints.
3. **Shunt Injection Defenses:** The LiteLLM Shunt avoids dynamic code evaluation, strictly parsing headers and telemetry using safe serialization.
4. **API Authentication:** Temporal gRPC/REST APIs use mTLS/JWT authentication. Signals from the Interactive Plane are treated as untrusted and strictly validated.
5. **MCP Authorization:** Tool invocations via the Centralized MCP require scoped authorization aligned with the calling agent's identity.
6. **Cross-Tenant Data Isolation:** Gateway caches, tool contexts, and states are strictly keyed by tenant/agent ID to prevent data bleed.

## 5. Key Global Risks & Mitigations

- **Temporal Workflow Non-Determinism:** LLM calls are inherently non-deterministic. They must be isolated into Temporal Activities.
- **State Synchronization:** Moving state securely and consistently between the fast Interactive Plane and the durable Automation Plane requires careful checkpointing via LangGraph and Temporal.
- **Shunt Latency:** The token-optimizing Shunt must not introduce synchronous blocking into the event loop, mitigating throughput degradation.
