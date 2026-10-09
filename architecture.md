# Architecture: Agent Control Plane

The `agent-director` is designed around an **Agent Control Plane** architecture where all components, including agent execution environments (Agent Substrate), run in the same Kubernetes cluster plane.

## 1. Execution Model

### The Automation Plane
Powered by **Temporal** and **LangGraph**, the Automation Plane is responsible for durable, long-running agent workflows. These include asynchronous tasks such as CI pipelines, background code reviews, and periodic project management standups.
- **Components:** Temporal Workers (`orchestrator`).
- **Characteristics:** Highly retriable, state-checkpointed, determinant workflow execution with LLM interactions isolated inside Temporal Activities.

### The Interactive Plane
Supports real-time, low-latency interaction between end-users and agents.
- **Components:** `interactive_plane` interfaces.
- **Characteristics:** Fast feedback loops, direct state mutation, and streaming UI integrations.

## 2. Core Components
### Gateway (Unified API + LiteLLM)
The central intelligence router for the platform.
- **LiteLLM Base:** Provides vendor-agnostic LLM routing. We use a **GitOps + Kubernetes Secrets** split model for configuration:
  - **Routing (GitOps):** `litellm_config.yaml` is tracked in version control. It defines fallbacks, rate limits, and model identifiers (e.g. mapping `claude-3` to Vertex AI).
  - **Authentication (Vault/Secrets):** API keys (`ANTHROPIC_API_KEY`, GCP Service Accounts for Vertex) are injected into the Gateway pods exclusively as environment variables (via Kubernetes Secrets or ExternalSecrets to Vault). `litellm_config.yaml` natively resolves these via `os.environ/` syntax, completely isolating auth from routing logic without requiring the LiteLLM UI.


### Context Synthesis
To combat Context Rot and Tool Blindness without relying on synchronous middleware, the platform uses a **Parallel Archivist Workflow** and **Progressive Disclosure** (reading staged memory files).
### Agent Substrate Sandbox
Provides the secure, isolated execution environment where agent-generated code and tool actions run. It uses RWO hardlinked clones for fast initialization.
- Limits network access (e.g., blocking cluster-internal routing and cloud IMDS, allowing only explicitly permitted MCP/Gateway endpoints).
- Drops capabilities (e.g., `CAP_SYS_ADMIN`) and defaults to read-only root filesystems.
- Uses explicit isolation to prevent cross-sandbox tampering of hardlinked files.

The base deployment layer defined by the `charts/platform` Helm chart manages:
- PostgreSQL (via Zalando Operator for Temporal state, LangGraph checkpoints).
- Temporal Cluster.
- Unified API and LiteLLM Gateway pods.
- Agent Substrate Worker nodes.

## 3. API Gateway & Ingress

The platform exposes its internal API and Frontend through an unprivileged Tailscale and Caddy ingress bridge, bypassing the need for standard Kubernetes Ingress controllers or public LoadBalancers.

- **Tailscale Integration:** Tailscale is deployed as a **Native Sidecar** (a Kubernetes 1.28+ feature utilizing `initContainers` with `restartPolicy: Always`). This ensures the Tailscale daemon starts before the primary web server (Caddy) and cleanly terminates when the Pod shuts down.
- **State Storage:** Tailscale node state is durably stored using a `ReadWriteOnce` PersistentVolumeClaim (PVC). This provides a resilient identity for the Tailscale node and completely avoids the security risks associated with granting secret-creation RBAC permissions to the pod.
- **Caddy Reverse Proxy:** Caddy handles local routing from the Tailscale interface to internal services (`unified-api` and `frontend`). To protect internal services from resource exhaustion, Caddy enforces specific volumetric limits:
  - `request_body` size is capped at 10MB.
  - `timeouts` are configured to prevent slowloris attacks (`read_header 5s`) while allowing sufficient time for larger payloads (`read_body 120s`).

## 4. Security & Boundary Constraints
To operate agents safely at scale, the architecture enforces multiple layers of security:

1. **Sandbox Escapes:** Agent Substrate strictly limits path traversal and capabilities (read-only root, no CAP_SYS_ADMIN).
2. **Network Isolation:** Agent sandboxes cannot reach internal services (Temporal, Postgres) or cloud metadata endpoints.
3. **API Authentication:** Temporal gRPC/REST APIs use mTLS/JWT authentication.
4. **Cross-Tenant Data Isolation:** Hardlinked clones must be strictly isolated to prevent one compromised sandbox from tampering with shared files.

## 5. Key Global Risks & Mitigations

- **Temporal Workflow Non-Determinism:** LLM calls are inherently non-deterministic. They must be isolated into Temporal Activities.
- **State Synchronization:** Moving state securely and consistently requires careful checkpointing via LangGraph and Temporal.
