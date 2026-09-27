# Agent Director

Agent Director (`agent-director`) is a Two-Plane multi-agent orchestration and management platform. It combines durable, long-running agent workflows with fast, interactive user-agent sessions, all built upon a secure, isolated sandboxing environment.

## Key Features

- **Two-Plane Architecture**:
  - **Automation Plane**: Built on Temporal and LangGraph for long-running, durable workflows (like CI pipelines and PM standups).
  - **Interactive Plane**: Powered by Omnigent for direct, real-time user-agent interaction.
- **Gateway (LiteLLM + Shunt)**: A high-throughput LiteLLM gateway with a token-optimizing Shunt middleware that routes, caches, and enforces policies on LLM requests.
- **Centralized MCP**: Model Context Protocol server manager that standardizes tool and context delivery to agents.
- **OpenShell Sandbox**: Secure container isolation, enforcing network restrictions and filesystem boundaries for agent tasks.
- **Cloud-Native Infrastructure**: Kubernetes/Helm-first deployment (`charts/platform`), backed by PostgreSQL for state.
- **Tailscale Ingress**: Exposes platform services securely over a private Tailnet without requiring public IP addresses or traditional Ingress Controllers. Utilizes Caddy for reverse-proxy routing and volumetric limits.

## Architecture

See [architecture.md](architecture.md) for a deep dive into the system components, data flow, and security model.

## Getting Started

### Prerequisites
- Python >= 3.11
- `uv` package manager (CRITICAL: always use `uv run` instead of `python` directly, and `uv add` instead of `pip install`)
- Helm & Kubernetes (for platform bring-up)

### Local Development

1. Install dependencies:
   ```bash
   uv sync
   ```
2. Run tests:
   ```bash
   uv run pytest tests/
   ```

See [CONTRIBUTING.md](CONTRIBUTING.md) for more details on project standards, branch workflow, and commit conventions.
