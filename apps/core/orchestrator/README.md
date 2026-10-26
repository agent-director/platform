# Orchestrator

The `orchestrator` package (`apps/core/orchestrator/` in the `agent-director-core` workspace package) runs a Temporal worker with `AgentWorkflow`, `CIPipelineWorkflow`, `PMStandupWorkflow` and the `execute_block` activity. These workflows currently return representative placeholder results; they do not execute real CI pipelines, standups or LLM calls. `make dev-temporal` starts a host-local Temporal CLI dev server (`127.0.0.1:7233`, UI `8233`) for local interactive workflow development; LangGraph-backed state remains future work.

## Runtime configuration

Run `mise exec -- uv run --locked -m orchestrator.temporal_worker` after setting the required `TEMPORAL_URL=host:port` (for example `TEMPORAL_URL=127.0.0.1:7233` against `make dev-temporal`, which listens on `127.0.0.1:7233` with UI on `8233`). Unit tests live in `apps/core/tests/unit/test_orchestrator.py` and run via `make pytest-unit` or `mise exec -- uv run --locked pytest apps/core/tests/unit/test_orchestrator.py`. The typed `WorkerSettings` boundary rejects missing or malformed addresses; Helm supplies the Service endpoint for the selected kind-CLI or full-server profile.

- `TEMPORAL_NAMESPACE`: defaults to `default`.
- `TEMPORAL_TASK_QUEUE`: defaults to `agent-task-queue`; clients must use the same queue.
- `WORKER_HEALTH_PORT`: defaults to `8081`; `/health` reports a running worker and `/ready` also checks Temporal's service health with a bounded timeout.

The health endpoint is implemented in-process, requiring no shell or extra utilities in the distroless runtime. Readiness is not a guarantee that an individual workflow has completed. SIGINT/SIGTERM shut down a running worker through the SDK. Pre-start validation failures propagate directly rather than waiting on a shutdown event that the SDK cannot signal.

## Workflow determinism

Network/LLM operations belong in Temporal activities, not workflow code. All workflows (`AgentWorkflow`, `CIPipelineWorkflow`, `PMStandupWorkflow`) execute inside the Temporal deterministic sandbox (`sandboxed=True`) and are verified against committed JSON history fixtures in `apps/core/tests/unit/fixtures/` via `temporalio.worker.Replayer`. After an intentional workflow change, refresh the histories with `make replay-fixtures` (generator: `apps/core/tests/regenerate_replay_fixtures.py`) and review the JSON diff; the fixtures include an `update_status` signal-path history so the command sequence around signal handling is replay-checked. Replay proves command compatibility only; the handler's status validation is covered by direct unit tests. Integration verification should execute an actual SDK workflow against the deployed worker rather than treating an API placeholder response as proof of Temporal execution.
