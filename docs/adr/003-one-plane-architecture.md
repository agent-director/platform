# ADR 003: One-Plane Architecture

## Status
Accepted

## Context
The previous "Two-Plane Architecture" strictly separated the Data Plane (Agent Substrate) from the Control Plane (Temporal, Gateway, etc.). However, maintaining this rigid boundary introduced significant networking complexity, overhead, and maintenance burden. Components like OpenShell and the Centralized MCP server created convoluted routing paths, especially when integrating with Git and internal APIs.

## Decision
We are moving to a "One-Plane Architecture" where all components, including agent execution environments (Agent Substrate), run in the same Kubernetes cluster plane. We will use strict isolation (read-only root, dropping capabilities, NetworkPolicies, and RWO hardlinked clones) to enforce security boundaries between agent execution and control plane services, rather than relying on an artificial network or separate cluster boundaries.

## Consequences
- **Positive:** Simplified deployment (single cluster), reduced latency, easier CI/CD (one-command local bring up via kind).
- **Negative:** Increased security risk if NetworkPolicies or SecurityContexts are misconfigured, as agent sandboxes run adjacent to control plane databases and Temporal.
- **Mitigation:** We enforce strict Kubernetes SecurityContexts (dropping `CAP_SYS_ADMIN`, ReadOnlyRootFilesystem) and restrictive NetworkPolicies for Agent Substrate pods.
