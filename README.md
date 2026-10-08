# Agent Director

Agent Director (`agent-director`) is a One-Plane multi-agent orchestration and management platform. It combines durable, long-running agent workflows with fast, interactive user-agent sessions, all built upon a secure, isolated sandboxing environment.

## Key Features

- **One-Plane Architecture**: All components, including agent execution environments, run in the same Kubernetes cluster plane, protected by strict isolation.
  - **Automation Plane**: Built on Temporal and LangGraph for long-running, durable workflows (like CI pipelines and PM standups).
  - **Interactive Plane**: For direct, real-time user-agent interaction.
- **Gateway (Unified API + LiteLLM)**: A high-throughput gateway that routes and enforces policies on LLM requests.
- **Centralized MCP**: Model Context Protocol server manager that standardizes tool and context delivery to agents.
- **Agent Substrate Sandbox**: Secure container isolation, enforcing network restrictions (read-only root, no CAP_SYS_ADMIN, and explicitly permitted network endpoints) for agent tasks via RWO hardlinked clones.
- **Cloud-Native Infrastructure**: Kubernetes/Helm-first deployment (`charts/platform`), backed by PostgreSQL (Zalando Operator) for state.
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
