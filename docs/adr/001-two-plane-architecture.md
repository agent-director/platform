# ADR 001: Two-Plane Architecture for Agent Orchestration

## Status
Superseded by ADR 003

## Context
The `agent-director` must support two fundamentally different types of agentic workloads:
1. **Long-running, asynchronous workflows:** Tasks like continuous integration monitoring, background code review, and periodic project management standups. These require high durability, state persistence, and the ability to suspend/resume over long periods.
2. **Real-time, interactive sessions:** Direct user-agent collaboration requiring low latency, streaming updates, and fast, mutable state transitions.

Attempting to run both workloads on a single execution framework (e.g., forcing all interactive traffic through a workflow engine) introduces unacceptable latency for users and unnecessary complexity for developers.

## Decision
We will adopt a **Two-Plane Architecture**:

1. **Automation Plane:** Built on **Temporal** and **LangGraph**. Temporal will provide the durable execution environment and retries, while LangGraph will manage the agent state, memory, and checkpoints.
2. **Interactive Plane:** Powered by **Omnigent**. This plane will handle direct, real-time user-agent interaction, focusing on low latency and rapid state mutation.

Both planes will share foundational infrastructure (Gateway, Centralized MCP, Ambient Sandbox) to ensure consistent security, tooling, and LLM access.

## Consequences
- **Positive:** Interactive sessions are not blocked by workflow persistence overhead.
- **Positive:** Long-running tasks benefit from Temporal's robust retry and durability guarantees.
- **Negative:** Increased architectural complexity. State synchronization between the two planes (e.g., an interactive session modifying a task managed by the automation plane) will require careful design, likely utilizing shared PostgreSQL schemas and explicit state handoffs.
