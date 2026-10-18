# Orchestrator

The `orchestrator` package runs a Temporal worker with `AgentWorkflow`, `CIPipelineWorkflow`, `PMStandupWorkflow` and the `execute_block` activity. These workflows currently return representative placeholder results; they do not execute real CI pipelines, standups or LLM calls. LangGraph-backed state and a host-local runner remain future work.

## Runtime configuration

Run `uv run --locked -m orchestrator.temporal_worker` after setting the required `TEMPORAL_URL=host:port`. The typed `WorkerSettings` boundary rejects missing or malformed addresses; Helm supplies the Service endpoint for the selected kind-CLI or full-server profile.

- `TEMPORAL_NAMESPACE`: defaults to `default`.
- `TEMPORAL_TASK_QUEUE`: defaults to `agent-task-queue`; clients must use the same queue.
- `WORKER_HEALTH_PORT`: defaults to `8081`; `/health` reports a running worker and `/ready` also checks Temporal's service health with a bounded timeout.

The health endpoint is implemented in-process, requiring no shell or extra utilities in the distroless runtime. Readiness is not a guarantee that an individual workflow has completed. SIGINT/SIGTERM shut down a running worker through the SDK. Pre-start validation failures propagate directly rather than waiting on a shutdown event that the SDK cannot signal.

## Workflow determinism

Network/LLM operations belong in Temporal activities, not workflow code. `AgentWorkflow` retains its existing non-sandboxed definition; the other workflows use the Temporal sandbox. Integration verification should execute an actual SDK workflow against the deployed worker rather than treating an API placeholder response as proof of Temporal execution.
