# ADR 004: Shunt Middleware Removal

## Status
Accepted

## Context
The Shunt Middleware (and the broader Omnigent concept) was originally introduced to intercept and optimize LLM requests passing through the Unified API gateway. It provided caching and specific routing rules. However, it tightly coupled the gateway logic to specific agent patterns, increased the complexity of the Unified API, and was difficult to maintain.

## Decision
We are removing the Shunt Middleware, Omnigent components, and the corresponding intercepts. The Unified API gateway will act as a standard BFF, forwarding LLM requests directly to LiteLLM without intercepting or attempting to context-optimize the payloads in transit.

## Consequences
- **Positive:** A much simpler Unified API gateway, fewer moving parts, and clear decoupling of LLM routing (handled by LiteLLM) from application logic.
- **Negative:** Loss of custom caching and interception capabilities previously provided by Shunt.
- **Mitigation:** We rely on LiteLLM's native capabilities for load balancing and retries. Caching is explicitly disabled to prevent agentic staleness, per current requirements.
