import asyncio
import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.worker import Worker

from orchestrator.workflows.ci_pipeline import build_ci_pipeline
from orchestrator.workflows.pm_standup import build_pm_standup


@dataclass
class JobInput:
    job_id: str
    workflow_type: str


# Global variables for graphs and pool
db_pool: AsyncConnectionPool | None = None
ci_graph: Any = None
pm_graph: Any = None


async def init_worker_state():
    global db_pool, ci_graph, pm_graph

    db_uri = os.environ.get(
        "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
    )
    db_pool = AsyncConnectionPool(
        db_uri,
        min_size=2,
        max_size=10,
        kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0},
        open=False,
    )
    await db_pool.open()

    # Setup schema once globally
    checkpointer = AsyncPostgresSaver(db_pool)
    await checkpointer.setup()

    ci_graph = build_ci_pipeline()
    pm_graph = build_pm_standup()


async def cleanup_worker_state():
    if db_pool:
        await db_pool.close()


@activity.defn
async def run_langgraph_workflow(job_input: JobInput) -> str:
    """
    We only accept `job_id` and do not serialize auth contexts or full state in Temporal.
    """
    # Create an isolated checkpointer for this activity execution to avoid global lock contention
    checkpointer = AsyncPostgresSaver(db_pool)

    if job_input.workflow_type == "ci_pipeline":
        compiled_graph = ci_graph.compile(checkpointer=checkpointer)
    elif job_input.workflow_type == "pm_standup":
        compiled_graph = pm_graph.compile(checkpointer=checkpointer)
    else:
        raise ValueError(f"Unknown workflow type: {job_input.workflow_type}")

    config = {"configurable": {"thread_id": job_input.job_id}}
    initial_state = {"job_id": job_input.job_id}

    # Execute workflow
    result = None
    async for event in compiled_graph.astream(
        initial_state, config=config, stream_mode="values"
    ):
        activity.heartbeat("running")
        result = event

    return result.get("status", "completed") if result else "completed"


@workflow.defn(sandboxed=False)
class AgentWorkflow:
    def __init__(self) -> None:
        self.status = "started"

    @workflow.run
    async def run(self, job_input: JobInput) -> str:
        # Schedule the activity
        result = await workflow.execute_activity(
            run_langgraph_workflow,
            job_input,
            start_to_close_timeout=timedelta(minutes=10),
        )
        if self.status in ["started", "processing"]:
            self.status = result
        return self.status

    @workflow.signal
    def update_status(self, new_status: str) -> None:
        if new_status in ["started", "processing", "paused", "waiting_on_human"]:
            self.status = new_status


async def main():
    await init_worker_state()
    try:
        temporal_url = os.environ.get("TEMPORAL_URL", "localhost:7233")
        client = await Client.connect(temporal_url)
        worker = Worker(
            client,
            task_queue="agent-task-queue",
            workflows=[AgentWorkflow],
            activities=[run_langgraph_workflow],
        )
        print("Starting worker...")
        await worker.run()
    finally:
        await cleanup_worker_state()


if __name__ == "__main__":
    asyncio.run(main())
