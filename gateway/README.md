# Gateway (Unified API + LiteLLM)

The `gateway` module provides the central LLM routing and intelligence proxy for the platform.

## Overview
This module acts as the interface between the agent execution environments (Agent Substrate) and the foundational models. It is built on **LiteLLM** to provide vendor-agnostic routing and a **Unified API** BFF for managing the control plane.

## Unified API
The Unified API serves as the primary ingress for configuring models, routing, and workflows without directly exposing the internal LiteLLM proxy or Temporal workers.

## Security
- **Injection Defenses:** Uses safe serialization exclusively.
- **Cross-Tenant Isolation:** State and caches are strictly keyed by tenant/agent IDs.
