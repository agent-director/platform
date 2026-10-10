# ADR 002: Token-Optimizing Shunt Middleware Implementation

## Status
Superseded by ADR 004

## Context
The `gateway` component routes all LLM calls through LiteLLM. To reduce costs and improve throughput, we need a **Shunt Middleware** that intercepts these calls, performs token optimization, caches responses, and enforces policy.

The critical constraint is that the Shunt must not introduce synchronous blocking into the event loop, which would degrade the overall control plane throughput, especially during high-concurrency streaming.

## Decision
We are currently evaluating two incompatible design paths via a speculative fork:

- **Option A (In-Process Python):** Implement the Shunt as a Python middleware directly within the LiteLLM application.
  - *Pros:* Simpler deployment, direct access to LiteLLM's internal state and context.
  - *Cons:* High risk of blocking the Python `asyncio` event loop during heavy CPU-bound token inspection or serialization tasks.

- **Option B (Standalone Reverse Proxy):** Implement the Shunt as a separate, high-performance reverse proxy (e.g., in Rust or Go) placed in front of LiteLLM.
  - *Pros:* Guaranteed isolation from the Python event loop, maximizing streaming throughput and concurrency.
  - *Cons:* Increased operational complexity (an additional service to deploy and monitor), and requires redefining the integration boundary via headers or a shared datastore.

## Consequences
- A speculative fork will be executed to benchmark both approaches under simulated heavy concurrent agent load.
- Until a final decision is made, any Shunt logic must strictly adhere to safe serialization practices and avoid dynamic template evaluation to prevent injection attacks.
- Both paths must support cross-tenant data isolation, keying all caches and states by tenant/agent identifier.
