# Orchestrator

The `orchestrator` module handles the **Automation Plane** of the One-Plane architecture.

## Overview
This plane is responsible for long-running, durable agent workflows (e.g., CI pipelines, background code reviews). It utilizes **Temporal** for robust, retriable orchestrations and **LangGraph** for managing the state and memory of agentic processes.

## Development Guidelines
- **Workflow Determinism:** Temporal Workflows must remain deterministic. Never invoke non-deterministic operations (like an LLM call or a network request) directly in a Workflow.
- **LLM Invocations:** All LLM calls via the LiteLLM Gateway must be wrapped in Temporal **Activities**.
- **State Checkpointing:** Use LangGraph's checkpointing capabilities to persist state, ensuring workflows can suspend and resume predictably.
