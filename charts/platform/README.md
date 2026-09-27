# Platform Helm Chart

This Helm chart (`charts/platform`) defines the base infrastructure for the `agent-director`.

## Components Deployed
- **PostgreSQL:** Backing store for Temporal state, LiteLLM routing rules, LangGraph checkpoints, and MCP registry schemas.
- **Temporal Cluster:** The core engine for the Automation Plane.
- **LiteLLM Proxy:** The Gateway pods intercepting and routing LLM calls.
- **OpenShell Worker Nodes:** The isolated sandbox environment where agent tools and execution occur.
- **Tailscale Ingress (Optional):** A secure, private ingress solution leveraging Tailscale and Caddy as unprivileged sidecars to route traffic to the unified API and frontend.

## Local Bring-Up
*(TBD: Full step-by-step for local deployment)*

Generally, deployment requires a local Kubernetes cluster (e.g., Docker Desktop, Minikube, kind):
```bash
helm upgrade --install platform ./charts/platform --namespace agent-director --create-namespace
```

## Prerequisites
- **Kubernetes 1.28+**: The chart utilizes Kubernetes 1.28 Native Sidecars (`initContainers` with `restartPolicy: Always`) for the Tailscale ingress deployment. Ensure your cluster supports this feature.

## Security Configurations
- The OpenShell worker nodes are configured with minimal capabilities.
- Network policies are defined to restrict the sandbox from accessing cluster-internal control plane services or cloud IMDS, except for explicitly allowlisted MCP endpoints.
- The Tailscale ingress utilizes a Caddy reverse proxy to enforce volumetric limits (e.g., 10MB max request body, 120s body read timeouts) to protect internal services.
- Tailscale state is durably stored in Kubernetes Secrets instead of PVCs to improve scheduling reliability.
